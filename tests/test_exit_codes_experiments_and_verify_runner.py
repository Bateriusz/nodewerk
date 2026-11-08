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

FAILURE_INJECTION_CODES = {
    "OsVersionUnsupported": (11, "platform_bootstrap"),
    "InboxRuntimeBlocker": (25, "device_and_capability"),
    "PresentationOccluded": (36, "resource_and_queue_init"),
    "ComputeVerificationFailed": (60, "gpu_execution_and_validation"),
    "D3D12ValidationError": (61, "gpu_execution_and_validation"),
}


def failure_injection_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Return the same structured diagnostic the fixture emits for --simulate-failure."""
    name = command[command.index("--simulate-failure") + 1]
    code, category = FAILURE_INJECTION_CODES[name]
    blocker_fields = (
        " win_build=22631 d3d12_dll=10.0.22631.4317"
        " feature=D3D12CreateDevice(D3D_FEATURE_LEVEL_12_0)"
        ' summary="synthetic blocker" recovery="Record blocker before changing profile."'
        if name == "InboxRuntimeBlocker"
        else ""
    )
    diagnostic = (
        f"[NW-DIAG][exit_code={code}][name={name}][category={category}]"
        f"[stage=simulated_failure_injection][hr=0x80004005]{blocker_fields}\n"
    )
    return subprocess.CompletedProcess(command, code, "", diagnostic)


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
    desktop_cpu_model_matches,
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
    resolve_artifact_provenance,
    record_shader_manifest_evidence,
    run_build_checks_mode,
    run_failure_recovery_checks,
    run_clean_system_mode,
    run_full_mode,
    verify_debug_symbol_evidence,
    _provenance_reconciliation_problems,
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

    # The clear/present window must be visible and pumped, and an occluded Present must
    # drain the already-submitted command list through the fence before releasing resources.
    assert "WS_VISIBLE" in bootstrap_cpp
    assert "ShowWindow(hwnd, SW_SHOW)" in bootstrap_cpp
    assert "PumpPendingWindowMessages(hwnd)" in bootstrap_cpp
    assert "DXGI_STATUS_OCCLUDED" in bootstrap_cpp
    occluded_return = bootstrap_cpp.index("if (presentation_occluded)")
    assert (
        bootstrap_cpp.rfind(
            "WaitForFenceValue(fence, signal_val, fence_event, 5000)",
            0,
            occluded_return,
        )
        != -1
    )
    assert bootstrap_cpp.index("ExitCode::PresentationOccluded") > bootstrap_cpp.rfind(
        "WaitForFenceValue(fence, signal_val, fence_event, 5000)",
        0,
        bootstrap_cpp.index("ExitCode::PresentationOccluded"),
    )


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


def test_verify_nw01_records_clean_system_evidence_and_gates_full_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    # This case specifically exercises missing native build coverage. Make that
    # precondition host-independent so a provisioned Windows CI runner does not
    # turn the synthetic repo (which intentionally has no CMakeLists.txt) into a
    # real build attempt and concrete build failure.
    import verify_nw01 as verify_module

    with monkeypatch.context() as m:
        m.setattr(verify_module.platform, "system", lambda: "Windows")
        m.setattr(verify_module.shutil, "which", lambda _name, **_kw: None)
        m.setattr(verify_module, "find_dxc_executable", lambda *_a, **_kw: None)
        full_code, full_summary = run_full_mode(sim_repo)

    # Missing host tools mean required build coverage is absent; that is an exit 3,
    # not the exit 2 "only physical evidence missing" state.
    assert full_code == 3
    assert full_summary["gate_status"] == "build_coverage_missing"
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

    # A CleanSystem PASS must now be bound to recorded artifact hashes even for
    # a raw binary directory. Bind the synthetic executables to the clang ledgers.
    raw_fixture_names = {
        "empty_player": "nw_empty_player.exe",
        "clear_present": "nw_clear_present_fixture.exe",
        "compute_shader": "nw_compute_shader_fixture.exe",
    }
    raw_sizes: dict[str, int] = {}
    for ledger_path in (sim_repo / "benchmarks" / "ledgers").glob("*-clang-cl-release.json"):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        fixture_id = str(ledger["fixture_id"])
        exe_payload = (bin_dir / raw_fixture_names[fixture_id]).read_bytes()
        raw_sizes[fixture_id] = len(exe_payload)
        for component in ledger.get("distributed_components", []):
            if component.get("category") == "executable":
                component["sha256"] = hashlib.sha256(exe_payload).hexdigest()
                component["size_bytes"] = len(exe_payload)
        ledger["verification_status"] = "verified"
        ledger.pop("unverified_reason", None)
        ledger["total_distributed_bytes"] = len(exe_payload)
        ledger["unavailable_measurements"] = [
            item
            for item in ledger.get("unavailable_measurements", [])
            if item["metric"] != "total_distributed_bytes"
        ]
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")

    # CleanSystem may certify only the selected toolchain after Full has verified its
    # three release ledgers. Simulate that post-Full state in the isolated repo.
    sim_toolchain_path = sim_repo / "benchmarks" / "toolchain-manifest.v1.json"
    sim_toolchain = json.loads(sim_toolchain_path.read_text(encoding="utf-8"))
    sim_toolchain["selected_toolchain"]["toolchain_id"] = "clang-cl-lld"
    for entry in sim_toolchain["evaluated_toolchains"]:
        if entry["toolchain_id"] == "clang-cl-lld":
            entry["status"] = "selected"
            entry["verification_status"] = "verified"
            entry["pdb_debugging_verified"] = True
            entry["deterministic_repro_verified"] = True
            entry["fixture_execution_verified"] = True
            entry["empty_player_bytes"] = raw_sizes.get("empty_player")
            entry["clear_present_bytes"] = raw_sizes.get("clear_present")
            entry["compute_fixture_bytes"] = raw_sizes.get("compute_shader")
        else:
            entry["status"] = "retained_alternative"
    sim_toolchain_path.write_text(
        json.dumps(sim_toolchain, indent=2) + "\n", encoding="utf-8"
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
        if fixture_id in {"clear_present", "compute_shader"}:
            return 0, observed_stdout
        return 0

    assert _format_nvidia_driver_version("32.0.15.6603") == "566.03"
    assert _format_nvidia_driver_version("32.0.15.7216") == "572.16"

    # Exercise stdout parsing without allowing the test host's Windows registry,
    # display adapter, or NVIDIA driver to influence the result.
    intel_stdout = (
        "NW_WINDOWS_BUILD=26100\n"
        "NW_ADAPTER_DESCRIPTION=Intel(R) UHD Graphics 770\n"
    )
    with monkeypatch.context() as m:
        m.setattr(verify_module.platform, "system", lambda: "Linux")
        parsed_host_env = detect_host_system_environment(observed_stdout)
        parsed_intel_env = detect_host_system_environment(intel_stdout)

    assert parsed_host_env["os_build"] == "10.0.26100.0"
    assert parsed_host_env["gpu_model"] == "NVIDIA GeForce RTX 5080"
    assert parsed_host_env["nvidia_driver_version"] is None
    assert parsed_intel_env["gpu_model"] is None

    # Every CleanSystem scenario below is a simulation. Bind host detection only
    # to the synthetic fixture stdout so the physical CI runner cannot leak its
    # registry/adapter/driver state into these unit-test outcomes.
    def simulated_host_environment(stdout: str = "") -> dict[str, str | None]:
        return {
            "os_build": "10.0.26100.0" if "NW_WINDOWS_BUILD=26100" in stdout else None,
            "gpu_model": (
                "NVIDIA GeForce RTX 5080"
                if "NW_ADAPTER_DESCRIPTION=NVIDIA GeForce RTX 5080" in stdout
                else None
            ),
            "nvidia_driver_version": None,
        }

    monkeypatch.setattr(
        verify_module, "detect_host_system_environment", simulated_host_environment
    )

    # Non-NVIDIA adapters in observed_stdout or system_environment must be rejected
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

    # A downloaded CI package keeps runtime closure under deploy/ and immutable build
    # evidence under evidence/. CleanSystem must infer both roots and must not classify
    # evidence JSON as runtime sidecars.
    package_root = sim_repo / "clean-system-package"
    package_deploy = package_root / "deploy"
    package_evidence = package_root / "evidence"
    package_ledgers = package_evidence / "ledgers"
    package_deploy.mkdir(parents=True)
    package_ledgers.mkdir(parents=True)
    package_hashes: dict[str, str] = {}
    package_sizes: dict[str, int] = {}
    fixture_names = {
        "empty_player": "nw_empty_player.exe",
        "clear_present": "nw_clear_present_fixture.exe",
        "compute_shader": "nw_compute_shader_fixture.exe",
    }
    for fixture_id, exe_name in fixture_names.items():
        payload_bytes = (bin_dir / exe_name).read_bytes()
        (package_deploy / exe_name).write_bytes(payload_bytes)
        package_hashes[fixture_id] = hashlib.sha256(payload_bytes).hexdigest()
        package_sizes[fixture_id] = len(payload_bytes)

    shutil.copy(manifest_path, package_evidence / "shaders-manifest.json")
    shutil.copy(
        sim_repo / "benchmarks" / "toolchain-manifest.v1.json",
        package_evidence / "toolchain-manifest.v1.json",
    )
    for ledger_path in (sim_repo / "benchmarks" / "ledgers").glob("*.json"):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if (
            ledger.get("toolchain_id") == "clang-cl-lld"
            and ledger.get("fixture_id") in fixture_names
        ):
            ledger["verification_status"] = "verified"
            ledger.pop("unverified_reason", None)
            for component in ledger.get("distributed_components", []):
                if component.get("category") == "executable":
                    fixture_id = str(ledger["fixture_id"])
                    component["sha256"] = package_hashes[fixture_id]
                    component["size_bytes"] = package_sizes[fixture_id]
            ledger["total_distributed_bytes"] = package_sizes[
                str(ledger["fixture_id"])
            ]
            ledger["unavailable_measurements"] = [
                item
                for item in ledger.get("unavailable_measurements", [])
                if item["metric"] != "total_distributed_bytes"
            ]
        (package_ledgers / ledger_path.name).write_text(
            json.dumps(ledger, indent=2) + "\n", encoding="utf-8"
        )

    package_manifest = {
        "schema_version": "1",
        "source_commit": "synthetic-test-sha",
        "source_build_directory": "build/repro-clean-a",
        "toolchain": "clang-cl-lld",
        "purpose": "NW-01 CleanSystem physical Windows execution",
        "files": [
            {
                "name": exe_name,
                "bytes": package_sizes[fixture_id],
                "sha256": package_hashes[fixture_id],
            }
            for fixture_id, exe_name in fixture_names.items()
        ],
    }
    (package_evidence / "artifact-manifest.json").write_text(
        json.dumps(package_manifest, indent=2) + "\n", encoding="utf-8"
    )

    build_checks = {
        "concrete_failures": [],
        "checks": {
            "reproducibility_build": {"passed": True},
            "clang_debug_symbols": {"passed": True},
            "offline_shader": {"passed": True},
            "failure_recovery": {"passed": True},
            "size_ledgers": {
                "recorded": {
                    "clang-cl-lld": {
                        "selected_toolchain": "clang-cl-lld",
                        "verification_status": "verified",
                        "recorded_fixtures": {
                            fixture_id: {"sha256": package_hashes[fixture_id]}
                            for fixture_id in fixture_names
                        },
                    }
                }
            },
        },
    }
    (package_evidence / "build-checks.json").write_text(
        json.dumps(build_checks, indent=2) + "\n", encoding="utf-8"
    )

    packaged_code, packaged_summary = run_clean_system_mode(
        sim_repo,
        package_root,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        system_environment=measured_system_env,
        executor=passing_executor,
    )
    assert packaged_code == 0
    assert packaged_summary["clean_system_passed"] is True
    assert packaged_summary["dependency_closure_failures"] == []
    assert packaged_summary["embedded_dxil_verified"] is True
    assert packaged_summary["artifact_provenance"]["toolchain_id"] == "clang-cl-lld"
    assert (
        packaged_summary["artifact_provenance"]["source"]
        == "recorded_ledger_artifact_hash"
    )

    # Verified ledgers must still be rejected when the packaged CI build evidence is tampered.
    tampered_build_checks = dict(build_checks)
    tampered_build_checks["checks"] = dict(build_checks["checks"])
    tampered_build_checks["checks"]["clang_debug_symbols"] = {"passed": False}
    (package_evidence / "build-checks.json").write_text(
        json.dumps(tampered_build_checks, indent=2) + "\n", encoding="utf-8"
    )
    tampered_code, tampered_summary = run_clean_system_mode(
        sim_repo,
        package_root,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        system_environment=measured_system_env,
        executor=passing_executor,
    )
    assert tampered_code == 1
    assert tampered_summary["clean_system_passed"] is False
    assert tampered_summary["artifact_provenance"]["toolchain_id"] is None
    (package_evidence / "build-checks.json").write_text(
        json.dumps(build_checks, indent=2) + "\n", encoding="utf-8"
    )
    restored_code, restored_summary = run_clean_system_mode(
        sim_repo,
        package_root,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        system_environment=measured_system_env,
        executor=passing_executor,
    )
    assert restored_code == 0
    assert restored_summary["clean_system_passed"] is True

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

    # Operator-supplied GPU metadata must be reconciled with the executing adapter.
    mismatched_system_env = dict(measured_system_env)
    mismatched_system_env["gpu_model"] = "NVIDIA GeForce RTX 4060"
    mismatch_code, mismatch_summary = run_clean_system_mode(
        sim_repo,
        bin_dir,
        allow_non_windows_simulation=True,
        env=clean_env,
        system32_dir=clean_sys32,
        system_environment=mismatched_system_env,
        executor=passing_executor,
    )
    assert mismatch_code == 1
    assert mismatch_summary["clean_system_passed"] is False
    assert mismatch_summary["system_environment_measured"] is False
    recorded_mismatch = json.loads(
        (sim_repo / "benchmarks" / "clean-system" / "clean-system-validation.json").read_text(
            encoding="utf-8"
        )
    )
    # The report must not attribute the run to the operator-supplied RTX 4060.
    assert recorded_mismatch["system_environment"]["gpu_model"] == "NVIDIA GeForce RTX 5080"
    assert recorded_mismatch["system_environment"]["nvidia_driver_version"] == "unverified"

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
        if "--simulate-failure" in command:
            return failure_injection_process(command)
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
        if "--simulate-failure" in command:
            return failure_injection_process(command)
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
    # The clang path built and executed, but the required MSVC/LTCG comparison coverage
    # is missing, so this must not be reported as an evidence-only exit 2.
    assert code == 3
    assert summary["gate_status"] == "build_coverage_missing"
    assert "msvc_ltcg_build" in summary["missing_build_coverage"]
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
        payload = (bin_dir / ledger_exe[fixture_id]).read_bytes()
        ledger["distributed_components"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
        ledger["distributed_components"][0]["size_bytes"] = len(payload)
        ledger["toolchain_id"] = "clang-cl-lld"
        ledger["verification_status"] = "verified"
        ledger.pop("unverified_reason", None)
        ledger["total_distributed_bytes"] = len(payload)
        ledger["unavailable_measurements"] = [
            item
            for item in ledger.get("unavailable_measurements", [])
            if item["metric"] != "total_distributed_bytes"
        ]
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    toolchain_path = repo / "benchmarks" / "toolchain-manifest.v1.json"
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    for entry in toolchain["evaluated_toolchains"]:
        if entry["toolchain_id"] == "clang-cl-lld":
            entry["status"] = "selected"
            entry["verification_status"] = "verified"
            entry["pdb_debugging_verified"] = True
            entry["deterministic_repro_verified"] = True
            entry["fixture_execution_verified"] = True
            for field, fixture_id in (
                ("empty_player_bytes", "empty_player"),
                ("clear_present_bytes", "clear_present"),
                ("compute_fixture_bytes", "compute_shader"),
            ):
                entry[field] = (bin_dir / ledger_exe[fixture_id]).stat().st_size
        else:
            entry["status"] = "retained_alternative"
    toolchain_path.write_text(
        json.dumps(toolchain, indent=2) + "\n", encoding="utf-8"
    )
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


def test_hardware_tier_requires_desktop_compatibility_class() -> None:
    assert hardware_tier_matches("NVIDIA GeForce RTX 4060", "rtx_4060_desktop") is True
    assert hardware_tier_matches("NVIDIA GeForce RTX 4060 Ti", "rtx_4060_desktop") is True
    assert hardware_tier_matches("NVIDIA GeForce RTX 4060 Laptop GPU", "rtx_4060_desktop") is False
    assert hardware_tier_matches("NVIDIA GeForce RTX 4060 Ti Laptop GPU", "rtx_4060_desktop") is False
    assert hardware_tier_matches("NVIDIA GeForce RTX 5080", "rtx_50_series_desktop") is True
    assert hardware_tier_matches("NVIDIA GeForce RTX 5080 Laptop GPU", "rtx_50_series_desktop") is False

    # A desktop GPU name is not sufficient: the recorded CPU must also identify a desktop part.
    assert desktop_cpu_model_matches("AMD Ryzen 5 5600 6-Core Processor") is True
    assert desktop_cpu_model_matches("AMD Ryzen 7 9800X3D 8-Core Processor") is True
    assert desktop_cpu_model_matches("13th Gen Intel(R) Core(TM) i7-1360P") is False
    assert desktop_cpu_model_matches("Intel(R) Core(TM) i7-1360P CPU @ 2.20GHz") is False
    assert desktop_cpu_model_matches("AMD Ryzen 7 7840HS w/ Radeon 780M Graphics") is False
    assert desktop_cpu_model_matches(None) is False
    assert (
        hardware_tier_matches(
            "NVIDIA GeForce RTX 5070 Ti",
            "rtx_50_series_desktop",
            cpu_model="13th Gen Intel(R) Core(TM) i7-1360P",
        )
        is False
    )


def test_artifact_provenance_rejects_unverified_release_ledgers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "shaders", "fixtures"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)

    bin_dir = tmp_path / "deploy"
    bin_dir.mkdir()
    fixture_names = {
        "empty_player": "nw_empty_player.exe",
        "clear_present": "nw_clear_present_fixture.exe",
        "compute_shader": "nw_compute_shader_fixture.exe",
    }
    for exe_name in fixture_names.values():
        (bin_dir / exe_name).write_bytes(build_synthetic_pe64(["kernel32.dll"]))

    sizes: dict[str, int] = {}
    # Bind matching artifact hashes into the copied release ledgers but deliberately leave
    # every ledger and the selected toolchain entry unverified.
    for ledger_path in (repo / "benchmarks" / "ledgers").glob("*-clang-cl-release.json"):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        fixture_id = str(ledger["fixture_id"])
        payload = (bin_dir / fixture_names[fixture_id]).read_bytes()
        sizes[fixture_id] = len(payload)
        for component in ledger.get("distributed_components", []):
            if component.get("category") == "executable":
                component["sha256"] = hashlib.sha256(payload).hexdigest()
                component["size_bytes"] = len(payload)
        ledger["total_distributed_bytes"] = len(payload)
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")

    provenance = resolve_artifact_provenance(repo, bin_dir)
    assert provenance["toolchain_id"] is None
    assert provenance["source"] == "unavailable"

    # A manifest entry marked verified without the execution/PDB/reproducibility flags
    # cannot establish provenance even when all three ledger hashes match.
    for ledger_path in (repo / "benchmarks" / "ledgers").glob("*-clang-cl-release.json"):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["verification_status"] = "verified"
        ledger.pop("unverified_reason", None)
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    toolchain_path = repo / "benchmarks" / "toolchain-manifest.v1.json"
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    clang_entry = next(
        entry
        for entry in toolchain["evaluated_toolchains"]
        if entry["toolchain_id"] == "clang-cl-lld"
    )
    clang_entry.update(
        status="selected",
        verification_status="verified",
        fixture_execution_verified=True,
        pdb_debugging_verified=True,
        deterministic_repro_verified=True,
        deployment_closure_identical=True,
        empty_player_bytes=sizes["empty_player"],
        clear_present_bytes=sizes["clear_present"],
        compute_fixture_bytes=sizes["compute_shader"],
    )
    toolchain_path.write_text(json.dumps(toolchain, indent=2) + "\n", encoding="utf-8")
    assert resolve_artifact_provenance(repo, bin_dir)["toolchain_id"] == "clang-cl-lld"

    for flag in (
        "fixture_execution_verified",
        "pdb_debugging_verified",
        "deterministic_repro_verified",
    ):
        clang_entry[flag] = False
        toolchain_path.write_text(json.dumps(toolchain, indent=2) + "\n", encoding="utf-8")
        provenance = resolve_artifact_provenance(repo, bin_dir)
        assert provenance["toolchain_id"] is None
        assert provenance["source"] == "unavailable"
        clang_entry[flag] = True
    toolchain_path.write_text(json.dumps(toolchain, indent=2) + "\n", encoding="utf-8")


def test_evidence_completeness_rejects_mobile_cpu_on_verified_hardware(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "shaders", "fixtures"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)
    hw_path = repo / "benchmarks" / "hardware" / "rtx-50-series-baseline.json"
    hw = json.loads(hw_path.read_text(encoding="utf-8"))
    hw["verification_status"] = "verified"
    hw["benchmark_passed"] = True
    hw["environment_metadata"]["cpu_model"] = "13th Gen Intel(R) Core(TM) i7-1360P"
    hw["artifact_provenance"] = {
        "toolchain_id": "clang-cl-lld",
        "artifact_sha256": {},
        "source": "recorded_ledger_artifact_hash",
    }
    hw_path.write_text(json.dumps(hw, indent=2) + "\n", encoding="utf-8")

    evidence = check_evidence_completeness(repo)
    assert evidence["complete"] is False
    assert any(
        "recognized desktop-class CPU" in item
        for item in evidence["unmet_requirements"]
    )


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
                    "stdout": "NW_WINDOWS_BUILD=26100\n",
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
    assert result["environment_metadata"]["os_build"] is None
    assert any(
        item["metric"] == "environment_metadata.os_build"
        for item in result["unavailable_measurements"]
    )
    validate_file_against_schema(
        repo / "benchmarks" / "hardware" / "rtx-50-series-baseline.json",
        repo / "schemas" / "benchmark-result.schema.json",
    )


def test_cli_infers_packaged_benchmark_evidence_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    package_root = tmp_path / "clean-system-package"
    (package_root / "deploy").mkdir(parents=True)
    (package_root / "evidence").mkdir()

    observed: dict[str, Path | None] = {}

    def fake_capture(
        _repo_root: Path,
        binary_dir: Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        observed["binary_dir"] = binary_dir
        observed["evidence_dir"] = kwargs.get("evidence_dir")
        return {
            "benchmark_passed": True,
            "fixture_results": [{"execution_status": "passed"}],
        }

    monkeypatch.setattr(verify_module, "capture_benchmark_result", fake_capture)
    monkeypatch.setattr(
        verify_module,
        "validate_all_repository_artifacts",
        lambda _repo_root: {"validated_count": 1},
    )
    monkeypatch.setattr(
        verify_module,
        "verify_all_recorded_ledgers",
        lambda _repo_root: [],
    )
    monkeypatch.setattr(
        verify_module,
        "generate_size_comparison_report",
        lambda _repo_root: {"selected_toolchain": "clang-cl-lld"},
    )

    code = verify_module.main(
        [
            "--mode",
            "ContractsOnly",
            "--binary-dir",
            str(package_root),
            "--record-benchmark",
            "rtx_50_series_desktop",
        ]
    )
    assert code == 0
    assert observed["binary_dir"] == package_root
    assert observed["evidence_dir"] == package_root / "evidence"


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


def test_failure_recovery_checks_execute_distinct_exit_codes_and_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "nw_empty_player.exe").write_bytes(b"MZ")

    def good_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return failure_injection_process([str(c) for c in cmd])

    monkeypatch.setattr(verify_module.subprocess, "run", good_run)
    result = run_failure_recovery_checks(deploy)
    assert result["passed"] is True
    assert result["distinct_exit_codes"] is True
    assert {
        case["observed_exit_code"] for case in result["cases"]
    } == {11, 25, 36, 60, 61}
    assert all(case["diagnostic_line_matched"] for case in result["cases"])

    # InboxRuntimeBlocker must carry every mandatory blocker field.
    def missing_blocker_fields_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        command = [str(c) for c in cmd]
        name = command[command.index("--simulate-failure") + 1]
        code, category = FAILURE_INJECTION_CODES[name]
        diagnostic = (
            f"[NW-DIAG][exit_code={code}][name={name}][category={category}]"
            "[stage=simulated_failure_injection][hr=0x80004005]\n"
        )
        return subprocess.CompletedProcess(command, code, "", diagnostic)

    monkeypatch.setattr(
        verify_module.subprocess, "run", missing_blocker_fields_run
    )
    result = run_failure_recovery_checks(deploy)
    assert result["passed"] is False
    blocker_case = next(
        case for case in result["cases"] if case["name"] == "InboxRuntimeBlocker"
    )
    assert blocker_case["missing_blocker_fields"]

    missing = run_failure_recovery_checks(tmp_path / "missing")
    assert missing["passed"] is False
    assert missing["skipped"] is True


def test_build_checks_mode_requires_actual_build_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    sim_repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(REPO_ROOT / subdir, sim_repo / subdir)

    # This synthetic repository intentionally has no build system. Force the
    # "required build coverage unavailable" precondition instead of depending
    # on what tools happen to be installed on the CI host.
    with monkeypatch.context() as m:
        m.setattr(verify_module.platform, "system", lambda: "Windows")
        m.setattr(verify_module.shutil, "which", lambda _name, **_kw: None)
        m.setattr(verify_module, "find_dxc_executable", lambda *_a, **_kw: None)
        code, summary = run_build_checks_mode(sim_repo)
    assert code == 1
    assert summary["mode"] == "BuildChecks"
    assert summary["build_checks_passed"] is False
    assert summary["full_baseline_passed"] is False
    assert "reproducibility_build" in summary["missing_required_checks"]
    assert "failure_recovery" in summary["missing_required_checks"]


def test_ci_provisions_pinned_msvc_toolset_and_windows_sdk() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "Microsoft.VisualStudio.Component.VC.14.40.17.10.x86.x64" in ci
    assert "Microsoft.VisualStudio.Component.Windows11SDK.26100" in ci
    assert "Microsoft.VisualStudio.Product.BuildTools" in ci
    assert '-version "[17.0,18.0)"' in ci
    assert "vcvars64.bat" in ci
    assert "-vcvars_ver=14.40" in ci
    assert 'call "$vcvars" 10.0.26100.0 -vcvars_ver=14.40' in ci
    assert 'VCToolsVersion -notmatch "^14\\.40\\."' in ci
    assert "cl.exe is outside the pinned VCToolsInstallDir" in ci
    assert "link.exe is outside the pinned VCToolsInstallDir" in ci
    assert "19\\.40\\.33821" in ci
    assert "14\\.40\\.33821\\.0" in ci
    assert "Compiler Version 19\\.40\\.33821" in ci
    assert "Linker Version 14\\.40\\.33821\\.0" in ci
    assert "--mode BuildChecks" in ci
    assert "build_checks_passed" in ci
    # Exit 2 is tolerated only after explicit required concrete checks pass.
    assert "Full mode exited 2 but required concrete check" in ci


def test_ci_cancels_superseded_runs_and_avoids_duplicate_agent_push_runs() -> None:
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert '"agent/**"' not in ci
    assert "concurrency:" in ci
    assert (
        "group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
        in ci
    )
    assert "cancel-in-progress: true" in ci


def test_evidence_completeness_requires_matching_artifact_hashes_and_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verify_nw01 as verify_module

    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(REPO_ROOT / subdir, repo / subdir)

    hashes = {
        "empty_player": "a" * 64,
        "clear_present": "b" * 64,
        "compute_shader": "c" * 64,
    }
    ledger_names = {
        "empty_player": "nw_empty_player.exe",
        "clear_present": "nw_clear_present_fixture.exe",
        "compute_shader": "nw_compute_shader_fixture.exe",
    }
    for ledger_path in (repo / "benchmarks" / "ledgers").glob("*-clang-cl-release.json"):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        fixture_id = ledger["fixture_id"]
        ledger["verification_status"] = "verified"
        ledger.pop("unverified_reason", None)
        ledger["total_distributed_bytes"] = 2048
        for component in ledger["distributed_components"]:
            if component["category"] == "executable":
                component["size_bytes"] = 2048
                component["sha256"] = hashes[fixture_id]
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")

    toolchain_path = repo / "benchmarks" / "toolchain-manifest.v1.json"
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    for entry in toolchain["evaluated_toolchains"]:
        entry["verification_status"] = "verified"
        entry["pdb_debugging_verified"] = True
        entry["deterministic_repro_verified"] = True
        entry["fixture_execution_verified"] = True
    toolchain_path.write_text(json.dumps(toolchain, indent=2) + "\n", encoding="utf-8")

    hlsl_path = repo / "shaders" / "compute_fixture.hlsl"
    hlsl_bytes = hlsl_path.read_bytes()
    manifest_path = repo / "shaders" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification_status"] = "verified"
    manifest.pop("unverified_reason", None)
    shader = manifest["shaders"][0]
    shader["compiled_offline_with_dxc"] = True
    shader["hlsl_source_bytes"] = len(hlsl_bytes)
    shader["hlsl_sha256"] = hashlib.sha256(hlsl_bytes).hexdigest()
    shader["offline_dxil_bytes"] = 1024
    shader["offline_dxil_sha256"] = "e" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    for repro_name in (
        "release-build-reproducibility.json",
        "msvc-ltcg-release-reproducibility.json",
    ):
        repro_path = repo / "benchmarks" / "reproducibility" / repro_name
        repro = json.loads(repro_path.read_text(encoding="utf-8"))
        repro["verification_status"] = "verified"
        repro["all_artifacts_match"] = True
        repro_path.write_text(json.dumps(repro, indent=2) + "\n", encoding="utf-8")

    provenance = {
        "toolchain_id": "clang-cl-lld",
        "artifact_sha256": hashes,
        "source": "recorded_ledger_artifact_hash",
    }
    hardware_paths = [
        repo / "benchmarks" / "hardware" / "rtx-4060-baseline.json",
        repo / "benchmarks" / "hardware" / "rtx-50-series-baseline.json",
    ]
    for hardware_path in hardware_paths:
        hardware = json.loads(hardware_path.read_text(encoding="utf-8"))
        hardware["verification_status"] = "verified"
        hardware["benchmark_passed"] = True
        hardware["environment_metadata"]["cpu_model"] = (
            "AMD Ryzen 7 9800X3D 8-Core Processor"
        )
        hardware["artifact_provenance"] = provenance
        hardware_path.write_text(
            json.dumps(hardware, indent=2) + "\n", encoding="utf-8"
        )

    clean_path = repo / "benchmarks" / "clean-system" / "clean-system-validation.json"
    clean = json.loads(clean_path.read_text(encoding="utf-8"))
    clean["verification_status"] = "verified"
    clean["clean_system_passed"] = True
    clean["artifact_provenance"] = provenance
    for item in clean["deployed_dependency_inventory"]["deployed_files"]:
        base = item["file_name"].replace("\\", "/").rsplit("/", 1)[-1]
        fixture_id = next(
            key for key, value in ledger_names.items() if value == base
        )
        item["size_bytes"] = 2048
        item["sha256"] = hashes[fixture_id]
    clean_path.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        verify_module,
        "generate_size_comparison_report",
        lambda _root: {
            "fixture_comparisons": [
                {"fixture_id": fixture_id, "verification_status": "verified"}
                for fixture_id in ("empty_player", "clear_present", "compute_shader")
            ]
        },
    )

    evidence = check_evidence_completeness(repo)
    assert evidence["complete"] is True, evidence["unmet_requirements"]

    hardware = json.loads(hardware_paths[0].read_text(encoding="utf-8"))
    hardware["artifact_provenance"]["artifact_sha256"]["clear_present"] = "f" * 64
    hardware_paths[0].write_text(
        json.dumps(hardware, indent=2) + "\n", encoding="utf-8"
    )
    evidence = check_evidence_completeness(repo)
    assert evidence["complete"] is False
    assert any("does not match" in item for item in evidence["unmet_requirements"])

    hardware["artifact_provenance"]["artifact_sha256"]["clear_present"] = hashes[
        "clear_present"
    ]
    hardware["artifact_provenance"]["toolchain_id"] = "msvc-ltcg"
    hardware_paths[0].write_text(
        json.dumps(hardware, indent=2) + "\n", encoding="utf-8"
    )
    evidence = check_evidence_completeness(repo)
    assert evidence["complete"] is False
    assert any("toolchain" in item for item in evidence["unmet_requirements"])

    problems = _provenance_reconciliation_problems(
        evidence_kind="synthetic evidence",
        evidence_hash="0" * 64,
        evidence_toolchain=None,
        selected_toolchain="clang-cl-lld",
        expected_hash="1" * 64,
        fixture_id="clear_present",
    )
    assert len(problems) >= 2
