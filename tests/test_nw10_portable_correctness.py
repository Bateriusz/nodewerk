from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_portable_correctness as portable  # noqa: E402


def test_parse_probe_only_accepts_nw10_fields() -> None:
    parsed = portable._parse_probe(
        "noise\nNW10_PACKAGE_SHA=abc\nNW10_RUNTIME_IMPLEMENTATION_MASK=511\n"
    )
    assert parsed == {
        "NW10_PACKAGE_SHA": "abc",
        "NW10_RUNTIME_IMPLEMENTATION_MASK": "511",
    }


def test_portable_source_does_not_use_dev_capture_controls() -> None:
    source = (
        REPO_ROOT / "tools" / "nw10" / "portable_correctness_main.cpp"
    ).read_text(encoding="utf-8")
    assert "NW07_CAPTURE_CONTROLS" not in source
    assert "readbackPresentedOutput" in source
