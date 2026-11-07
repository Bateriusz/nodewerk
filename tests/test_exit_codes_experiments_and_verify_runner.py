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

from test_dependency_closure_and_accounting import build_synthetic_pe64  # noqa: E402
from test_offline_shader_and_compute_fixture import make_test_signed_dxil_container  # noqa: E402
from verify_nw01 import (  # noqa: E402
    _format_nvidia_driver_version,
    build_and_compare_two_clean_directories,
    check_evidence_completeness,
    compare_two_build_directories,
    detect_host_system_environment,
    evaluate_offline_shader,
    find_embedded_dxil_container,
    load_compute_expected_output,
    parse_observed_compute_readback,
    probe_sdk_absence,
    record_shader_manifest_evidence,
    run_clean_system_mode,
    run_full_mode,
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
        ("nw_empty_player.exe", build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"])),
        (
            "nw_clear_present_fixture.exe",
            build_synthetic_pe64(["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]),
        ),
        (
            "nw_compute_shader_fixture.exe",
            build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]),
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

    # Verify non-zero TimeDateStamp (violating /Brepro) is rejected
    (dir_b / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"], time_date_stamp=12345)
    )
    with pytest.raises(ValueError, match="TimeDateStamp"):
        compare_two_build_directories(dir_a, dir_b)

    # Restore valid PE with different section content in dir_b to verify mismatch detection
    (dir_b / "nw_empty_player.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"], extra_text_bytes=512)
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
    (bin_dir / "nw_empty_player.exe").write_bytes(build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"]))
    (bin_dir / "nw_clear_present_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"])
    )
    (bin_dir / "nw_compute_shader_fixture.exe").write_bytes(
        build_synthetic_pe64(["kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]) + dxil_blob
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
    assert recorded_bad["offline_shader_execution_evidence"]["compiled_offline_with_dxc"] is None
    assert recorded_bad["offline_shader_execution_evidence"]["observed_output_sha256"] is None

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
    assert recorded_unverified["offline_shader_execution_evidence"]["compiled_offline_with_dxc"] is None


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
        "nw_empty_player.exe": build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"]),
        "nw_clear_present_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]
        ),
        "nw_compute_shader_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]
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
        "nw_empty_player.exe": build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll"]),
        "nw_clear_present_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]
        ),
        "nw_compute_shader_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]
        )
        + dxil_blob,
    }
    msvc_artifacts = {
        "nw_empty_player.exe": build_synthetic_pe64(
            ["kernel32.dll", "ucrtbase.dll"], extra_text_bytes=512
        ),
        "nw_clear_present_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"],
            extra_text_bytes=512,
        ),
        "nw_compute_shader_fixture.exe": build_synthetic_pe64(
            ["kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"],
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
            (build_dir / "shaders").mkdir(parents=True, exist_ok=True)
            (build_dir / "shaders" / "compute_fixture.dxil").write_bytes(dxil_blob)
            source_map = msvc_artifacts if "msvc-release-ltcg" in str(build_dir) else clang_artifacts
            for name, payload in source_map.items():
                (build_dir / name).write_bytes(payload)
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
    assert summary["checks"]["size_ledgers"]["comparison_verified"] is True
    assert summary["checks"]["size_ledgers"]["passed"] is True
    assert summary["checks"]["dependency_closures"]["passed"] is True
    assert summary["checks"]["schemas"]["passed"] is True

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
    assert bad_code == 2
    assert bad_summary["checks"]["schemas"]["passed"] is False
    assert "simulated post-recording schema error" in bad_summary["checks"]["schemas"]["reason"]
