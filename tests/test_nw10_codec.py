from __future__ import annotations

import hashlib
import os
import random
import shutil
import struct
import subprocess
import sys
import zlib
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
    if sys.platform != "win32":
        assert compact.mszip_compress(payload) == encoded
        assert encoded[:2] == b"CK"
    assert compact.mszip_decompress(encoded, max_output_bytes=len(payload)) == payload


def test_mszip_encoding_identity_is_host_scoped() -> None:
    payload = bytes((index * 37 + 11) & 0xFF for index in range(100_000))
    encoded = compact.mszip_compress(payload)
    identity = compact.mszip_encoding_identity()
    assert identity["encoding_id"] == "nwc.codec.mszip.v1"
    assert identity["cross_platform_byte_parity"] is False
    assert identity["decoded_package_sha256_invariant"] is True
    assert identity["encoded_payload_hash_scope"] == "host_specific"
    assert compact.contract.CODEC_MSZIP_ID == identity["codec_id"]
    if sys.platform != "win32":
        assert hashlib.sha256(encoded).hexdigest() == (
            "6306329d17df2af293189ecac16f1ad2a5611a5d751d9170970ea4a36cfdd07f"
        )
        assert compact._zlib_mszip_decompress(encoded, max_output_bytes=len(payload)) == payload


def test_windows_mszip_dispatches_to_inbox_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compact.sys, "platform", "win32")
    monkeypatch.setattr(compact, "_windows_mszip_compress", lambda data: b"native:" + data)
    monkeypatch.setattr(
        compact, "_windows_mszip_decompress",
        lambda data, *, max_output_bytes: data.removeprefix(b"native:"),
    )
    encoded = compact.mszip_compress(b"payload")
    assert encoded == b"native:payload"
    assert compact.mszip_decompress(encoded, max_output_bytes=7) == b"payload"


def test_mszip_reference_decoder_rejects_malformed_truncated_corrupt_and_expansion() -> None:
    if sys.platform == "win32":
        pytest.skip("portable CK reference decoder is exercised on non-Windows hosts")
    payload = bytes((index * 131 + 7) & 0xFF for index in range(70_000))
    encoded = compact._zlib_mszip_compress(payload)
    assert compact._zlib_mszip_decompress(
        encoded, max_output_bytes=len(payload)
    ) == payload

    with pytest.raises(compact.CompactCodecError) as caught:
        compact._zlib_mszip_decompress(
            b"XX" + encoded[2:], max_output_bytes=len(payload)
        )
    assert caught.value.code == "compact_codec_malformed"

    with pytest.raises(compact.CompactCodecError) as caught:
        compact._zlib_mszip_decompress(
            encoded[: len(encoded) // 2], max_output_bytes=len(payload)
        )
    assert caught.value.code in {
        "compact_codec_malformed",
        "compact_decode_limit",
    }

    corrupted = bytearray(encoded)
    corrupted[3] ^= 0xFF
    with pytest.raises(compact.CompactCodecError) as caught:
        compact._zlib_mszip_decompress(
            bytes(corrupted), max_output_bytes=len(payload)
        )
    assert caught.value.code == "compact_codec_malformed"

    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    bomb = (
        b"CK"
        + compressor.compress(b"\x00" * (compact.MSZIP_BLOCK_BYTES + 1))
        + compressor.flush()
    )
    with pytest.raises(compact.CompactCodecError) as caught:
        compact._zlib_mszip_decompress(
            bomb, max_output_bytes=compact.MSZIP_BLOCK_BYTES + 1
        )
    assert caught.value.code == "compact_decode_limit"


def test_parameter_packing_preserves_declared_seed_bits_by_identity() -> None:
    parameters = [
        nwp.ParameterRecord(
            parameter_id=1,
            name="nw05.material_recipe.seed",
            type="float",
            phase="initialization",
            component_count=1,
            value=struct.pack("<I", 0x4E9A9E6B),
        ),
        nwp.ParameterRecord(
            parameter_id=2,
            name="nw05.material_recipe.radius",
            type="float",
            phase="initialization",
            component_count=1,
            value=struct.pack("<f", 0.25),
        ),
    ]
    f16 = compact._parameter_layout_size(parameters, value_encoding="f16")
    assert "nw05.material_recipe.seed" in f16["non_float_parameter_names"]
    assert f16["non_float_components_preserved"] is True
    assert f16["maximum_absolute_error"] == 0.0

    q16 = compact._parameter_layout_size(parameters, value_encoding="q16")
    assert "nw05.material_recipe.seed" in q16["non_float_parameter_names"]
    assert q16["non_float_components_preserved"] is True
    assert q16["maximum_absolute_error"] < 1.0
    assert q16["maximum_absolute_error"] > 0.0


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


def test_shipped_cpp_decoder_matches_python_encoder_and_fuzzes_cleanly(
    tmp_path: Path,
) -> None:
    compiler = os.environ.get("CXX") or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("no C++ compiler available for the shipped-decoder parity harness")
    canonical = _package_bytes()
    stored = compact.encode_compact(
        canonical,
        codec_id=contract.CODEC_STORED_ID,
        layout_id=contract.LAYOUT_AOS_ID,
        delta_references=True,
    )
    mszip = compact.encode_compact(
        canonical,
        codec_id=contract.CODEC_MSZIP_ID,
        layout_id=contract.LAYOUT_SOA_ID,
        delta_references=True,
    )
    (tmp_path / "canonical.bin").write_bytes(canonical)
    (tmp_path / "stored.nwc").write_bytes(stored.bytes)
    (tmp_path / "mszip.nwc").write_bytes(mszip.bytes)
    binary = tmp_path / (
        "nw10_compact_decoder_fuzz.exe"
        if sys.platform == "win32"
        else "nw10_compact_decoder_fuzz"
    )
    command = [
        compiler,
        "-std=c++20",
        "-I",
        str(REPO_ROOT / "src"),
        "-I",
        str(REPO_ROOT / "src" / "package"),
        str(REPO_ROOT / "tests" / "nw10_compact_decoder_fuzz.cpp"),
        str(REPO_ROOT / "src" / "package" / "nw_compact.cpp"),
        str(REPO_ROOT / "src" / "package" / "nwp_format.cpp"),
        "-o",
        str(binary),
    ]
    if sys.platform != "win32":
        # libstdc++ debug iterators catch accidental self-range vector inserts.
        command.insert(2, "-D_GLIBCXX_DEBUG")
    if sys.platform == "win32":
        # MinGW ignores MSVC's #pragma comment(lib, "cabinet.lib"); the shipped
        # decoder's Compression API calls still need the inbox import library.
        command.append("-lcabinet")
    commands = [command]
    if sys.platform == "win32":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        commands.append(
            command[:-1] + [str(windir / "System32" / "cabinet.dll")]
        )
    completed = None
    for candidate in commands:
        completed = subprocess.run(
            candidate,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            break
    assert completed.returncode == 0, completed.stderr
    decoded = subprocess.run(
        [
            str(binary),
            str(tmp_path / "canonical.bin"),
            str(tmp_path / "stored.nwc"),
            str(tmp_path / "mszip.nwc"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert decoded.returncode == 0, decoded.stderr


def test_byte_ledger_is_complete_only_with_a_measured_decoder_cost() -> None:
    canonical = _package_bytes()
    encoded = compact.encode_compact(
        canonical,
        codec_id=contract.CODEC_MSZIP_ID,
        layout_id=contract.LAYOUT_SOA_ID,
    )
    canonical_player = 200
    decoder = 512
    ledger = compact.build_byte_ledger(
        artifact_kind="canonical_nw07_proof",
        canonical_executable_bytes=len(canonical) + 32 + 92 + canonical_player,
        canonical_package_bytes=len(canonical),
        manifest_bytes=1024,
        compact=encoded,
        compact_executable_bytes=(
            len(encoded.bytes) + 32 + 92 + canonical_player + decoder
        ),
        decoder_code_bytes=decoder,
        shader_bytes=0,
        dependencies=[{"name": "d3d12", "kind": "windows_inbox"}],
        evidence_status="informational_contract_only",
    )
    assert ledger["all_required_bytes_accounted"] is True
    assert ledger["unaccounted_components"] == []
    assert ledger["components"]["decoder_code_bytes"] == decoder
    assert ledger["components"]["compact_player_image_bytes"] == canonical_player + decoder
    assert "cabinet.dll" in {row["name"].lower() for row in ledger["dependencies"]}


def _harness_container(
    source: bytes,
    layout: int,
    *,
    use_reference: bool = False,
    forward: bool = False,
    short: bool = False,
) -> bytes:
    """Mirror tests/nw10_compact_decoder_fuzz.cpp container construction."""
    count = 3
    table = bytearray()
    raw = bytearray()
    if layout == compact.LAYOUT_SOA:
        kinds = bytearray(count)
        refs: list[int] = []
        lengths: list[int] = []
        offsets: list[int] = []
        for index in range(count):
            start = index * len(source) // count
            end = (index + 1) * len(source) // count
            if use_reference and index == 1:
                kinds[index] = compact.RECORD_REFERENCE
                refs.append(2 if forward else 0)
                lengths.append((end - start) - (1 if short else 0))
                offsets.append(0)
            else:
                kinds[index] = compact.RECORD_RAW
                refs.append(0)
                lengths.append(end - start)
                offsets.append(len(raw))
                raw += source[start:end]
        table += kinds
        for values in (refs, lengths, offsets):
            for value in values:
                table += struct.pack("<I", value)
    else:
        for index in range(count):
            start = index * len(source) // count
            end = (index + 1) * len(source) // count
            if use_reference and index == 1:
                table += struct.pack(
                    "<BBIII",
                    compact.RECORD_REFERENCE,
                    0,
                    2 if forward else 0,
                    (end - start) - (1 if short else 0),
                    0,
                )
            else:
                table += struct.pack(
                    "<BBIII", compact.RECORD_RAW, 0, 0, end - start, len(raw)
                )
                raw += source[start:end]
    return struct.pack("<I", count) + bytes(table) + bytes(raw)


def test_cpp_fuzz_harness_container_construction_round_trips() -> None:
    canonical = _package_bytes()
    chunk = len(canonical) // 3
    delta = canonical[:chunk] + canonical[:chunk] + canonical[-chunk:]
    for layout in (compact.LAYOUT_AOS, compact.LAYOUT_SOA):
        raw = _harness_container(canonical, layout)
        assert compact.decode_compact(
            _wrap_container(
                raw,
                decoded_size=len(canonical),
                sha=hashlib.sha256(canonical).digest(),
                layout=layout,
            )
        ) == canonical
        delta_container = _harness_container(delta, layout, use_reference=True)
        assert compact.decode_compact(
            _wrap_container(
                delta_container,
                decoded_size=len(delta),
                sha=hashlib.sha256(delta).digest(),
                layout=layout,
            )
        ) == delta
        for forward, short in ((True, False), (False, True)):
            bad = _harness_container(
                delta, layout, use_reference=True, forward=forward, short=short
            )
            with pytest.raises(compact.CompactCodecError):
                compact.decode_compact(
                    _wrap_container(
                        bad,
                        decoded_size=len(delta),
                        sha=hashlib.sha256(delta).digest(),
                        layout=layout,
                    )
                )
