from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from test_dependency_closure_and_accounting import (  # noqa: E402
    build_synthetic_msf_pdb,
    build_synthetic_pe64,
)
from test_offline_shader_and_compute_fixture import make_test_signed_dxil_container  # noqa: E402
from verify_nw01 import (  # noqa: E402
    _format_nvidia_driver_version,
    build_and_compare_two_clean_directories,
    capture_benchmark_result,
    classify_loaded_module_evidence,
    hardware_tier_matches,
    check_evidence_completeness,
    compare_two_build_directories,
    detect_host_system_environment,
    evaluate_offline_shader,
    find_embedded_dxil_container,
    load_compute_expected_output,
    parse_observed_compute_readback,
    parse_loaded_modules,
    probe_sdk_absence,
    record_shader_manifest_evidence,
    run_clean_system_mode,
    run_full_mode,
    verify_debug_symbol_evidence,
)


def test_exit_codes_and_d3d12_bootstrap_safety_contracts() -> None:
    exit_codes_h = (REPO_ROOT / "src" / "player" / "exit_codes.h").read_text(encoding="utf-8")
    diagnostics_cpp = (REPO_ROOT / "src" / "player" / "diagnostics.cpp").read_text(encoding="utf-8")
    bootstrap_cpp = (REPO_ROOT / "src" / "player" / "d3d12_bootstrap.cpp").read_text(encoding="utf-8")
    exit_docs = (REPO_ROOT / "docs" / "exit-codes-and-diagnostics.md").read_text(encoding="utf-8")

    matches = re.findall(r"^\s*([A-Za-z0-9_]+)\s*=\s*([0-9]+),", exit_codes_h, flags=re.MULTILINE)
    assert len(matches) >= 20, "Expected at least 20 distinct initialization/execution exit codes"

    seen_names: set[str] = set()
    seen_values: set[int] = set()
    for name, val_str in matches:
        val = int(val_str)
        assert name not in seen_names, f"Duplicate exit code name {name}"
        assert val not in seen_values, f"Duplicate exit code numeric value {val} for {name}"
        seen_names.add(name)
        seen_values.add(val)
        assert f"ExitCode::{name}" in diagnostics_cpp, f"{name} must be registered in TryParseSimulatedFailure"
        assert name in exit_docs, f"{name} must be documented in docs/exit-codes-and-diagnostics.md"

    # Verify swapchain QueryInterface(IDXGISwapChain3) checks HRESULT and null pointer before dereferencing
    assert 'swapchain1->QueryInterface(__uuidof(IDXGISwapChain3)' in bootstrap_cpp
    assert '"IDXGISwapChain1::QueryInterface(IDXGISwapChain3) failed"' in bootstrap_cpp

    # Verify ExecuteEmptyPlayerBootstrap queries real OS build (RtlGetVersion) and adapter on _WIN32
    assert "RtlGetVersion" in bootstrap_cpp
    assert 'LoadLibraryExA("dxgi.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32)' in bootstrap_cpp
    assert 'LoadLibraryExA("d3d12.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32)' in bootstrap_cpp
    assert "PopulateAdapterCapabilitySnapshot(device, win_build" in bootstrap_cpp
    # All three adapter enumeration loops must check FAILED(enum_hr) || !candidate before dereferencing
    assert bootstrap_cpp.count("const HRESULT enum_hr = factory->EnumAdapters1(idx, &candidate);") == 3
    assert bootstrap_cpp.count("if (FAILED(enum_hr) || !candidate)") == 3
    assert bootstrap_cpp.count("if (FAILED(candidate->GetDesc1(&desc)))") == 3
    # Clean-system runs must emit observed GPU readback bytes, not a copied expected hash.
    assert 'std::strcmp(arg, "--emit-readback-values")' in bootstrap_cpp
    assert 'NW_READBACK_U32=' in bootstrap_cpp


def test_preserved_experiments_retain_reproducible_commands_and_honest_unverified_state() -> None:
    exp_dir = REPO_ROOT / "benchmarks" / "experiments"
    expected_experiments = {
        "msvc-ltcg-comparison.json": ("toolchain_comparison", "rejected_preserved"),
        "compact-profile-experiment.json": ("compact_profile", "failed_preserved"),
        "crt-elimination-experiment.json": ("crt_elimination", "failed_preserved"),
    }
    for filename, (expected_cat, expected_status) in expected_experiments.items():
        record = json.loads((exp_dir / filename).read_text(encoding="utf-8"))
        assert record["category"] == expected_cat
        assert record["status"] == expected_status
        assert record["verification_status"] == "unverified"
        assert record["substituted_silently"] is False
        assert "cmake --preset" in record["reproducible_command"]
        assert record["configuration"]["security_mitigations_retained"] is True
        assert len(record["initialization_and_cleanup_obligations"]) >= 1
        assert record["measurements"]["total_distributed_bytes"] is None
        assert record["measurements"]["delta_vs_release_baseline_bytes"] is None
        assert record["diagnostics"]["exit_code"] is None
        assert len(record["unavailable_measurements"]) >= 2

    # Ensure compact-profile-experiment does NOT map 0xC000007B to ExitCode::OsVersionUnsupported (11)
    compact_rec = json.loads((exp_dir / "compact-profile-experiment.json").read_text(encoding="utf-8"))
    assert compact_rec["diagnostics"]["exit_code"] != 11
    assert "NW_ENABLE_UNSAFE_SECTION_MERGE_EXPERIMENT=ON" in compact_rec["reproducible_command"]


def test_reproducibility_comparison_and_pinned_toolchain_build_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    dir_a = tmp_path / "repro-clean-a"
    dir_b = tmp_path / "repro-clean-b"
    dir_a.mkdir()
    dir_b.mkdir()
    repro_out = tmp_path / "release-build-reproducibility.json"

    artifacts = [
        ("nw_empty_player.exe", build_synthetic_pe64(["kernel32.dll"])),
        (
            "nw_clear_present_fixture.exe",
            build_synthetic_pe64(["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"]),
        ),
        (
            "nw_compute_shader_fixture.exe",
            build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll"]),
        ),
    ]
    for name, payload in artifacts:
        (dir_a / name).write_bytes(payload)
        (dir_b / name).write_bytes(payload)

    match_report = compare_two_build_directories(dir_a, dir_b, output_report_path=repro_out)
    assert match_report["all_artifacts_match"] is True
    assert match_report["verification_status"] == "verified"
    assert repro_out.exists()
    assert json.loads(repro_out.read_text(encoding="utf-8"))["all_artifacts_match"] is True

    # A differing TimeDateStamp is recorded as a reproducibility mismatch, not thrown away.
    (dir_b / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll"], time_date_stamp=12345)
    )
    timestamp_mismatch = compare_two_build_directories(
        dir_a, dir_b, output_report_path=repro_out
    )
    assert timestamp_mismatch["verification_status"] == "mismatch_detected"
    empty_artifact = next(
        a for a in timestamp_mismatch["artifacts"] if a["fixture_id"] == "empty_player"
    )
    assert empty_artifact["time_date_stamp_identical"] is False
    assert empty_artifact["sha256_a"] is not None
    assert empty_artifact["sha256_b"] is not None
    assert json.loads(repro_out.read_text(encoding="utf-8"))["verification_status"] == "mismatch_detected"

    # A deterministic hash-derived timestamp (lld-link /Brepro) is accepted when identical
    # across both clean directories.
    for d in (dir_a, dir_b):
        (d / "nw_empty_player.exe").write_bytes(
            build_synthetic_pe64(["kernel32.dll"], time_date_stamp=0x1234ABCD)
        )
    deterministic = compare_two_build_directories(dir_a, dir_b)
    assert deterministic["all_artifacts_match"] is True

    # Restore valid PE with different section content in dir_b to verify mismatch detection
    (dir_a / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll"])
    )
    (dir_b / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll"], extra_text_bytes=512)
    )
    mismatch_report = compare_two_build_directories(dir_a, dir_b, output_report_path=repro_out)
    assert mismatch_report["all_artifacts_match"] is False
    assert mismatch_report["verification_status"] == "mismatch_detected"
    assert mismatch_report["mismatch_investigation"]["mismatches_found"] == 1

    # When pinned tools are absent, build_and_compare_two_clean_directories must record unverified
    monkeypatch.setattr(verify_module.shutil, "which", lambda _name, **_kw: None)
    monkeypatch.setattr(verify_module, "find_dxc_executable", lambda *_a, **_kw: None)
    live_repro_out = tmp_path / "live-repro.json"
    live_report = build_and_compare_two_clean_directories(
        REPO_ROOT,
        tmp_path / "live-a",
        tmp_path / "live-b",
        output_report_path=live_repro_out,
    )
    assert live_repro_out.exists()
    assert "missing_toolchain_tools" in live_report
    assert live_report["verification_status"] == "unverified"
    assert live_report["all_artifacts_match"] is False


def test_run_full_mode_isolates_debug_symbol_failure_from_reproducibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    sim_repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(REPO_ROOT / subdir, sim_repo / subdir)

    monkeypatch.setattr(verify_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(verify_module.shutil, "which", lambda name, **_kw: f"/fake/{name}")
    monkeypatch.setattr(verify_module, "find_dxc_executable", lambda *_a, **_kw: "/fake/dxc")
    monkeypatch.setattr(
        verify_module,
        "build_and_compare_two_clean_directories",
        lambda *_a, **_kw: {"all_artifacts_match": True, "verification_status": "verified"},
    )
    monkeypatch.setattr(
        verify_module,
        "build_and_compare_two_msvc_clean_directories",
        lambda *_a, **_kw: {"all_artifacts_match": True, "verification_status": "verified"},
    )

    def fail_debug_symbols(*_args, **_kwargs):
        # A raised PDB verification error must be contained to the debug-symbol check.
        raise RuntimeError("synthetic PDB verification failure")

    monkeypatch.setattr(verify_module, "build_and_verify_debug_symbols", fail_debug_symbols)
    monkeypatch.setattr(
        verify_module,
        "evaluate_offline_shader",
        lambda *_a, **_kw: {"passed": False, "reason": "synthetic test"},
    )
    monkeypatch.setattr(
        verify_module,
        "execute_release_fixture_validation",
        lambda *_a, **_kw: {"passed": False, "reason": "synthetic test"},
    )

    _code, report = run_full_mode(sim_repo)

    assert report["checks"]["reproducibility_build"]["passed"] is True
    assert report["checks"]["msvc_ltcg_build"]["passed"] is True
    assert report["checks"]["clang_debug_symbols"]["passed"] is False
    assert report["checks"]["msvc_debug_symbols"]["passed"] is False
    assert "synthetic PDB verification failure" in report["checks"]["clang_debug_symbols"]["reason"]
    assert "synthetic PDB verification failure" in report["checks"]["msvc_debug_symbols"]["reason"]


def test_verify_nw01_records_clean_system_evidence_and_gates_full_mode(tmp_path: Path) -> None:
    ps1_path = REPO_ROOT / "tools" / "verify-nw01.ps1"
    assert ps1_path.exists()
    ps1_text = ps1_path.read_text(encoding="utf-8")
    assert ".SYNOPSIS" in ps1_text
    assert '[ValidateSet("Full", "CleanSystem")]' in ps1_text
    assert "verify_nw01.py" in ps1_text

    # On the unverified repository checkout, offline-shader plus all physical/toolchain evidence items are unmet.
    evidence = check_evidence_completeness(REPO_ROOT)
    assert evidence["complete"] is False
    assert len(evidence["unmet_requirements"]) >= 4

    # Test active SDK-absence probing and CleanSystem recording in an isolated repo copy
    sim_repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(REPO_ROOT / subdir, sim_repo / subdir)

    full_code, full_summary = run_full_mode(sim_repo)
    assert full_code == 2
    assert full_summary["full_baseline_passed"] is False
    assert full_summary["checks"]["schemas"]["passed"] is True
    assert isinstance(full_summary["checks"]["offline_shader"]["passed"], bool)
    assert full_summary["checks"]["dependency_closures"]["passed"] is True
    assert full_summary["checks"]["size_ledgers"]["contracts_valid"] is True
    assert full_summary["checks"]["size_ledgers"]["comparison_verified"] is False
    assert full_summary["checks"]["size_ledgers"]["passed"] is False
    assert isinstance(full_summary["checks"]["fixture_validation"]["passed"], bool)

    # Simulate the state after a successful offline DXC build recorded by Full mode.
    dxil_blob = make_test_signed_dxil_container()
    manifest_path = sim_repo / "shaders" / "manifest.json"
    shader_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shader_manifest["verification_status"] = "verified"
    shader_manifest.pop("unverified_reason", None)
    shader_manifest["shaders"][0]["compiled_offline_with_dxc"] = True
    shader_manifest["shaders"][0]["measured_dxc_version"] = "1.8.2407.12"
    shader_manifest["shaders"][0]["offline_dxil_bytes"] = len(dxil_blob)
    shader_manifest["shaders"][0]["offline_dxil_sha256"] = hashlib.sha256(dxil_blob).hexdigest()
    shader_manifest["unavailable_measurements"] = []
    manifest_path.write_text(json.dumps(shader_manifest, indent=2) + "\n", encoding="utf-8")

    bin_dir = sim_repo / "build" / "clang-cl-release"
    bin_dir.mkdir(parents=True)
    (bin_dir / "nw_empty_player.exe").write_bytes(build_synthetic_pe64(["kernel32.dll"]))
    (bin_dir / "nw_clear_present_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"])
    )
    (bin_dir / "nw_compute_shader_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll"]) + dxil_blob
    )

    clean_sys32 = tmp_path / "System32"
    clean_sys32.mkdir()
    clean_env: dict[str, str] = {}

    sdk_checks = probe_sdk_absence(bin_dir, env=clean_env, system32_dir=clean_sys32)
    assert all(sdk_checks.values())

    # If WindowsSdkDir is set or Windows Kits Include exists in ProgramFiles(x86), probe_sdk_absence detects it
    dirty_checks = probe_sdk_absence(
        bin_dir,
        env={"WindowsSdkDir": "C:/Program Files (x86)/Windows Kits/10/"},
        system32_dir=clean_sys32,
    )
    assert dirty_checks["windows_sdk_absent"] is False

    fake_pf_x86 = tmp_path / "FakeProgramFilesX86"
    (fake_pf_x86 / "Windows Kits" / "10" / "Include").mkdir(parents=True)
    kits_checks = probe_sdk_absence(
        bin_dir,
        env={},
        system32_dir=clean_sys32,
        program_files_x86_dir=fake_pf_x86,
    )
    assert kits_checks["windows_sdk_absent"] is False

    expected = load_compute_expected_output(sim_repo)
    observed_stdout = (
        "NW_WINDOWS_BUILD=26100\n"
        "NW_ADAPTER_DESCRIPTION=NVIDIA GeForce RTX 5080\n"
        "NW_LOADED_MODULE=C:\\Windows\\System32\\D3D12Core.dll\n"
        f"NW_READBACK_ELEMENT_COUNT={expected['element_count']}\n"
        "NW_READBACK_U32=" + ",".join(f"{v:08X}" for v in expected["elements_u32"]) + "\n"
    )

    def passing_executor(_exe: Path, fixture_id: str) -> int | tuple[int, str]:
        if fixture_id == "compute_shader":
            return 0, observed_stdout
        return 0

    assert _format_nvidia_driver_version("32.0.15.6603") == "566.03"
    assert _format_nvidia_driver_version("32.0.15.7216") == "572.16"
    parsed_host_env = detect_host_system_environment(observed_stdout)
    assert parsed_host_env["gpu_model"] is not None

    # Non-NVIDIA adapters in observed_stdout or system_environment must be rejected
    intel_stdout = (
        "NW_WINDOWS_BUILD=26100\n"
        "NW_ADAPTER_DESCRIPTION=Intel(R) UHD Graphics 770\n"
    )
    assert detect_host_system_environment(intel_stdout)["gpu_model"] is None or (
        "nvidia" in (detect_host_system_environment(intel_stdout)["gpu_model"] or "").lower()
    )
    non_nv_code, non_nv_summary = run_clean_system_mode(
        sim_repo,
        bin_dir,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        system_environment={
            "os_build": "10.0.26100.3194",
            "gpu_model": "Intel(R) UHD Graphics 770",
            "nvidia_driver_version": "572.16",
        },
        executor=passing_executor,
    )
    assert non_nv_code == 1
    assert non_nv_summary["clean_system_passed"] is False
    assert non_nv_summary["system_environment_measured"] is False

    # Without measured or operator-provided system_environment (e.g. missing driver version on non-Windows),
    # CleanSystem must NOT copy the static template into a verified report.
    no_env_stdout = (
        f"NW_READBACK_ELEMENT_COUNT={expected['element_count']}\n"
        "NW_READBACK_U32=" + ",".join(f"{v:08X}" for v in expected["elements_u32"]) + "\n"
    )
    if parsed_host_env.get("nvidia_driver_version") is None:
        no_env_code, no_env_summary = run_clean_system_mode(
            sim_repo,
            bin_dir,
            allow_non_windows_simulation=True,
            env=clean_env,
            system32_dir=clean_sys32,
            executor=lambda _exe, fx: (0, no_env_stdout) if fx == "compute_shader" else 0,
        )
        assert no_env_code == 1
        assert no_env_summary["clean_system_passed"] is False
        assert no_env_summary["system_environment_measured"] is False

    measured_system_env = {
        "os_build": "10.0.26100.3194",
        "gpu_model": "NVIDIA GeForce RTX 5080",
        "nvidia_driver_version": "572.16",
    }

    clean_code, clean_summary = run_clean_system_mode(
        sim_repo,
        bin_dir,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        system_environment=measured_system_env,
        executor=passing_executor,
    )
    assert clean_code == 0
    assert clean_summary["clean_system_passed"] is True
    assert clean_summary["verification_status"] == "verified"

    recorded_clean = json.loads(
        (sim_repo / "benchmarks" / "clean-system" / "clean-system-validation.json").read_text(encoding="utf-8")
    )
    assert recorded_clean["verification_status"] == "verified"
    assert recorded_clean["clean_system_passed"] is True
    assert recorded_clean["system_environment"]["os_build"] == "10.0.26100.3194"
    assert recorded_clean["system_environment"]["gpu_model"] == "NVIDIA GeForce RTX 5080"
    assert recorded_clean["system_environment"]["nvidia_driver_version"] == "572.16"
    assert all(recorded_clean["sdk_absence_checks"].values())
    assert recorded_clean["offline_shader_execution_evidence"]["output_verified"] is True
    assert recorded_clean["offline_shader_execution_evidence"]["inbox_d3d12core_observed"] is True
    assert (
        recorded_clean["offline_shader_execution_evidence"][
            "agility_d3d12core_redist_loaded"
        ]
        is False
    )
    assert (
        recorded_clean["offline_shader_execution_evidence"]["observed_output_sha256"]
        == expected["output_sha256"]
    )

    # Any uncounted release payload/sidecar must fail the clean-system dependency closure.
    payload = bin_dir / "demo_payload.bin"
    payload.write_bytes(b"unexpected payload")
    payload_code, payload_summary = run_clean_system_mode(
        sim_repo,
        bin_dir,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        system_environment=measured_system_env,
        executor=passing_executor,
    )
    assert payload_code == 1
    assert payload_summary["clean_system_passed"] is False
    payload.unlink()

    # A wrong readback value must fail the clean-system gate rather than copying the expected hash.
    wrong_values = list(expected["elements_u32"])
    wrong_values[0] ^= 0xFFFFFFFF
    wrong_stdout = (
        f"NW_READBACK_ELEMENT_COUNT={expected['element_count']}\n"
        "NW_READBACK_U32=" + ",".join(f"{v:08X}" for v in wrong_values) + "\n"
    )

    def wrong_executor(_exe: Path, fixture_id: str) -> int | tuple[int, str]:
        if fixture_id == "compute_shader":
            return 0, wrong_stdout
        return 0

    bad_code, bad_summary = run_clean_system_mode(
        sim_repo,
        bin_dir,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        system_environment=measured_system_env,
        executor=wrong_executor,
    )
    assert bad_code == 1
    assert bad_summary["clean_system_passed"] is False
    recorded_bad = json.loads(
        (sim_repo / "benchmarks" / "clean-system" / "clean-system-validation.json").read_text(encoding="utf-8")
    )
    assert recorded_bad["verification_status"] == "unverified"
    # Failed runs preserve the measured/executed evidence separately from the pass flag.
    assert recorded_bad["offline_shader_execution_evidence"]["compiled_offline_with_dxc"] is True
    assert (
        recorded_bad["offline_shader_execution_evidence"]["observed_output_sha256"]
        != expected["output_sha256"]
    )
    assert recorded_bad["fixture_runs"][2]["exit_code"] == 0

    # Without verified offline DXC evidence, a clean run may not claim offline shader execution.
    shader_manifest["verification_status"] = "unverified"
    shader_manifest["shaders"][0]["compiled_offline_with_dxc"] = False
    shader_manifest["shaders"][0]["offline_dxil_bytes"] = None
    shader_manifest["shaders"][0]["offline_dxil_sha256"] = None
    manifest_path.write_text(json.dumps(shader_manifest, indent=2) + "\n", encoding="utf-8")
    unverified_code, unverified_summary = run_clean_system_mode(
        sim_repo,
        bin_dir,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        system_environment=measured_system_env,
        executor=passing_executor,
    )
    assert unverified_code == 1
    assert unverified_summary["clean_system_passed"] is False
    recorded_unverified = json.loads(
        (sim_repo / "benchmarks" / "clean-system" / "clean-system-validation.json").read_text(encoding="utf-8")
    )
    assert recorded_unverified["offline_shader_execution_evidence"]["compiled_offline_with_dxc"] is False


def test_observed_readback_parser_never_copies_expected_hash() -> None:
    expected = load_compute_expected_output(REPO_ROOT)
    good_stdout = "NW_READBACK_ELEMENT_COUNT=256\nNW_READBACK_U32=" + ",".join(
        f"{v:08X}" for v in expected["elements_u32"]
    )
    good = parse_observed_compute_readback(good_stdout, expected)
    assert good["elements_match"] is True
    assert good["hash_match"] is True
    assert good["observed_output_sha256"] == expected["output_sha256"]

    missing = parse_observed_compute_readback("", expected)
    assert missing["observed_output_sha256"] is None
    assert missing["elements_match"] is False


def test_record_shader_manifest_evidence_uses_validated_build_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "shaders").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "shaders" / "compute_fixture.hlsl", repo / "shaders" / "compute_fixture.hlsl")
    shutil.copy(REPO_ROOT / "shaders" / "manifest.json", repo / "shaders" / "manifest.json")

    dxil_blob = make_test_signed_dxil_container()
    stale_dxil = repo / "build" / "clang-cl-release" / "shaders" / "compute_fixture.dxil"
    stale_dxil.parent.mkdir(parents=True)
    stale_dxil.write_bytes(dxil_blob)

    mock_dxc = tmp_path / "mock_dxc.py"
    mock_dxc.write_text(
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n",
        encoding="utf-8",
    )

    # Pre-build evaluation (record_from_build=False) must NOT record from pre-existing build/... artifacts.
    pre_eval = evaluate_offline_shader(repo, str(mock_dxc), record_from_build=False)
    assert pre_eval["passed"] is False
    unrecorded = json.loads((repo / "shaders" / "manifest.json").read_text(encoding="utf-8"))
    assert unrecorded["verification_status"] == "unverified"

    # Fresh two-clean-directory build evaluation records evidence and preserves canonical paths.
    clean_a = repo / "build" / "repro-clean-a"
    clean_b = repo / "build" / "repro-clean-b"
    for d in (clean_a, clean_b):
        p = d / "shaders" / "compute_fixture.dxil"
        p.parent.mkdir(parents=True)
        p.write_bytes(dxil_blob)

    post_eval = evaluate_offline_shader(
        repo,
        str(mock_dxc),
        search_dirs=[clean_a, clean_b],
        record_from_build=True,
    )
    assert post_eval["passed"] is True
    recorded = json.loads((repo / "shaders" / "manifest.json").read_text(encoding="utf-8"))
    shader = recorded["shaders"][0]
    assert recorded["verification_status"] == "verified"
    assert shader["compiled_offline_with_dxc"] is True
    assert shader["offline_dxil_bytes"] == len(dxil_blob)
    assert shader["offline_dxil_sha256"] == hashlib.sha256(dxil_blob).hexdigest()
    assert shader["dxil_build_output_path"] == "build/clang-cl-release/shaders/compute_fixture.dxil"
    assert (
        shader["embedded_header_build_output_path"]
        == "build/clang-cl-release/shaders/generated/compute_fixture_dxil.h"
    )


def test_find_embedded_dxil_container_validates_candidate_bytes() -> None:
    dxil_blob = make_test_signed_dxil_container()
    synthetic_player = b"MZ" + b"\x00" * 64 + dxil_blob + b"trailing-data"
    assert find_embedded_dxil_container(synthetic_player) == dxil_blob
    assert find_embedded_dxil_container(b"MZ-not-a-shader") is None

    corrupted = bytearray(dxil_blob)
    corrupted[24:28] = b"\x00\x00\x00\x00"
    assert find_embedded_dxil_container(bytes(corrupted)) is None


def test_debug_symbol_evidence_requires_codeview_and_msf_pdb(tmp_path: Path) -> None:
    pdb_bytes = build_synthetic_msf_pdb()
    for exe_name, imported in (
        ("nw_empty_player.exe", ["kernel32.dll"]),
        (
            "nw_clear_present_fixture.exe",
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"],
        ),
        (
            "nw_compute_shader_fixture.exe",
            ["kernel32.dll", "d3d12.dll", "dxgi.dll"],
        ),
    ):
        pdb_name = exe_name.replace(".exe", ".pdb")
        (tmp_path / exe_name).write_bytes(build_synthetic_pe64(imported, pdb_path=pdb_name))
        (tmp_path / pdb_name).write_bytes(pdb_bytes)

    verified = verify_debug_symbol_evidence(tmp_path)
    assert verified["verified"] is True
    assert verified["missing_or_invalid"] == []
    empty_record = next(a for a in verified["artifacts"] if a["executable"] == "nw_empty_player.exe")
    assert empty_record["pdb_guid_age_match"] is True
    assert empty_record["pdb_guid"] == bytes(range(1, 17)).hex()
    assert empty_record["pdb_age"] == 1

    # A PDB without the MSF signature must not count as debugging evidence.
    (tmp_path / "nw_empty_player.pdb").write_bytes(b"not-a-pdb")
    bad = verify_debug_symbol_evidence(tmp_path)
    assert bad["verified"] is False
    assert "nw_empty_player.exe" in bad["missing_or_invalid"]

    # A stale MSF PDB from another build with a mismatched GUID or age must be rejected.
    (tmp_path / "nw_empty_player.pdb").write_bytes(
        build_synthetic_msf_pdb(guid=b"\xff" * 16, age=1)
    )
    stale_guid = verify_debug_symbol_evidence(tmp_path)
    assert stale_guid["verified"] is False
    assert "nw_empty_player.exe" in stale_guid["missing_or_invalid"]
    stale_record = next(
        a for a in stale_guid["artifacts"] if a["executable"] == "nw_empty_player.exe"
    )
    assert stale_record["pdb_guid_age_match"] is False
    assert stale_record["pdb_mismatches"][-1]["reason"] == "guid_or_age_mismatch"
    assert stale_record["pdb_mismatches"][-1]["observed_guid"] == ("ff" * 16)

    (tmp_path / "nw_empty_player.pdb").write_bytes(
        build_synthetic_msf_pdb(guid=bytes(range(1, 17)), age=2)
    )
    stale_age = verify_debug_symbol_evidence(tmp_path)
    assert stale_age["verified"] is False
    assert "nw_empty_player.exe" in stale_age["missing_or_invalid"]

    # A PE without a CodeView debug directory must not count either.
    (tmp_path / "nw_empty_player.pdb").write_bytes(pdb_bytes)
    (tmp_path / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll"])
    )
    no_debug = verify_debug_symbol_evidence(tmp_path)
    assert no_debug["verified"] is False
    assert "nw_empty_player.exe" in no_debug["missing_or_invalid"]


def test_build_runner_invokes_pinned_two_clean_directory_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    def fake_which(name: str) -> str | None:
        if name in {"cmake", "ninja", "clang-cl", "lld-link"}:
            return f"/fake/{name}"
        return None

    monkeypatch.setattr(verify_module.shutil, "which", fake_which)
    monkeypatch.setattr(verify_module, "find_dxc_executable", lambda: "/fake/dxc")

    artifacts = {
        "nw_empty_player.exe": build_synthetic_pe64(["kernel32.dll"]),
        "nw_clear_present_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"]
        ),
        "nw_compute_shader_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "d3d12.dll", "dxgi.dll"]
        ),
    }
    configure_calls: list[list[str]] = []
    build_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        command = list(cmd)
        if "-B" in command:
            configure_calls.append(command)
        elif "--build" in command:
            build_calls.append(command)
            build_dir = Path(command[command.index("--build") + 1])
            build_dir.mkdir(parents=True, exist_ok=True)
            for name, payload in artifacts.items():
                (build_dir / name).write_bytes(payload)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(verify_module.subprocess, "run", fake_run)

    report_path = tmp_path / "release-build-reproducibility.json"
    report = build_and_compare_two_clean_directories(
        REPO_ROOT,
        tmp_path / "clean-a",
        tmp_path / "clean-b",
        output_report_path=report_path,
    )

    assert len(configure_calls) == 2
    assert len(build_calls) == 2
    for command in configure_calls:
        joined = " ".join(command)
        assert "clang-cl-x64.cmake" in joined
        assert "NW_ENFORCE_PINNED_TOOLCHAIN=ON" in joined
        assert "NW_BUILD_PROFILE=release" in joined
    assert report["all_artifacts_match"] is True
    recorded = json.loads(report_path.read_text(encoding="utf-8"))
    assert recorded["verification_status"] == "verified"
    assert all(a["hash_match"] is True for a in recorded["artifacts"])


def test_run_full_mode_builds_both_toolchains_and_reflects_post_recording_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    sim_repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(REPO_ROOT / subdir, sim_repo / subdir)

    mock_dxc = tmp_path / "mock_dxc.py"
    mock_dxc.write_text(
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(verify_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        verify_module.shutil,
        "which",
        lambda name, **_kw: f"/fake/{name}"
        if name in {"cmake", "ninja", "clang-cl", "lld-link", "cl", "link"}
        else None,
    )
    monkeypatch.setattr(verify_module, "find_dxc_executable", lambda: str(mock_dxc))

    dxil_blob = make_test_signed_dxil_container()
    clang_artifacts = {
        "nw_empty_player.exe": build_synthetic_pe64(["kernel32.dll"]),
        "nw_clear_present_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"]
        ),
        "nw_compute_shader_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "d3d12.dll", "dxgi.dll"]
        )
        + dxil_blob,
    }
    msvc_artifacts = {
        "nw_empty_player.exe": build_synthetic_pe64(
            ["kernel32.dll"], extra_text_bytes=512
        ),
        "nw_clear_present_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"],
            extra_text_bytes=512,
        ),
        "nw_compute_shader_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "d3d12.dll", "dxgi.dll"],
            extra_text_bytes=512,
        )
        + dxil_blob,
    }

    expected = load_compute_expected_output(sim_repo)
    compute_stdout = (
        f"NW_READBACK_ELEMENT_COUNT={expected['element_count']}\n"
        "NW_READBACK_U32=" + ",".join(f"{v:08X}" for v in expected["elements_u32"]) + "\n"
    )

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        command = [str(c) for c in cmd]
        if any(c.endswith("mock_dxc.py") for c in command):
            return subprocess.CompletedProcess(command, 0, "dxcompiler.dll: 1.8 - 1.8.2407.12\n", "")
        if "--build" in command:
            build_dir = Path(command[command.index("--build") + 1])
            build_dir.mkdir(parents=True, exist_ok=True)

            if "debug-symbols" in str(build_dir):
                # Simulate a dev /DEBUG:FULL build: CodeView RSDS debug directory + MSF PDB.
                for fixture_id, name in (
                    ("empty_player", "nw_empty_player.exe"),
                    ("clear_present", "nw_clear_present_fixture.exe"),
                    ("compute_shader", "nw_compute_shader_fixture.exe"),
                ):
                    pdb_name = name.replace(".exe", ".pdb")
                    imported = (
                        ["kernel32.dll"]
                        if name == "nw_empty_player.exe"
                        else ["kernel32.dll", "d3d12.dll", "dxgi.dll"]
                    )
                    target = build_dir / "staging" / fixture_id
                    target.mkdir(parents=True, exist_ok=True)
                    (target / name).write_bytes(
                        build_synthetic_pe64(imported, pdb_path=pdb_name)
                    )
                    (target / pdb_name).write_bytes(build_synthetic_msf_pdb())
                return subprocess.CompletedProcess(command, 0, "", "")

            (build_dir / "shaders").mkdir(parents=True, exist_ok=True)
            (build_dir / "shaders" / "compute_fixture.dxil").write_bytes(dxil_blob)
            source_map = msvc_artifacts if "msvc-release-ltcg" in str(build_dir) else clang_artifacts
            fixture_ids = {
                "nw_empty_player.exe": "empty_player",
                "nw_clear_present_fixture.exe": "clear_present",
                "nw_compute_shader_fixture.exe": "compute_shader",
            }
            for name, payload in source_map.items():
                target = build_dir / "staging" / fixture_ids[name]
                target.mkdir(parents=True, exist_ok=True)
                (target / name).write_bytes(payload)
            return subprocess.CompletedProcess(command, 0, "", "")
        if any(c.endswith("nw_compute_shader_fixture.exe") for c in command):
            return subprocess.CompletedProcess(command, 0, compute_stdout, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(verify_module.subprocess, "run", fake_run)

    code, summary = run_full_mode(sim_repo)
    # Physical hardware and clean-system evidence are still unverified, so Full returns 2,
    # while reproducibility, MSVC build, and toolchain comparison are all verified!
    assert code == 2
    assert summary["checks"]["reproducibility_build"]["passed"] is True
    assert summary["checks"]["msvc_ltcg_build"]["passed"] is True
    assert summary["checks"]["clang_debug_symbols"]["passed"] is True
    assert summary["checks"]["msvc_debug_symbols"]["passed"] is True
    assert summary["checks"]["size_ledgers"]["comparison_verified"] is True
    assert summary["checks"]["size_ledgers"]["passed"] is True
    assert summary["checks"]["dependency_closures"]["passed"] is True
    assert summary["checks"]["schemas"]["passed"] is True
    toolchain = json.loads(
        (sim_repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    assert all(
        t["verification_status"] == "verified"
        and t["deterministic_repro_verified"] is True
        and t["pdb_debugging_verified"] is True
        for t in toolchain["evaluated_toolchains"]
    )
    assert "Measured distributed bytes" in toolchain["selected_toolchain"]["selection_rationale"]
    assert "Verified CodeView/PDB" in toolchain["selected_toolchain"]["debugging_evidence"]

    # Verify that a post-recording schema validation failure sets checks.schemas.passed=False
    call_count = {"n": 0}
    orig_validate = verify_module.validate_all_repository_artifacts

    def failing_post_validate(root: Path):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise ValueError("simulated post-recording schema error")
        return orig_validate(root)

    monkeypatch.setattr(verify_module, "validate_all_repository_artifacts", failing_post_validate)
    bad_code, bad_summary = run_full_mode(sim_repo)
    assert bad_code == 1
    assert bad_summary["gate_status"] == "failed"
    assert bad_summary["checks"]["schemas"]["passed"] is False
    assert "simulated post-recording schema error" in bad_summary["checks"]["schemas"]["reason"]

    # Verify that if build_and_verify_debug_symbols raises after a passing two-clean-directory
    # reproducibility build, reproducibility_build.passed and msvc_ltcg_build.passed stay True
    # while clang_debug_symbols.passed and msvc_debug_symbols.passed become False.
    monkeypatch.setattr(verify_module, "validate_all_repository_artifacts", orig_validate)
    monkeypatch.setattr(
        verify_module,
        "build_and_verify_debug_symbols",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["cmake", "--build", "debug-symbols"])
        ),
    )
    dbg_fail_code, dbg_fail_summary = run_full_mode(sim_repo)
    assert dbg_fail_code == 1
    assert dbg_fail_summary["gate_status"] == "failed"
    assert dbg_fail_summary["checks"]["reproducibility_build"]["passed"] is True
    assert dbg_fail_summary["checks"]["msvc_ltcg_build"]["passed"] is True
    assert dbg_fail_summary["checks"]["clang_debug_symbols"]["passed"] is False
    assert dbg_fail_summary["checks"]["msvc_debug_symbols"]["passed"] is False
    assert dbg_fail_summary["checks"]["size_ledgers"]["contracts_valid"] is True
    assert dbg_fail_summary["checks"]["size_ledgers"]["comparison_verified"] is False
    assert dbg_fail_summary["checks"]["size_ledgers"]["passed"] is False
    assert dbg_fail_summary["checks"]["schemas"]["passed"] is True
    for ledger_file in (sim_repo / "benchmarks" / "ledgers").glob("*.json"):
        ldata = json.loads(ledger_file.read_text(encoding="utf-8"))
        assert ldata["verification_status"] == "unverified"
        assert ldata["total_distributed_bytes"] is not None


def test_run_full_mode_runs_clang_path_without_msvc_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    sim_repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(REPO_ROOT / subdir, sim_repo / subdir)

    mock_dxc = tmp_path / "mock_dxc.py"
    mock_dxc.write_text(
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(verify_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        verify_module.shutil,
        "which",
        lambda name, **_kw: f"/fake/{name}"
        if name in {"cmake", "ninja", "clang-cl", "lld-link"}
        else None,
    )
    monkeypatch.setattr(verify_module, "find_dxc_executable", lambda: str(mock_dxc))

    dxil_blob = make_test_signed_dxil_container()
    clang_artifacts = {
        "nw_empty_player.exe": build_synthetic_pe64(["kernel32.dll"]),
        "nw_clear_present_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll"]
        ),
        "nw_compute_shader_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "d3d12.dll", "dxgi.dll"]
        )
        + dxil_blob,
    }
    expected = load_compute_expected_output(sim_repo)
    compute_stdout = (
        f"NW_READBACK_ELEMENT_COUNT={expected['element_count']}\n"
        "NW_READBACK_U32=" + ",".join(f"{v:08X}" for v in expected["elements_u32"]) + "\n"
    )

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        command = [str(c) for c in cmd]
        if any(c.endswith("mock_dxc.py") for c in command):
            return subprocess.CompletedProcess(command, 0, "dxcompiler.dll: 1.8 - 1.8.2407.12\n", "")
        if "--build" in command:
            build_dir = Path(command[command.index("--build") + 1])
            build_dir.mkdir(parents=True, exist_ok=True)
            if "debug-symbols" in str(build_dir):
                for fixture_id, name in (
                    ("empty_player", "nw_empty_player.exe"),
                    ("clear_present", "nw_clear_present_fixture.exe"),
                    ("compute_shader", "nw_compute_shader_fixture.exe"),
                ):
                    pdb_name = name.replace(".exe", ".pdb")
                    imported = (
                        ["kernel32.dll"]
                        if name == "nw_empty_player.exe"
                        else ["kernel32.dll", "d3d12.dll", "dxgi.dll"]
                    )
                    target = build_dir / "staging" / fixture_id
                    target.mkdir(parents=True, exist_ok=True)
                    (target / name).write_bytes(
                        build_synthetic_pe64(imported, pdb_path=pdb_name)
                    )
                    (target / pdb_name).write_bytes(build_synthetic_msf_pdb())
                return subprocess.CompletedProcess(command, 0, "", "")
            (build_dir / "shaders").mkdir(parents=True, exist_ok=True)
            (build_dir / "shaders" / "compute_fixture.dxil").write_bytes(dxil_blob)
            fixture_ids = {
                "nw_empty_player.exe": "empty_player",
                "nw_clear_present_fixture.exe": "clear_present",
                "nw_compute_shader_fixture.exe": "compute_shader",
            }
            for name, payload in clang_artifacts.items():
                target = build_dir / "staging" / fixture_ids[name]
                target.mkdir(parents=True, exist_ok=True)
                (target / name).write_bytes(payload)
            return subprocess.CompletedProcess(command, 0, "", "")
        if any(c.endswith("nw_compute_shader_fixture.exe") for c in command):
            return subprocess.CompletedProcess(command, 0, compute_stdout, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(verify_module.subprocess, "run", fake_run)

    code, summary = run_full_mode(sim_repo)
    assert code == 2
    assert summary["checks"]["reproducibility_build"]["passed"] is True
    assert summary["checks"]["clang_debug_symbols"]["passed"] is True
    assert summary["checks"]["fixture_validation"]["passed"] is True
    assert summary["checks"]["msvc_ltcg_build"]["passed"] is False
    assert "missing host prerequisites" in summary["checks"]["msvc_ltcg_build"]["reason"]
    assert any("cl (pinned MSVC" in item for item in summary["missing_prerequisites"])
    clang_entry = next(
        t
        for t in json.loads(
            (sim_repo / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
        )["evaluated_toolchains"]
        if t["toolchain_id"] == "clang-cl-lld"
    )
    assert clang_entry["verification_status"] == "verified"
    assert clang_entry["pdb_debugging_verified"] is True


def test_capture_benchmark_result_records_measured_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module
    from schema_validator import validate_file_against_schema

    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)

    bin_dir = tmp_path / "deploy"
    bin_dir.mkdir()
    (bin_dir / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll"])
    )
    (bin_dir / "nw_clear_present_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll"])
    )
    (bin_dir / "nw_compute_shader_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll"])
    )
    ledger_exe = {
        "empty_player": "nw_empty_player.exe",
        "clear_present": "nw_clear_present_fixture.exe",
        "compute_shader": "nw_compute_shader_fixture.exe",
    }
    for ledger_path in (repo / "benchmarks" / "ledgers").glob("*-clang-cl-release.json"):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        fixture_id = ledger["fixture_id"]
        ledger["distributed_components"][0]["sha256"] = hashlib.sha256(
            (bin_dir / ledger_exe[fixture_id]).read_bytes()
        ).hexdigest()
        ledger["toolchain_id"] = "clang-cl-lld"
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    expected = load_compute_expected_output(repo)
    metrics = {
        "startup_time_ms": 12.5,
        "cpu_time_ms": 20.25,
        "gpu_time_ms": 0.75,
        "committed_memory_bytes": 12345678,
        "gpu_committed_memory_bytes": 23456789,
        "vendor_id": 0x10DE,
        "device_id": 0x2C02,
        "vram_mb": 16384,
        "max_feature_level": "12_1",
        "feature_level": "12_1",
        "max_shader_model": "6_0",
        "baseline_feature_level_12_0_supported": 1,
        "baseline_shader_model_6_0_supported": 1,
        "resource_binding_tier": 2,
        "resource_heap_tier": 2,
        "wave_ops_queried": 1,
        "wave_ops_supported": 1,
        "int64_shader_ops_queried": 1,
        "int64_shader_ops_supported": 1,
        "fp16_shader_ops_queried": 1,
        "fp16_shader_ops_supported": 1,
        "typed_uav_additional_formats_supported": 1,
        "warmup_frames": 0,
        "measured_frames": 2,
    }
    fake_validation = {
        "passed": True,
        "fixture_runs": [
            {
                "fixture_id": "empty_player",
                "execution_status": "passed",
                "exit_code": 0,
                "metrics": {},
                "stdout": "",
            },
            {
                "fixture_id": "clear_present",
                "execution_status": "passed",
                "exit_code": 0,
                "metrics": metrics,
                "d3d12_validation_clean": True,
                "stdout": "NW_ADAPTER_DESCRIPTION=NVIDIA GeForce RTX 5080\n",
            },
            {
                "fixture_id": "compute_shader",
                "execution_status": "passed",
                "exit_code": 0,
                "metrics": metrics,
                "d3d12_validation_clean": True,
                "stdout": "NW_ADAPTER_DESCRIPTION=NVIDIA GeForce RTX 5080\n",
                "readback": {
                    "observed_output_sha256": expected["output_sha256"],
                    "elements_match": True,
                    "hash_match": True,
                },
            },
        ],
    }
    monkeypatch.setattr(
        verify_module,
        "execute_release_fixture_validation",
        lambda *_a, **_kw: fake_validation,
    )
    result = capture_benchmark_result(
        repo,
        bin_dir,
        hardware_tier="rtx_50_series_desktop",
        system_environment={
            "os_build": "10.0.26100.3194",
            "gpu_model": "NVIDIA GeForce RTX 5080",
            "nvidia_driver_version": "572.16",
            "cpu_model": "AMD Ryzen 7 9800X3D 8-Core Processor",
            "gpu_architecture": "Blackwell (GB203)",
            "wddm_driver_version": "32.0.15.7216",
            "windows_power_plan": "High performance",
            "nvidia_power_management_mode": "Prefer maximum performance",
            "pci_express_link_state_power_management": "Off",
            "refresh_hz": "60",
            "driver_shader_cache_state": "warm_primed",
            "os_file_cache_state": "warm",
        },
    )
    assert result["verification_status"] == "verified"
    assert result["benchmark_passed"] is True
    for fixture in result["fixture_results"]:
        assert fixture["startup_time_ms"] == 12.5
        assert fixture["gpu_time_ms"] == 0.75
    validate_file_against_schema(
        repo / "benchmarks" / "hardware" / "rtx-50-series-baseline.json",
        repo / "schemas" / "benchmark-result.schema.json",
    )


def test_release_fixture_validation_fails_on_info_queue_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    binary_dir = tmp_path / "deploy"
    binary_dir.mkdir()
    for name in (
        "nw_empty_player.exe",
        "nw_clear_present_fixture.exe",
        "nw_compute_shader_fixture.exe",
    ):
        (binary_dir / name).write_bytes(b"MZ")
    expected = load_compute_expected_output(REPO_ROOT)
    readback = (
        f"NW_READBACK_ELEMENT_COUNT={expected['element_count']}\n"
        "NW_READBACK_U32=" + ",".join(f"{v:08X}" for v in expected["elements_u32"]) + "\n"
    )

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        command = [str(c) for c in cmd]
        if any(c.endswith("nw_clear_present_fixture.exe") for c in command):
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                "NW_D3D12_VALIDATION_ERROR=index=0 severity=2 id=1 description=bad barrier\n",
            )
        if any(c.endswith("nw_compute_shader_fixture.exe") for c in command):
            return subprocess.CompletedProcess(command, 0, readback, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(verify_module.subprocess, "run", fake_run)
    result = verify_module.execute_release_fixture_validation(REPO_ROOT, binary_dir)
    assert result["passed"] is False
    clear_run = next(r for r in result["fixture_runs"] if r["fixture_id"] == "clear_present")
    assert clear_run["exit_code"] == 0
    assert clear_run["execution_status"] == "failed"
    assert clear_run["d3d12_validation_clean"] is False
    assert "bad barrier" in clear_run["d3d12_validation_errors"][0]


def test_loaded_module_paths_distinguish_inbox_d3d12core_from_redistributable() -> None:
    stdout = (
        "NW_LOADED_MODULE=C:\\Windows\\System32\\D3D12Core.dll\n"
        "NW_LOADED_MODULE=C:\\Games\\Demo\\D3D12Core.dll\n"
        "NW_LOADED_MODULE=C:\\Games\\Demo\\dxcompiler.dll\n"
    )
    modules = parse_loaded_modules(stdout)
    assert len(modules) == 3
    classification = classify_loaded_module_evidence(modules, system_root="C:/Windows")
    assert classification["inbox_d3d12core_observed"] is True
    assert classification["agility_d3d12core_redist_loaded"] is True
    assert classification["runtime_dxcompiler_loaded"] is True
    assert classification["d3d12core_classification_unknown"] is False

    basename_only = classify_loaded_module_evidence(["D3D12Core.dll"])
    assert basename_only["d3d12core_classification_unknown"] is True
    assert basename_only["agility_d3d12core_redist_loaded"] is False


def test_hardware_tier_requires_exact_desktop_class() -> None:
    assert hardware_tier_matches("NVIDIA GeForce RTX 4060", "rtx_4060_desktop") is True
    assert hardware_tier_matches("NVIDIA GeForce RTX 4060 Ti", "rtx_4060_desktop") is False
    assert hardware_tier_matches("NVIDIA GeForce RTX 4060 Laptop GPU", "rtx_4060_desktop") is False
    assert hardware_tier_matches("NVIDIA GeForce RTX 5080", "rtx_50_series_desktop") is True
    assert hardware_tier_matches("NVIDIA GeForce RTX 5080 Laptop GPU", "rtx_50_series_desktop") is False


def test_benchmark_capture_rejects_wrong_hardware_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)
    bin_dir = tmp_path / "deploy"
    bin_dir.mkdir()
    for name in ("nw_clear_present_fixture.exe", "nw_compute_shader_fixture.exe"):
        (bin_dir / name).write_bytes(build_synthetic_pe64(["kernel32.dll"]))
    monkeypatch.setattr(
        verify_module,
        "execute_release_fixture_validation",
        lambda *_a, **_kw: {
            "passed": True,
            "fixture_runs": [
                {
                    "fixture_id": "clear_present",
                    "execution_status": "passed",
                    "exit_code": 0,
                    "metrics": {},
                    "d3d12_validation_clean": True,
                    "stdout": "NW_ADAPTER_DESCRIPTION=NVIDIA GeForce RTX 5080\n",
                },
                {
                    "fixture_id": "compute_shader",
                    "execution_status": "passed",
                    "exit_code": 0,
                    "metrics": {},
                    "d3d12_validation_clean": True,
                    "stdout": "NW_ADAPTER_DESCRIPTION=NVIDIA GeForce RTX 5080\n",
                    "readback": {
                        "observed_output_sha256": "a" * 64,
                        "elements_match": True,
                        "hash_match": True,
                    },
                },
            ],
        },
    )
    result = capture_benchmark_result(
        repo,
        bin_dir,
        hardware_tier="rtx_4060_desktop",
        system_environment={
            "os_build": "10.0.26100.3194",
            "gpu_model": "NVIDIA GeForce RTX 5080",
            "nvidia_driver_version": "572.16",
        },
    )
    assert result["verification_status"] == "unverified"
    assert result["benchmark_passed"] is False
    assert any("does not match requested tier" in m["reason"] for m in result["unavailable_measurements"])


def test_benchmark_capture_missing_environment_is_valid_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module
    from schema_validator import validate_file_against_schema

    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)
    bin_dir = tmp_path / "deploy"
    bin_dir.mkdir()
    for name in ("nw_clear_present_fixture.exe", "nw_compute_shader_fixture.exe"):
        (bin_dir / name).write_bytes(build_synthetic_pe64(["kernel32.dll"]))
    monkeypatch.setattr(
        verify_module,
        "execute_release_fixture_validation",
        lambda *_a, **_kw: {
            "passed": True,
            "fixture_runs": [
                {
                    "fixture_id": "clear_present",
                    "execution_status": "passed",
                    "exit_code": 0,
                    "metrics": {},
                    "d3d12_validation_clean": True,
                    "stdout": "",
                },
                {
                    "fixture_id": "compute_shader",
                    "execution_status": "passed",
                    "exit_code": 0,
                    "metrics": {},
                    "d3d12_validation_clean": True,
                    "stdout": "",
                    "readback": {
                        "observed_output_sha256": "a" * 64,
                        "elements_match": True,
                        "hash_match": True,
                    },
                },
            ],
        },
    )
    result = capture_benchmark_result(
        repo,
        bin_dir,
        hardware_tier="rtx_50_series_desktop",
        system_environment={},
    )
    assert result["verification_status"] == "unverified"
    assert result["benchmark_passed"] is False
    validate_file_against_schema(
        repo / "benchmarks" / "hardware" / "rtx-50-series-baseline.json",
        repo / "schemas" / "benchmark-result.schema.json",
    )


def test_explicit_benchmark_capture_failure_affects_command_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import verify_nw01 as verify_module

    monkeypatch.setattr(
        verify_module,
        "capture_benchmark_result",
        lambda *_a, **_kw: {
            "benchmark_passed": False,
            "fixture_results": [{"execution_status": "passed"}],
        },
    )
    code = verify_module.main(
        ["--mode", "ContractsOnly", "--record-benchmark", "rtx_50_series_desktop"]
    )
    assert code == 2

    monkeypatch.setattr(
        verify_module,
        "capture_benchmark_result",
        lambda *_a, **_kw: {
            "benchmark_passed": False,
            "fixture_results": [{"execution_status": "failed"}],
        },
    )
    code = verify_module.main(
        ["--mode", "ContractsOnly", "--record-benchmark", "rtx_50_series_desktop"]
    )
    assert code == 1
