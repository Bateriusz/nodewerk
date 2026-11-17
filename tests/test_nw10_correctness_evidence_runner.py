from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_correctness_runner_reproduces_and_binds_common_artifact() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    lowered = text.lower()

    assert 'validateset("ada", "blackwell")' in lowered
    assert "[switch]$compare" in lowered
    assert "[switch]$bundle" in lowered
    assert "enter-nwbuildenvironment" in lowered
    assert "tools/nw10_perf_candidate.py" in text
    assert "tools/nw10_common_correctness.py" in text
    assert '"build-diagnostic"' in text
    assert '"capture"' in text
    assert '"compare"' in text
    assert "performance-preservation-rtx4060ti.v1.json" in text
    assert 'Assert-NwSameValue "artifact_key"' in text
    assert 'Assert-NwSameValue "package_sha256"' in text
    assert 'Assert-NwSameValue "manifest_sha256"' in text
    assert 'Assert-NwSameValue "executable_sha256"' in text
    assert 'Assert-NwSameValue "complete_distribution_bytes"' in text
    assert "COMMON_ARTIFACT=EXACT_MATCH" in text


def test_correctness_runner_keeps_raw_capture_out_of_git_workflow() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    lowered = text.lower()

    assert "git add" not in lowered
    assert "git commit" not in lowered
    assert "git push" not in lowered
    assert "raw_capture_bytes_embedded" in text
    assert "local_paths_included" in text
    assert "$env:USERNAME" in text
    assert "$env:USERPROFILE" in text
    assert "Assert-NwPrivacy" in text


def test_correctness_runner_can_create_transfer_bundle() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")

    assert "bundle-manifest.json" in text
    assert "Get-FileHash -Algorithm SHA256" in text
    assert "Compress-Archive" in text
    assert "nw10-correctness-$Tier-bundle.zip" in text


def test_correctness_runner_compare_requires_both_hardware_sets() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")

    for parameter in (
        "AdaRecord",
        "AdaCaptures",
        "BlackwellRecord",
        "BlackwellCaptures",
    ):
        assert parameter in text
    assert "Cross-hardware correctness comparison is not verified." in text


def test_correctness_compare_runs_privacy_audit() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    compare_block = text[text.index('if ($Compare)'):]
    assert "Assert-NwPrivacy -JsonObject $comparison" in compare_block
