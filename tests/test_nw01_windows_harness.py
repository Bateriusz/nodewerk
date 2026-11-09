import hashlib
import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = os.name == "nt"
PWSH_EXECUTABLE = shutil.which("pwsh")
HAS_PWSH = PWSH_EXECUTABLE is not None
BEHAVIOR_TESTS_ENABLED = os.environ.get("NW_HARNESS_BEHAVIOR_TESTS") == "1"
requires_windows_harness = pytest.mark.skipif(
    not (HAS_PWSH and IS_WINDOWS and BEHAVIOR_TESTS_ENABLED),
    reason="PowerShell harness behaviour tests require NW_HARNESS_BEHAVIOR_TESTS=1 on Windows",
)


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_windows_harness_keeps_runtime_pins_in_existing_toolchain_manifest() -> None:
    sources = json.loads(read("tools/nw01/toolchain-sources.json"))
    manifest = json.loads(read("benchmarks/toolchain-manifest.v1.json"))
    module = read("tools/nw01/Nw01.Windows.psm1")

    # The manifest is the only authoritative version contract. Sources may
    # describe component *templates* but must not re-pin toolset/SDK versions.
    assert "vcvars_version" not in sources["visual_studio"]
    components = "\n".join(sources["visual_studio"]["components"])
    assert "{msvc_toolset_major_minor}" in components
    assert "{windows_sdk_build}" in components
    sources_text = read("tools/nw01/toolchain-sources.json")
    assert "14.40.33807" not in sources_text
    assert "Windows11SDK.26100" not in sources_text
    assert str(manifest["pinned_dependencies"]["windows_sdk_version"]) not in sources_text
    for key in ("cmake", "ninja", "llvm", "dxc"):
        assert len(sources[key]["sha256"]) == 64
    assert sources["visual_studio"]["bootstrapper_authenticode_subject_contains"]
    assert "bootstrapper_sha256" in sources["visual_studio"]

    identities = json.loads(read("tools/nw01/tool-version-identities.json"))
    assert identities["dxc"]["1.8.2407"]["reported_versions"]
    assert "1.8.2407.7" in identities["dxc"]["1.8.2407"]["reported_versions"]

    assert "benchmarks/toolchain-manifest.v1.json" in module
    assert "Get-NwVisualStudioSelection" in module
    assert "MsvcToolsetVersion" in module
    assert "does not match pinned MSVC toolset" in module
    assert "does not match pinned Windows SDK" in module
    assert str(manifest["pinned_dependencies"]["cmake_version"]) not in sources_text
    assert str(manifest["pinned_dependencies"]["ninja_version"]) not in sources_text


def test_visual_studio_component_templates_expand_to_manifest_pins() -> None:
    sources = json.loads(read("tools/nw01/toolchain-sources.json"))
    manifest = json.loads(read("benchmarks/toolchain-manifest.v1.json"))
    msvc = next(
        entry
        for entry in manifest["evaluated_toolchains"]
        if entry["toolchain_id"] == "msvc-ltcg"
    )
    toolset = ".".join(str(msvc["linker_version"]).split(".")[:3])
    major_minor = ".".join(toolset.split(".")[:2])
    sdk_build = str(manifest["pinned_dependencies"]["windows_sdk_version"]).split(".")[2]
    components = [
        component.replace("{msvc_toolset}", toolset)
        .replace("{msvc_toolset_major_minor}", major_minor)
        .replace("{windows_sdk_version}", manifest["pinned_dependencies"]["windows_sdk_version"])
        .replace("{windows_sdk_build}", sdk_build)
        for component in sources["visual_studio"]["components"]
    ]
    assert f"Microsoft.VisualStudio.Component.VC.{major_minor}.17.10.x86.x64" in components
    assert f"Microsoft.VisualStudio.Component.Windows11SDK.{sdk_build}" in components
    assert toolset == "14.40.33821"


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
    assert "Invoke-NwDownloadWithFallback" in bootstrap
    assert "Assert-NwAuthenticodeSignature" in bootstrap
    assert "Assert-NwExactToolVersion" in bootstrap
    assert "Invoke-NwDownloadWithFallback" in module
    assert "tool-version-identities.json" in module
    assert "Get-NwReportedToolVersions" in module
    assert "UnknownError" not in module
    assert "Assert-NwMsvcSelectedExecutables" in module
    assert "is outside required MSVC family" in module
    assert 'VcVarsVersion = $contract.MsvcToolsetMajorMinor' in module


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
    # The downstream gate must reject stale/foreign Full summaries even though
    # run-full itself now always writes one on every path.
    assert "wrapper_invocation_id" in ci
    assert "source_commit" in ci
    assert "tools/nw01/run-full.ps1" in ci
    assert "NW_HARNESS_BEHAVIOR_TESTS" in ci
    # Behavioural coverage is a required gate, not diagnostic output.
    assert "NW-01 Windows harness behaviour tests (required)" in ci
    behavior_step = ci[ci.index("NW-01 Windows harness behaviour tests (required)") :]
    assert "continue-on-error" not in behavior_step[:200]


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
    # No literal `exit 2` anywhere: exit 2 must only ever flow through the
    # preserved verifier exit code.
    assert re.search(r"(?m)^\s*exit\s+2\s*$", script) is None
    assert "Write-NwFullFailureSummary" in script
    assert "wrapper_invocation_id" in script


def test_hardware_capture_measures_host_fields_and_keeps_unreliable_inputs_explicit() -> None:
    script = read("tools/nw01/capture-hardware.ps1")

    assert "CurrentBuildNumber" in script and "UBR" in script
    assert "Get-CimInstance Win32_Processor" in script
    assert "nvidia-smi" in script
    assert "--query-gpu=index,name,driver_version,pci.bus_id" in script
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
    assert "adapter identity is ambiguous" in script
    assert "will remain unverified" in script


def test_clean_system_package_uses_local_git_sha_when_ci_sha_is_absent() -> None:
    package = read("tools/package-clean-system.ps1")
    wrapper = read("tools/nw01/prepare-clean-system.ps1")

    assert "$env:GITHUB_SHA" in package
    assert "git -C $RepoRoot rev-parse HEAD" in package
    assert "source_commit = $sourceCommit" in package
    assert "git-checkout-head" in package
    assert "does not match checked-out HEAD" in package
    assert "Full summary source_commit" in package
    assert "git -C $RepoRoot rev-parse HEAD" in wrapper
    assert "artifact-manifest.json" in wrapper
    assert "Compress-Archive" in wrapper
    assert "bootstrap-windows.ps1" not in wrapper
    assert "Refusing to overwrite existing CleanSystem output" in wrapper
    assert "nodewerk-evidence" in wrapper
    assert "expectedSource = $sha" in wrapper
    assert "does not match checked-out HEAD" in wrapper


def test_archive_and_git_preparation_keep_non_repo_artifacts_out_and_stage_by_allowlist() -> None:
    archive = read("tools/nw01/archive-artifacts.ps1")
    stage = read("tools/nw01/prepare-evidence-commit.ps1")

    assert "ArchiveRoot must be outside the Git working tree" in archive
    assert "archive-manifest.json" in archive
    assert "source_commit_origin" in archive
    assert "evidence_class" in archive
    assert "artifact-manifest.json" in archive
    assert "historical" in archive.lower()
    assert "[switch]$Replace" in archive
    assert "git add ." not in stage
    assert "git -C $root add -- $path" in stage
    assert "--amend" not in stage
    assert "push --force" not in stage
    assert "git push" not in stage
    assert "git -C $root commit -m $CommitMessage" in stage
    assert "Assert-NwNoHistoryOperationInProgress" in stage
    assert "MERGE_HEAD" in stage and "rebase-merge" in stage and "rebase-apply" in stage
    assert "both staged and unstaged changes" in stage
    assert "Assert-NwEvidenceContent" in stage
    assert "stale" in stage
    assert "benchmarks/hardware/rtx-4060-baseline\\.json" in stage
    assert "benchmarks/hardware/rtx-50-series-baseline\\.json" in stage
    assert "(hardware|clean-system|reproducibility|ledgers)" not in stage
    assert "build/" not in "\n".join(
        line for line in stage.splitlines() if line.strip().startswith("'")
    )
    assert "\\.zip" not in "\n".join(
        line for line in stage.splitlines() if line.strip().startswith("'")
    )


def test_evidence_summary_is_derived_from_validated_current_evidence() -> None:
    script = read("tools/nw01/evidence-summary.ps1")

    assert "benchmarks/toolchain-manifest.v1.json" in script
    assert "benchmarks/evidence-status.json" not in script
    assert "benchmarks/ledgers" in script
    assert "benchmarks/hardware/rtx-4060-baseline.json" in script
    assert "benchmarks/hardware/rtx-50-series-baseline.json" in script
    assert "benchmarks/clean-system/clean-system-validation.json" in script
    assert "stale" in script
    assert "NOT_EVALUATED" in script
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
    assert '$nvidiaSmiArgs = @(' in clean
    assert '"--query-gpu=name,driver_version"' in clean
    assert '"--format=csv,noheader,nounits"' in clean
    assert "$nvidiaSmiExitCode = $LASTEXITCODE" in clean

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


# ---------------------------------------------------------------------------
# Behavioural PowerShell tests. These actually execute the repository harness
# (via pwsh) instead of only text-matching source files. Profile- and
# OS-specific tests skip where the required host is unavailable.
# ---------------------------------------------------------------------------


def run_pwsh(arguments: list[str], *, extra_env: dict[str, str] | None = None, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    return subprocess.run(
        [PWSH_EXECUTABLE or "pwsh", "-NoProfile", "-NonInteractive", *arguments],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_pwsh_script(
    script: Path,
    *arguments: object,
    extra_env: dict[str, str] | None = None,
    timeout: int = 240,
) -> subprocess.CompletedProcess[str]:
    return run_pwsh(["-File", str(script), *[str(arg) for arg in arguments]], extra_env=extra_env, timeout=timeout)


def run_pwsh_command(command: str, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return run_pwsh(["-Command", command], extra_env=extra_env)


def ps_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def git_run(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {proc.stderr}")
    return proc


def init_git_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    git_run(repo, "init", "-q")
    git_run(repo, "config", "user.email", "nw-harness@example.invalid")
    git_run(repo, "config", "user.name", "NW Harness")
    return repo


def commit_all(repo: Path, message: str = "baseline") -> str:
    git_run(repo, "add", "-A")
    git_run(repo, "commit", "-q", "-m", message)
    return git_run(repo, "rev-parse", "HEAD").stdout.strip()


def build_archive_package(
    package: Path,
    *,
    source_commit: str,
    toolchain: str = "msvc-ltcg",
    full_baseline_passed: bool = True,
) -> dict:
    fixture_executables = {
        "empty_player": "nw_empty_player.exe",
        "clear_present": "nw_clear_present_fixture.exe",
        "compute_shader": "nw_compute_shader_fixture.exe",
    }
    deploy = package / "deploy"
    evidence = package / "evidence"
    ledger_dir = evidence / "ledgers"
    deploy.mkdir(parents=True, exist_ok=True)
    ledger_dir.mkdir(parents=True, exist_ok=True)

    files = []
    fixture_hashes: dict[str, tuple[str, str]] = {}
    for fixture, name in fixture_executables.items():
        payload = (f"fixture-{fixture}-{toolchain}-".encode("ascii") * 48)
        (deploy / name).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        files.append({"name": name, "bytes": len(payload), "sha256": digest})
        fixture_hashes[fixture] = (name, digest)

    manifest = {
        "schema_version": "1",
        "source_commit": source_commit,
        "toolchain": toolchain,
        "files": files,
    }
    (evidence / "artifact-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (evidence / "build-checks.json").write_text(
        json.dumps({"full_baseline_passed": full_baseline_passed}) + "\n",
        encoding="utf-8",
    )
    (evidence / "toolchain-manifest.v1.json").write_text(
        json.dumps({"selected_toolchain": {"toolchain_id": toolchain}}) + "\n",
        encoding="utf-8",
    )
    for fixture, (name, digest) in fixture_hashes.items():
        ledger = {
            "fixture_id": fixture,
            "toolchain_id": toolchain,
            "build_profile": "release",
            "verification_status": "verified",
            "distributed_components": [
                {"category": "executable", "name": name, "sha256": digest}
            ],
        }
        (ledger_dir / f"{fixture.replace('_', '-')}-{toolchain}-release.json").write_text(
            json.dumps(ledger) + "\n", encoding="utf-8"
        )
    return manifest


def build_archive_zip(zip_path: Path, package: Path, *, replace: dict[str, bytes] | None = None) -> None:
    import zipfile

    replacements = replace or {}
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path in sorted(package.rglob("*")):
            if not path.is_file():
                continue
            entry_name = path.relative_to(package).as_posix()
            archive.writestr(entry_name, replacements.get(entry_name, path.read_bytes()))


def write_fake_tool(path: Path, output: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        path.write_text("@echo off\r\n" + "echo " + output + "\r\n", encoding="ascii")
    else:
        path.write_text("#!/bin/sh\nprintf '%s\\n' " + ps_quote(output) + "\n", encoding="utf-8")
        path.chmod(0o755)


@requires_windows_harness
def test_dxc_exact_version_uses_pinned_release_identity_and_rejects_others(tmp_path: Path) -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    executable = tmp_path / ("dxc.cmd" if IS_WINDOWS else "dxc")

    def exact(output: str, expected: str) -> int:
        write_fake_tool(executable, output)
        command = (
            f"Import-Module {ps_quote(module)} -Force; "
            f"if (Test-NwExactVersion -Tool dxc -Executable {ps_quote(executable)} -Expected {ps_quote(expected)}) "
            "{ exit 0 } else { exit 1 }"
        )
        return run_pwsh_command(command).returncode

    # The real pinned distribution's banner carries the release build suffix.
    assert exact("dxcompiler.dll: 1.8 - 1.8.2407.7", "1.8.2407") == 0
    assert exact("dxcompiler.dll: 1.8 - 1.8.2407.12", "1.8.2407") == 0
    assert exact("dxcompiler.dll: 1.8 - 1.8.2407.13", "1.8.2407") == 1
    assert exact("dxcompiler.dll: 1.8.24070.12", "1.8.2407") == 1
    assert exact("dxcompiler.dll: 11.8.2407.12", "1.8.2407") == 1


@requires_windows_harness
def test_version_identity_mapping_is_required_and_exact(tmp_path: Path) -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    command = (
        f"Import-Module {ps_quote(module)} -Force; "
        "$versions = @(Get-NwReportedToolVersions -Tool dxc -Expected '1.8.2407'); "
        "[ordered]@{ Versions = $versions } | ConvertTo-Json -Compress"
    )
    proc = run_pwsh_command(command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    versions = json.loads(proc.stdout.strip().splitlines()[-1])["Versions"]
    assert "1.8.2407.7" in versions

    proc = run_pwsh_command(
        f"Import-Module {ps_quote(module)} -Force; "
        "Get-NwReportedToolVersions -Tool dxc -Expected '9.9.9999' | Out-Null"
    )
    assert proc.returncode != 0
    assert "exact identity" in (proc.stdout + proc.stderr)


@requires_windows_harness
def test_installed_dxc_banner_matches_repository_identity_mapping() -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    if not _exact_environment_available(module):
        pytest.skip("exact pinned Windows toolchain is not provisioned on this host")
    command = textwrap.dedent(
        f"""
        Import-Module {ps_quote(module)} -Force
        $contract = Get-NwToolchainContract
        $dxc = (Get-Command dxc.exe -ErrorAction Stop).Source
        $banner = (& $dxc --version 2>&1 | Out-String).Trim()
        [ordered]@{{
            Banner = $banner
            Exact = Test-NwExactVersion -Tool dxc -Executable $dxc -Expected $contract.DxcVersion
            Identities = @(Get-NwReportedToolVersions -Tool dxc -Expected $contract.DxcVersion)
        }} | ConvertTo-Json -Compress
        """
    )
    proc = run_pwsh_command(command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["Exact"] is True
    assert any(identity in data["Banner"] for identity in data["Identities"])


@requires_windows_harness
def test_authenticode_unknown_error_is_a_hard_failure(tmp_path: Path) -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    payload = tmp_path / "bootstrapper.exe"
    payload.write_bytes(b"not really signed")
    script = textwrap.dedent(
        f"""
        Import-Module {ps_quote(module)} -Force
        function global:Get-AuthenticodeSignature {{
            param([string]$FilePath)
            return [pscustomobject]@{{
                Status = 'UnknownError'
                SignerCertificate = [pscustomobject]@{{ Subject = 'CN=Microsoft Corporation' }}
            }}
        }}
        $threw = $false
        try {{
            Assert-NwAuthenticodeSignature -Path {ps_quote(payload)} -SubjectContains 'Microsoft'
        }} catch {{ $threw = $true }}
        if (-not $threw) {{ throw 'UnknownError signature status was accepted' }}
        Write-Output 'unknown-rejected'
        """
    )
    proc = run_pwsh_command(script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unknown-rejected" in proc.stdout

    valid_script = textwrap.dedent(
        f"""
        Import-Module {ps_quote(module)} -Force
        function global:Get-AuthenticodeSignature {{
            param([string]$FilePath)
            return [pscustomobject]@{{
                Status = 'Valid'
                SignerCertificate = [pscustomobject]@{{ Subject = 'CN=Microsoft Corporation' }}
            }}
        }}
        Assert-NwAuthenticodeSignature -Path {ps_quote(payload)} -SubjectContains 'Microsoft'
        Write-Output 'valid-accepted'
        """
    )
    proc = run_pwsh_command(valid_script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "valid-accepted" in proc.stdout


@requires_windows_harness
def test_exact_tool_detection_reuses_preinstalled_and_rejects_wrong_versions(tmp_path: Path) -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    executable = tmp_path / ("cmake.cmd" if IS_WINDOWS else "cmake")
    write_fake_tool(executable, "cmake version 3.30.2")
    command = (
        f"Import-Module {ps_quote(module)} -Force; "
        f"$exact = Find-NwExactCommand -Name 'nw-fake-cmake' -Tool cmake -Expected '3.30.2' -AdditionalCandidates @({ps_quote(executable)}); "
        f"$wrong = Find-NwExactCommand -Name 'nw-fake-cmake' -Tool cmake -Expected '9.9.9' -AdditionalCandidates @({ps_quote(executable)}); "
        "[ordered]@{ Exact = [string]$exact; Missing = [string]::IsNullOrWhiteSpace([string]$wrong) } | ConvertTo-Json -Compress"
    )
    proc = run_pwsh_command(command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(result["Exact"]) == executable
    assert result["Missing"] is True


@requires_windows_harness
def test_doctor_reports_expected_versus_detected_mismatch(tmp_path: Path) -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    executable = tmp_path / ("cmake.cmd" if IS_WINDOWS else "cmake")
    write_fake_tool(executable, "cmake version 3.29.0")
    command = (
        f"Import-Module {ps_quote(module)} -Force; "
        f"$row = Resolve-NwToolCandidate -Name 'nw-fake-cmake' -Tool cmake -Expected '3.30.2' -AdditionalCandidates @({ps_quote(executable)}); "
        "[ordered]@{ Expected = '3.30.2'; Detected = $row.Detected; Pass = [bool]$row.Exact } | ConvertTo-Json -Compress"
    )
    proc = run_pwsh_command(command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    row = json.loads(proc.stdout.strip().splitlines()[-1])
    assert row["Expected"] == "3.30.2"
    assert "3.29.0" in row["Detected"]
    assert row["Pass"] is False


@requires_windows_harness
def test_doctor_fails_when_activation_fails_even_if_all_rows_pass(tmp_path: Path) -> None:
    harness = tmp_path / "doctor-harness"
    harness.mkdir()
    shutil.copy(ROOT / "tools/nw01/doctor.ps1", harness / "doctor.ps1")
    (harness / "Nw01.Windows.psm1").write_text(
        textwrap.dedent(
            """
            function Enter-NwBuildEnvironment {
                param([string]$RepoRoot = "", [switch]$ExportGitHubEnv)
                throw "synthetic contract-level activation failure"
            }
            function Get-NwDoctorResults {
                param([string]$RepoRoot = "")
                @([pscustomobject]@{
                    Name = "CMake"
                    Expected = "3.30.2"
                    Detected = "3.30.2"
                    Pass = $true
                    Executable = "cmake.exe"
                })
            }
            Export-ModuleMember -Function *
            """
        ),
        encoding="utf-8",
    )
    proc = run_pwsh_script(harness / "doctor.ps1", "-RepoRoot", tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "could not activate" in (proc.stdout + proc.stderr).lower()


@requires_windows_harness
def test_msvc_selection_validates_active_paths_ownership_and_exact_versions(tmp_path: Path) -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    if not _exact_environment_available(module):
        pytest.skip("exact pinned Windows build environment is not provisioned on this host")

    # Exercise the real activated toolchain rather than trying to override
    # Get-Command from global scope (module command resolution has its own
    # session state). The verified host intentionally has VCToolsVersion
    # 14.40.33807 while cl/link report ...33821 file versions.
    exact_script = textwrap.dedent(
        f"""
        Import-Module {ps_quote(module)} -Force
        $entered = Enter-NwBuildEnvironment
        Assert-NwMsvcSelectedExecutables `
            -MsvcCompilerVersion $entered.Contract.MsvcCompilerVersion `
            -MsvcLinkerVersion $entered.Contract.MsvcLinkerVersion
        Write-Output 'msvc-exact'
        """
    )
    proc = run_pwsh_command(exact_script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "msvc-exact" in proc.stdout

    # Exact file-version validation must reject a nearby compiler version even
    # though the selected executable is owned by the correct VCToolsInstallDir.
    wrong_version_script = textwrap.dedent(
        f"""
        Import-Module {ps_quote(module)} -Force
        $entered = Enter-NwBuildEnvironment
        $threw = $false
        try {{
            Assert-NwMsvcSelectedExecutables `
                -MsvcCompilerVersion '19.40.33822' `
                -MsvcLinkerVersion $entered.Contract.MsvcLinkerVersion
        }} catch {{
            $threw = $true
            Write-Output $_.Exception.Message
        }}
        if (-not $threw) {{ throw 'wrong active compiler version was accepted' }}
        """
    )
    proc = run_pwsh_command(wrong_version_script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "expected exact 19.40.33822" in (proc.stdout + proc.stderr)

    # Ownership is independently fail-closed: changing VCToolsInstallDir to an
    # unrelated directory must make the same active cl/link unacceptable.
    fake_tools = tmp_path / "VC" / "Tools" / "MSVC" / "14.40.99999"
    fake_tools.mkdir(parents=True)
    wrong_owner_script = textwrap.dedent(
        f"""
        Import-Module {ps_quote(module)} -Force
        $entered = Enter-NwBuildEnvironment
        $env:VCToolsInstallDir = {ps_quote(fake_tools)}
        $threw = $false
        try {{
            Assert-NwMsvcSelectedExecutables `
                -MsvcCompilerVersion $entered.Contract.MsvcCompilerVersion `
                -MsvcLinkerVersion $entered.Contract.MsvcLinkerVersion
        }} catch {{
            $threw = $true
            Write-Output $_.Exception.Message
        }}
        if (-not $threw) {{ throw 'foreign VCToolsInstallDir ownership was accepted' }}
        """
    )
    proc = run_pwsh_command(wrong_owner_script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "is not owned by VCToolsInstallDir" in (proc.stdout + proc.stderr)


@requires_windows_harness
def test_archive_installation_validation_rejects_wrong_and_malformed_tools(tmp_path: Path) -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    wrong = tmp_path / ("dxc.cmd" if IS_WINDOWS else "dxc")
    write_fake_tool(wrong, "dxcompiler.dll: 1.8 - 1.8.2407.13")
    missing = tmp_path / "missing-dxc.exe"
    command = textwrap.dedent(
        f"""
        Import-Module {ps_quote(module)} -Force
        $wrongRejected = $false
        try {{ Assert-NwExactToolVersion -Tool dxc -Executable {ps_quote(wrong)} -Expected '1.8.2407' -Label 'DXC archive installed' }}
        catch {{ $wrongRejected = $true }}
        $missingRejected = $false
        try {{ Assert-NwExactToolVersion -Tool dxc -Executable {ps_quote(missing)} -Expected '1.8.2407' -Label 'DXC archive installed' }}
        catch {{ $missingRejected = $true }}
        if (-not $wrongRejected) {{ throw 'wrong version was accepted' }}
        if (-not $missingRejected) {{ throw 'missing executable was accepted' }}
        Write-Output 'archive-validation-ok'
        """
    )
    proc = run_pwsh_command(command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "archive-validation-ok" in proc.stdout


@requires_windows_harness
def test_visual_studio_selection_derives_versions_and_components_from_manifest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "benchmarks").mkdir(parents=True)
    (repo / "tools/nw01").mkdir(parents=True)
    shutil.copy(ROOT / "benchmarks/toolchain-manifest.v1.json", repo / "benchmarks/toolchain-manifest.v1.json")
    sources_path = repo / "tools/nw01/toolchain-sources.json"
    shutil.copy(ROOT / "tools/nw01/toolchain-sources.json", sources_path)

    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    command = (
        f"Import-Module {ps_quote(module)} -Force; "
        f"$selection = Get-NwVisualStudioSelection -RepoRoot {ps_quote(repo)}; "
        "[ordered]@{ VcVars = $selection.VcVarsVersion; Fallback = $selection.VcVarsFallbackVersion; Sdk = $selection.WindowsSdkVersion; "
        "Components = @($selection.Components) } | ConvertTo-Json -Depth 4 -Compress"
    )
    proc = run_pwsh_command(command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    selection = json.loads(proc.stdout.strip().splitlines()[-1])
    # vcvars selects the MSVC package family. Exactness is enforced by the
    # active cl.exe/link.exe file versions, not by the package-directory patch.
    assert selection["VcVars"] == "14.40"
    assert selection["Fallback"] == ""
    assert selection["Sdk"] == "10.0.26100.0"
    components = "\n".join(selection["Components"])
    assert "Microsoft.VisualStudio.Component.VC.14.40.17.10.x86.x64" in components
    assert "Microsoft.VisualStudio.Component.Windows11SDK.26100" in components

    # A divergent literal component must be rejected rather than overriding the
    # authoritative manifest.
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    sources["visual_studio"]["components"][1] = "Microsoft.VisualStudio.Component.VC.14.39.17.10.x86.x64"
    sources_path.write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
    proc = run_pwsh_command(command)
    assert proc.returncode != 0
    assert "does not match pinned MSVC toolset" in (proc.stdout + proc.stderr)


@requires_windows_harness
def test_download_retries_fallbacks_and_content_validation(tmp_path: Path) -> None:
    module = ps_quote(ROOT / "tools/nw01/Nw01.Windows.psm1")
    retry = "([pscustomobject]@{ max_attempts = 4; initial_delay_seconds = 0; backoff_multiplier = 2 })"
    output = ps_quote(tmp_path / "artifact.bin")

    retry_script = textwrap.dedent(
        f"""
        Import-Module {module} -Force
        $global:attempts = 0
        $global:fallbacks = 0
        $downloader = {{
            param([string]$TargetUri, [string]$TargetOutFile)
            $global:attempts++
            if ($global:attempts -le 2) {{ throw [System.Exception]::new('temporarily unavailable') }}
            Set-Content -Path $TargetOutFile -Value 'ok'
        }}
        $result = Invoke-NwDownloadWithFallback -Name 'test' -Uri 'https://example.invalid/a' -OutFile {output} -RetryPolicy {retry} -Downloader $downloader -FallbackProbe {{ $global:fallbacks++; 'unused' }}
        if ($result.Downloaded -ne $true -or $result.FallbackUsed -ne $false) {{ throw 'unexpected result' }}
        if ($global:attempts -ne 3) {{ throw "attempts=$($global:attempts)" }}
        if ($global:fallbacks -ne 0) {{ throw "fallbacks=$($global:fallbacks)" }}
        if (-not (Test-Path {output})) {{ throw 'download file missing' }}
        Write-Output 'retry-ok'
        """
    )
    proc = run_pwsh_command(retry_script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "retry-ok" in proc.stdout

    fallback_script = textwrap.dedent(
        f"""
        Import-Module {module} -Force
        $downloader = {{ param([string]$TargetUri, [string]$TargetOutFile) throw [System.Exception]::new('temporarily unavailable') }}
        $result = Invoke-NwDownloadWithFallback -Name 'test' -Uri 'https://example.invalid/b' -OutFile {output} -RetryPolicy {retry} -Downloader $downloader -FallbackProbe {{ 'exact-fallback' }}
        if ($result.FallbackUsed -ne $true -or $result.FallbackValue -ne 'exact-fallback') {{ throw 'fallback was not used' }}
        Write-Output 'fallback-ok'
        """
    )
    proc = run_pwsh_command(fallback_script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "fallback-ok" in proc.stdout

    no_fallback_script = textwrap.dedent(
        f"""
        Import-Module {module} -Force
        $downloader = {{ param([string]$TargetUri, [string]$TargetOutFile) throw [System.Exception]::new('temporarily unavailable') }}
        $threw = $false
        try {{
            Invoke-NwDownloadWithFallback -Name 'test' -Uri 'https://example.invalid/c' -OutFile {output} -RetryPolicy {retry} -Downloader $downloader -FallbackProbe {{ $null }} | Out-Null
        }} catch {{ $threw = $true }}
        if (-not $threw) {{ throw 'expected failure without exact fallback' }}
        Write-Output 'no-fallback-ok'
        """
    )
    proc = run_pwsh_command(no_fallback_script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no-fallback-ok" in proc.stdout

    non_transient_script = textwrap.dedent(
        f"""
        Import-Module {module} -Force
        $global:attempts = 0
        $global:fallbacks = 0
        $downloader = {{ param([string]$TargetUri, [string]$TargetOutFile) $global:attempts++; throw [System.Exception]::new('Forbidden') }}
        $threw = $false
        try {{
            Invoke-NwDownloadWithFallback -Name 'test' -Uri 'https://example.invalid/d' -OutFile {output} -RetryPolicy {retry} -Downloader $downloader -FallbackProbe {{ $global:fallbacks++; 'should-not-run' }} | Out-Null
        }} catch {{ $threw = $true }}
        if (-not $threw -or $global:attempts -ne 1 -or $global:fallbacks -ne 0) {{ throw "attempts=$($global:attempts) fallbacks=$($global:fallbacks)" }}
        Write-Output 'non-transient-ok'
        """
    )
    proc = run_pwsh_command(non_transient_script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "non-transient-ok" in proc.stdout

    content_script = textwrap.dedent(
        f"""
        Import-Module {module} -Force
        $path = {output}
        Set-Content -Path $path -Value 'payload'
        $hash = (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLowerInvariant()
        Assert-NwSha256 -Path $path -Expected $hash
        $threw = $false
        try {{ Assert-NwSha256 -Path $path -Expected ('0' * 64) }} catch {{ $threw = $true }}
        if (-not $threw) {{ throw 'checksum mismatch was accepted' }}
        Write-Output 'content-validation-ok'
        """
    )
    proc = run_pwsh_command(content_script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "content-validation-ok" in proc.stdout


def _exact_environment_available(module: Path) -> bool:
    command = (
        f"Import-Module {ps_quote(module)} -Force; "
        "try { Enter-NwBuildEnvironment | Out-Null; exit 0 } catch { Write-Output $_.Exception.Message; exit 1 }"
    )
    return run_pwsh_command(command).returncode == 0


@requires_windows_harness
def test_enter_build_environment_selects_owned_exact_cl_and_link(tmp_path: Path) -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    if not _exact_environment_available(module):
        pytest.skip("exact pinned Windows build environment is not provisioned on this host")
    command = textwrap.dedent(
        f"""
        Import-Module {ps_quote(module)} -Force
        $entered = Enter-NwBuildEnvironment
        $cl = (Get-Command cl.exe -ErrorAction Stop).Source
        $link = (Get-Command link.exe -ErrorAction Stop).Source
        [ordered]@{{
            Toolset = [string]$env:VCToolsVersion
            VCToolsInstallDir = [string]$env:VCToolsInstallDir
            Sdk = [string]$env:WindowsSDKVersion
            Cl = [string]$cl
            Link = [string]$link
            ClExact = Test-NwExactVersion -Tool cl -Executable $cl -Expected $entered.Contract.MsvcCompilerVersion
            LinkExact = Test-NwExactVersion -Tool link -Executable $link -Expected $entered.Contract.MsvcLinkerVersion
            ExpectedToolsetFamily = $entered.Contract.MsvcToolsetMajorMinor
            ExpectedSdk = $entered.Contract.WindowsSdkVersion
        }} | ConvertTo-Json -Compress
        """
    )
    proc = run_pwsh_command(command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    expected_tools = os.path.normcase(os.path.normpath(data["VCToolsInstallDir"].rstrip("\\/")))
    for key in ("Cl", "Link"):
        actual = os.path.normcase(os.path.normpath(data[key]))
        assert actual == expected_tools or actual.startswith(expected_tools + os.sep)
    assert data["ClExact"] is True
    assert data["LinkExact"] is True
    toolset_parts = data["Toolset"].rstrip("\\/").split(".")
    assert ".".join(toolset_parts[:2]) == data["ExpectedToolsetFamily"]
    # Visual Studio's package directory patch is not required to equal the
    # cl/link file-version patch. The exact binary checks above are authoritative.
    assert data["Sdk"].rstrip("\\/") == data["ExpectedSdk"]


@requires_windows_harness
def test_bootstrap_reuses_already_correct_installations(tmp_path: Path) -> None:
    module = ROOT / "tools/nw01/Nw01.Windows.psm1"
    if not _exact_environment_available(module):
        pytest.skip("exact pinned Windows toolchain is not provisioned on this host")
    provenance = tmp_path / "bootstrap-provenance.json"
    proc = run_pwsh_script(
        ROOT / "tools/nw01/bootstrap-windows.ps1",
        "-ProvenancePath",
        provenance,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(provenance.read_text(encoding="utf-8-sig"))
    tools = data["tools"]
    for name in ("CMake", "Ninja", "LLVM clang-cl/lld-link", "DXC", "MSVC/Windows SDK"):
        assert name in tools
        assert tools[name]["source"] == "preinstalled-exact", tools[name]
        assert tools[name]["executable"]


@requires_windows_harness
def test_run_full_always_writes_fresh_failure_summary(tmp_path: Path) -> None:
    summary = tmp_path / "nw01-full-mode.json"
    summary.write_text(
        json.dumps({"full_baseline_passed": True, "wrapper_invocation_id": "stale"}),
        encoding="utf-8",
    )
    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()

    proc = run_pwsh_script(
        ROOT / "tools/nw01/run-full.ps1",
        "-RepoRoot",
        empty_repo,
        "-SummaryPath",
        summary,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    data = json.loads(summary.read_text(encoding="utf-8-sig"))
    assert data["full_baseline_passed"] is False
    assert data["gate_status"] == "wrapper_failure"
    assert data["wrapper_invocation_id"] != "stale"
    assert data["wrapper_error"]["stage"] == "wrapper"


@requires_windows_harness
@pytest.mark.skipif(not IS_WINDOWS, reason="Full wrapper is Windows-only")
def test_run_full_preserves_all_verifier_exit_codes_and_exports_fresh_summary(tmp_path: Path) -> None:
    harness = tmp_path / "harness"
    harness.mkdir()
    shutil.copy(ROOT / "tools/nw01/run-full.ps1", harness / "run-full.ps1")
    (harness / "Nw01.Windows.psm1").write_text(
        textwrap.dedent(
            """
            function Enter-NwBuildEnvironment {
                param([string]$RepoRoot = "", [switch]$ExportGitHubEnv)
                return [pscustomobject]@{ Contract = [pscustomobject]@{ SelectedToolchain = "test" } }
            }
            Export-ModuleMember -Function *
            """
        ),
        encoding="utf-8",
    )
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / "tools").mkdir(parents=True)
    (fake_repo / "tools/verify_nw01.py").write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys

            code = int(os.environ.get("NW_TEST_VERIFIER_EXIT", "0"))
            summary_path = sys.argv[sys.argv.index("--summary-json") + 1]
            with open(summary_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "mode": "Full",
                        "source_commit": "a" * 40,
                        "full_baseline_passed": code == 0,
                        "gate_status": "fake",
                        "checks": {},
                        "concrete_failures": [],
                    },
                    handle,
                )
            if os.environ.get("NW_TEST_VERIFIER_SKIP_SUMMARY") == "1":
                os.remove(summary_path)
            sys.exit(code)
            """
        ),
        encoding="utf-8",
    )

    for expected_code in (0, 1, 2, 3):
        summary = tmp_path / f"summary-{expected_code}.json"
        github_env = tmp_path / f"github-env-{expected_code}"
        github_env.write_text("", encoding="utf-8")
        proc = run_pwsh_script(
            harness / "run-full.ps1",
            "-RepoRoot",
            fake_repo,
            "-SummaryPath",
            summary,
            "-ExportGitHubEnv",
            extra_env={
                "NW_TEST_VERIFIER_EXIT": str(expected_code),
                "GITHUB_ENV": str(github_env),
            },
        )
        assert proc.returncode == expected_code, proc.stdout + proc.stderr
        data = json.loads(summary.read_text(encoding="utf-8-sig"))
        assert data["wrapper_invocation_id"]
        assert f"NW_FULL_SUMMARY={summary}" in github_env.read_text(encoding="utf-8")
        if expected_code == 2:
            # Exit 2 must never be advertised as a passing Full result.
            assert data["full_baseline_passed"] is False

    for expected_code in (0, 1, 2, 3):
        crash_summary = tmp_path / f"summary-crash-{expected_code}.json"
        proc = run_pwsh_script(
            harness / "run-full.ps1",
            "-RepoRoot",
            fake_repo,
            "-SummaryPath",
            crash_summary,
            extra_env={
                "NW_TEST_VERIFIER_EXIT": str(expected_code),
                "NW_TEST_VERIFIER_SKIP_SUMMARY": "1",
            },
        )
        assert proc.returncode == expected_code, proc.stdout + proc.stderr
        crash = json.loads(crash_summary.read_text(encoding="utf-8-sig"))
        assert crash["full_baseline_passed"] is False
        assert crash["wrapper_error"]["stage"] == "wrapper"


@requires_windows_harness
def test_prepare_evidence_commit_guardrails_and_optional_new_commit(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo with spaces")
    shutil.copytree(ROOT / "benchmarks", repo / "benchmarks")
    shutil.copytree(ROOT / "shaders", repo / "shaders")
    shutil.copytree(ROOT / "fixtures/compute_shader", repo / "fixtures/compute_shader")
    evidence = repo / "benchmarks/hardware/rtx-4060-baseline.json"
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    commit_all(repo)
    script = ROOT / "tools/nw01/prepare-evidence-commit.ps1"
    relative_evidence = "benchmarks/hardware/rtx-4060-baseline.json"

    def mutate_hardware(value: str) -> None:
        data = json.loads(evidence.read_text(encoding="utf-8"))
        data["benchmark_id"] = f"nw01-hardware-rtx-4060-desktop-baseline-{value}"
        evidence.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no evidence changes" in (proc.stdout + proc.stderr)

    (repo / "notes.txt").write_text("unexpected\n", encoding="utf-8")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert "unexpected paths" in (proc.stdout + proc.stderr)
    (repo / "notes.txt").unlink()

    (repo / "artifact.zip").write_bytes(b"zip")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert "unexpected paths" in (proc.stdout + proc.stderr)
    (repo / "artifact.zip").unlink()

    # An unexpected candidate inside an evidence directory is still rejected.
    (repo / "benchmarks/hardware/credentials.json").write_text('{"secret": 1}\n', encoding="utf-8")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert "unexpected paths" in (proc.stdout + proc.stderr)
    (repo / "benchmarks/hardware/credentials.json").unlink()

    # AM (staged add + unstaged modification) must be rejected rather than
    # silently replacing the pre-staged content.
    mutate_hardware("staged")
    git_run(repo, "add", "--", relative_evidence)
    mutate_hardware("unstaged")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert "both staged and unstaged changes" in (proc.stdout + proc.stderr)
    git_run(repo, "reset", "-q", "--", relative_evidence)
    git_run(repo, "checkout", "--", relative_evidence)

    # Stale provenance is rejected before the index changes.
    data = json.loads(evidence.read_text(encoding="utf-8"))
    data["artifact_provenance"]["artifact_sha256"]["clear_present"] = "0" * 64
    evidence.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert "stale" in (proc.stdout + proc.stderr)
    assert git_run(repo, "diff", "--cached", "--name-only").stdout.strip() == ""
    git_run(repo, "checkout", "--", relative_evidence)

    # An internally consistent but stale reproducibility report is rejected
    # unless its hashes still match the verified release ledgers.
    relative_reproducibility = "benchmarks/reproducibility/msvc-ltcg-release-reproducibility.json"
    reproducibility = repo / relative_reproducibility
    repro_data = json.loads(reproducibility.read_text(encoding="utf-8"))
    repro_data["artifacts"][0]["sha256_a"] = "1" * 64
    repro_data["artifacts"][0]["sha256_b"] = "1" * 64
    reproducibility.write_text(json.dumps(repro_data, indent=2) + "\n", encoding="utf-8")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert "stale" in (proc.stdout + proc.stderr)
    assert git_run(repo, "diff", "--cached", "--name-only").stdout.strip() == ""
    git_run(repo, "checkout", "--", relative_reproducibility)

    # Evidence status must reference current, validated evidence rather than a
    # stale but syntactically valid list.
    relative_status = "benchmarks/evidence-status.json"
    status_path = repo / relative_status
    status_data = json.loads(status_path.read_text(encoding="utf-8"))
    status_data["required_measured_evidence"][0]["files"] = "benchmarks/ledgers/missing.json"
    status_path.write_text(json.dumps(status_data, indent=2) + "\n", encoding="utf-8")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert "references no current files" in (proc.stdout + proc.stderr)
    assert git_run(repo, "diff", "--cached", "--name-only").stdout.strip() == ""
    git_run(repo, "checkout", "--", relative_status)

    status_data = json.loads(status_path.read_text(encoding="utf-8"))
    status_data["measured_minimum_windows_build"] = "10.0.99999.0"
    status_path.write_text(json.dumps(status_data, indent=2) + "\n", encoding="utf-8")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert "does not match current measured" in (proc.stdout + proc.stderr)
    assert git_run(repo, "diff", "--cached", "--name-only").stdout.strip() == ""
    git_run(repo, "checkout", "--", relative_status)

    # Malformed content is rejected before staging.
    evidence.write_text("{not-json\n", encoding="utf-8")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert (
        "Unable to parse JSON" in (proc.stdout + proc.stderr)
        or "Schema validation failed" in (proc.stdout + proc.stderr)
    )
    assert git_run(repo, "diff", "--cached", "--name-only").stdout.strip() == ""
    git_run(repo, "checkout", "--", relative_evidence)

    # An in-progress merge must never turn -Commit into a merge commit.
    git_dir = git_run(repo, "rev-parse", "--git-dir").stdout.strip()
    git_dir_path = Path(git_dir) if Path(git_dir).is_absolute() else repo / git_dir
    merge_head = git_dir_path / "MERGE_HEAD"
    merge_head.write_text("0" * 40 + "\n", encoding="utf-8")
    mutate_hardware("merge")
    proc = run_pwsh_script(script, "-RepoRoot", repo, "-Commit")
    assert proc.returncode != 0
    assert "MERGE_HEAD" in (proc.stdout + proc.stderr)
    assert git_run(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    merge_head.unlink()

    proc = run_pwsh_script(script, "-RepoRoot", repo, "-Commit")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "created new commit" in (proc.stdout + proc.stderr)
    assert git_run(repo, "rev-list", "--count", "HEAD").stdout.strip() == "2"
    assert (
        git_run(repo, "log", "-1", "--pretty=%s").stdout.strip()
        == "NW-01: record verified physical evidence"
    )


@requires_windows_harness
def test_clean_system_package_records_checkout_provenance_and_rejects_stale_inputs(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "package repo")
    for subdir in ("benchmarks", "shaders"):
        shutil.copytree(ROOT / subdir, repo / subdir)
    deploy = repo / "build/msvc-release-ltcg-clean-a/deploy"
    deploy.mkdir(parents=True)
    executables = {
        "nw_empty_player.exe": b"empty",
        "nw_clear_present_fixture.exe": b"clear",
        "nw_compute_shader_fixture.exe": b"compute",
    }
    for name, payload in executables.items():
        (deploy / name).write_bytes(payload)
    # Rewrite the copied MSVC release ledgers to match the fake artifacts so the
    # package can be validated against verified ledger hashes.
    fixtures = {
        "nw_empty_player.exe": ("empty-player-msvc-ltcg-release.json", "empty_player"),
        "nw_clear_present_fixture.exe": ("clear-present-msvc-ltcg-release.json", "clear_present"),
        "nw_compute_shader_fixture.exe": ("compute-fixture-msvc-ltcg-release.json", "compute_shader"),
    }
    ledger_hashes = {}
    for name, (ledger_name, fixture_id) in fixtures.items():
        ledger_path = repo / "benchmarks/ledgers" / ledger_name
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        payload = executables[name]
        digest = hashlib.sha256(payload).hexdigest()
        ledger["distributed_components"][0]["sha256"] = digest
        ledger["distributed_components"][0]["size_bytes"] = len(payload)
        ledger["total_distributed_bytes"] = len(payload)
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
        ledger_hashes[fixture_id] = digest
    head = commit_all(repo)

    summary = tmp_path / "full-summary.json"
    summary.write_text(
        json.dumps(
            {
                "mode": "Full",
                "source_commit": head,
                "checks": {
                    "size_ledgers": {
                        "passed": True,
                        "recorded": {
                            "msvc-ltcg": {
                                "toolchain_id": "msvc-ltcg",
                                "verification_status": "verified",
                                "recorded_fixtures": {
                                    fixture_id: {
                                        "verification_status": "verified",
                                        "sha256": digest,
                                    }
                                    for fixture_id, digest in ledger_hashes.items()
                                },
                            }
                        },
                    },
                    "fixture_validation": {"passed": True},
                    "msvc_fixture_validation": {"passed": True},
                },
            }
        ),
        encoding="utf-8",
    )
    script = ROOT / "tools/package-clean-system.ps1"
    output = tmp_path / "output"
    proc = run_pwsh_script(
        script,
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
        "-OutputRoot",
        output,
        extra_env={"GITHUB_SHA": ""},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    manifest = json.loads((output / "evidence/artifact-manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["source_commit"] == head
    assert manifest["source_revision_origin"] == "git-checkout-head"
    assert {entry["name"] for entry in manifest["files"]} == set(executables)
    assert (output / "evidence/shaders-manifest.json").is_file()
    assert (output / "evidence/toolchain-manifest.v1.json").is_file()
    assert list((output / "evidence/ledgers").glob("*.json"))
    assert list((output / "evidence/reproducibility").glob("*.json"))
    for entry in manifest["files"]:
        payload = (output / "deploy" / entry["name"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["bytes"] == len(payload)

    # Replacing a binary after a successful Full run at the same HEAD must not
    # produce a supposedly verified package.
    (deploy / "nw_clear_present_fixture.exe").write_bytes(b"tampered after full run")
    tampered_output = tmp_path / "tampered-output"
    proc = run_pwsh_script(
        script,
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
        "-OutputRoot",
        tampered_output,
        extra_env={"GITHUB_SHA": ""},
    )
    assert proc.returncode != 0
    normalized_error = " ".join(
        re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout + proc.stderr).split()
    )
    assert "does not match verified msvc-ltcg" in normalized_error
    assert "release-ledger hash" in normalized_error
    assert not (tampered_output / "evidence/artifact-manifest.json").exists()
    (deploy / "nw_clear_present_fixture.exe").write_bytes(executables["nw_clear_present_fixture.exe"])

    stale_sha_output = tmp_path / "stale-sha-output"
    proc = run_pwsh_script(
        script,
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
        "-OutputRoot",
        stale_sha_output,
        extra_env={"GITHUB_SHA": "b" * 40},
    )
    assert proc.returncode != 0
    assert "does not match checked-out HEAD" in (proc.stdout + proc.stderr)
    assert not (stale_sha_output / "evidence/artifact-manifest.json").exists()

    stale_summary = tmp_path / "stale-summary.json"
    stale_summary.write_text(
        json.dumps(
            {
                "mode": "Full",
                "source_commit": "c" * 40,
                "checks": {
                    "size_ledgers": {"passed": True},
                    "fixture_validation": {"passed": True},
                    "msvc_fixture_validation": {"passed": True},
                },
            }
        ),
        encoding="utf-8",
    )
    stale_summary_output = tmp_path / "stale-summary-output"
    proc = run_pwsh_script(
        script,
        "-RepoRoot",
        repo,
        "-FullSummary",
        stale_summary,
        "-OutputRoot",
        stale_summary_output,
        extra_env={"GITHUB_SHA": ""},
    )
    assert proc.returncode != 0
    assert "Full summary source_commit" in (proc.stdout + proc.stderr)


@requires_windows_harness
def test_archive_uses_artifact_source_revision_and_preserves_prior_evidence(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "archive repo")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    head = commit_all(repo)

    archived_commit = "a" * 40
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps({"source_commit": archived_commit, "full_baseline_passed": True}),
        encoding="utf-8",
    )
    package = tmp_path / "package"
    build_archive_package(package, source_commit=archived_commit)

    archive_root = tmp_path / "archive"
    script = ROOT / "tools/nw01/archive-artifacts.ps1"
    for _ in range(2):
        proc = run_pwsh_script(
            script,
            "-ArchiveRoot",
            archive_root,
            "-RepoRoot",
            repo,
            "-FullSummary",
            summary,
            "-CleanSystemPackage",
            package,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    archives = [path for path in archive_root.iterdir() if path.is_dir()]
    assert len(archives) == 2
    for directory in archives:
        manifest = json.loads((directory / "archive-manifest.json").read_text(encoding="utf-8-sig"))
        assert manifest["source_commit"] == archived_commit
        assert manifest["checkout_head"] == head
        assert manifest["evidence_class"] == "historical"
        assert manifest["certifying"] is False
        assert manifest["certification_problems"] == []
        assert directory.name.startswith(archived_commit[:12])

    conflicting_summary = tmp_path / "conflicting.json"
    conflicting_summary.write_text(
        json.dumps({"source_commit": "b" * 40, "full_baseline_passed": True}),
        encoding="utf-8",
    )
    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        archive_root,
        "-RepoRoot",
        repo,
        "-FullSummary",
        conflicting_summary,
        "-CleanSystemPackage",
        package,
    )
    assert proc.returncode != 0
    assert "conflicting source revisions" in (proc.stdout + proc.stderr)

    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        repo / "inside",
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
    )
    assert proc.returncode != 0
    assert "outside the Git working tree" in (proc.stdout + proc.stderr)


@requires_windows_harness
def test_archive_rejects_missing_and_tampered_package_deploy_files(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "archive package repo")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    head = commit_all(repo)
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps({"source_commit": head, "full_baseline_passed": True}),
        encoding="utf-8",
    )
    script = ROOT / "tools/nw01/archive-artifacts.ps1"

    tampered_package = tmp_path / "tampered-package"
    build_archive_package(tampered_package, source_commit=head)
    executable = tampered_package / "deploy/nw_empty_player.exe"
    payload = executable.read_bytes()
    executable.write_bytes(bytes([payload[0] ^ 0xFF]) + payload[1:])
    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        tmp_path / "tampered-archive",
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
        "-CleanSystemPackage",
        tampered_package,
    )
    assert proc.returncode != 0
    assert "does not match recorded" in (proc.stdout + proc.stderr).lower()

    missing_package = tmp_path / "missing-package"
    build_archive_package(missing_package, source_commit=head)
    (missing_package / "deploy/nw_compute_shader_fixture.exe").unlink()
    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        tmp_path / "missing-archive",
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
        "-CleanSystemPackage",
        missing_package,
    )
    assert proc.returncode != 0
    assert "missing deploy executable" in (proc.stdout + proc.stderr).lower()


@requires_windows_harness
def test_archive_classifies_current_incomplete_full_as_non_certifying(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "archive classification repo")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    head = commit_all(repo)
    script = ROOT / "tools/nw01/archive-artifacts.ps1"

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(
        json.dumps({"source_commit": head, "full_baseline_passed": False}),
        encoding="utf-8",
    )
    incomplete_root = tmp_path / "incomplete-archive"
    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        incomplete_root,
        "-RepoRoot",
        repo,
        "-FullSummary",
        incomplete,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    directory = next(path for path in incomplete_root.iterdir() if path.is_dir())
    manifest = json.loads((directory / "archive-manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["evidence_class"] == "current-non-certifying"
    assert manifest["certifying"] is False
    assert any("full baseline" in problem for problem in manifest["certification_problems"])

    complete = tmp_path / "complete.json"
    complete.write_text(
        json.dumps({"source_commit": head, "full_baseline_passed": True}),
        encoding="utf-8",
    )
    complete_root = tmp_path / "complete-archive"
    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        complete_root,
        "-RepoRoot",
        repo,
        "-FullSummary",
        complete,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    directory = next(path for path in complete_root.iterdir() if path.is_dir())
    manifest = json.loads((directory / "archive-manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["evidence_class"] == "current-certifying"
    assert manifest["certifying"] is True


@requires_windows_harness
def test_archive_validates_clean_system_zip_provenance(tmp_path: Path) -> None:
    import zipfile

    repo = init_git_repo(tmp_path / "archive zip repo")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    head = commit_all(repo)
    archived_commit = "a" * 40

    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps({"source_commit": archived_commit, "full_baseline_passed": True}),
        encoding="utf-8",
    )
    package = tmp_path / "package"
    build_archive_package(package, source_commit=archived_commit)

    matching_zip = tmp_path / "matching.zip"
    build_archive_zip(matching_zip, package)
    archive_root = tmp_path / "archive"
    script = ROOT / "tools/nw01/archive-artifacts.ps1"
    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        archive_root,
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
        "-CleanSystemPackage",
        package,
        "-CleanSystemZip",
        matching_zip,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    directory = next(path for path in archive_root.iterdir() if path.is_dir())
    archive_manifest = json.loads((directory / "archive-manifest.json").read_text(encoding="utf-8-sig"))
    assert {entry["artifact"] for entry in archive_manifest["source_revisions"]} == {
        "full-summary",
        "clean-system-package",
        "clean-system-zip",
    }
    assert archive_manifest["certification_problems"] == []

    conflicting_zip = tmp_path / "conflicting.zip"
    conflicting_package = tmp_path / "conflicting-package"
    shutil.copytree(package, conflicting_package)
    conflicting_manifest_path = conflicting_package / "evidence/artifact-manifest.json"
    conflicting_manifest = json.loads(conflicting_manifest_path.read_text(encoding="utf-8"))
    conflicting_manifest["source_commit"] = "b" * 40
    conflicting_manifest_path.write_text(json.dumps(conflicting_manifest), encoding="utf-8")
    build_archive_zip(conflicting_zip, conflicting_package)
    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        archive_root,
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
        "-CleanSystemPackage",
        package,
        "-CleanSystemZip",
        conflicting_zip,
    )
    assert proc.returncode != 0
    assert "conflicting source revisions" in (proc.stdout + proc.stderr)

    tampered_payload = bytearray((package / "deploy/nw_empty_player.exe").read_bytes())
    tampered_payload[0] ^= 0xFF
    tampered_zip = tmp_path / "tampered.zip"
    build_archive_zip(
        tampered_zip,
        package,
        replace={"deploy/nw_empty_player.exe": bytes(tampered_payload)},
    )
    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        archive_root,
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
        "-CleanSystemPackage",
        package,
        "-CleanSystemZip",
        tampered_zip,
    )
    assert proc.returncode != 0
    assert "hash" in (proc.stdout + proc.stderr).lower()

    malformed_zip = tmp_path / "no-manifest.zip"
    with zipfile.ZipFile(malformed_zip, "w") as archive:
        archive.writestr("deploy/nw_empty_player.exe", b"placeholder")
    before = {path.name for path in archive_root.iterdir() if path.is_dir()}
    proc = run_pwsh_script(
        script,
        "-ArchiveRoot",
        archive_root,
        "-RepoRoot",
        repo,
        "-FullSummary",
        summary,
        "-CleanSystemZip",
        malformed_zip,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    directory = next(
        path for path in archive_root.iterdir() if path.is_dir() and path.name not in before
    )
    archive_manifest = json.loads((directory / "archive-manifest.json").read_text(encoding="utf-8-sig"))
    assert "clean-system-zip" in archive_manifest["unverified_artifacts"]
    assert archive_manifest["evidence_class"] != "current-certifying"


@requires_windows_harness
def test_evidence_summary_distinguishes_current_and_historical_full_results(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "summary repo")
    shutil.copytree(ROOT / "benchmarks", repo / "benchmarks")
    head = commit_all(repo)
    script = ROOT / "tools/nw01/evidence-summary.ps1"

    # Without a supplied Full result the baseline is explicitly not evaluated,
    # while current file-level evidence is still validated against the ledgers.
    no_summary_output = tmp_path / "no-summary-status.json"
    proc = run_pwsh_script(script, "-RepoRoot", repo, "-JsonPath", no_summary_output)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(no_summary_output.read_text(encoding="utf-8-sig"))
    assert data["full_baseline"] == "NOT_EVALUATED"
    assert data["rtx_40_representative_hardware"] == "VERIFIED"
    assert data["rtx_50_representative_hardware"] == "VERIFIED"
    assert data["clean_system"] == "VERIFIED"

    current = tmp_path / "current.json"
    current.write_text(
        json.dumps(
            {
                "mode": "Full",
                "source_commit": head,
                "full_baseline_passed": False,
                "concrete_failures": [],
                "missing_build_coverage": [],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "status.json"
    proc = run_pwsh_script(script, "-RepoRoot", repo, "-FullSummary", current, "-JsonPath", output)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(output.read_text(encoding="utf-8-sig"))
    assert data["selected_toolchain"] == "msvc-ltcg"
    assert data["concrete_checks"] == "PASS"
    assert data["full_baseline"] == "INCOMPLETE"
    assert data["full_summary_matches_checkout"] is True

    historical = tmp_path / "historical.json"
    historical.write_text(
        json.dumps(
            {
                "mode": "Full",
                "source_commit": "d" * 40,
                "full_baseline_passed": True,
                "concrete_failures": [],
                "missing_build_coverage": [],
            }
        ),
        encoding="utf-8",
    )
    historical_output = tmp_path / "historical-status.json"
    proc = run_pwsh_script(
        script,
        "-RepoRoot",
        repo,
        "-FullSummary",
        historical,
        "-JsonPath",
        historical_output,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(historical_output.read_text(encoding="utf-8-sig"))
    assert data["full_baseline"] == "HISTORICAL"
    assert data["full_summary_matches_checkout"] is False

    # A successful Full summary does not have to carry a missing-coverage list.
    verified = tmp_path / "verified.json"
    verified.write_text(
        json.dumps(
            {
                "mode": "Full",
                "source_commit": head,
                "full_baseline_passed": True,
                "concrete_failures": [],
            }
        ),
        encoding="utf-8",
    )
    verified_output = tmp_path / "verified-status.json"
    proc = run_pwsh_script(
        script,
        "-RepoRoot",
        repo,
        "-FullSummary",
        verified,
        "-JsonPath",
        verified_output,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(verified_output.read_text(encoding="utf-8-sig"))
    assert data["full_baseline"] == "VERIFIED"
    assert data["concrete_checks"] == "PASS"

    # An executable/ledger change invalidates the physical captures even when a
    # current exit-2 Full summary is supplied. The summary must not keep
    # advertising stale hardware or CleanSystem evidence as verified.
    ledger_path = repo / "benchmarks/ledgers/clear-present-msvc-ltcg-release.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["distributed_components"][0]["sha256"] = "f" * 64
    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    stale_output = tmp_path / "stale-status.json"
    proc = run_pwsh_script(script, "-RepoRoot", repo, "-FullSummary", current, "-JsonPath", stale_output)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(stale_output.read_text(encoding="utf-8-sig"))
    assert data["full_baseline"] == "INCOMPLETE"
    assert data["rtx_40_representative_hardware"] == "PENDING"
    assert data["rtx_50_representative_hardware"] == "PENDING"
    assert data["clean_system"] == "PENDING"
    assert "stale" in data["rtx_40_representative_hardware_reason"]


@requires_windows_harness
def test_clean_system_runner_is_target_only_and_fails_without_package(tmp_path: Path) -> None:
    proc = run_pwsh_script(
        ROOT / "tools/nw01/run-clean-system.ps1",
        "-PackageRoot",
        tmp_path / "package",
    )
    assert proc.returncode == 1
    assert "deploy directory is missing" in (proc.stdout + proc.stderr).lower()


def test_capture_benchmark_rejects_adapter_conflicts_and_missing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    import verify_nw01 as verify_module

    repo = tmp_path / "repo"
    for subdir in ("benchmarks", "schemas", "fixtures", "shaders"):
        shutil.copytree(ROOT / subdir, repo / subdir)
    binary_dir = tmp_path / "deploy"
    binary_dir.mkdir()
    for name in ("nw_clear_present_fixture.exe", "nw_compute_shader_fixture.exe"):
        (binary_dir / name).write_bytes(b"MZ" + name.encode("ascii") + b"\0" * 1024)

    def fixture_runs(adapters: dict[str, str]) -> list[dict[str, object]]:
        runs: list[dict[str, object]] = []
        for fixture_id in ("clear_present", "compute_shader"):
            stdout = ""
            if fixture_id in adapters:
                stdout += f"NW_ADAPTER_DESCRIPTION={adapters[fixture_id]}\n"
            run: dict[str, object] = {
                "fixture_id": fixture_id,
                "execution_status": "passed",
                "exit_code": 0,
                "metrics": {},
                "d3d12_validation_clean": True,
                "stdout": stdout,
            }
            if fixture_id == "compute_shader":
                run["readback"] = {
                    "observed_output_sha256": "a" * 64,
                    "elements_match": True,
                    "hash_match": True,
                }
            runs.append(run)
        return runs

    measured = {
        "os_build": "10.0.26100.1000",
        "nvidia_driver_version": "610.88",
        "cpu_model": "AMD Ryzen 7 9800X3D",
        "gpu_architecture": "Blackwell",
        "wddm_driver_version": "32.0.16.1088",
        "windows_power_plan": "Balanced",
        "nvidia_power_management_mode": "Prefer maximum performance",
        "pcie_power_management": "Off",
        "refresh_hz": "240",
        "driver_shader_cache_state": "warm_primed",
        "os_file_cache_state": "warm",
    }

    def capture(runs: list[dict[str, object]], environment: dict[str, str] | None = None) -> dict:
        monkeypatch.setattr(
            verify_module,
            "execute_release_fixture_validation",
            lambda *_args, **_kwargs: {"passed": True, "fixture_runs": runs},
        )
        return verify_module.capture_benchmark_result(
            repo,
            binary_dir,
            hardware_tier="rtx_50_series_desktop",
            system_environment=environment or {},
        )

    same_adapter = {
        "clear_present": "NVIDIA GeForce RTX 5070 Ti",
        "compute_shader": "NVIDIA GeForce RTX 5070 Ti",
    }
    result = capture(fixture_runs(same_adapter), measured)
    assert result["environment_metadata"]["gpu_model"] == "NVIDIA GeForce RTX 5070 Ti"

    conflicting = capture(
        fixture_runs(
            {
                "clear_present": "NVIDIA GeForce RTX 5070 Ti",
                "compute_shader": "NVIDIA GeForce RTX 4060 Ti",
            }
        ),
        measured,
    )
    reasons = [
        item["reason"]
        for item in conflicting["unavailable_measurements"]
        if item["metric"] == "environment_metadata.gpu_model"
    ]
    assert any("same executing adapter" in reason for reason in reasons)
    assert conflicting["benchmark_passed"] is False

    missing = capture(fixture_runs({}), measured)
    reasons = [
        item["reason"]
        for item in missing["unavailable_measurements"]
        if item["metric"] == "environment_metadata.gpu_model"
    ]
    assert any("Adapter description was not emitted" in reason for reason in reasons)
    assert missing["benchmark_passed"] is False

    partial = capture(
        fixture_runs({"clear_present": "NVIDIA GeForce RTX 5070 Ti"}),
        measured,
    )
    reasons = [
        item["reason"]
        for item in partial["unavailable_measurements"]
        if item["metric"] == "environment_metadata.gpu_model"
    ]
    assert any("compute_shader" in reason for reason in reasons)
    assert partial["environment_metadata"]["gpu_model"] is None
    assert partial["benchmark_passed"] is False

    operator_conflict = capture(
        fixture_runs(same_adapter),
        {**measured, "gpu_model": "NVIDIA GeForce RTX 4060 Ti"},
    )
    reasons = [
        item["reason"]
        for item in operator_conflict["unavailable_measurements"]
        if item["metric"] == "environment_metadata.gpu_model"
    ]
    assert any("Operator-provided GPU model does not match" in reason for reason in reasons)
    assert operator_conflict["benchmark_passed"] is False
