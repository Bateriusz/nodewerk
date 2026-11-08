from __future__ import annotations

import hashlib
import json
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import offline_shader_pack  # noqa: E402
from offline_shader_pack import (  # noqa: E402
    CS_6_0_PROGRAM_VERSION,
    FORBIDDEN_SM66_HEAP_INDEXING_MASK,
    OfflineShaderCompilerError,
    check_hlsl_source_constraints,
    compile_and_record_manifest,
    compile_cpp_dxil_validator_harness,
    compile_shader_with_dxc,
    find_dxc_executable,
    find_host_cpp_compiler,
    find_windows_kits_dxc_executable,
    format_cpp_dxil_header,
    pack_dxil_to_header,
    validate_dxil_container,
    verify_dxil_blob_with_cpp_validator,
)
from size_ledger import (  # noqa: E402
    _apply_selected_toolchain_pin,
    generate_size_comparison_report,
    record_release_ledgers,
)
from schema_validator import validate_file_against_schema  # noqa: E402
from test_dependency_closure_and_accounting import build_synthetic_pe64  # noqa: E402


def make_test_signed_dxil_container(
    *,
    prog_version: int = CS_6_0_PROGRAM_VERSION,
    feature_flags: int = 0,
    include_psv0: bool = True,
    unsigned_digest: bool = False,
    synthetic_md5_digest: bool = False,
    first_bitstream_word: int = 0x00000021,
) -> bytes:
    """Build a test DXBC container with SFI0, PSV0, and LLVM 3.7 MODULE_BLOCK DXIL chunks for validator unit tests."""
    sfi0_payload = struct.pack("<Q", feature_flags)
    sfi0_chunk = b"SFI0" + struct.pack("<I", len(sfi0_payload)) + sfi0_payload

    psv0_info = bytearray(36)
    struct.pack_into("<III", psv0_info, 12, 64, 1, 1)
    psv0_payload = struct.pack("<I", len(psv0_info)) + bytes(psv0_info) + struct.pack("<II", 0, 1)
    psv0_chunk = b"PSV0" + struct.pack("<I", len(psv0_payload)) + psv0_payload

    bitcode_words = [
        first_bitstream_word,
        0x00000002,
        0x00000021,
        0x0000000C,
    ] + [0x00000000] * 16
    bitcode = b"BC\xc0\xde" + struct.pack(f"<{len(bitcode_words)}I", *bitcode_words)
    dxil_header_size = 24
    dxil_chunk_size = dxil_header_size + len(bitcode)
    dxil_payload = (
        struct.pack(
            "<II4sIII",
            prog_version,
            dxil_chunk_size // 4,
            b"DXIL",
            0x00010100,
            16,
            len(bitcode),
        )
        + bitcode
    )
    dxil_chunk = b"DXIL" + struct.pack("<I", len(dxil_payload)) + dxil_payload

    chunks = [sfi0_chunk]
    if include_psv0:
        chunks.append(psv0_chunk)
    chunks.append(dxil_chunk)

    chunk_count = len(chunks)
    header_size = 32 + chunk_count * 4
    offsets = []
    pos = header_size
    for ch in chunks:
        offsets.append(pos)
        pos += len(ch)
    total_size = pos
    joined_chunks = b"".join(chunks)

    if unsigned_digest:
        digest = b"\x00" * 16
    elif synthetic_md5_digest:
        digest = hashlib.md5(joined_chunks).digest()
    else:
        # Non-zero, non-plain-MD5/SHA256 retail-style HMAC digest for unit test validation
        digest = hashlib.blake2s(b"dxc-retail-sig:" + joined_chunks, digest_size=16).digest()

    header = (
        b"DXBC"
        + digest
        + struct.pack("<HHII", 1, 0, total_size, chunk_count)
        + struct.pack(f"<{chunk_count}I", *offsets)
    )
    return header + joined_chunks


def test_no_synthetic_dxil_generator_and_honest_unverified_manifest() -> None:
    assert not hasattr(offline_shader_pack, "build_deterministic_dxil_container"), (
        "offline_shader_pack.py must never synthesize fake DXBC containers"
    )
    assert not (REPO_ROOT / "shaders" / "compute_fixture.dxil").exists(), (
        "Fake tracked shaders/compute_fixture.dxil must not exist in the source tree"
    )

    manifest = json.loads((REPO_ROOT / "shaders" / "manifest.json").read_text(encoding="utf-8"))
    shader_entry = manifest["shaders"][0]
    assert shader_entry["target_profile"] == "cs_6_0"
    assert shader_entry["pinned_dxc_version"] == "1.8.2407"
    assert shader_entry["runtime_compilation_used"] is False
    assert shader_entry["agility_sdk_required"] is False
    # Evidence must be internally consistent: unverified means no DXC claim/hash, verified means measured.
    if manifest["verification_status"] == "unverified":
        assert shader_entry["compiled_offline_with_dxc"] is False
        assert shader_entry["offline_dxil_bytes"] is None
        assert shader_entry["offline_dxil_sha256"] is None
    else:
        assert manifest["verification_status"] == "verified"
        assert shader_entry["compiled_offline_with_dxc"] is True
        assert isinstance(shader_entry["offline_dxil_bytes"], int)
        assert len(shader_entry["offline_dxil_sha256"]) == 64


def test_hermetic_cmake_shader_compilation_rules() -> None:
    cmake_lists = (REPO_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    compile_shaders = (REPO_ROOT / "cmake" / "CompileShaders.cmake").read_text(encoding="utf-8")

    # CMakeLists.txt must place DXIL and generated header inside ${CMAKE_CURRENT_BINARY_DIR}
    assert '"${CMAKE_CURRENT_BINARY_DIR}/shaders/compute_fixture.dxil"' in cmake_lists
    assert '"${CMAKE_CURRENT_BINARY_DIR}/shaders/generated/compute_fixture_dxil.h"' in cmake_lists
    # The tracked source-tree stub must never be on the CMake include path.
    assert '"${CMAKE_CURRENT_SOURCE_DIR}/shaders/generated"' not in cmake_lists

    # CompileShaders.cmake must attach both outputs to the custom target and provide a Python-free DXC -Fh fallback
    assert 'OUTPUT "${output_dxil}" "${output_header}"' in compile_shaders
    assert 'DEPENDS "${output_dxil}" "${output_header}"' in compile_shaders
    assert '-Fh "${output_header}"' in compile_shaders
    assert "-Vn kComputeFixtureDxil" in compile_shaders


def test_hlsl_source_constraint_checker_rejects_non_baseline_features() -> None:
    hlsl_text = (REPO_ROOT / "shaders" / "compute_fixture.hlsl").read_text(encoding="utf-8")
    assert check_hlsl_source_constraints(hlsl_text) == []

    for forbidden_snippet in (
        "RWByteAddressBuffer buf = ResourceDescriptorHeap[0];",
        "SamplerState s = SamplerDescriptorHeap[1];",
        "RaytracingAccelerationStructure scene : register(t0);",
        "SetMeshOutputCounts(64, 32);",
        "DispatchMesh(1, 1, 1, payload);",
    ):
        violations = check_hlsl_source_constraints(forbidden_snippet)
        assert len(violations) == 1, f"Expected violation for: {forbidden_snippet}"


def test_dxil_container_validator_enforces_signature_psv0_sfi0_and_bitcode() -> None:
    with pytest.raises(ValueError, match="too small"):
        validate_dxil_container(b"DXBCshort")

    valid_blob = make_test_signed_dxil_container()
    info = validate_dxil_container(valid_blob)
    assert info["valid"] is True
    assert info["shader_model"] == "cs_6_0"
    assert info["has_psv0"] is True

    # 1. Reject unsigned (all-zero digest) containers
    with pytest.raises(ValueError, match="all-zero unsigned digest"):
        validate_dxil_container(make_test_signed_dxil_container(unsigned_digest=True))

    # 2. Reject synthetic MD5 digest containers
    with pytest.raises(ValueError, match="synthetic pseudo-DXBC"):
        validate_dxil_container(make_test_signed_dxil_container(synthetic_md5_digest=True))

    # 3. Reject missing PSV0 chunk
    with pytest.raises(ValueError, match="PSV0"):
        validate_dxil_container(make_test_signed_dxil_container(include_psv0=False))

    # 4. Reject SM 6.6 heap indexing SFI0 flags
    with pytest.raises(ValueError, match="SM 6.6"):
        validate_dxil_container(
            make_test_signed_dxil_container(feature_flags=FORBIDDEN_SM66_HEAP_INDEXING_MASK)
        )

    # 5. Reject wrong shader model (cs_6_6)
    prog_ver_cs_6_6 = (5 << 16) | (6 << 4) | 6
    with pytest.raises(ValueError, match="cs_6_6"):
        validate_dxil_container(make_test_signed_dxil_container(prog_version=prog_ver_cs_6_6))

    # 6. Reject fake bitcode body lacking LLVM 3.7 MODULE_BLOCK / top-level ENTER_SUBBLOCK
    with pytest.raises(ValueError, match="MODULE_BLOCK"):
        validate_dxil_container(make_test_signed_dxil_container(first_bitstream_word=0xDEADBEEF))

    # 7. Accept LLVM bitcode starting with BLOCKINFO_BLOCK (id=0 -> 0x0801),
    #    MODULE_BLOCK with CodeLen=3 (id=8 -> 0x0C21), or IDENTIFICATION_BLOCK (id=13 -> 0x0C35)
    for valid_word1 in (0x00000801, 0x00000C21, 0x00000C35):
        variant_blob = make_test_signed_dxil_container(first_bitstream_word=valid_word1)
        assert validate_dxil_container(variant_blob)["valid"] is True

    # 8. Reject unit-test synthetic blake2s container when allow_test_container=False
    with pytest.raises(ValueError, match="unit-test synthetic blake2s"):
        validate_dxil_container(valid_blob, allow_test_container=False)


def test_real_dxc_compilation_and_chunk_layout_when_available(tmp_path: Path) -> None:
    real_dxc = find_dxc_executable() or find_windows_kits_dxc_executable()
    if not real_dxc:
        return
    hlsl_path = REPO_ROOT / "shaders" / "compute_fixture.hlsl"
    out_dxil = tmp_path / "real_dxc" / "compute_fixture.dxil"
    out_header = tmp_path / "real_dxc" / "compute_fixture_dxil.h"
    res = compile_shader_with_dxc(hlsl_path, out_dxil, dxc_executable=real_dxc)
    assert res["valid"] is True
    assert res["is_test_container"] is False
    assert res["shader_model"] == "cs_6_0"
    assert res["has_psv0"] is True
    packed = pack_dxil_to_header(hlsl_path, out_dxil, out_header)
    assert packed["sha256"] == res["sha256"]
    assert out_header.exists()

    # Exercise C++ nw::player::ValidateOfflineDxilContainer against the real DXC-compiled blob
    cpp_comp = find_host_cpp_compiler()
    if cpp_comp is not None:
        assert res["cpp_validator_verified"] is True
        assert res["cpp_validator_exit_code"] == 0
        cpp_dir = tmp_path / "cpp_real_dxc"
        harness_exe = compile_cpp_dxil_validator_harness(cpp_dir, repo_root=REPO_ROOT, compiler_info=cpp_comp)
        real_blob = out_dxil.read_bytes()
        assert verify_dxil_blob_with_cpp_validator(real_blob, cpp_dir, harness_exe=harness_exe) == 0

        # Mutate the real DXC blob and verify C++ ValidateOfflineDxilContainer returns 40 (OfflineShaderPayloadInvalid)
        unsigned_real = bytearray(real_blob)
        unsigned_real[4:20] = b"\x00" * 16
        assert verify_dxil_blob_with_cpp_validator(bytes(unsigned_real), cpp_dir, harness_exe=harness_exe) == 40

        bad_magic_real = bytearray(real_blob)
        bad_magic_real[0:4] = b"BAD!"
        assert verify_dxil_blob_with_cpp_validator(bytes(bad_magic_real), cpp_dir, harness_exe=harness_exe) == 40


def test_compile_shader_with_dxc_always_runs_the_cpp_validator_on_the_emitted_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real-DXC path must validate the exact emitted container with the player's C++ parser."""
    dxil_blob = bytearray(make_test_signed_dxil_container())
    # Replace the blake2s unit-test digest so the container is accepted as a (mock) real
    # DXC-signed artifact; the structural SFI0/PSV0/DXIL chunks remain unchanged.
    dxil_blob[4:20] = b"\x5a" * 16
    dxil_blob = bytes(dxil_blob)
    fake_dxc = tmp_path / "fake_dxc"
    fake_dxc.write_text("", encoding="utf-8")

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        command = [str(c) for c in cmd]
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "dxcompiler.dll: 1.8 - 1.8.2407.12\n", "")
        output_index = command.index("-Fo") + 1
        Path(command[output_index]).write_bytes(dxil_blob)
        return subprocess.CompletedProcess(command, 0, "", "")

    calls: dict[str, object] = {}

    def fake_compile_harness(out_dir: Path, repo_root=None, compiler_info=None):  # type: ignore[no-untyped-def]
        calls["harness_compile"] = (out_dir, repo_root, compiler_info)
        out_dir.mkdir(parents=True, exist_ok=True)
        harness_exe = out_dir / "dxil_validator_harness"
        harness_exe.write_bytes(b"harness")
        calls["harness_path"] = harness_exe
        return harness_exe

    def fake_verify(blob: bytes, work_dir: Path, repo_root=None, *, harness_exe=None):  # type: ignore[no-untyped-def]
        calls["verify"] = (blob, work_dir, repo_root, harness_exe)
        return 0

    monkeypatch.setattr(offline_shader_pack.subprocess, "run", fake_run)
    monkeypatch.setattr(offline_shader_pack, "find_host_cpp_compiler", lambda: ("fake-cxx", "gnu"))
    monkeypatch.setattr(offline_shader_pack, "compile_cpp_dxil_validator_harness", fake_compile_harness)
    monkeypatch.setattr(offline_shader_pack, "verify_dxil_blob_with_cpp_validator", fake_verify)

    result = compile_shader_with_dxc(
        REPO_ROOT / "shaders" / "compute_fixture.hlsl",
        tmp_path / "out" / "compute_fixture.dxil",
        dxc_executable=fake_dxc,
    )
    assert result["cpp_validator_verified"] is True
    assert result["cpp_validator_exit_code"] == 0
    verified_blob, verified_work_dir, _, verified_harness = calls["verify"]  # type: ignore[misc]
    assert verified_blob == dxil_blob
    assert verified_work_dir == tmp_path / "out" / "cpp_validator_check"
    assert verified_harness == tmp_path / "out" / "cpp_validator_check" / "dxil_validator_harness"

    # A C++ parser rejection must fail the offline compile rather than being ignored.
    def rejecting_verify(*args, **kwargs):  # type: ignore[no-untyped-def]
        return 40

    monkeypatch.setattr(offline_shader_pack, "verify_dxil_blob_with_cpp_validator", rejecting_verify)
    with pytest.raises(OfflineShaderCompilerError, match="ExitCode=40"):
        compile_shader_with_dxc(
            REPO_ROOT / "shaders" / "compute_fixture.hlsl",
            tmp_path / "out" / "rejected.dxil",
            dxc_executable=fake_dxc,
        )


def test_cpp_validate_offline_dxil_container_harness_and_parser_parity(tmp_path: Path) -> None:
    cap_cpp = (REPO_ROOT / "src" / "player" / "capability_check.cpp").read_text(encoding="utf-8")
    # Verify C++ parser enforces the same structural invariants as validate_dxil_container
    assert "size_in_uint32 < 6u ||" in cap_cpp
    assert "static_cast<std::size_t>(size_in_uint32) * 4u > chunk_size" in cap_cpp
    assert "dxil_major != 1u || dxil_minor > 8u" in cap_cpp
    assert "block_id == 0u || block_id == 8u || block_id == 13u" in cap_cpp

    cpp_comp = find_host_cpp_compiler()
    if cpp_comp is None:
        return
    cpp_dir = tmp_path / "cpp_unit"
    harness_exe = compile_cpp_dxil_validator_harness(cpp_dir, repo_root=REPO_ROOT, compiler_info=cpp_comp)
    for word1 in (0x00000021, 0x00000801, 0x00000C21, 0x00000C35):
        valid_blob = make_test_signed_dxil_container(first_bitstream_word=word1)
        assert verify_dxil_blob_with_cpp_validator(valid_blob, cpp_dir, harness_exe=harness_exe) == 0

    for invalid_blob in (
        b"DXBCshort",
        make_test_signed_dxil_container(unsigned_digest=True),
        make_test_signed_dxil_container(include_psv0=False),
        make_test_signed_dxil_container(feature_flags=FORBIDDEN_SM66_HEAP_INDEXING_MASK),
        make_test_signed_dxil_container(prog_version=(5 << 16) | (6 << 4) | 6),
        make_test_signed_dxil_container(first_bitstream_word=0xDEADBEEF),
    ):
        assert verify_dxil_blob_with_cpp_validator(invalid_blob, cpp_dir, harness_exe=harness_exe) == 40


def test_offline_dxc_compile_and_pack_into_build_dir(tmp_path: Path) -> None:
    hlsl_path = REPO_ROOT / "shaders" / "compute_fixture.hlsl"
    out_dxil = tmp_path / "build" / "shaders" / "compute_fixture.dxil"
    out_header = tmp_path / "build" / "shaders" / "generated" / "compute_fixture_dxil.h"

    # Without DXC in PATH, compile_shader_with_dxc must fail rather than synthesizing a container
    with pytest.raises(OfflineShaderCompilerError, match="refusing to synthesize"):
        compile_shader_with_dxc(hlsl_path, out_dxil, dxc_executable=tmp_path / "nonexistent_dxc")

    # Create a mock DXC executable in tmp_path to test the offline compile + header generation flow
    valid_dxil_hex = make_test_signed_dxil_container().hex()
    mock_dxc = tmp_path / "mock_dxc.py"
    mock_dxc.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        "fo_idx = sys.argv.index('-Fo')\n"
        "out_file = Path(sys.argv[fo_idx + 1])\n"
        "out_file.parent.mkdir(parents=True, exist_ok=True)\n"
        f"out_file.write_bytes(bytes.fromhex({valid_dxil_hex!r}))\n",
        encoding="utf-8",
    )
    mock_dxc.chmod(mock_dxc.stat().st_mode | stat.S_IEXEC)

    res = compile_shader_with_dxc(hlsl_path, out_dxil, dxc_executable=mock_dxc)
    assert res["valid"] is True
    assert res["measured_dxc_version"] == "1.8.2407.12"
    assert out_dxil.exists()

    pack_res = pack_dxil_to_header(hlsl_path, out_dxil, out_header)
    assert out_header.exists()
    assert out_header.read_text(encoding="utf-8") == format_cpp_dxil_header(
        out_dxil.read_bytes(), pack_res["sha256"]
    )

    # Test compile_and_record_manifest in an isolated copy of repo shaders/
    fake_repo = tmp_path / "repo"
    (fake_repo / "shaders").mkdir(parents=True)
    (fake_repo / "shaders" / "compute_fixture.hlsl").write_bytes(hlsl_path.read_bytes())
    manifest = compile_and_record_manifest(fake_repo, fake_repo / "build", dxc_executable=mock_dxc)
    assert manifest["verification_status"] == "verified"
    assert manifest["shaders"][0]["compiled_offline_with_dxc"] is True
    assert manifest["shaders"][0]["offline_dxil_bytes"] == len(out_dxil.read_bytes())


def test_compute_fixture_expected_output_matches_reference_formula() -> None:
    expected_path = REPO_ROOT / "fixtures" / "compute_shader" / "expected_output.json"
    data = json.loads(expected_path.read_text(encoding="utf-8"))
    assert data["element_count"] == 256
    assert data["thread_group_size"] == [64, 1, 1]
    assert data["dispatch_groups"] == [4, 1, 1]

    seed_mask = int(data["seed_mask_hex"], 16)
    recomputed = [((tid * 1664525 + 1013904223) & 0xFFFFFFFF) ^ seed_mask for tid in range(256)]
    assert data["elements_u32"] == recomputed
    raw_bytes = struct.pack("<256I", *recomputed)
    assert data["output_sha256"] == hashlib.sha256(raw_bytes).hexdigest()


def test_verified_shader_manifest_does_not_break_unverified_size_ledgers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "shaders", "fixtures"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)

    manifest_path = repo / "shaders" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification_status"] = "verified"
    manifest["shaders"][0]["compiled_offline_with_dxc"] = True
    manifest["shaders"][0]["offline_dxil_bytes"] = 4096
    manifest["shaders"][0]["offline_dxil_sha256"] = "c" * 64
    manifest["unavailable_measurements"] = []
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # This test targets the unverified-ledger state; reset the copied repository evidence.
    for ledger_path in (repo / "benchmarks" / "ledgers").glob("*.json"):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["verification_status"] = "unverified"
        ledger["unverified_reason"] = "synthetic unverified-ledger test state"
        ledger["total_distributed_bytes"] = None
        for component in ledger.get("distributed_components", []):
            component["size_bytes"] = None
            component["sha256"] = None
        for shader in ledger.get("shaders", []):
            shader["offline_dxil_bytes"] = None
            shader["embedded_in_executable_bytes"] = None
        if not any(
            item["metric"] == "total_distributed_bytes"
            for item in ledger.get("unavailable_measurements", [])
        ):
            ledger.setdefault("unavailable_measurements", []).append(
                {
                    "metric": "total_distributed_bytes",
                    "reason": "Synthetic test resets ledger bytes to unverified.",
                }
            )
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")

    report = generate_size_comparison_report(repo)
    assert report["selected_toolchain"] == "msvc-ltcg"
    assert all(c["verification_status"] == "unverified" for c in report["fixture_comparisons"])


def test_record_release_ledgers_from_measured_binaries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "shaders", "fixtures"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)
    _reset_repo_evidence(repo)

    dxil_blob = make_test_signed_dxil_container()
    manifest_path = repo / "shaders" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification_status"] = "verified"
    manifest["shaders"][0]["compiled_offline_with_dxc"] = True
    manifest["shaders"][0]["offline_dxil_bytes"] = len(dxil_blob)
    manifest["shaders"][0]["offline_dxil_sha256"] = hashlib.sha256(dxil_blob).hexdigest()
    manifest["unavailable_measurements"] = []
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    empty_pe = build_synthetic_pe64(["kernel32.dll"])
    clear_pe = build_synthetic_pe64(
        ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"]
    )
    compute_pe = build_synthetic_pe64(
        ["kernel32.dll", "d3d12.dll", "dxgi.dll"]
    ) + dxil_blob
    for fixture_id, name, payload in (
        ("empty_player", "nw_empty_player.exe", empty_pe),
        ("clear_present", "nw_clear_present_fixture.exe", clear_pe),
        ("compute_shader", "nw_compute_shader_fixture.exe", compute_pe),
    ):
        (bin_dir / "staging" / fixture_id).mkdir(parents=True, exist_ok=True)
        (bin_dir / "staging" / fixture_id / name).write_bytes(payload)

    recorded = record_release_ledgers(
        repo,
        bin_dir,
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )
    assert set(recorded["recorded_fixtures"]) == {"empty_player", "clear_present", "compute_shader"}
    assert recorded["verification_status"] == "verified"
    assert recorded["comparison_toolchain_bytes_remain_unverified"] is True

    empty_ledger = json.loads(
        (repo / "benchmarks" / "ledgers" / "empty-player-clang-cl-release.json").read_text(encoding="utf-8")
    )
    assert empty_ledger["verification_status"] == "verified"
    assert empty_ledger["total_distributed_bytes"] == len(empty_pe)
    assert empty_ledger["imports"][0]["dll_name"] == "kernel32.dll"

    toolchain = json.loads(
        (repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    clang_entry = next(t for t in toolchain["evaluated_toolchains"] if t["toolchain_id"] == "clang-cl-lld")
    msvc_entry = next(t for t in toolchain["evaluated_toolchains"] if t["toolchain_id"] == "msvc-ltcg")
    assert clang_entry["verification_status"] == "verified"
    assert clang_entry["deterministic_repro_verified"] is True
    assert clang_entry["pdb_debugging_verified"] is True
    assert clang_entry["empty_player_bytes"] == len(empty_pe)
    assert clang_entry["clear_present_bytes"] == len(clear_pe)
    assert clang_entry["compute_fixture_bytes"] == len(compute_pe)
    assert msvc_entry["verification_status"] == "unverified"

    comparison = generate_size_comparison_report(repo)
    assert comparison["fixture_comparisons"][0]["clang_cl_lld_bytes"] == len(empty_pe)
    assert all(c["verification_status"] == "unverified" for c in comparison["fixture_comparisons"])

    # Now record larger MSVC/LTCG binaries and verify both toolchains and all comparisons become verified
    msvc_dir = tmp_path / "msvc_bin"
    msvc_dir.mkdir()
    for fixture_id, name, payload in (
        ("empty_player", "nw_empty_player.exe", build_synthetic_pe64(["kernel32.dll"], extra_text_bytes=512)),
        (
            "clear_present",
            "nw_clear_present_fixture.exe",
            build_synthetic_pe64(
                ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"],
                extra_text_bytes=512,
            ),
        ),
        (
            "compute_shader",
            "nw_compute_shader_fixture.exe",
            build_synthetic_pe64(
                ["kernel32.dll", "d3d12.dll", "dxgi.dll"],
                extra_text_bytes=512,
            )
            + dxil_blob,
        ),
    ):
        (msvc_dir / "staging" / fixture_id).mkdir(parents=True, exist_ok=True)
        (msvc_dir / "staging" / fixture_id / name).write_bytes(payload)
    msvc_recorded = record_release_ledgers(
        repo,
        msvc_dir,
        toolchain_id="msvc-ltcg",
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )
    assert msvc_recorded["verification_status"] == "verified"
    assert msvc_recorded["comparison_toolchain_bytes_remain_unverified"] is False
    full_comparison = generate_size_comparison_report(repo)
    assert all(
        c["verification_status"] == "verified" and c["delta_msvc_minus_clang_bytes"] == 512
        for c in full_comparison["fixture_comparisons"]
    )


def test_record_release_ledgers_requires_pdb_and_reproducibility_evidence(tmp_path: Path) -> None:
    from size_ledger import _derive_toolchain_evidence_text

    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "shaders", "fixtures"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)
    _reset_repo_evidence(repo)

    dxil_blob = make_test_signed_dxil_container()
    manifest_path = repo / "shaders" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification_status"] = "verified"
    manifest["shaders"][0]["compiled_offline_with_dxc"] = True
    manifest["shaders"][0]["offline_dxil_bytes"] = len(dxil_blob)
    manifest["shaders"][0]["offline_dxil_sha256"] = hashlib.sha256(dxil_blob).hexdigest()
    manifest["unavailable_measurements"] = []
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for fixture_id in ("empty_player", "clear_present", "compute_shader"):
        (bin_dir / "staging" / fixture_id).mkdir(parents=True, exist_ok=True)
    (bin_dir / "staging" / "empty_player" / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll"])
    )
    (bin_dir / "staging" / "clear_present" / "nw_clear_present_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"])
    )
    (bin_dir / "staging" / "compute_shader" / "nw_compute_shader_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll"]) + dxil_blob
    )

    # Missing CodeView/PDB evidence must never yield a verified toolchain or ledger,
    # nor leave deterministic_repro_verified=True on an unverified toolchain entry,
    # while still preserving the measured binary byte counts in an unverified record.
    empty_pe_bytes = (bin_dir / "staging" / "empty_player" / "nw_empty_player.exe").read_bytes()
    clear_pe_bytes = (
        bin_dir / "staging" / "clear_present" / "nw_clear_present_fixture.exe"
    ).read_bytes()
    compute_pe_bytes = (
        bin_dir / "staging" / "compute_shader" / "nw_compute_shader_fixture.exe"
    ).read_bytes()

    recorded = record_release_ledgers(
        repo,
        bin_dir,
        deterministic_repro_verified=True,
        pdb_debugging_verified=False,
        fixture_execution_verified=True,
    )
    assert recorded["verification_status"] == "unverified"
    assert set(recorded["recorded_fixtures"]) == {"empty_player", "clear_present", "compute_shader"}
    assert recorded["recorded_fixtures"]["empty_player"]["verification_status"] == "unverified"
    assert recorded["recorded_fixtures"]["empty_player"]["total_distributed_bytes"] == len(empty_pe_bytes)
    assert recorded["deterministic_repro_verified"] is False
    assert recorded["pdb_debugging_verified"] is False

    toolchain = json.loads(
        (repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    clang_entry = next(
        t for t in toolchain["evaluated_toolchains"] if t["toolchain_id"] == "clang-cl-lld"
    )
    assert clang_entry["verification_status"] == "unverified"
    assert clang_entry["pdb_debugging_verified"] is False
    assert clang_entry["deterministic_repro_verified"] is False
    assert clang_entry["empty_player_bytes"] == len(empty_pe_bytes)
    assert clang_entry["clear_present_bytes"] == len(clear_pe_bytes)
    assert clang_entry["compute_fixture_bytes"] == len(compute_pe_bytes)
    # With the real schemas present, the unverified gate must still return normally and
    # write a manifest that satisfies the PDB/reproducibility consistency invariants.
    validate_file_against_schema(
        repo / "benchmarks" / "toolchain-manifest.v1.json",
        repo / "schemas" / "toolchain-manifest.schema.json",
    )

    ledger = json.loads(
        (
            repo / "benchmarks" / "ledgers" / "empty-player-clang-cl-release.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger["verification_status"] == "unverified"
    assert ledger["total_distributed_bytes"] == len(empty_pe_bytes)
    assert "unverified" in ledger["unverified_reason"]
    validate_file_against_schema(
        repo / "benchmarks" / "ledgers" / "empty-player-clang-cl-release.json",
        repo / "schemas" / "size-ledger.schema.json",
    )
    assert all(
        c["verification_status"] == "unverified"
        for c in generate_size_comparison_report(repo)["fixture_comparisons"]
    )

    # Even when both repro and PDB flags are True, if not all three fixture executables
    # exist in binary_dir, the toolchain and ledgers must remain unverified with false flags,
    # preserving only the available binary measurement and clearing missing fixtures.
    partial_dir = tmp_path / "partial_bin"
    partial_dir.mkdir()
    (partial_dir / "staging" / "empty_player").mkdir(parents=True, exist_ok=True)
    (partial_dir / "staging" / "empty_player" / "nw_empty_player.exe").write_bytes(empty_pe_bytes)
    partial_recorded = record_release_ledgers(
        repo,
        partial_dir,
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )
    assert partial_recorded["verification_status"] == "unverified"
    assert set(partial_recorded["recorded_fixtures"]) == {"empty_player"}
    assert partial_recorded["recorded_fixtures"]["empty_player"]["total_distributed_bytes"] == len(empty_pe_bytes)
    assert partial_recorded["deterministic_repro_verified"] is False
    assert partial_recorded["pdb_debugging_verified"] is False

    clear_ledger = json.loads(
        (repo / "benchmarks" / "ledgers" / "clear-present-clang-cl-release.json").read_text(encoding="utf-8")
    )
    assert clear_ledger["verification_status"] == "unverified"
    assert clear_ledger["total_distributed_bytes"] is None
    assert all(
        c["verification_status"] == "unverified"
        for c in generate_size_comparison_report(repo)["fixture_comparisons"]
    )

    # Verify _derive_toolchain_evidence_text refreshes selection_rationale when both toolchains
    # have complete byte measurements but the comparison toolchain is still unverified.
    toolchain_partial = json.loads(
        (repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    for t in toolchain_partial["evaluated_toolchains"]:
        if t["toolchain_id"] == "clang-cl-lld":
            t["verification_status"] = "verified"
            t["empty_player_bytes"] = 1536
            t["clear_present_bytes"] = 1536
            t["compute_fixture_bytes"] = 1712
            t["deterministic_repro_verified"] = True
            t["pdb_debugging_verified"] = True
        else:
            t["verification_status"] = "unverified"
            t["empty_player_bytes"] = 2048
            t["clear_present_bytes"] = 2048
            t["compute_fixture_bytes"] = 2224
            t["deterministic_repro_verified"] = False
            t["pdb_debugging_verified"] = False
    _derive_toolchain_evidence_text(toolchain_partial)
    rationale = toolchain_partial["selected_toolchain"]["selection_rationale"]
    assert "empty_player=1536 B" in rationale
    assert "empty_player=2048 B" in rationale
    assert "byte counts remain null" not in rationale


def test_smaller_msvc_ltcg_toolchain_is_selected_when_verified(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "shaders", "fixtures"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)

    dxil_blob = make_test_signed_dxil_container()
    manifest_path = repo / "shaders" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification_status"] = "verified"
    manifest["shaders"][0]["compiled_offline_with_dxc"] = True
    manifest["shaders"][0]["offline_dxil_bytes"] = len(dxil_blob)
    manifest["shaders"][0]["offline_dxil_sha256"] = hashlib.sha256(dxil_blob).hexdigest()
    manifest["unavailable_measurements"] = []
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Build clang-cl binaries that are 512 bytes LARGER than MSVC/LTCG on every fixture.
    clang_dir = tmp_path / "clang_bin"
    clang_dir.mkdir()
    for fixture_id, name, payload in (
        ("empty_player", "nw_empty_player.exe", build_synthetic_pe64(["kernel32.dll"], extra_text_bytes=512)),
        (
            "clear_present",
            "nw_clear_present_fixture.exe",
            build_synthetic_pe64(
                ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"],
                extra_text_bytes=512,
            ),
        ),
        (
            "compute_shader",
            "nw_compute_shader_fixture.exe",
            build_synthetic_pe64(
                ["kernel32.dll", "d3d12.dll", "dxgi.dll"],
                extra_text_bytes=512,
            )
            + dxil_blob,
        ),
    ):
        (clang_dir / "staging" / fixture_id).mkdir(parents=True, exist_ok=True)
        (clang_dir / "staging" / fixture_id / name).write_bytes(payload)

    msvc_dir = tmp_path / "msvc_bin"
    msvc_dir.mkdir()
    for fixture_id, name, payload in (
        ("empty_player", "nw_empty_player.exe", build_synthetic_pe64(["kernel32.dll"])),
        (
            "clear_present",
            "nw_clear_present_fixture.exe",
            build_synthetic_pe64(["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"]),
        ),
        (
            "compute_shader",
            "nw_compute_shader_fixture.exe",
            build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll"]) + dxil_blob,
        ),
    ):
        (msvc_dir / "staging" / fixture_id).mkdir(parents=True, exist_ok=True)
        (msvc_dir / "staging" / fixture_id / name).write_bytes(payload)

    record_release_ledgers(
        repo,
        clang_dir,
        toolchain_id="clang-cl-lld",
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )
    msvc_recorded = record_release_ledgers(
        repo,
        msvc_dir,
        toolchain_id="msvc-ltcg",
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )
    assert msvc_recorded["selected_toolchain"] == "msvc-ltcg"

    toolchain = json.loads(
        (repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    assert toolchain["selected_toolchain"]["toolchain_id"] == "msvc-ltcg"
    assert toolchain["selected_toolchain"]["compiler_executable"] == "cl.exe"
    assert toolchain["selected_toolchain"]["linker_executable"] == "link.exe"
    assert "clang-cl-lld is larger on every fixture" in toolchain["selected_toolchain"]["selection_rationale"]
    by_id = {t["toolchain_id"]: t for t in toolchain["evaluated_toolchains"]}
    assert by_id["msvc-ltcg"]["status"] == "selected"
    assert by_id["clang-cl-lld"]["status"] == "retained_alternative"

    comparison = generate_size_comparison_report(repo)
    assert comparison["selected_toolchain"] == "msvc-ltcg"
    assert comparison["smaller_verified_toolchain"] == "msvc-ltcg"
    assert all(
        c["verification_status"] == "verified"
        and c["smaller_toolchain"] == "msvc-ltcg"
        and c["delta_msvc_minus_clang_bytes"] == -512
        for c in comparison["fixture_comparisons"]
    )
    validate_file_against_schema(
        repo / "benchmarks" / "toolchain-manifest.v1.json",
        repo / "schemas" / "toolchain-manifest.schema.json",
    )


def _copied_repo_with_verified_shader(tmp_path: Path) -> tuple[Path, bytes]:
    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "shaders", "fixtures"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)

    dxil_blob = make_test_signed_dxil_container()
    manifest_path = repo / "shaders" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification_status"] = "verified"
    manifest["shaders"][0]["compiled_offline_with_dxc"] = True
    manifest["shaders"][0]["offline_dxil_bytes"] = len(dxil_blob)
    manifest["shaders"][0]["offline_dxil_sha256"] = hashlib.sha256(dxil_blob).hexdigest()
    manifest["unavailable_measurements"] = []
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return repo, dxil_blob


def _reset_repo_evidence(repo: Path, selected_toolchain: str = "clang-cl-lld") -> Path:
    """Reset copied verified evidence to the unverified baseline expected by older tests."""
    for ledger_path in (repo / "benchmarks" / "ledgers").glob("*.json"):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["verification_status"] = "unverified"
        ledger["unverified_reason"] = "synthetic test reset to unverified state"
        ledger["total_distributed_bytes"] = None
        for component in ledger.get("distributed_components", []):
            component["size_bytes"] = None
            component["sha256"] = None
        for section in ledger.get("executable_sections", []):
            section["virtual_size_bytes"] = None
            section["raw_size_bytes"] = None
        for shader in ledger.get("shaders", []):
            shader["offline_dxil_bytes"] = None
            shader["embedded_in_executable_bytes"] = None
        if not any(
            item["metric"] == "total_distributed_bytes"
            for item in ledger.get("unavailable_measurements", [])
        ):
            ledger.setdefault("unavailable_measurements", []).append(
                {
                    "metric": "total_distributed_bytes",
                    "reason": "Synthetic test resets ledger evidence to unverified.",
                }
            )
        unavailable_metrics = {
            item["metric"] for item in ledger.get("unavailable_measurements", [])
        }
        for metric, reason in (
            (
                "executable_sections",
                "Synthetic test resets section measurements to unverified.",
            ),
            (
                "shaders[0].offline_dxil_bytes",
                "Synthetic test resets shader measurements to unverified.",
            ),
        ):
            if metric not in unavailable_metrics and (
                metric == "executable_sections" or ledger.get("shaders")
            ):
                ledger.setdefault("unavailable_measurements", []).append(
                    {"metric": metric, "reason": reason}
                )
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")

    toolchain_path = repo / "benchmarks" / "toolchain-manifest.v1.json"
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    for entry in toolchain["evaluated_toolchains"]:
        entry["verification_status"] = "unverified"
        entry["pdb_debugging_verified"] = False
        entry["deterministic_repro_verified"] = False
        entry["fixture_execution_verified"] = False
        entry["empty_player_bytes"] = None
        entry["clear_present_bytes"] = None
        entry["compute_fixture_bytes"] = None
    _apply_selected_toolchain_pin(toolchain, selected_toolchain)
    toolchain_path.write_text(json.dumps(toolchain, indent=2) + "\n", encoding="utf-8")
    return repo


def _write_fixture_binaries(binary_dir: Path, dxil_blob: bytes, extra_text_bytes: int = 0) -> None:
    for fixture_id in ("empty_player", "clear_present", "compute_shader"):
        (binary_dir / "staging" / fixture_id).mkdir(parents=True, exist_ok=True)
    (binary_dir / "staging" / "empty_player" / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll"], extra_text_bytes=extra_text_bytes)
    )
    (binary_dir / "staging" / "clear_present" / "nw_clear_present_fixture.exe").write_bytes(
        build_synthetic_pe64(
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"],
            extra_text_bytes=extra_text_bytes,
        )
    )
    (binary_dir / "staging" / "compute_shader" / "nw_compute_shader_fixture.exe").write_bytes(
        build_synthetic_pe64(
            ["kernel32.dll", "d3d12.dll", "dxgi.dll"],
            extra_text_bytes=extra_text_bytes,
        )
        + dxil_blob
    )


def test_only_verified_msvc_toolchain_is_selected_instead_of_clang_default(tmp_path: Path) -> None:
    repo, dxil_blob = _copied_repo_with_verified_shader(tmp_path)
    repo = _reset_repo_evidence(repo)
    bin_dir = tmp_path / "msvc_only"
    _write_fixture_binaries(bin_dir, dxil_blob)

    recorded = record_release_ledgers(
        repo,
        bin_dir,
        toolchain_id="msvc-ltcg",
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )
    assert recorded["verification_status"] == "verified"
    assert recorded["selected_toolchain"] == "msvc-ltcg"

    toolchain = json.loads(
        (repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    assert toolchain["selected_toolchain"]["toolchain_id"] == "msvc-ltcg"
    by_id = {t["toolchain_id"]: t for t in toolchain["evaluated_toolchains"]}
    assert by_id["msvc-ltcg"]["status"] == "selected"
    assert by_id["clang-cl-lld"]["status"] == "retained_alternative"
    validate_file_against_schema(
        repo / "benchmarks" / "toolchain-manifest.v1.json",
        repo / "schemas" / "toolchain-manifest.schema.json",
    )

    comparison = generate_size_comparison_report(repo)
    assert comparison["selected_toolchain"] == "msvc-ltcg"
    assert comparison["smaller_verified_toolchain"] is None
    assert all(c["verification_status"] == "unverified" for c in comparison["fixture_comparisons"])


def test_incomplete_verification_preserves_measured_compute_binary_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "shaders", "fixtures"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)
    _reset_repo_evidence(repo)
    shader_manifest_path = repo / "shaders" / "manifest.json"
    shader_manifest = json.loads(shader_manifest_path.read_text(encoding="utf-8"))
    shader_manifest["verification_status"] = "unverified"
    shader_manifest["shaders"][0]["compiled_offline_with_dxc"] = False
    shader_manifest["shaders"][0]["offline_dxil_bytes"] = None
    shader_manifest["shaders"][0]["offline_dxil_sha256"] = None
    shader_manifest_path.write_text(json.dumps(shader_manifest, indent=2) + "\n", encoding="utf-8")

    # The shader manifest intentionally remains unverified (offline DXIL bytes are null),
    # but the compute fixture PE binary itself is measurable.
    bin_dir = tmp_path / "clang_no_dxil"
    _write_fixture_binaries(bin_dir, b"")
    compute_bytes = (
        bin_dir / "staging" / "compute_shader" / "nw_compute_shader_fixture.exe"
    ).read_bytes()

    recorded = record_release_ledgers(
        repo,
        bin_dir,
        toolchain_id="clang-cl-lld",
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )
    assert recorded["verification_status"] == "unverified"
    assert set(recorded["recorded_fixtures"]) == {
        "empty_player",
        "clear_present",
        "compute_shader",
    }
    assert recorded["recorded_fixtures"]["compute_shader"]["total_distributed_bytes"] == len(
        compute_bytes
    )

    compute_ledger = json.loads(
        (
            repo / "benchmarks" / "ledgers" / "compute-fixture-clang-cl-release.json"
        ).read_text(encoding="utf-8")
    )
    assert compute_ledger["verification_status"] == "unverified"
    assert compute_ledger["total_distributed_bytes"] == len(compute_bytes)
    assert compute_ledger["shaders"][0]["offline_dxil_bytes"] is None
    unavailable = {m["metric"] for m in compute_ledger["unavailable_measurements"]}
    assert "shaders[0].offline_dxil_bytes" in unavailable
    validate_file_against_schema(
        repo / "benchmarks" / "ledgers" / "compute-fixture-clang-cl-release.json",
        repo / "schemas" / "size-ledger.schema.json",
    )

    toolchain = json.loads(
        (repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    clang_entry = next(t for t in toolchain["evaluated_toolchains"] if t["toolchain_id"] == "clang-cl-lld")
    assert clang_entry["compute_fixture_bytes"] == len(compute_bytes)
    assert clang_entry["verification_status"] == "unverified"
    assert clang_entry["pdb_debugging_verified"] is False
    assert clang_entry["deterministic_repro_verified"] is False


def test_selection_rationale_uses_mixed_per_fixture_ordering(tmp_path: Path) -> None:
    from size_ledger import _derive_toolchain_evidence_text

    repo, _dxil_blob = _copied_repo_with_verified_shader(tmp_path)
    toolchain_path = repo / "benchmarks" / "toolchain-manifest.v1.json"
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    by_id = {t["toolchain_id"]: t for t in toolchain["evaluated_toolchains"]}
    clang = by_id["clang-cl-lld"]
    msvc = by_id["msvc-ltcg"]
    # clang wins empty_player but loses clear_present and compute_shader; msvc has the
    # smaller aggregate footprint. The rationale must not claim clang is larger everywhere.
    clang.update(
        verification_status="verified",
        empty_player_bytes=1000,
        clear_present_bytes=5000,
        compute_fixture_bytes=1000,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
        deterministic_repro_verified=True,
    )
    msvc.update(
        verification_status="verified",
        empty_player_bytes=1100,
        clear_present_bytes=4000,
        compute_fixture_bytes=1100,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
        deterministic_repro_verified=True,
    )

    _derive_toolchain_evidence_text(toolchain)
    assert toolchain["selected_toolchain"]["toolchain_id"] == "msvc-ltcg"
    rationale = toolchain["selected_toolchain"]["selection_rationale"]
    assert "clang-cl-lld is larger on every fixture" not in rationale
    assert "smaller aggregate distributed footprint" in rationale
    assert "per-fixture smaller toolchain" in rationale


def test_generate_report_reconciles_stale_manifest_toolchain_selection(tmp_path: Path) -> None:
    repo, dxil_blob = _copied_repo_with_verified_shader(tmp_path)
    repo = _reset_repo_evidence(repo)
    clang_dir = tmp_path / "clang_bin"
    msvc_dir = tmp_path / "msvc_bin"
    _write_fixture_binaries(clang_dir, dxil_blob, extra_text_bytes=512)
    _write_fixture_binaries(msvc_dir, dxil_blob)
    record_release_ledgers(
        repo,
        clang_dir,
        toolchain_id="clang-cl-lld",
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )
    record_release_ledgers(
        repo,
        msvc_dir,
        toolchain_id="msvc-ltcg",
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )

    toolchain_path = repo / "benchmarks" / "toolchain-manifest.v1.json"
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    assert toolchain["selected_toolchain"]["toolchain_id"] == "msvc-ltcg"

    # Simulate a stale manifest still naming clang-cl after the MSVC/LTCG measurements won.
    toolchain["selected_toolchain"].update(
        toolchain_id="clang-cl-lld",
        compiler_name="LLVM clang-cl",
        compiler_executable="clang-cl.exe",
        compiler_version="18.1.8",
        linker_name="LLVM lld-link",
        linker_executable="lld-link.exe",
        linker_version="18.1.8",
        selection_rationale="Selected clang-cl-lld pending identical-fixture measurement.",
    )
    for entry in toolchain["evaluated_toolchains"]:
        entry["status"] = (
            "selected" if entry["toolchain_id"] == "clang-cl-lld" else "retained_alternative"
        )
    toolchain_path.write_text(json.dumps(toolchain, indent=2) + "\n", encoding="utf-8")

    comparison = generate_size_comparison_report(repo)
    assert comparison["selected_toolchain"] == "msvc-ltcg"
    assert comparison["smaller_verified_toolchain"] == "msvc-ltcg"
    assert comparison["selection_reconciled"] is True

    reconciled = json.loads(toolchain_path.read_text(encoding="utf-8"))
    assert reconciled["selected_toolchain"]["toolchain_id"] == "msvc-ltcg"
    assert reconciled["selected_toolchain"]["compiler_executable"] == "cl.exe"
    by_id = {t["toolchain_id"]: t for t in reconciled["evaluated_toolchains"]}
    assert by_id["msvc-ltcg"]["status"] == "selected"
    assert by_id["clang-cl-lld"]["status"] == "retained_alternative"
    validate_file_against_schema(
        toolchain_path, repo / "schemas" / "toolchain-manifest.schema.json"
    )


def test_failed_reverification_downgrades_previously_verified_ledgers(tmp_path: Path) -> None:
    repo, dxil_blob = _copied_repo_with_verified_shader(tmp_path)
    repo = _reset_repo_evidence(repo)
    bin_dir = tmp_path / "clang_bin"
    _write_fixture_binaries(bin_dir, dxil_blob)
    first = record_release_ledgers(
        repo,
        bin_dir,
        toolchain_id="clang-cl-lld",
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=True,
    )
    assert first["verification_status"] == "verified"

    second = record_release_ledgers(
        repo,
        bin_dir,
        toolchain_id="clang-cl-lld",
        deterministic_repro_verified=True,
        pdb_debugging_verified=False,
        fixture_execution_verified=True,
    )
    assert second["verification_status"] == "unverified"
    for ledger_file in (repo / "benchmarks" / "ledgers").glob("*-clang-cl-release.json"):
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        assert ledger["verification_status"] == "unverified"
        assert ledger["total_distributed_bytes"] is not None
        assert ledger["unverified_reason"]

    toolchain = json.loads(
        (repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    clang_entry = next(t for t in toolchain["evaluated_toolchains"] if t["toolchain_id"] == "clang-cl-lld")
    assert clang_entry["verification_status"] == "unverified"
    assert clang_entry["pdb_debugging_verified"] is False
    assert clang_entry["deterministic_repro_verified"] is False
    # The report must not fail after a failed re-verification run.
    generate_size_comparison_report(repo)


def test_sync_evaluated_toolchains_from_ledgers_updates_manifest_fields() -> None:
    from size_ledger import _sync_evaluated_toolchains_from_ledgers

    toolchain = json.loads(
        (REPO_ROOT / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    ledgers_by_key = {
        (fixture_id, "clang-cl-lld"): {
            "total_distributed_bytes": 4096,
            "verification_status": "verified",
        }
        for fixture_id in ("empty_player", "clear_present", "compute_shader")
    }
    # Make the entry eligible for promotion to exercise the evidence flags as well.
    clang = next(t for t in toolchain["evaluated_toolchains"] if t["toolchain_id"] == "clang-cl-lld")
    clang["pdb_debugging_verified"] = True
    clang["deterministic_repro_verified"] = True
    clang["fixture_execution_verified"] = True

    changed = _sync_evaluated_toolchains_from_ledgers(toolchain, ledgers_by_key)
    assert changed is True
    assert clang["empty_player_bytes"] == 4096
    assert clang["clear_present_bytes"] == 4096
    assert clang["compute_fixture_bytes"] == 4096
    assert clang["verification_status"] == "verified"

    # An unverified ledger for a single fixture downgrades the whole toolchain.
    ledgers_by_key[("clear_present", "clang-cl-lld")]["verification_status"] = "unverified"
    assert _sync_evaluated_toolchains_from_ledgers(toolchain, ledgers_by_key) is True
    assert clang["verification_status"] == "unverified"
    assert clang["pdb_debugging_verified"] is False
    assert clang["deterministic_repro_verified"] is False
    assert clang["fixture_execution_verified"] is False


def test_toolchain_is_not_selectable_without_fixture_execution_evidence(
    tmp_path: Path,
) -> None:
    repo, dxil_blob = _copied_repo_with_verified_shader(tmp_path)
    repo = _reset_repo_evidence(repo)
    bin_dir = tmp_path / "clang_bin"
    _write_fixture_binaries(bin_dir, dxil_blob)

    recorded = record_release_ledgers(
        repo,
        bin_dir,
        toolchain_id="clang-cl-lld",
        deterministic_repro_verified=True,
        pdb_debugging_verified=True,
        fixture_execution_verified=False,
    )
    assert recorded["verification_status"] == "unverified"
    assert recorded["fixture_execution_verified"] is False

    toolchain = json.loads(
        (repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    clang = next(t for t in toolchain["evaluated_toolchains"] if t["toolchain_id"] == "clang-cl-lld")
    assert clang["verification_status"] == "unverified"
    assert clang["fixture_execution_verified"] is False
    assert toolchain["selected_toolchain"]["toolchain_id"] == "clang-cl-lld"
