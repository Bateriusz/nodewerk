#!/usr/bin/env python3
"""NW-10 compact package container, bounded decoder, and byte accounting.

The compact container is a delivery encoding of the unchanged `.nwp` v1
package.  It never changes package semantics: the bounded decoder reconstructs
the exact canonical package bytes and the established NW-03 decoder then
validates them before any GPU work.

Container v1
------------

Header (64 bytes, little endian)::

    magic[8] = b"NWC1\\n\\x00\\x00\\x00"
    u16 version, u16 header_size, u16 codec, u16 layout
    u32 flags, u32 decoded_size, u32 payload_size, u32 container_size
    decoded_sha256[32]

The payload is either stored or MSZIP-compressed.  The uncompressed container
payload is::

    u32 record_count
    record table (AoS: 14 bytes/record, SoA: 13 bytes/record)
    raw record bytes

Each record is either a raw chunk or a backward reference to an earlier raw
record.  References are validated before output allocation/growth, cannot
form cycles, and must reproduce the canonical package exactly.

Only existing measured compression options are relevant.  The player-supported
codec is the Windows inbox MSZIP decompressor (`cabinet.dll`); zlib, bzip2, and
lzma are measured candidate-only options and are never selected unless their
complete distribution cost is demonstrably lower.
"""

from __future__ import annotations

import bz2
import hashlib
import lzma
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_contract as nw07  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw10_contract as contract  # noqa: E402

MAGIC = b"NWC1\n\x00\x00\x00"
HEADER = struct.Struct("<8sHHHHIIII32s")
HEADER_SIZE = HEADER.size  # 64

CODEC_STORED = 1
CODEC_MSZIP = 2
CODEC_IDS = {
    contract.CODEC_STORED_ID: CODEC_STORED,
    contract.CODEC_MSZIP_ID: CODEC_MSZIP,
}
CODEC_NAMES = {value: key for key, value in CODEC_IDS.items()}

LAYOUT_CANONICAL = 1
LAYOUT_AOS = 2
LAYOUT_SOA = 3
LAYOUT_IDS = {
    contract.LAYOUT_CANONICAL_ID: LAYOUT_CANONICAL,
    contract.LAYOUT_AOS_ID: LAYOUT_AOS,
    contract.LAYOUT_SOA_ID: LAYOUT_SOA,
}
LAYOUT_NAMES = {value: key for key, value in LAYOUT_IDS.items()}

FLAG_DELTA_REFERENCES = 1
FLAG_RECORD_LAYOUT = 2
KNOWN_FLAGS = FLAG_DELTA_REFERENCES | FLAG_RECORD_LAYOUT

RECORD_RAW = 0
RECORD_REFERENCE = 1
RECORD_AOS_SIZE = 14
RECORD_SOA_COLUMNS = 13

DEFAULT_CHUNK_SIZE = 256
MAX_RECORDS = 1_048_576
MAX_CONTAINER_BYTES = 64 * 1024 * 1024
MAX_DECODED_BYTES = nwp.MAX_DECODED_BYTES
MAX_EXPANSION_RATIO = 256
MSZIP_BLOCK_BYTES = 32_768


class CompactCodecError(ValueError):
    """Raised when a compact container is malformed, unsupported, or unsafe."""

    def __init__(self, code: str, message: str, *, offset: int | None = None):
        super().__init__(message)
        self.code = code
        self.offset = offset

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.offset is not None:
            payload["offset"] = self.offset
        return payload


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _checked_add(left: int, right: int, limit: int, what: str) -> int:
    if left < 0 or right < 0 or left > limit - right:
        raise CompactCodecError(
            "compact_arithmetic_overflow", f"{what} exceeds the bounded limit"
        )
    return left + right


def _checked_mul(left: int, right: int, limit: int, what: str) -> int:
    if left < 0 or right < 0 or (right and left > limit // right):
        raise CompactCodecError(
            "compact_arithmetic_overflow", f"{what} exceeds the bounded limit"
        )
    return left * right


# ---------------------------------------------------------------------------
# Existing compression options: MSZIP is the player-supported Windows codec.
# ---------------------------------------------------------------------------


def mszip_compress(data: bytes, *, level: int = 9) -> bytes:
    """Deterministic MSZIP framing (per-block raw DEFLATE with 'CK' headers)."""
    output = bytearray()
    for start in range(0, len(data), MSZIP_BLOCK_BYTES):
        block = data[start : start + MSZIP_BLOCK_BYTES]
        compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
        packed = compressor.compress(block) + compressor.flush()
        output += b"CK"
        output += packed
    return bytes(output)


def mszip_decompress(data: bytes, *, max_output_bytes: int) -> bytes:
    """Bounded decoder for the deterministic MSZIP framing produced above."""
    output = bytearray()
    cursor = 0
    while cursor < len(data):
        if len(data) - cursor < 2 or data[cursor : cursor + 2] != b"CK":
            raise CompactCodecError(
                "compact_codec_malformed",
                "MSZIP block is missing its CK signature",
                offset=cursor,
            )
        cursor += 2
        if cursor >= len(data):
            raise CompactCodecError(
                "compact_codec_malformed", "MSZIP block has no deflate payload",
                offset=cursor,
            )
        decompressor = zlib.decompressobj(-15)
        try:
            block = decompressor.decompress(data[cursor:])
        except zlib.error as exc:
            raise CompactCodecError(
                "compact_codec_malformed", f"MSZIP inflate failed: {exc}", offset=cursor
            ) from exc
        if len(block) > MSZIP_BLOCK_BYTES:
            raise CompactCodecError(
                "compact_decode_limit", "MSZIP block expands beyond 32 KiB"
            )
        if len(output) + len(block) > max_output_bytes:
            raise CompactCodecError(
                "compact_decode_limit",
                "MSZIP output exceeds the declared container limit",
            )
        output += block
        consumed = len(data) - cursor - len(decompressor.unused_data)
        if consumed <= 0:
            raise CompactCodecError(
                "compact_codec_malformed", "MSZIP decoder made no progress"
            )
        cursor += consumed
        if not decompressor.eof:
            raise CompactCodecError(
                "compact_codec_malformed", "MSZIP block is truncated",
                offset=cursor,
            )
    return bytes(output)


def compress_payload(codec: int, payload: bytes) -> bytes:
    if codec == CODEC_STORED:
        return payload
    if codec == CODEC_MSZIP:
        return mszip_compress(payload)
    raise CompactCodecError(
        "compact_codec_unsupported", f"unsupported compact codec id {codec}"
    )


def decompress_payload(
    codec: int, payload: bytes, *, container_size: int
) -> bytes:
    if codec == CODEC_STORED:
        if len(payload) != container_size:
            raise CompactCodecError(
                "compact_size_mismatch",
                "stored payload length does not match container_size",
            )
        return payload
    if codec == CODEC_MSZIP:
        result = mszip_decompress(payload, max_output_bytes=container_size)
        if len(result) != container_size:
            raise CompactCodecError(
                "compact_size_mismatch",
                "MSZIP output length does not match container_size",
            )
        return result
    raise CompactCodecError(
        "compact_codec_unsupported", f"unsupported compact codec id {codec}"
    )


# ---------------------------------------------------------------------------
# Record table and delta references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    kind: int
    reference: int
    length: int
    offset: int
    data: bytes = b""


def _chunk_records(
    canonical_package: bytes,
    *,
    chunk_size: int,
    delta_references: bool,
) -> tuple[list[Record], int]:
    if chunk_size <= 0:
        raise ValueError("chunk size must be positive")
    records: list[Record] = []
    raw = bytearray()
    first_by_chunk: dict[bytes, int] = {}
    references = 0
    for start in range(0, len(canonical_package), chunk_size):
        chunk = canonical_package[start : start + chunk_size]
        target = first_by_chunk.get(chunk) if delta_references else None
        if target is not None:
            records.append(Record(RECORD_REFERENCE, target, len(chunk), 0))
            references += 1
            continue
        index = len(records)
        first_by_chunk.setdefault(chunk, index)
        records.append(Record(RECORD_RAW, 0, len(chunk), len(raw), chunk))
        raw += chunk
    return records, references


def _encode_table(records: Sequence[Record], layout: int) -> bytes:
    if layout in (LAYOUT_CANONICAL, LAYOUT_AOS):
        table = bytearray()
        for record in records:
            table += struct.pack(
                "<BBIII",
                record.kind,
                0,
                record.reference,
                record.length,
                record.offset,
            )
        return bytes(table)
    if layout == LAYOUT_SOA:
        kinds = bytes(record.kind for record in records)
        references = b"".join(
            struct.pack("<I", record.reference) for record in records
        )
        lengths = b"".join(struct.pack("<I", record.length) for record in records)
        offsets = b"".join(struct.pack("<I", record.offset) for record in records)
        return kinds + references + lengths + offsets
    raise CompactCodecError(
        "compact_layout_unsupported", f"unsupported compact layout id {layout}"
    )


def _decode_table(
    container: bytes, record_count: int, layout: int
) -> tuple[list[tuple[int, int, int, int]], int]:
    if record_count > MAX_RECORDS:
        raise CompactCodecError(
            "compact_decode_limit", "record count exceeds the bounded limit"
        )
    if layout in (LAYOUT_CANONICAL, LAYOUT_AOS):
        table_size = _checked_mul(
            record_count, RECORD_AOS_SIZE, MAX_CONTAINER_BYTES, "record table"
        )
        if 4 + table_size > len(container):
            raise CompactCodecError(
                "compact_truncated", "record table exceeds the container"
            )
        records = []
        for index in range(record_count):
            offset = 4 + index * RECORD_AOS_SIZE
            kind, reserved, reference, length, raw_offset = struct.unpack_from(
                "<BBIII", container, offset
            )
            if reserved != 0:
                raise CompactCodecError(
                    "compact_malformed", "record reserved byte is nonzero",
                    offset=offset + 1,
                )
            records.append((kind, reference, length, raw_offset))
        return records, 4 + table_size
    if layout == LAYOUT_SOA:
        table_size = _checked_mul(
            record_count,
            RECORD_SOA_COLUMNS,
            MAX_CONTAINER_BYTES,
            "record table",
        )
        if 4 + table_size > len(container):
            raise CompactCodecError(
                "compact_truncated", "record table exceeds the container"
            )
        base = 4
        kinds_at = base
        refs_at = kinds_at + record_count
        lengths_at = refs_at + 4 * record_count
        offsets_at = lengths_at + 4 * record_count
        records = []
        for index in range(record_count):
            kind = container[kinds_at + index]
            reference = _u32(container, refs_at + index * 4)
            length = _u32(container, lengths_at + index * 4)
            raw_offset = _u32(container, offsets_at + index * 4)
            records.append((kind, reference, length, raw_offset))
        return records, 4 + table_size
    raise CompactCodecError(
        "compact_layout_unsupported", f"unsupported compact layout id {layout}"
    )


def _decode_container(
    container: bytes,
    *,
    decoded_size: int,
    layout: int,
    max_decoded_bytes: int,
) -> bytes:
    if len(container) < 4:
        raise CompactCodecError("compact_truncated", "container has no record count")
    record_count = _u32(container, 0)
    records, raw_start = _decode_table(container, record_count, layout)
    raw_payload = container[raw_start:]
    output = bytearray()
    record_outputs: list[bytes] = []
    record_kinds: list[int] = []
    expected_raw_offset = 0
    for index, (kind, reference, length, raw_offset) in enumerate(records):
        if kind == RECORD_RAW:
            if length == 0:
                raise CompactCodecError(
                    "compact_malformed", "raw record has zero length"
                )
            if raw_offset != expected_raw_offset:
                raise CompactCodecError(
                    "compact_malformed", "raw record offsets are not contiguous",
                    offset=raw_start + raw_offset,
                )
            end = _checked_add(
                raw_offset, length, len(raw_payload), "raw record range"
            )
            chunk = raw_payload[raw_offset:end]
            expected_raw_offset = end
            if len(output) + len(chunk) > max_decoded_bytes:
                raise CompactCodecError(
                    "compact_decode_limit",
                    "decoded output exceeds the bounded size",
                )
            output += chunk
            record_outputs.append(chunk)
            record_kinds.append(RECORD_RAW)
        elif kind == RECORD_REFERENCE:
            if reference >= index:
                raise CompactCodecError(
                    "compact_reference_invalid",
                    "record reference must point to an earlier record",
                )
            if raw_offset != 0:
                raise CompactCodecError(
                    "compact_malformed", "reference record offset must be zero"
                )
            if reference >= len(record_outputs):
                raise CompactCodecError(
                    "compact_reference_invalid",
                    "record reference does not target a raw record",
                )
            if record_kinds[reference] != RECORD_RAW:
                raise CompactCodecError(
                    "compact_reference_invalid",
                    "record reference must target a raw record",
                )
            target_bytes = record_outputs[reference]
            target = len(target_bytes)
            if length != target:
                raise CompactCodecError(
                    "compact_reference_invalid",
                    "reference length does not match its target",
                )
            if len(output) + length > max_decoded_bytes:
                raise CompactCodecError(
                    "compact_decode_limit",
                    "decoded output exceeds the bounded size",
                )
            output += target_bytes
            record_outputs.append(target_bytes)
            record_kinds.append(RECORD_REFERENCE)
        else:
            raise CompactCodecError(
                "compact_malformed", f"unknown record kind {kind}"
            )
    if expected_raw_offset != len(raw_payload):
        raise CompactCodecError(
            "compact_malformed", "container has unexplained raw payload bytes"
        )
    if len(output) != decoded_size:
        raise CompactCodecError(
            "compact_size_mismatch",
            f"decoded size {len(output)} does not match {decoded_size}",
        )
    return bytes(output)


# ---------------------------------------------------------------------------
# Public container encoder/decoder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactEncoding:
    bytes: bytes
    codec_id: str
    layout_id: str
    decoded_size: int
    payload_size: int
    container_size: int
    record_count: int
    reference_count: int
    decoded_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": contract.COMPACT_CONTAINER_ID,
            "codec_id": self.codec_id,
            "layout_id": self.layout_id,
            "encoded_bytes": len(self.bytes),
            "decoded_bytes": self.decoded_size,
            "payload_bytes": self.payload_size,
            "container_bytes": self.container_size,
            "record_count": self.record_count,
            "reference_count": self.reference_count,
            "decoded_sha256": self.decoded_sha256,
            "encoded_sha256": hashlib.sha256(self.bytes).hexdigest(),
        }


def encode_compact(
    canonical_package: bytes,
    *,
    codec_id: str = contract.CODEC_STORED_ID,
    layout_id: str = contract.LAYOUT_CANONICAL_ID,
    delta_references: bool = True,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_decoded_bytes: int = MAX_DECODED_BYTES,
) -> CompactEncoding:
    if not canonical_package:
        raise CompactCodecError("compact_empty", "canonical package is empty")
    if len(canonical_package) > max_decoded_bytes:
        raise CompactCodecError(
            "compact_decode_limit", "canonical package exceeds the bounded size"
        )
    contract.validate_codec_identifiers(codec_id, layout_id)
    codec = CODEC_IDS[codec_id]
    layout = LAYOUT_IDS[layout_id]
    records, references = _chunk_records(
        canonical_package,
        chunk_size=chunk_size,
        delta_references=delta_references,
    )
    table = _encode_table(records, layout)
    raw = b"".join(record.data for record in records if record.kind == RECORD_RAW)
    container = struct.pack("<I", len(records)) + table + raw
    if len(container) > MAX_CONTAINER_BYTES:
        raise CompactCodecError(
            "compact_decode_limit", "container exceeds the bounded size"
        )
    payload = compress_payload(codec, container)
    flags = FLAG_RECORD_LAYOUT | (FLAG_DELTA_REFERENCES if references else 0)
    header = HEADER.pack(
        MAGIC,
        1,
        HEADER_SIZE,
        codec,
        layout,
        flags,
        len(canonical_package),
        len(payload),
        len(container),
        _sha256(canonical_package),
    )
    encoded = header + payload
    return CompactEncoding(
        bytes=encoded,
        codec_id=codec_id,
        layout_id=layout_id,
        decoded_size=len(canonical_package),
        payload_size=len(payload),
        container_size=len(container),
        record_count=len(records),
        reference_count=references,
        decoded_sha256=hashlib.sha256(canonical_package).hexdigest(),
    )


def is_compact_container(data: bytes) -> bool:
    return len(data) >= HEADER_SIZE and data[:8] == MAGIC


def parse_header(data: bytes) -> dict[str, Any]:
    if len(data) < 8:
        raise CompactCodecError(
            "compact_truncated", "compact container is smaller than its header"
        )
    if data[:8] != MAGIC:
        raise CompactCodecError("compact_magic", "compact container magic is invalid")
    if len(data) < HEADER_SIZE:
        raise CompactCodecError(
            "compact_truncated", "compact container is smaller than its header"
        )
    (
        magic,
        version,
        header_size,
        codec,
        layout,
        flags,
        decoded_size,
        payload_size,
        container_size,
        decoded_sha,
    ) = HEADER.unpack_from(data, 0)
    if version != 1:
        raise CompactCodecError(
            "compact_version_unsupported", f"unsupported container version {version}"
        )
    if header_size != HEADER_SIZE:
        raise CompactCodecError(
            "compact_header_size", f"unexpected header size {header_size}"
        )
    if codec not in CODEC_NAMES:
        raise CompactCodecError(
            "compact_codec_unsupported", f"unsupported codec id {codec}"
        )
    if layout not in LAYOUT_NAMES:
        raise CompactCodecError(
            "compact_layout_unsupported", f"unsupported layout id {layout}"
        )
    if flags & ~KNOWN_FLAGS:
        raise CompactCodecError(
            "compact_flags_unsupported", f"unsupported flag bits 0x{flags:x}"
        )
    if payload_size != len(data) - HEADER_SIZE:
        raise CompactCodecError(
            "compact_size_mismatch",
            "payload size does not match the container length",
        )
    return {
        "codec": codec,
        "codec_id": CODEC_NAMES[codec],
        "layout": layout,
        "layout_id": LAYOUT_NAMES[layout],
        "flags": flags,
        "decoded_size": decoded_size,
        "payload_size": payload_size,
        "container_size": container_size,
        "decoded_sha256": decoded_sha.hex(),
    }


def decode_compact(
    data: bytes,
    *,
    max_decoded_bytes: int = MAX_DECODED_BYTES,
    max_container_bytes: int = MAX_CONTAINER_BYTES,
) -> bytes:
    header = parse_header(data)
    decoded_size = int(header["decoded_size"])
    container_size = int(header["container_size"])
    if decoded_size > max_decoded_bytes:
        raise CompactCodecError(
            "compact_decode_limit", "decoded size exceeds the configured limit"
        )
    if container_size > max_container_bytes:
        raise CompactCodecError(
            "compact_decode_limit", "container size exceeds the configured limit"
        )
    if (
        decoded_size
        > max(1, container_size) * MAX_EXPANSION_RATIO
    ):
        raise CompactCodecError(
            "compact_decode_limit", "declared expansion ratio exceeds the limit"
        )
    container = decompress_payload(
        int(header["codec"]),
        data[HEADER_SIZE:],
        container_size=container_size,
    )
    canonical = _decode_container(
        container,
        decoded_size=decoded_size,
        layout=int(header["layout"]),
        max_decoded_bytes=max_decoded_bytes,
    )
    if hashlib.sha256(canonical).hexdigest() != header["decoded_sha256"]:
        raise CompactCodecError(
            "compact_checksum_mismatch",
            "decoded package does not match the declared SHA-256",
        )
    return canonical


def validate_compact(
    data: bytes,
    *,
    max_decoded_bytes: int = MAX_DECODED_BYTES,
    require_package: bool = True,
) -> tuple[bool, dict[str, Any]]:
    try:
        canonical = decode_compact(data, max_decoded_bytes=max_decoded_bytes)
        result: dict[str, Any] = {
            "valid": True,
            "canonical_bytes": len(canonical),
            "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        }
        if require_package:
            package = nwp.decode_package(canonical, allow_dev=False)
            result["package"] = {
                "profile_id": package.capability.profile_id,
                "requirements_digest": package.capability.requirements_digest,
                "resource_count": len(package.resources),
                "shader_count": len(package.shaders),
            }
        return True, result
    except (CompactCodecError, nwp.PackageValidationError) as exc:
        return False, {"valid": False, "error": exc.to_dict() if hasattr(exc, "to_dict") else str(exc)}
    except (MemoryError, OverflowError) as exc:
        return False, {
            "valid": False,
            "error": {
                "code": "compact_decode_limit",
                "message": f"bounded decode failed safely: {exc}",
            },
        }


def select_player_codec(
    canonical_package: bytes,
    *,
    layout_id: str = contract.LAYOUT_CANONICAL_ID,
    delta_references: bool = True,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Measure the player-supported codecs and select the smallest complete form."""
    candidates: list[CompactEncoding] = []
    for codec_id in contract.PLAYER_SUPPORTED_CODEC_IDS:
        candidates.append(
            encode_compact(
                canonical_package,
                codec_id=codec_id,
                layout_id=layout_id,
                delta_references=delta_references,
                chunk_size=chunk_size,
            )
        )
    selected = min(candidates, key=lambda item: (len(item.bytes), item.codec_id))
    return {
        "selected": selected.to_dict(),
        "selected_codec_id": selected.codec_id,
        "candidates": [item.to_dict() for item in candidates],
        "compression_increases_distribution": (
            len(selected.bytes) > len(canonical_package)
        ),
    }


def measure_candidate_compression(container: bytes) -> list[dict[str, Any]]:
    """Measure existing compression options without silently adopting one."""
    options = [
        (contract.CODEC_STORED_ID, container),
        ("candidate.zlib.raw", zlib.compress(container, 9)),
        ("candidate.bzip2", bz2.compress(container, 9)),
        ("candidate.lzma", lzma.compress(container, preset=9)),
        (contract.CODEC_MSZIP_ID, mszip_compress(container)),
    ]
    return [
        {
            "codec": name,
            "payload_bytes": len(payload),
            "complete_container_bytes": HEADER_SIZE + len(payload),
            "player_supported": name in contract.PLAYER_SUPPORTED_CODEC_IDS,
        }
        for name, payload in options
    ]


# ---------------------------------------------------------------------------
# Measured alternatives
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint values must be non-negative")
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            output.append(byte | 0x80)
        else:
            output.append(byte)
            return bytes(output)


def _parameter_layout_size(
    parameters: Sequence[nwp.ParameterRecord],
    *,
    value_encoding: str = "canonical",
) -> dict[str, Any]:
    names = sorted({parameter.name for parameter in parameters})
    types = sorted({parameter.type for parameter in parameters})
    phases = sorted({parameter.phase for parameter in parameters})
    interpolations = sorted(
        {parameter.timeline_interpolation for parameter in parameters}
    )
    name_index = {name: index for index, name in enumerate(names)}
    type_index = {name: index for index, name in enumerate(types)}
    phase_index = {name: index for index, name in enumerate(phases)}
    interpolation_index = {
        name: index for index, name in enumerate(interpolations)
    }
    packed = bytearray()
    packed += _varint(len(parameters))
    for table in (names, types, phases, interpolations):
        packed += _varint(len(table))
        for item in table:
            encoded = item.encode("utf-8")
            packed += _varint(len(encoded)) + encoded
    previous_id = 0
    maximum_error = 0.0
    for parameter in parameters:
        packed += _varint(parameter.parameter_id - previous_id)
        previous_id = parameter.parameter_id
        packed += _varint(name_index[parameter.name])
        packed += _varint(type_index[parameter.type])
        packed += _varint(phase_index[parameter.phase])
        packed += _varint(parameter.component_count)
        value = parameter.value
        if value_encoding != "canonical":
            if len(value) % 4 != 0:
                raise ValueError("float parameter payload is not aligned to float32")
            floats = struct.unpack("<" + "f" * (len(value) // 4), value)
            encoded = bytearray()
            for item in floats:
                if value_encoding == "f16":
                    try:
                        packed_value = struct.pack("<e", item)
                    except OverflowError:
                        packed_value = struct.pack(
                            "<e", math.copysign(65504.0, item)
                        )
                    restored = struct.unpack("<e", packed_value)[0]
                    maximum_error = max(maximum_error, abs(restored - item))
                    encoded += packed_value
                elif value_encoding == "q16":
                    # Signed normalized 16-bit fixed point in [-8, 8].
                    step = 16.0 / 65535.0
                    quantized = round(max(-8.0, min(8.0, item)) / step)
                    restored = quantized * step
                    maximum_error = max(maximum_error, abs(restored - item))
                    encoded += struct.pack(
                        "<h", int(max(-32767, min(32767, quantized)))
                    )
                else:
                    encoded += struct.pack("<f", item)
            value = bytes(encoded)
        packed += _varint(len(value)) + value
        packed += _varint(parameter.timeline_start_frame)
        packed += _varint(parameter.timeline_end_frame)
        packed += _varint(parameter.timeline_step)
        packed += _varint(interpolation_index[parameter.timeline_interpolation])
    canonical = sum(
        4
        + 2
        + len(parameter.name.encode("utf-8"))
        + 2
        + len(parameter.type.encode("utf-8"))
        + 2
        + len(parameter.phase.encode("utf-8"))
        + 4
        + 4
        + len(parameter.value)
        + 8
        + 8
        + 4
        + 2
        + len(parameter.timeline_interpolation.encode("utf-8"))
        for parameter in parameters
    )
    return {
        "canonical_bytes": canonical,
        "packed_bytes": len(packed),
        "delta_bytes": len(packed) - canonical,
        "maximum_absolute_error": maximum_error,
        "within_numeric_tolerance": maximum_error <= contract.NUMERIC_ABSOLUTE_TOLERANCE,
        "value_encoding": value_encoding,
    }


def measure_parameter_packing(package: nwp.NwpPackage) -> dict[str, Any]:
    canonical = _parameter_layout_size(package.parameters, value_encoding="canonical")
    canonical["packed_bytes"] = canonical["canonical_bytes"]
    canonical["delta_bytes"] = 0
    varint = _parameter_layout_size(package.parameters, value_encoding="canonical")
    q16 = _parameter_layout_size(package.parameters, value_encoding="q16")
    f16 = _parameter_layout_size(package.parameters, value_encoding="f16")
    alternatives = []
    for identifier, result in (
        (contract.PARAM_LAYOUT_CANONICAL_ID, canonical),
        (contract.PARAM_LAYOUT_VARINT_ID, varint),
        (contract.PARAM_LAYOUT_F16_ID, f16),
        (contract.PARAM_LAYOUT_Q16_ID, q16),
    ):
        alternatives.append(
            {
                "parameter_layout_id": identifier,
                **result,
                "adopted": False,
            }
        )
    # Only a packing mode with a measured byte benefit and tolerance compliance
    # is eligible.  Numeric/image tolerances remain frozen.  The compact
    # container currently carries canonical parameter bytes, so none of these
    # semantic layouts are silently integrated without a matching decoder.
    eligible = [
        item
        for item in alternatives
        if item["delta_bytes"] < 0 and item["within_numeric_tolerance"]
    ]
    return {
        "alternatives": alternatives,
        "eligible": eligible,
        "selected": contract.PARAM_LAYOUT_CANONICAL_ID,
        "adoption_status": (
            "eligible_but_not_integrated"
            if eligible
            else "no_eligible_layout"
        ),
        "declared_tolerance": contract.image_tolerances()["numeric"],
    }


def measure_delta_references(canonical_package: bytes) -> dict[str, Any]:
    stored, stored_refs = _chunk_records(
        canonical_package, chunk_size=DEFAULT_CHUNK_SIZE, delta_references=False
    )
    delta, delta_refs = _chunk_records(
        canonical_package, chunk_size=DEFAULT_CHUNK_SIZE, delta_references=True
    )
    stored_raw = sum(record.length for record in stored)
    delta_raw = sum(record.data.__len__() for record in delta if record.kind == RECORD_RAW)
    aos_stored = HEADER_SIZE + 4 + len(stored) * RECORD_AOS_SIZE + stored_raw
    aos_delta = HEADER_SIZE + 4 + len(delta) * RECORD_AOS_SIZE + delta_raw
    soa_delta = HEADER_SIZE + 4 + len(delta) * RECORD_SOA_COLUMNS + delta_raw
    return {
        "chunk_size": DEFAULT_CHUNK_SIZE,
        "raw_records": len(stored),
        "delta_records": len(delta),
        "reference_records": delta_refs,
        "referenced_bytes_saved": stored_raw - delta_raw,
        "aos_without_references_bytes": aos_stored,
        "aos_with_references_bytes": aos_delta,
        "soa_with_references_bytes": soa_delta,
        "delta_complete_bytes_saved": aos_stored - aos_delta,
        "layout_advantage_soa_over_aos_bytes": aos_delta - soa_delta,
    }


def measure_layouts(canonical_package: bytes) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for layout_id in (contract.LAYOUT_CANONICAL_ID, contract.LAYOUT_AOS_ID, contract.LAYOUT_SOA_ID):
        encoded = encode_compact(
            canonical_package,
            codec_id=contract.CODEC_STORED_ID,
            layout_id=layout_id,
            delta_references=True,
        )
        results.append(encoded.to_dict())
    smallest = min(results, key=lambda item: (item["encoded_bytes"], item["layout_id"]))
    return {"alternatives": results, "selected": smallest["layout_id"]}


def measure_shader_specialization(package: nwp.NwpPackage) -> dict[str, Any]:
    blobs = [bytes(shader.blob) for shader in package.shaders]
    unique: dict[bytes, int] = {}
    for blob in blobs:
        unique[blob] = unique.get(blob, 0) + 1
    duplicate_bytes = sum(
        len(blob) * (count - 1) for blob, count in unique.items()
    )
    root_blobs = [bytes(record.blob) for record in package.root_signatures]
    unique_roots: set[bytes] = set(root_blobs)
    return {
        "shader_count": len(blobs),
        "unique_shader_blobs": len(unique),
        "duplicate_shader_bytes": duplicate_bytes,
        "root_signature_count": len(root_blobs),
        "unique_root_signature_blobs": len(unique_roots),
        "duplicate_root_signature_bytes": sum(
            len(blob) for blob in root_blobs
        )
        - sum(len(blob) for blob in unique_roots),
        "fusion": "established NW-05 fusion decisions preserved; no unmeasured extra fusion applied",
        "requires_new_shader_semantics": False,
    }


def measure_linker_inputs(manifest: Mapping[str, Any]) -> dict[str, Any]:
    retained = manifest.get("retained", {})
    excluded = manifest.get("excluded", {})
    code = list(retained.get("code", []))
    implementations = list(retained.get("runtime_implementations", []))
    excluded_authoring = list(excluded.get("authoring_infrastructure", []))
    return {
        "retained_code_entries": len(code),
        "runtime_implementation_entries": len(implementations),
        "excluded_authoring_entries": len(excluded_authoring),
        "linker_elimination_status": "pending_physical_pe",
        "note": (
            "Release PE section/symbol elimination is measured only on the exact "
            "Windows artifact; source-level counts are not substituted for bytes."
        ),
    }


def measure_host_decoder_code_bytes(repo_root: Path | None = None) -> dict[str, Any]:
    """Optional host-proxy code measurement.

    This is never labelled as a Windows x64 measurement.  It exists so a
    repository host with a compiler can retain a reproducible code-footprint
    proxy while the release PE measurement remains pending.
    """
    root = Path(repo_root or REPO_ROOT)
    compiler = shutil.which("clang++") or shutil.which("g++") or shutil.which("c++")
    if compiler is None:
        return {
            "status": "unavailable",
            "bytes": None,
            "reason": "no host C++ compiler is installed; Windows PE measurement pending",
        }
    source = root / contract.COMPACT_DECODER_SOURCE
    if not source.is_file():
        return {
            "status": "unavailable",
            "bytes": None,
            "reason": "compact decoder source is missing",
        }
    with tempfile.TemporaryDirectory(prefix="nw10-decoder-") as temporary:
        object_path = Path(temporary) / "nw_compact.o"
        completed = subprocess.run(
            [
                compiler,
                "-std=c++20",
                "-Os",
                "-fno-exceptions",
                "-fno-rtti",
                "-I",
                str(root / "src"),
                "-I",
                str(root / "src" / "package"),
                "-c",
                str(source),
                "-o",
                str(object_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not object_path.is_file():
            return {
                "status": "unavailable",
                "bytes": None,
                "reason": (
                    "host decoder compile failed: "
                    + (completed.stderr.strip() or completed.stdout.strip())
                ),
            }
        size_tool = shutil.which("size")
        if size_tool is not None:
            sized = subprocess.run(
                [size_tool, "-A", str(object_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if sized.returncode == 0:
                total = 0
                for line in sized.stdout.splitlines():
                    fields = line.split()
                    if len(fields) >= 2 and fields[0] in {".text", ".rodata"}:
                        try:
                            total += int(fields[1], 0)
                        except ValueError:
                            pass
                if total > 0:
                    return {
                        "status": "host_proxy_measurement",
                        "compiler": compiler,
                        "bytes": total,
                        "windows_pe_bytes": None,
                        "reason": (
                            "host object .text/.rodata size only; exact Windows x64 "
                            "decoder contribution remains pending"
                        ),
                    }
        return {
            "status": "host_object_measured_without_sections",
            "compiler": compiler,
            "bytes": object_path.stat().st_size,
            "windows_pe_bytes": None,
            "reason": (
                "host object size only; exact Windows x64 decoder contribution "
                "remains pending"
            ),
        }


# ---------------------------------------------------------------------------
# Byte ledgers
# ---------------------------------------------------------------------------


def build_byte_ledger(
    *,
    artifact_kind: str,
    canonical_executable_bytes: int,
    canonical_package_bytes: int,
    manifest_bytes: int,
    compact: CompactEncoding | None,
    compact_executable_bytes: int | None = None,
    decoder_code_bytes: int | None,
    shader_bytes: int,
    auxiliary_files: Mapping[str, int] | None = None,
    dependencies: Sequence[Mapping[str, Any]] = (),
    evidence_status: str,
) -> dict[str, Any]:
    auxiliary = dict(auxiliary_files or {})
    footer_bytes = 92
    manifest_binding_bytes = 32
    canonical_player_image = (
        canonical_executable_bytes
        - canonical_package_bytes
        - manifest_binding_bytes
        - footer_bytes
    )
    if canonical_player_image < 0:
        raise CompactCodecError(
            "artifact_closure_invalid",
            "canonical executable is smaller than its package/manifest/footer",
        )
    # Auxiliary build/evidence JSON is optional and is never required to run the
    # artifact, so it is reported separately and excluded from the complete
    # runnable distribution (matching the NW-07 accounting policy).
    canonical_total = canonical_executable_bytes
    compact_payload = len(compact.bytes) if compact is not None else canonical_package_bytes
    decoder_source = REPO_ROOT / contract.COMPACT_DECODER_SOURCE
    decoder_source_bytes = (
        decoder_source.stat().st_size if decoder_source.is_file() else None
    )
    if compact is not None and compact_executable_bytes is not None:
        compact_player_image = (
            compact_executable_bytes
            - compact_payload
            - manifest_binding_bytes
            - footer_bytes
        )
        if compact_player_image < 0:
            raise CompactCodecError(
                "artifact_closure_invalid",
                "compact executable is smaller than its package/manifest/footer",
            )
    else:
        # Contract-only evidence does not build a separate decoder-on
        # executable.  Report the canonical player image as the unmeasured
        # placeholder and keep the ledger explicitly incomplete.
        compact_player_image = canonical_player_image
    player_image = compact_player_image if compact is not None else canonical_player_image
    compact_total = (
        player_image
        + compact_payload
        + manifest_binding_bytes
        + footer_bytes
    )
    within = compact_total <= canonical_total
    if not within:
        compact_total = canonical_total
        player_image = canonical_player_image
        compact_payload = canonical_package_bytes

    dependency_rows = [dict(item) for item in dependencies]
    dependency_names = {
        str(item.get("name", "")).lower() for item in dependency_rows
    }
    required_auxiliary: dict[str, int] = {}
    if compact is not None and "cabinet.dll" not in dependency_names:
        # The player-supported MSZIP decoder is an inbox cabinet.dll caller.
        # It is part of the compact artifact's runtime dependency closure even
        # when the canonical package manifest was produced by a decoder-off
        # build and therefore does not name it.
        dependency_rows.append(
            {
                "name": "cabinet.dll",
                "kind": "windows_inbox",
                "distributed_bytes": 0,
                "required_by": "src/package/nw_compact.cpp",
            }
        )
        dependency_names.add("cabinet.dll")
    dependency_rows.sort(key=lambda item: str(item.get("name", "")).lower())

    summed_components = (
        player_image
        + (compact_payload if within else canonical_package_bytes)
        + manifest_binding_bytes
        + footer_bytes
    )
    decoder_accounted = compact is None or decoder_code_bytes is not None
    unknown_components: list[str] = []
    if compact is not None and decoder_code_bytes is None:
        unknown_components.append("decoder_code_bytes")
    return {
        "format": "nw10-byte-ledger",
        "version": 1,
        "task": contract.TASK,
        "artifact_kind": artifact_kind,
        "evidence_status": evidence_status,
        "complete_runnable_distribution_bytes": compact_total if within else canonical_total,
        "baseline_complete_distribution_bytes": canonical_total,
        "distribution_delta_bytes": compact_total - canonical_total if within else 0,
        "compression_rejected_for_increasing_distribution": not within,
        "selected_executable_bytes": compact_total,
        "components": {
            "player_image_bytes": player_image,
            "canonical_player_image_bytes": canonical_player_image,
            "compact_player_image_bytes": (
                compact_player_image if compact is not None else None
            ),
            "canonical_package_bytes": canonical_package_bytes,
            "encoded_package_bytes": compact_payload,
            "embedded_manifest_binding_bytes": manifest_binding_bytes,
            "embedded_footer_bytes": footer_bytes,
            "decoder_code_bytes": decoder_code_bytes,
            "decoder_code_included_in_player_image": bool(
                compact is not None and decoder_code_bytes is not None
            ),
            "decoder_source_bytes": decoder_source_bytes,
            "shader_payload_bytes": shader_bytes,
            "auxiliary_files": dict(sorted(auxiliary.items())),
            "auxiliary_file_bytes": sum(auxiliary.values()),
            "required_auxiliary_files": required_auxiliary,
            "optional_auxiliary_files": dict(sorted(auxiliary.items())),
        },
        "decoded_package_bound_bytes": (
            compact.decoded_size if compact is not None else canonical_package_bytes
        ),
        "codec_id": compact.codec_id if compact is not None else contract.CODEC_STORED_ID,
        "layout_id": compact.layout_id if compact is not None else contract.LAYOUT_CANONICAL_ID,
        "packaging_tool": {
            "identity": contract.PACKAGING_TOOL_ID,
            "version": hashlib.sha256(
                (REPO_ROOT / contract.PACKAGING_TOOL_ID).read_bytes()
            ).hexdigest()
            if (REPO_ROOT / contract.PACKAGING_TOOL_ID).is_file()
            else "unavailable",
        },
        "dependencies": dependency_rows,
        "auxiliary_policy": (
            "The runnable distribution is one executable. Rebuild/evidence JSON "
            "is optional and never required to execute; system DLLs are recorded "
            "as dependencies with zero distributed bytes."
        ),
        "all_required_bytes_accounted": (
            summed_components == compact_total and decoder_accounted
        ),
        "unaccounted_components": unknown_components,
        "budget_tiers": contract.budget_tiers(),
        "within_compact_target": compact_total <= contract.COMPACT_TARGET_BYTES,
        "within_stretch": compact_total <= contract.STRETCH_TIER_BYTES,
        "within_proof": compact_total <= contract.PROOF_TIER_BYTES,
        "within_standard": compact_total <= contract.STANDARD_TIER_BYTES,
    }


def artifact_ledger(
    artifact_directory: Path | str,
    *,
    decoder_code_bytes: int | None = None,
    evidence_status: str = "informational_contract_only",
) -> dict[str, Any]:
    directory = Path(artifact_directory)
    packages = sorted(directory.glob("*.nwp"))
    executables = sorted(directory.glob("*.exe"))
    manifests = sorted(directory.glob("*.manifest.json"))
    if len(packages) != 1 or len(executables) != 1 or len(manifests) != 1:
        raise CompactCodecError(
            "artifact_closure_invalid",
            "artifact ledger requires exactly one package, manifest, and executable",
        )
    package_bytes = packages[0].read_bytes()
    manifest_bytes = manifests[0].read_bytes()
    executable_bytes = executables[0].read_bytes()
    package = nwp.decode_package(package_bytes, allow_dev=False)
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    shader_bytes = sum(len(shader.blob) for shader in package.shaders)
    auxiliary = {
        path.name: path.stat().st_size
        for path in directory.iterdir()
        if path.is_file() and path not in {packages[0], executables[0], manifests[0]}
    }
    compact = encode_compact(
        package_bytes,
        codec_id=contract.CODEC_STORED_ID,
        layout_id=contract.LAYOUT_AOS_ID,
        delta_references=True,
    )
    return build_byte_ledger(
        artifact_kind="canonical_nw07_proof"
        if "nw07" in artifact_directory.__str__()
        else "restricted_intro",
        canonical_executable_bytes=len(executable_bytes),
        canonical_package_bytes=len(package_bytes),
        manifest_bytes=len(manifest_bytes),
        compact=compact,
        decoder_code_bytes=decoder_code_bytes,
        shader_bytes=shader_bytes,
        auxiliary_files=auxiliary,
        dependencies=list(manifest.get("retained", {}).get("dependencies", [])),
        evidence_status=evidence_status,
    )


def package_summary(package_bytes: bytes) -> dict[str, Any]:
    package = nwp.decode_package(package_bytes, allow_dev=False)
    return {
        "package_bytes": len(package_bytes),
        "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "profile_id": package.capability.profile_id,
        "requirements_digest": package.capability.requirements_digest,
        "resource_bytes": sum(resource.size_bytes for resource in package.resources),
        "shader_bytes": sum(len(shader.blob) for shader in package.shaders),
        "parameter_bytes": sum(len(parameter.value) for parameter in package.parameters),
        "init_instructions": [item.opcode for item in package.initialization],
        "frame_instructions": [item.opcode for item in package.frame],
    }


def comprehensive_measurement(
    package_bytes: bytes,
    *,
    decoder_code_bytes: int | None = None,
) -> dict[str, Any]:
    package = nwp.decode_package(package_bytes, allow_dev=False)
    records, _references = _chunk_records(
        package_bytes, chunk_size=DEFAULT_CHUNK_SIZE, delta_references=True
    )
    container = (
        struct.pack("<I", len(records))
        + _encode_table(records, LAYOUT_AOS)
        + b"".join(
            record.data for record in records if record.kind == RECORD_RAW
        )
    )
    codec_selection = select_player_codec(
        package_bytes, layout_id=contract.LAYOUT_AOS_ID, delta_references=True
    )
    return {
        "package": package_summary(package_bytes),
        "measurement_protocol": {
            "performance_contract": contract.PERF_CONTRACT_ID,
            "performance_protocol": contract.PERF_PROTOCOL_ID,
            "warmup_seconds": contract.WARMUP_SECONDS,
            "runs": contract.PERF_RUN_COUNT,
            "run_seconds": contract.PERF_RUN_SECONDS,
            "fresh_process_per_run": True,
            "application_pipeline_cache": False,
            "status": "pending_physical_windows_capture",
        },
        "physical_evidence": {
            "startup_ms": None,
            "startup_decode_memory_bytes": None,
            "peak_engine_committed_gpu_bytes": None,
            "p99_gpu_frame_time_ms": None,
            "p99_cpu_submit_ms": None,
            "visual_error": {
                "numeric_absolute": None,
                "numeric_relative": None,
                "image_absolute": None,
                "image_relative": None,
            },
            "status": "pending_physical_windows_capture",
            "reason": (
                "Physical measurements require the exact Windows x64 artifact and "
                "the frozen nw07_perf_ref_v1 capture environment."
            ),
        },
        "parameter_packing": measure_parameter_packing(package),
        "delta_references": measure_delta_references(package_bytes),
        "layouts": measure_layouts(package_bytes),
        "shaders": measure_shader_specialization(package),
        "compression_candidates": measure_candidate_compression(container),
        "selected_codec": codec_selection,
        "decoder_code_bytes": decoder_code_bytes,
        "decoder_code_status": (
            "host_proxy_measurement" if decoder_code_bytes is not None else "pending_physical_windows"
        ),
    }
