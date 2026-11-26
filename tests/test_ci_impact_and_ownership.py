import fnmatch
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "ci"))

import changed_scope
import portable_cache_key


def mapping():
    return json.loads((ROOT / "ci" / "impact-map.v1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("paths", "expected", "docs"),
    [
        (["tools/nw10_pe_ledger.py", "tests/test_nw10_pe_ledger.py"], {"nw10"}, False),
        (["tools/nw08_editor.py"], {"nw08", "nw08a"}, False),
        (["tools/nw08a_presentation_cases.py"], {"nw08a"}, False),
        (["tools/nw07_export.py"], {"nw07", "nw08", "nw08a", "nw10", "nw11"}, False),
        (["tools/nw01/bootstrap-windows.ps1"], {"nw01", "nw07", "nw08", "nw08a", "nw10", "nw11"}, False),
        (["src/player/package_player.cpp", "shaders/common.hlsl"], set(changed_scope.SCOPES), False),
        (["docs/architecture.md", "README.md"], set(), True),
        (["src-new-unknown/new.cpp"], set(changed_scope.SCOPES), False),
        ([".github/workflows/nw10-compact.yml"], set(changed_scope.SCOPES), False),
    ],
)
def test_impact_routing(paths, expected, docs):
    result = changed_scope.classify_paths(paths, mapping())
    assert set(result.scopes) == expected
    assert result.docs_only is docs


def test_dot_github_workflow_change_is_explicit_ci_infrastructure_not_unknown():
    result = changed_scope.classify_paths([".github/workflows/nw10-compact.yml"], mapping())
    assert set(result.scopes) == set(changed_scope.SCOPES)
    assert result.full_regression is True
    assert result.fail_safe is False
    assert all(reason == "CI/routing infrastructure changed" for reason in result.reasons.values())


def test_invalid_mapping_fails_safe(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"version":999}', encoding="utf-8")
    result = changed_scope.classify("pull_request", ["tools/nw10_pe_ledger.py"], bad)
    assert set(result.scopes) == set(changed_scope.SCOPES)
    assert result.fail_safe is True
    assert result.full_regression is True


def test_non_pr_modes_are_full_regression():
    result = changed_scope.classify("push", [], ROOT / "ci" / "impact-map.v1.json")
    assert set(result.scopes) == set(changed_scope.SCOPES)
    assert result.full_regression is True


def test_test_ownership_is_complete_and_unambiguous():
    data = json.loads((ROOT / "ci" / "test-ownership.v1.json").read_text(encoding="utf-8"))
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "tests/test_ci_impact_and_ownership.py":
            continue
        matches = [
            family
            for family in data["families"]
            if any(fnmatch.fnmatchcase(rel, pattern) for pattern in family["patterns"])
        ]
        assert len(matches) == 1, f"{rel} ownership matches={[item['name'] for item in matches]}"
        assert matches[0]["broad_gate"]
        assert matches[0]["platform_gate"]


def test_new_test_namespace_would_fail_ownership():
    data = json.loads((ROOT / "ci" / "test-ownership.v1.json").read_text(encoding="utf-8"))
    rel = "tests/test_nw12_new_feature.py"
    matches = [
        family
        for family in data["families"]
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in family["patterns"])
    ]
    assert matches == []


def test_portable_cache_key_binds_manifest_versions_and_digests():
    key = portable_cache_key.build_key(
        ROOT / "benchmarks" / "toolchain-manifest.v1.json",
        ROOT / "tools" / "nw01" / "toolchain-sources.json",
        "Windows",
        "X64",
    )
    toolchain = json.loads((ROOT / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8"))
    sources = json.loads((ROOT / "tools" / "nw01" / "toolchain-sources.json").read_text(encoding="utf-8"))
    assert "windows-x64" in key
    assert toolchain["pinned_dependencies"]["cmake_version"] in key
    for name in ("cmake", "llvm", "dxc"):
        assert sources[name]["sha256"] in key


def test_portable_cache_key_changes_when_source_identity_changes(tmp_path):
    toolchain = ROOT / "benchmarks" / "toolchain-manifest.v1.json"
    source = ROOT / "tools" / "nw01" / "toolchain-sources.json"
    copied = tmp_path / "sources.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    copied.write_text(json.dumps(data), encoding="utf-8")
    first = portable_cache_key.build_key(toolchain, copied, "Windows", "X64")
    data["cmake"]["sha256"] = "0" * 64
    copied.write_text(json.dumps(data), encoding="utf-8")
    second = portable_cache_key.build_key(toolchain, copied, "Windows", "X64")
    assert first != second
    assert "0" * 64 in second


def test_nw10_windows_job_uses_msvc_only_bootstrap_profile():
    workflow = (ROOT / ".github" / "workflows" / "nw10-compact.yml").read_text(encoding="utf-8")
    bootstrap = (ROOT / "tools" / "nw01" / "bootstrap-windows.ps1").read_text(encoding="utf-8")
    env_script = (ROOT / "tools" / "nw01" / "enter-build-env.ps1").read_text(encoding="utf-8")
    module = (ROOT / "tools" / "nw01" / "Nw01.Windows.psm1").read_text(encoding="utf-8")

    correctness = (ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1").read_text(encoding="utf-8")
    assert workflow.count("-SkipLlvm") >= 6
    assert "[switch]$SkipLlvm" in correctness
    assert "Enter-NwBuildEnvironment -RepoRoot $repo -SkipLlvm:$SkipLlvm" in correctness
    assert "LLVM provisioning skipped for MSVC-only consumer" in bootstrap
    assert "[switch]$SkipLlvm" in env_script
    assert "-RequireLlvm:(-not $SkipLlvm)" in module
    assert "cmake --preset nw07-release" in workflow

    windows = workflow.split("  windows-compact-export:", 1)[1]
    assert "- name: Install pytest" not in windows
    assert 'cmake --preset nw07-release -B "$PWD/build/nw10-decoder-off"' not in windows
    assert 'cmake --build "$PWD/build/nw10-decoder-off"' not in windows
    assert "build/nw07-release-decoder-off/staging/procedural_image_player/nw_procedural_image_player.exe" in windows
    assert '"--decoder-off-template", $canonicalTemplate,' in windows


def test_windows_bootstrap_cache_is_digest_verified_and_vs_install_is_minimal():
    bootstrap = (ROOT / "tools" / "nw01" / "bootstrap-windows.ps1").read_text(encoding="utf-8")
    assert "NW01_DOWNLOAD_CACHE" in bootstrap
    assert "Get-PinnedPayload" in bootstrap
    assert "Assert-NwSha256" in bootstrap
    assert "CACHE_HIT" in bootstrap
    assert "CACHE_INVALID" in bootstrap
    assert "cached-pinned-artifact" in bootstrap
    assert "Get-MissingVisualStudioComponents" in bootstrap
    assert '-version "[17.0,18.0)"' in bootstrap
    assert "-latest -products * -requires" not in bootstrap
    assert "Get-VisualStudioModifyTarget" in bootstrap
    assert "Invoke-VisualStudioInstanceModify" in bootstrap
    assert "VS_MODIFY_TARGET" in bootstrap
    assert '"modify"' in bootstrap
    assert '"--installPath"' in bootstrap
    assert '"--noUpdateInstaller"' in bootstrap
    modify_block = bootstrap.split("function Invoke-VisualStudioInstanceModify", 1)[1].split("function Ensure-VisualStudio", 1)[0]
    assert '"--channelId"' not in modify_block
    assert '"--nocache"' not in modify_block
    assert "VS_BOOTSTRAP_COMPONENTS" in bootstrap
    assert "$componentsToInstall = @($visualStudio.Components)" in bootstrap
    sources = json.loads((ROOT / "tools" / "nw01" / "toolchain-sources.json").read_text(encoding="utf-8"))
    assert "Microsoft.VisualStudio.Component.VC.Tools.x86.x64" in sources["visual_studio"]["components"]
    assert "--includeRecommended" not in bootstrap
    assert "Test-ExistingVisualStudio" in bootstrap


def test_workflows_route_heavy_pr_jobs_and_cache_portable_payloads():
    expectations = {
        ".github/workflows/ci.yml": ("nw01", "windows-reproducibility-and-fixtures"),
        ".github/workflows/nw07-regression.yml": ("nw07", "windows-release-build"),
        ".github/workflows/nw08-studio.yml": ("nw08", "windows-integration"),
        ".github/workflows/nw08a-presentation-geometry.yml": ("nw08a", "windows-canonical-build-checks"),
        ".github/workflows/nw10-compact.yml": ("nw10", "windows-compact-export"),
        ".github/workflows/nw11-advanced-gpu.yml": ("nw11", "contract-and-capability-checks"),
    }
    for relative, (scope, job) in expectations.items():
        workflow = (ROOT / relative).read_text(encoding="utf-8")
        assert "ci-impact:" in workflow
        assert "tools/ci/changed_scope.py" in workflow
        assert f"needs.ci-impact.outputs.{scope}" in workflow
        assert job + ":" in workflow
        assert "runs-on: windows-latest" not in workflow
        if job.startswith("windows"):
            assert "actions/cache@v4" in workflow
            assert "portable_cache_key.py" in workflow
            assert "NW01_DOWNLOAD_CACHE" in workflow


def test_nw01_splits_build_and_runtime_by_host_capability():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    build = workflow.split("  windows-reproducibility-and-fixtures:", 1)[1].split("  windows-runtime-smoke:", 1)[0]
    runtime = workflow.split("  windows-runtime-smoke:", 1)[1].split("  nw04-native-verification:", 1)[0]
    native = workflow.split("  nw04-native-verification:", 1)[1].split("  windows-physical-evidence:", 1)[0]

    assert "runs-on: windows-2022" in build
    assert "Build current-head runtime-smoke inputs without launching" in build
    assert "NW05_RUNTIME_BUNDLE_OUTPUT" in build
    assert "NW09_REAL_PLAYER_BUILD_OUTPUT" in build
    assert '$env:NW05_REAL_PLAYER_TEMPLATE = $template' in build
    assert 'Copy-Item $template (Join-Path $playerDir "nw_procedural_image_player.exe")' in build
    assert "--skip-smoke-launch" in build
    assert "nw01-runtime-smoke-inputs-${{ github.sha }}" in build
    assert "source_commit = (git rev-parse HEAD).Trim()" in build

    assert "runs-on: windows-2025" in runtime
    assert "actions/download-artifact@v4" in runtime
    assert "needs: [ci-impact, windows-reproducibility-and-fixtures]" in runtime
    assert "source_commit does not match the checked-out current head" in runtime
    assert "Assert-NwExactToolVersion" in runtime
    assert "NW05_RUNTIME_BUNDLE_INPUT" in runtime
    assert "NW09_REAL_PLAYER_RUNTIME_INPUT" in runtime
    assert "--smoke-only" in runtime

    assert "runs-on: windows-2025" in native


def test_runtime_sensitive_workflows_use_windows_2025():
    nw08 = (ROOT / ".github" / "workflows" / "nw08-studio.yml").read_text(encoding="utf-8")
    nw08a = (ROOT / ".github" / "workflows" / "nw08a-presentation-geometry.yml").read_text(encoding="utf-8")
    nw07 = (ROOT / ".github" / "workflows" / "nw07-regression.yml").read_text(encoding="utf-8")

    assert "runs-on: windows-2025" in nw08
    assert "runs-on: windows-2022" not in nw08
    assert "runs-on: windows-2025" in nw08a
    assert "runs-on: windows-2022" not in nw08a
    assert "runs-on: windows-2022" in nw07


def test_runtime_split_helpers_preserve_build_and_launch_phases():
    nw05 = (ROOT / "tests" / "test_nw05_real_export.py").read_text(encoding="utf-8")
    nw09 = (ROOT / "tests" / "test_nw09_review_fixes.py").read_text(encoding="utf-8")
    examples = (ROOT / "tools" / "nw09_ci_examples.py").read_text(encoding="utf-8")
    assert "NW05_RUNTIME_BUNDLE_OUTPUT" in nw05
    assert "NW05_RUNTIME_BUNDLE_INPUT" in nw05
    assert "NW09_REAL_PLAYER_BUILD_OUTPUT" in nw09
    assert "NW09_REAL_PLAYER_RUNTIME_INPUT" in nw09
    assert "--skip-smoke-launch" in examples
    assert "--smoke-only" in examples


def test_windows_full_pytest_is_not_duplicated():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    marker = "- name: Run NW-01 contract test suite on Windows"
    tail = workflow.split(marker, 1)[1].split("- name:", 1)[0]
    assert "run: pytest -v" not in tail
    assert "tests/test_nw01_windows_harness.py" in tail


def test_nw10_transfer_workflow_reuses_same_mode_build_for_second_tier():
    workflow = (ROOT / ".github" / "workflows" / "nw10-compact.yml").read_text(encoding="utf-8")
    runner = (ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1").read_text(encoding="utf-8")
    assert "ReusePreparedTransfer" in runner
    assert "reused transfer file hash" in runner
    assert workflow.count("-ReusePreparedTransfer") >= 2
