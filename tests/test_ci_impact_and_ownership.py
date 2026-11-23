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
        (["tools/nw07_export.py"], {"nw07", "nw08", "nw08a", "nw10"}, False),
        (["tools/nw01/bootstrap-windows.ps1"], {"nw01", "nw07", "nw08", "nw08a", "nw10"}, False),
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
    rel = "tests/test_nw11_new_feature.py"
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
        assert sources[name]["sha256"][:16] in key


def test_windows_bootstrap_cache_is_digest_verified_and_vs_install_is_minimal():
    bootstrap = (ROOT / "tools" / "nw01" / "bootstrap-windows.ps1").read_text(encoding="utf-8")
    assert "NW01_DOWNLOAD_CACHE" in bootstrap
    assert "Get-PinnedPayload" in bootstrap
    assert "Assert-NwSha256" in bootstrap
    assert "CACHE_HIT" in bootstrap
    assert "CACHE_INVALID" in bootstrap
    assert "cached-pinned-artifact" in bootstrap
    assert "Get-MissingVisualStudioComponents" in bootstrap
    assert "--includeRecommended" not in bootstrap
    assert "Test-ExistingVisualStudio" in bootstrap


def test_workflows_route_heavy_pr_jobs_and_cache_portable_payloads():
    expectations = {
        ".github/workflows/ci.yml": ("nw01", "windows-reproducibility-and-fixtures"),
        ".github/workflows/nw07-regression.yml": ("nw07", "windows-release-build"),
        ".github/workflows/nw08-studio.yml": ("nw08", "windows-integration"),
        ".github/workflows/nw08a-presentation-geometry.yml": ("nw08a", "windows-canonical-build-checks"),
        ".github/workflows/nw10-compact.yml": ("nw10", "windows-compact-export"),
    }
    for relative, (scope, job) in expectations.items():
        workflow = (ROOT / relative).read_text(encoding="utf-8")
        assert "ci-impact:" in workflow
        assert "tools/ci/changed_scope.py" in workflow
        assert f"needs.ci-impact.outputs.{scope}" in workflow
        assert job + ":" in workflow
        if job.startswith("windows"):
            assert "actions/cache@v4" in workflow
            assert "portable_cache_key.py" in workflow
            assert "NW01_DOWNLOAD_CACHE" in workflow


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
