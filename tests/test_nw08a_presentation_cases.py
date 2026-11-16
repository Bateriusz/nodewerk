from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw08a_presentation_cases as cases  # noqa: E402


def test_presentation_case_tool_exports_explicit_geometry(tmp_path: Path) -> None:
    report = cases.run(work=tmp_path / "cases")
    assert report["success"] is True
    assert report["export_mode"] == "test_payload"
    assert report["hardware_evidence"] is False
    assert report["real_player_validation"] == "unexecuted"
    rows = {row["name"]: row for row in report["cases"]}
    assert set(rows) == {"landscape", "portrait", "canonical_hd", "square"}
    for name, width, height in cases.CASES:
        row = rows[name]
        assert row["status"] == "passed"
        assert row["present_width"] == width
        assert row["present_height"] == height
        assert row["preview_dimensions"] == [width, height]


def test_presentation_case_tool_report_is_deterministic(tmp_path: Path) -> None:
    first = cases.run(work=tmp_path / "first")
    second = cases.run(work=tmp_path / "second")
    # Case records are stable apart from the work-directory field.
    assert json.dumps(first["cases"], sort_keys=True) == json.dumps(
        second["cases"], sort_keys=True
    )
