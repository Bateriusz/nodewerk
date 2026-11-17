from __future__ import annotations

import hashlib
import random
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw10_compact as compact  # noqa: E402
import nw10_contract as contract  # noqa: E402


def _package_bytes() -> bytes:
    package = nwp.make_test_package(shader_blob=nw04_fixture.make_dxil_container())
    return nwp.encode_package(package)


@pytest.mark.parametrize("codec_id", contract.PLAYER_SUPPORTED_CODEC_IDS)
@pytest.mark.parametrize("layout_id", contract.KNOWN_LAYOUT_IDS)
def test_compact_container_round_trips_all_supported_identities(
    codec_id: str, layout_id: str
) -> None:
    canonical = _package_bytes()
    encoded = compact.encode_compact(
        canonical, codec_id=codec_id, layout_id=layout_id, delta_references=True
    )
    assert compact.is_compact_container(encoded.bytes)
    assert compact.decode_compact(encoded.bytes) == canonical
    valid, details = compact.validate_compact(encoded.bytes)
    assert valid is True
    assert details["canonical_sha256"] == hashlib.sha256(canonical).hexdigest()
    nwp.decode_package(compact.decode_compact(encoded.bytes), allow_dev=False)


def test_soa_layout_is_never_larger_than_aos_for_the_record_table() -> None:
    canonical = _package_bytes()
    aos = compact.encode_compact(
        canonical,
        codec_id=contract.CODEC_STORED_ID,
        layout_id=contract.LAYOUT_AOS_ID,
    )
    soa = compact.encode_compact(
        canonical,
        codec_id=contract.CODEC_STORED_ID,
        layout_id=contract.LAYOUT_SOA_ID,
    )
    assert soa.record_count == aos.record_count
    assert len(soa.bytes) <= len(aos.bytes)


def test_delta_references_are_deterministic_and_backward_only() -> None:
    canonical = b"".join(bytes([index]) * 256 for index in range(8)) * 4
    delta = compact.encode_compact(
        canonical,
        codec_id=contract.CODEC_STORED_ID,
        layout_id=contract.LAYOUT_AOS_ID,
        delta_references=True,
    )
    assert delta.reference_count > 0
    assert compact.decode_compact(delta.bytes) == canonical
    repeat = compact.encode_compact(
        canonical,
        codec_id=contract.CODEC_STORED_ID,
        layout_id=contract.LAYOUT_AOS_ID,
        delta_references=True,
    )
    assert repeat.bytes == delta.bytes


def test_mszip_handles_incompressible_and_multiblock_inputs() -> None:
    payload = bytes((index * 37 + 11) & 0xFF for index in range(100_000))
    encoded = compact.mszip_compress(payload)
    assert compact.mszip_decompress(
        encoded, max_output_bytes=len(payload)
    ) == payload


def _stored_container_with_table(
    records: list[tuple[int, int, int, int]], raw: bytes
) -> bytes:
    table = bytearray()
    for kind, reference, length, offset in records:
        table += struct.pack("<BBIII", kind, 0, reference, length, offset)
    return struct.pack("<I", len(records)) + bytes(table) + raw


def _wrap_container(
    container: bytes,
    *,
    decoded_size: int | None = None,
    sha: bytes | None = None,
    codec: int = compact.CODEC_STORED,
    layout: int = compact.LAYOUT_AOS,
    flags: int = compact.FLAG_RECORD_LAYOUT | compact.FLAG_DELTA_REFERENCES,
) -> bytes:
    payload = container if codec == compact.CODEC_STORED else compact.mszip_compress(container)
    content = b"\x00" * 16
    header = compact.HEADER.pack(
        compact.MAGIC,
        1,
        compact.HEADER_SIZE,
        codec,
        layout,
        flags,
        decoded_size if decoded_size is not None else len(content),
        len(payload),
        len(container),
        sha if sha is not None else hashlib.sha256(content).digest(),
    )
    return header + payload


def test_forward_and_out_of_range_references_fail_closed() -> None:
    raw = b"A" * 8 + b"B" * 8
    for records in (
        [(compact.RECORD_REFERENCE, 0, 8, 0), (compact.RECORD_RAW, 0, 8, 0)],
        [(compact.RECORD_RAW, 0, 8, 0), (compact.RECORD_REFERENCE, 9, 8, 0)],
        [(compact.RECORD_RAW, 0, 8, 0), (compact.RECORD_REFERENCE, 0, 4, 0)],
    ):
        container = _stored_container_with_table(records, raw)
        data = _wrap_container(container, decoded_size=16, sha=hashlib.sha256(b"A" * 8 + b"B" * 8).digest())
        with pytest.raises(compact.CompactCodecError):
            compact.decode_compact(data)


def test_truncated_payload_and_header_fail_before_gpu_use() -> None:
    canonical = _package_bytes()
    encoded = compact.encode_compact(canonical)
    for candidate in (
        encoded.bytes[:-1],
        encoded.bytes[: compact.HEADER_SIZE - 1],
    ):
        with pytest.raises(compact.CompactCodecError):
            compact.decode_compact(candidate)


def test_unknown_codec_layout_version_and_flags_fail_deterministically() -> None:
    canonical = _package_bytes()
    encoded = bytearray(compact.encode_compact(canonical).bytes)
    mutations = (
        (12, struct.pack("<H", 99), "compact_codec_unsupported"),
        (14, struct.pack("<H", 99), "compact_layout_unsupported"),
        (8, struct.pack("<H", 9), "compact_version_unsupported"),
        (16, struct.pack("<I", 0x80000000), "compact_flags_unsupported"),
    )
    for offset, replacement, code in mutations:
        candidate = bytearray(encoded)
        candidate[offset : offset + len(replacement)] = replacement
        with pytest.raises(compact.CompactCodecError) as caught:
            compact.decode_compact(bytes(candidate))
        assert caught.value.code == code


def test_checksum_mismatch_and_worst_case_expansion_are_rejected() -> None:
    canonical = _package_bytes()
    encoded = bytearray(compact.encode_compact(canonical).bytes)
    encoded[compact.HEADER_SIZE : compact.HEADER_SIZE + 8] = b"\x00" * 8
    with pytest.raises(compact.CompactCodecError) as caught:
        compact.decode_compact(bytes(encoded))
    assert caught.value.code in {
        "compact_checksum_mismatch",
        "compact_codec_malformed",
        "compact_reference_invalid",
        "compact_size_mismatch",
        "compact_malformed",
    }

    header = bytearray(compact.encode_compact(canonical, codec_id=contract.CODEC_MSZIP_ID).bytes)
    struct.pack_into("<I", header, 20, compact.MAX_DECODED_BYTES)
    struct.pack_into("<I", header, 28, 8)
    with pytest.raises(compact.CompactCodecError) as caught:
        compact.decode_compact(bytes(header))
    assert caught.value.code in {"compact_decode_limit", "compact_size_mismatch"}


def test_record_count_and_offset_arithmetic_are_bounded() -> None:
    container = struct.pack("<I", compact.MAX_RECORDS + 1) + b"\x00" * 32
    data = _wrap_container(container, decoded_size=32)
    with pytest.raises(compact.CompactCodecError) as caught:
        compact.decode_compact(data)
    assert caught.value.code == "compact_decode_limit"

    records = [(compact.RECORD_RAW, 0, 64, 0)]
    container = _stored_container_with_table(records, b"x" * 8)
    data = _wrap_container(container, decoded_size=64)
    with pytest.raises(compact.CompactCodecError):
        compact.decode_compact(data)


def test_validation_reports_errors_instead_of_raising() -> None:
    valid, details = compact.validate_compact(b"not a container")
    assert valid is False
    assert details["valid"] is False
    assert details["error"]["code"] == "compact_magic"


def test_deterministic_mutation_fuzz_never_raises_or_allocates_unbounded() -> None:
    rng = random.Random(0x4E573130)
    base = compact.encode_compact(_package_bytes()).bytes
    for _ in range(200):
        candidate = bytearray(base)
        mutations = rng.randint(1, 4)
        for _index in range(mutations):
            position = rng.randrange(len(candidate))
            candidate[position] ^= rng.randrange(1, 256)
        valid, details = compact.validate_compact(bytes(candidate))
        assert valid is False or details["valid"] is True
        assert isinstance(details, dict)
