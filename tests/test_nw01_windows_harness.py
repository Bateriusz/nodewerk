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
requires_windows_harness = pytest.mark.skipif(
    not (HAS_PWSH and IS_WINDOWS),
    reason="PowerShell harness behaviour tests run in Windows integration",
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
    assert "Invoke-NwDownloadWithFallback" in module


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


def run_pwsh_script(script: Path, *arguments: object, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return run_pwsh(["-File", str(script), *[str(arg) for arg in arguments]], extra_env=extra_env)


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


def write_fake_tool(path: Path, output: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        path.write_text("@echo off\r\n" + "echo " + output + "\r\n", encoding="ascii")
    else:
        path.write_text("#!/bin/sh\nprintf '%s\\n' " + ps_quote(output) + "\n", encoding="utf-8")
        path.chmod(0o755)


@requires_windows_harness
def test_dxc_exact_version_rejects_longer_substrings(tmp_path: Path) -> None:
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

    assert exact("dxcompiler.dll: 1.8.2407 (deadbeef); dxil.dll: 1.8.2407", "1.8.2407") == 0
    assert exact("dxcompiler.dll: 1.8.24070 (deadbeef)", "1.8.2407") == 1
    assert exact("dxcompiler.dll: 11.8.2407 (deadbeef)", "1.8.2407") == 1


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
        "[ordered]@{ VcVars = $selection.VcVarsVersion; Sdk = $selection.WindowsSdkVersion; "
        "Components = @($selection.Components) } | ConvertTo-Json -Depth 4 -Compress"
    )
    proc = run_pwsh_command(command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    selection = json.loads(proc.stdout.strip().splitlines()[-1])
    assert selection["VcVars"] == "14.40.33821"
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
    retry = "[pscustomobject]@{ max_attempts = 4; initial_delay_seconds = 0; backoff_multiplier = 2 }"
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

    crash_summary = tmp_path / "summary-crash.json"
    proc = run_pwsh_script(
        harness / "run-full.ps1",
        "-RepoRoot",
        fake_repo,
        "-SummaryPath",
        crash_summary,
        extra_env={
            "NW_TEST_VERIFIER_EXIT": "1",
            "NW_TEST_VERIFIER_SKIP_SUMMARY": "1",
        },
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    crash = json.loads(crash_summary.read_text(encoding="utf-8-sig"))
    assert crash["full_baseline_passed"] is False
    assert crash["wrapper_error"]["stage"] == "wrapper"


@requires_windows_harness
def test_prepare_evidence_commit_guardrails_and_optional_new_commit(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "repo with spaces")
    (repo / "benchmarks/hardware").mkdir(parents=True)
    evidence = repo / "benchmarks/hardware/rtx.json"
    evidence.write_text('{"value": 1}\n', encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    commit_all(repo)
    script = ROOT / "tools/nw01/prepare-evidence-commit.ps1"

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

    # AM (staged add + unstaged modification) must be rejected rather than
    # silently replacing the pre-staged content.
    evidence.write_text('{"value": 2}\n', encoding="utf-8")
    git_run(repo, "add", "--", "benchmarks/hardware/rtx.json")
    evidence.write_text('{"value": 3}\n', encoding="utf-8")
    proc = run_pwsh_script(script, "-RepoRoot", repo)
    assert proc.returncode != 0
    assert "both staged and unstaged changes" in (proc.stdout + proc.stderr)
    git_run(repo, "reset", "-q", "--", "benchmarks/hardware/rtx.json")
    git_run(repo, "checkout", "--", "benchmarks/hardware/rtx.json")

    # An in-progress merge must never turn -Commit into a merge commit.
    git_dir = git_run(repo, "rev-parse", "--git-dir").stdout.strip()
    git_dir_path = Path(git_dir) if Path(git_dir).is_absolute() else repo / git_dir
    merge_head = git_dir_path / "MERGE_HEAD"
    merge_head.write_text("0" * 40 + "\n", encoding="utf-8")
    evidence.write_text('{"value": 4}\n', encoding="utf-8")
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
    head = commit_all(repo)

    summary = tmp_path / "full-summary.json"
    summary.write_text(
        json.dumps(
            {
                "mode": "Full",
                "source_commit": head,
                "checks": {
                    "size_ledgers": {"passed": True},
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
    for entry in manifest["files"]:
        payload = (output / "deploy" / entry["name"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["bytes"] == len(payload)

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
    summary.write_text(json.dumps({"source_commit": archived_commit}), encoding="utf-8")
    package = tmp_path / "package"
    (package / "evidence").mkdir(parents=True)
    (package / "evidence/artifact-manifest.json").write_text(
        json.dumps({"source_commit": archived_commit}), encoding="utf-8"
    )

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
        assert directory.name.startswith(archived_commit[:12])

    conflicting_summary = tmp_path / "conflicting.json"
    conflicting_summary.write_text(json.dumps({"source_commit": "b" * 40}), encoding="utf-8")
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
def test_evidence_summary_distinguishes_current_and_historical_full_results(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path / "summary repo")
    (repo / "benchmarks").mkdir()
    shutil.copy(ROOT / "benchmarks/toolchain-manifest.v1.json", repo / "benchmarks/toolchain-manifest.v1.json")
    shutil.copy(ROOT / "benchmarks/evidence-status.json", repo / "benchmarks/evidence-status.json")
    head = commit_all(repo)
    script = ROOT / "tools/nw01/evidence-summary.ps1"

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
    assert any("No adapter description" in reason for reason in reasons)
    assert missing["benchmark_passed"] is False

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
