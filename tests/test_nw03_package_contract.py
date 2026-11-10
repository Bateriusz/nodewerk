from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nwp_package as nwp  # noqa: E402


def make_dxil_container() -> bytes:
    sfi0 = b"SFI0" + struct.pack("<I", 8) + struct.pack("<Q", 0)
    psv0_info = bytearray(36)
    struct.pack_into("<III", psv0_info, 12, 64, 1, 1)
    psv0_payload = (
        struct.pack("<I", len(psv0_info)) + bytes(psv0_info) + struct.pack("<II", 0, 1)
    )
    psv0 = b"PSV0" + struct.pack("<I", len(psv0_payload)) + psv0_payload
    words = [0x00000021, 0x00000002, 0x00000021, 0x0000000C] + [0] * 16
    bitcode = b"BC\xc0\xde" + struct.pack(f"<{len(words)}I", *words)
    dxil_payload = (
        struct.pack(
            "<II4sIII",
            (5 << 16) | (6 << 4),
            (24 + len(bitcode)) // 4,
            b"DXIL",
            0x00010100,
            16,
            len(bitcode),
        )
        + bitcode
    )
    dxil = b"DXIL" + struct.pack("<I", len(dxil_payload)) + dxil_payload
    chunks = [sfi0, psv0, dxil]
    header_size = 32 + len(chunks) * 4
    offsets = []
    cursor = header_size
    for chunk in chunks:
        offsets.append(cursor)
        cursor += len(chunk)
    joined = b"".join(chunks)
    digest = hashlib.blake2s(b"dxc-retail-sig:" + joined, digest_size=16).digest()
    header = (
        b"DXBC"
        + digest
        + struct.pack("<HHII", 1, 0, cursor, len(chunks))
        + struct.pack(f"<{len(chunks)}I", *offsets)
    )
    return header + joined


def valid_package() -> nwp.NwpPackage:
    return nwp.make_test_package(
        shader_blob=make_dxil_container(), specialization_count=2
    )


def test_package_roundtrip_is_deterministic_and_inspectable(tmp_path: Path) -> None:
    package = valid_package()
    first = nwp.encode_package(package)
    second = nwp.encode_package(valid_package())
    assert first == second
    assert nwp.sha256_hex(first) == nwp.sha256_hex(second)
    # The canonical directory records semantic record counts.
    assert struct.unpack_from("<I", first, 64 + 8)[0] == 1  # CAP record
    assert struct.unpack_from("<I", first, 64 + 32 + 8)[0] == 3  # FRAM instructions

    decoded = nwp.decode_package(first)
    assert nwp.encode_package(decoded) == first

    inspection = nwp.inspect_package_bytes(first)
    assert inspection.valid
    assert inspection.diagnostics == ()
    payload = inspection.to_dict()
    assert payload["format"] == "nwp"
    assert payload["version"] == {"major": 1, "minor": 0}
    assert payload["checksum"]["purpose"] == "corruption_detection_not_authenticity"
    assert payload["package_sha256"] == hashlib.sha256(first).hexdigest()
    assert payload["capability"]["profile_id"] == nwp.COMMON_PROFILE_ID
    assert payload["capability"]["requirements_digest"] == nwp.COMMON_REQUIREMENTS_DIGEST
    assert payload["capability"]["feature_level"] == 0xC000
    assert payload["capability"]["feature_level_text"] == "12_0"
    assert payload["capability"]["shader_model"] == 0x60
    assert payload["capability"]["shader_model_text"] == "6_0"
    assert payload["capability"]["resource_binding_tier"] == 2
    assert payload["capability"]["resource_heap_tier"] == 2
    assert payload["capability"]["limits"]["max_root_signature_dword_cost"] == 16
    assert payload["capability"]["runtime_requirements"]["runtime_dxc_dependency"] is False
    assert payload["capability"]["optional_profile"]["profile_id"] == ""
    assert payload["capability"]["emitted_shader_targets"] == ["cs_6_0"]
    assert payload["deduplication"]["shader_specialization_count"] == 2
    assert payload["deduplication"]["unique_shader_blob_count"] == 1
    assert payload["release"]["contains_authoring_metadata"] is False
    assert payload["diagnostics"] == []


def test_package_writer_is_reproducible_across_directories(tmp_path: Path) -> None:
    hashes = []
    for name in ("a", "b"):
        directory = tmp_path / name
        directory.mkdir()
        destination = directory / "fixture.nwp"
        nwp.write_package_atomic(valid_package(), destination)
        hashes.append(hashlib.sha256(destination.read_bytes()).hexdigest())
    assert hashes[0] == hashes[1]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda package: replace(
            package,
            capability=replace(
                package.capability,
                profile_id="nw.common.d3d12.fl12_0.sm6_0.v2",
            ),
        ),
        lambda package: replace(
            package,
            capability=replace(
                package.capability,
                requirements_digest="sha256:v1:" + "a" * 64,
            ),
        ),
        lambda package: replace(
            package,
            capability=replace(package.capability, shader_model=0x66),
        ),
        lambda package: replace(
            package,
            capability=replace(package.capability, feature_level=0xC100),
        ),
        lambda package: replace(
            package,
            capability=replace(package.capability, runtime_dxc_dependency=True),
        ),
        lambda package: replace(
            package,
            capability=replace(package.capability, runtime_slang_dependency=True),
        ),
        lambda package: replace(
            package,
            capability=replace(
                package.capability,
                optional_profile_id="nw.optional.unknown.v1",
                optional_requirements_digest="sha256:v1:" + "b" * 64,
            ),
        ),
        lambda package: replace(
            package,
            resources=[
                replace(package.resources[1], width=8193),
                package.resources[0],
            ],
        ),
        lambda package: replace(
            package,
            resources=[
                replace(package.resources[1], size_bytes=0),
                package.resources[0],
            ],
        ),
        lambda package: replace(
            package,
            frame=[nwp.Instruction("frame.unknown_primitive")],
        ),
        lambda package: replace(
            package,
            frame=[
                replace(
                    package.frame[0],
                    operands=(nwp.InstructionOperand("root_signature_ref", 99),),
                )
            ],
        ),
        lambda package: replace(
            package,
            parameters=[
                replace(package.parameters[0], component_count=4, value=b"\x00" * 4)
            ],
        ),
        lambda package: replace(
            package,
            shaders=[
                replace(package.shaders[0], target="gs_6_0"),
                package.shaders[1],
            ],
        ),
    ],
)
def test_package_validation_rejects_contradictions(mutator) -> None:
    package = valid_package()
    bad = mutator(package)
    diagnostics = nwp.validate_package_model(bad)
    assert diagnostics
    assert all(item["category"] == "package_validation" for item in diagnostics)
    with pytest.raises(nwp.PackageValidationError):
        nwp.encode_package(bad)


def _with_recomputed_checksum(data: bytes) -> bytes:
    mutated = bytearray(data)
    checksum_offset = struct.unpack_from("<Q", mutated, 48)[0]
    mutated[checksum_offset:] = hashlib.sha256(bytes(mutated[:checksum_offset])).digest()
    return bytes(mutated)


def test_package_reader_rejects_header_directory_and_checksum_defects() -> None:
    data = nwp.encode_package(valid_package())

    cases = {
        "magic": b"BAD!" + data[4:],
        "version": _with_recomputed_checksum(
            (lambda value: value)(bytearray(data[:8] + struct.pack("<H", 2) + data[10:]))
        ),
        "checksum": data[:-1] + bytes([data[-1] ^ 1]),
        "truncated": data[:-1],
    }
    for name, candidate in cases.items():
        valid, diagnostics = nwp.validate_package_bytes(candidate)
        assert not valid, name
        assert diagnostics
        assert all(item["category"] == "package_validation" for item in diagnostics)

    # Section count cannot exceed the directory/filesize bounds.
    bad_count = bytearray(data)
    struct.pack_into("<I", bad_count, 40, nwp.MAX_SECTION_COUNT + 1)
    assert not nwp.validate_package_bytes(_with_recomputed_checksum(bad_count))[0]

    # Unknown section ID reaches the checked directory validator.
    unknown = bytearray(data)
    unknown[64:68] = b"ZZZZ"
    valid, diagnostics = nwp.validate_package_bytes(_with_recomputed_checksum(unknown))
    assert not valid
    assert any(item["code"] == "package_section_unknown" for item in diagnostics)

    # Directory record counts must agree with the decoded section payload.
    bad_records = bytearray(data)
    struct.pack_into("<I", bad_records, 64 + 8, 999)
    valid, diagnostics = nwp.validate_package_bytes(
        _with_recomputed_checksum(bad_records)
    )
    assert not valid
    assert any(item["code"] == "package_section_record_count" for item in diagnostics)


def test_reader_rejects_duplicate_blob_tables() -> None:
    package = valid_package()
    data = bytearray(nwp.encode_package(package))
    # Locate SHDR and its blob table, then append a second complete blob and
    # update counts by rebuilding the section through the private encoder path.
    shdr_payload = nwp._encode_shaders(package.shaders)
    duplicate = (
        shdr_payload[:2]
        + struct.pack("<I", 2)
        + struct.pack("<I", len(package.shaders[0].blob))
        + package.shaders[0].blob
        + struct.pack("<I", len(package.shaders[0].blob))
        + package.shaders[0].blob
        + shdr_payload[6:]
    )
    diagnostics: list[dict] = []
    decoded = nwp._decode_shaders(duplicate, diagnostics)
    assert decoded is None
    assert any(item["code"] == "package_shader_blob_not_deduplicated" for item in diagnostics)


def test_release_and_development_metadata_are_separated(tmp_path: Path) -> None:
    dev_package = replace(valid_package(), flags=0, dev_metadata=b'{"kind":"dev"}')
    dev_bytes = nwp.encode_package(dev_package)
    valid, diagnostics = nwp.validate_package_bytes(dev_bytes, allow_dev=False)
    assert not valid
    assert any(item["code"] == "package_section_forbidden" for item in diagnostics)
    valid, diagnostics = nwp.validate_package_bytes(dev_bytes, allow_dev=True)
    assert valid, diagnostics

    release_bytes = nwp.encode_package(valid_package())
    report = nwp.leak_scan_report(release_bytes)
    assert report["valid"], report
    assert report["checks"]["no_source_paths"]
    assert report["checks"]["no_reflection_tables"]


def test_atomic_publication_preserves_previous_output_on_failure(tmp_path: Path) -> None:
    destination = tmp_path / "fixture.nwp"
    nwp.write_package_atomic(valid_package(), destination)
    previous = destination.read_bytes()
    bad = replace(
        valid_package(),
        capability=replace(
            valid_package().capability,
            requirements_digest="sha256:v1:" + "f" * 64,
        ),
    )
    with pytest.raises(nwp.PackageValidationError):
        nwp.write_package_atomic(bad, destination)
    assert destination.read_bytes() == previous
    assert not list(tmp_path.glob(".nwp-*.tmp"))


def test_package_capability_rejects_unused_requirements() -> None:
    package = valid_package()
    bad = replace(
        package,
        capability=replace(
            package.capability,
            required_dxgi_formats=nwp.REQUIRED_DXGI_FORMATS
            + ("DXGI_FORMAT_R16G16B16A16_FLOAT",),
        ),
    )
    diagnostics = nwp.validate_package_model(bad)
    assert any(item["code"] == "package_capability_format_set" for item in diagnostics)


def test_package_optional_profile_identity_roundtrips() -> None:
    package = valid_package()
    optional = replace(
        package,
        capability=replace(
            package.capability,
            optional_profile_id=nwp.OPTIONAL_PROFILE_WAVE_OPS,
            optional_requirements_digest=nwp.OPTIONAL_PROFILE_WAVE_OPS_DIGEST,
            optional_profile_separate_artifact=True,
        ),
    )
    optional = replace(
        optional,
        capability=replace(
            optional.capability,
            profile_fingerprint=optional.capability.computed_profile_fingerprint(),
        ),
    )
    data = nwp.encode_package(optional)
    decoded = nwp.decode_package(data)
    assert decoded.capability.optional_profile_id == nwp.OPTIONAL_PROFILE_WAVE_OPS
    assert (
        decoded.capability.optional_requirements_digest
        == nwp.OPTIONAL_PROFILE_WAVE_OPS_DIGEST
    )


def test_cpp_package_reader_parity_when_compiler_available(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip(
            "Cross-language package parity is exercised on POSIX hosts; "
            "Windows CMake builds cover the native package library."
        )
    compiler = None
    for name in ("clang++", "g++", "c++"):
        compiler = shutil.which(name)
        if compiler:
            break
    if compiler is None:
        pytest.skip("no host C++ compiler is available")
    package_bytes = nwp.encode_package(valid_package())
    package_path = tmp_path / "python.nwp"
    package_path.write_bytes(package_bytes)
    harness = tmp_path / "harness.cpp"
    harness.write_text(
        '#include "nwp_format.h"\n'
        "#include <cstdio>\n"
        "#include <fstream>\n"
        "#include <iterator>\n"
        "#include <vector>\n"
        "int main(int argc, char** argv) {\n"
        "  if (argc < 3) return 2;\n"
        "  std::ifstream in(argv[1], std::ios::binary);\n"
        "  std::vector<std::uint8_t> data((std::istreambuf_iterator<char>(in)),\n"
        "                                 std::istreambuf_iterator<char>());\n"
        "  nw::package::Package package;\n"
        "  std::vector<nw::package::Diagnostic> diagnostics;\n"
        "  if (!nw::package::DecodePackage(data.data(), data.size(), package, diagnostics)) {\n"
        "    for (const auto& d : diagnostics) std::fprintf(stderr, \"%s: %s\\n\", d.code.c_str(), d.message.c_str());\n"
        "    return 3;\n"
        "  }\n"
        "  diagnostics.clear();\n"
        "  auto encoded = nw::package::EncodePackage(package, diagnostics);\n"
        "  if (encoded.empty()) return 4;\n"
        "  std::ofstream out(argv[2], std::ios::binary);\n"
        "  out.write(reinterpret_cast<const char*>(encoded.data()), static_cast<std::streamsize>(encoded.size()));\n"
        "  return out.good() ? 0 : 5;\n"
        "}\n",
        encoding="utf-8",
    )
    executable = tmp_path / ("nwp_harness.exe" if os.name == "nt" else "nwp_harness")
    command = [
        compiler,
        "-std=c++20",
        "-O1",
        "-I",
        str(REPO_ROOT / "src" / "package"),
        str(harness),
        str(REPO_ROOT / "src" / "package" / "nwp_format.cpp"),
        "-o",
        str(executable),
    ]
    build = subprocess.run(command, capture_output=True, text=True, check=False)
    if build.returncode != 0:
        pytest.fail(f"C++ package library failed to build:\n{build.stdout}\n{build.stderr}")
    output_path = tmp_path / "cpp.nwp"
    run = subprocess.run(
        [str(executable), str(package_path), str(output_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert output_path.read_bytes() == package_bytes
