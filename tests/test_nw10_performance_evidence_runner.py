from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_capture  # noqa: E402


def test_performance_evidence_runner_is_fail_closed_and_explicit() -> None:
    path = REPO_ROOT / "tools" / "nw10" / "run-performance-evidence.ps1"
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "[switch]$commit" in lowered
    assert "[switch]$push" in lowered
    assert "-push requires -commit" in lowered
    assert "enter-nwbuildenvironment" in lowered
    assert "tools/nw10_perf_candidate.py" in text
    assert '"collect-after"' in text
    assert '"publish"' in text
    assert "tools/schema_validator.py" in text
    assert "git diff --check" in text
    assert "git add ." not in lowered
    assert "git add -a" not in lowered
    assert "build/nw10-perf-after-raw.json" in text
    assert "benchmarks/nw10/performance-preservation-rtx4060ti.v1.json" in text
    assert "benchmarks/nw10/evidence-status.v1.json" in text
    assert "raw_samples_included" in text
    assert "local_paths_included" in text
    assert "$env:USERNAME" in text
    assert "$env:USERPROFILE" in text


def test_performance_evidence_runner_only_stages_public_allowlist() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-performance-evidence.ps1"
    ).read_text(encoding="utf-8")
    stage_marker = '$stage = @('
    start = text.index(stage_marker)
    end = text.index(')', start)
    stage_block = text[start:end]
    assert "benchmarks/nw10/evidence-status.v1.json" in stage_block
    assert "benchmarks/nw10/performance-preservation-rtx4060ti.v1.json" in stage_block
    assert "build/" not in stage_block

def test_performance_wrapper_forwards_baseline_root_and_selected_presentmon() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-performance-evidence.ps1"
    ).read_text(encoding="utf-8")
    assert "[string]$BeforeArtifactRoot" in text
    assert '"--before-artifact-root"' in text
    assert "$presentMonPath = Resolve-NwRepoPath $PresentMon" in text
    assert "--presentmon build/tools/presentmon/PresentMon-2.6.0-x64.exe" not in text


def test_windows_external_command_split_preserves_quoted_path_with_spaces() -> None:
    command = (
        'python tools/nw07_presentmon_collector.py --presentmon '
        '"C:\\Program Files\\PresentMon\\PresentMon.exe"'
    )
    tokens = nw07_capture._split_external_command(command, windows=True)
    assert tokens[-1] == "C:\\Program Files\\PresentMon\\PresentMon.exe"
