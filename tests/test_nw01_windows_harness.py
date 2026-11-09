import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_windows_harness_keeps_runtime_pins_in_existing_toolchain_manifest() -> None:
    sources = json.loads(read("tools/nw01/toolchain-sources.json"))
    manifest = json.loads(read("benchmarks/toolchain-manifest.v1.json"))
    module = read("tools/nw01/Nw01.Windows.psm1")

    assert sources["visual_studio"]["vcvars_version"] == "14.40.33807"
    assert "Microsoft.VisualStudio.Component.VC.14.40.17.10.x86.x64" in sources["visual_studio"]["components"]
    assert "Microsoft.VisualStudio.Component.Windows11SDK.26100" in sources["visual_studio"]["components"]
    for key in ("cmake", "ninja", "llvm", "dxc"):
        assert len(sources[key]["sha256"]) == 64

    assert "benchmarks/toolchain-manifest.v1.json" in module
    assert str(manifest["pinned_dependencies"]["cmake_version"]) not in read("tools/nw01/toolchain-sources.json")
    assert str(manifest["pinned_dependencies"]["ninja_version"]) not in read("tools/nw01/toolchain-sources.json")


def test_bootstrap_is_exact_version_fail_closed_and_has_bounded_delivery_fallback() -> None:
    bootstrap = read("tools/nw01/bootstrap-windows.ps1")
    module = read("tools/nw01/Nw01.Windows.psm1")

    assert "Find-NwExactCommand" in bootstrap
    assert "Assert-NwSha256" in bootstrap
    assert "runner-preinstalled-after-transient-download-failure" in bootstrap
    assert "downloaded-pinned-artifact" in bootstrap
    assert "Test-NwTransientDownloadException" in module
    assert "max_attempts" in module
    assert "backoff_multiplier" in module
    assert "Test-NwExactVersion" in module
    assert "Get-NwExactStandaloneToolSelection" in module
    assert "Pinned DXC precedence failure" in module
    assert "runner-provided fallback" in module
    assert "checksum" in bootstrap.lower() or "sha256" in bootstrap.lower()


def test_ci_consumes_shared_harness_and_exercises_idempotence() -> None:
    ci = read(".github/workflows/ci.yml")

    assert ci.count("tools/nw01/bootstrap-windows.ps1") >= 2
    assert "tools/nw01/enter-build-env.ps1" in ci
    assert "tools/nw01/doctor.ps1" in ci
    assert "tools/nw01/run-build-checks.ps1" in ci
    assert "tools/nw01/run-full.ps1" in ci
    assert "tools/nw01/prepare-clean-system.ps1" in ci
    assert "tools/nw01/evidence-summary.ps1" in ci
    assert "Microsoft.VisualStudio.Component.VC.14.40.17.10.x86.x64" not in ci
    assert "vs_buildtools.exe" not in ci
    assert "Parse CleanSystem runner with Windows PowerShell 5.1" in ci
    assert "System.Management.Automation.Language.Parser" in ci
    assert "shell: powershell" in ci


def test_full_wrapper_preserves_verifier_exit_contract() -> None:
    script = read("tools/nw01/run-full.ps1")

    assert "--mode Full" in script
    assert "--summary-json" in script
    assert "0 {" in script
    assert "1 {" in script
    assert "2 {" in script
    assert "3 {" in script
    assert "INCOMPLETE" in script
    assert "exit $code" in script
    assert re.search(r"(?m)^\\s*exit\\s+2\\s*$", script) is None


def test_hardware_capture_measures_host_fields_and_keeps_unreliable_inputs_explicit() -> None:
    script = read("tools/nw01/capture-hardware.ps1")

    assert "CurrentBuildNumber" in script and "UBR" in script
    assert "Get-CimInstance Win32_Processor" in script
    assert "nvidia-smi" in script
    assert "Win32_VideoController" in script
    assert "CurrentRefreshRate" in script
    assert "powercfg /getactivescheme" in script
    for name in (
        "GpuArchitecture",
        "NvidiaPowerManagementMode",
        "PciePowerManagement",
        "DriverShaderCacheState",
        "OsFileCacheState",
    ):
        assert name in script
    assert "--record-benchmark" in script
    assert '"--binary-dir", $BinaryDir' in script
    assert 'build\\msvc-release-ltcg-clean-a' in script
    assert 'build\\repro-clean-a' in script
    assert "Run tools/nw01/run-full.ps1 successfully before capture-hardware.ps1" in script


def test_clean_system_package_uses_local_git_sha_when_ci_sha_is_absent() -> None:
    package = read("tools/package-clean-system.ps1")
    wrapper = read("tools/nw01/prepare-clean-system.ps1")

    assert "$env:GITHUB_SHA" in package
    assert "git -C $RepoRoot rev-parse HEAD" in package
    assert "source_commit = $sourceCommit" in package
    assert "git -C $RepoRoot rev-parse HEAD" in wrapper
    assert "artifact-manifest.json" in wrapper
    assert "Compress-Archive" in wrapper
    assert "bootstrap-windows.ps1" not in wrapper
    assert "Refusing to overwrite existing CleanSystem output" in wrapper
    assert "nodewerk-evidence" in wrapper


def test_archive_and_git_preparation_keep_non_repo_artifacts_out_and_stage_by_allowlist() -> None:
    archive = read("tools/nw01/archive-artifacts.ps1")
    stage = read("tools/nw01/prepare-evidence-commit.ps1")

    assert "ArchiveRoot must be outside the Git working tree" in archive
    assert "archive-manifest.json" in archive
    assert "git add ." not in stage
    assert "git -C $root add -- $path" in stage
    assert "--amend" not in stage
    assert "push --force" not in stage
    assert "git push" not in stage
    assert "git -C $root commit -m $CommitMessage" in stage
    assert "build/" not in "\n".join(
        line for line in stage.splitlines() if line.strip().startswith("'")
    )
    assert "\\.zip" not in "\n".join(
        line for line in stage.splitlines() if line.strip().startswith("'")
    )


def test_evidence_summary_is_derived_from_existing_authoritative_artifacts() -> None:
    script = read("tools/nw01/evidence-summary.ps1")

    assert "benchmarks/toolchain-manifest.v1.json" in script
    assert "benchmarks/evidence-status.json" in script
    assert "Derived convenience view only" in script
    assert "selected_toolchain" in script
    assert "full_baseline" in script


def test_documented_clean_system_role_never_bootstraps_full_toolchain() -> None:
    docs = read("docs/evidence-capture.md")

    assert "Do **not** run" in docs
    assert "bootstrap-windows.ps1" in docs
    assert "run-full.ps1" in docs
    assert "capture-hardware.ps1" in docs
    assert "prepare-clean-system.ps1" in docs
    assert "run-clean-system.ps1" in docs
    assert "prepare-evidence-commit.ps1" in docs
    assert "Windows SDK: 10.0.26100.0" not in docs
    assert "vcvars:      -vcvars_ver=14.40.33807" not in docs

    clean = read("tools/nw01/run-clean-system.ps1")
    assert "bootstrap-windows.ps1" not in clean
    assert "Enter-NwBuildEnvironment" not in clean
    assert "--mode" in clean and "CleanSystem" in clean
    assert "WindowsSdkDir" in clean
    assert "dxc.exe" in clean
    assert "Get-Variable -Name IsWindows" in clean
    assert '$env:OS -eq "Windows_NT"' in clean
    assert "Do not require PowerShell 7 on CleanSystem hosts" in clean
    assert '[string]$RepoRoot = ""' in clean
    assert "$MyInvocation.MyCommand.Path" in clean
    assert "Resolve the repository root after entering the script body" in clean
    assert "artifact-manifest.json" in clean
    assert "source_commit" in clean
    assert "git -C $RepoRoot rev-parse HEAD" in clean
    assert "Refusing stale package" in clean

    for path in (
        "tools/nw01/bootstrap-windows.ps1",
        "tools/nw01/run-build-checks.ps1",
        "tools/nw01/run-full.ps1",
        "tools/nw01/capture-hardware.ps1",
    ):
        assert "artifacts\\nw01" not in read(path)


def test_msvc_version_detection_is_locale_independent() -> None:
    module = read("tools/nw01/Nw01.Windows.psm1")

    # MSVC banners are localized by the installed Windows/VS language pack.
    # Exact numeric version tokens, not English banner phrases, are the contract.
    assert '"cl" { return $output -match ("(?<![0-9.])"' in module
    assert '"link" { return $output -match ("(?<![0-9.])"' in module
    assert "Compiler Version\\s+" not in module
    assert "Linker Version\\s+" not in module
    assert "MSVC localizes the surrounding banner text" in module
