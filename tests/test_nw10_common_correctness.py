from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_common_correctness as correctness  # noqa: E402


def test_probe_requires_hardware_identity_and_uncapped_field() -> None:
    parsed = correctness._parse_probe(
        "\n".join(
            (
                "NW05_ADAPTER=NVIDIA GeForce RTX 4060 Ti",
                "NW05_DRIVER=32.0.16.1088",
                "NW05_VENDOR_ID=4318",
                "NW05_DEVICE_ID=10243",
                "NW05_PRESENT_SYNC_INTERVAL=0",
            )
        )
    )
    assert parsed["adapter_description"] == "NVIDIA GeForce RTX 4060 Ti"
    assert parsed["vendor_id"] == 4318
    assert parsed["device_id"] == 10243
    assert parsed["present_sync_interval"] == 0


def test_probe_rejects_missing_identity() -> None:
    with pytest.raises(ValueError, match="omitted"):
        correctness._parse_probe("NW05_ADAPTER=NVIDIA GeForce RTX 4060 Ti\n")


def test_same_common_artifact_allows_only_executable_to_differ() -> None:
    expected = {
        "artifact_key": "a",
        "package_sha256": "b",
        "manifest_sha256": "c",
        "executable_sha256": "release",
    }
    observed = {
        "artifact_key": "a",
        "package_sha256": "b",
        "manifest_sha256": "c",
        "executable_sha256": "diagnostic",
    }
    correctness._require_same_common_artifact(expected, observed)
    observed["package_sha256"] = "changed"
    with pytest.raises(ValueError, match="package_sha256"):
        correctness._require_same_common_artifact(expected, observed)


def test_cross_hardware_comparison_requires_same_artifact(tmp_path: Path, monkeypatch) -> None:
    ada = {
        "tier": "ada",
        "common_artifact": {"package_sha256": "a"},
        "hardware": {},
        "captures": [],
        "loop_boundary": {},
    }
    blackwell = {
        "tier": "blackwell",
        "common_artifact": {"package_sha256": "b"},
        "hardware": {},
        "captures": [],
        "loop_boundary": {},
    }
    a = tmp_path / "ada.json"
    b = tmp_path / "blackwell.json"
    a.write_text(json.dumps(ada), encoding="utf-8")
    b.write_text(json.dumps(blackwell), encoding="utf-8")
    with pytest.raises(ValueError, match="same common artifact"):
        correctness.compare(
            a,
            b,
            ada_capture_dir=tmp_path / "ada",
            blackwell_capture_dir=tmp_path / "blackwell",
            output=tmp_path / "public.json",
        )
