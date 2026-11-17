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


def test_same_common_artifact_allows_diagnostic_manifest_and_executable_to_differ() -> None:
    expected = {
        "artifact_key": "a",
        "package_sha256": "b",
        "manifest_sha256": "release-manifest",
        "executable_sha256": "release",
        "profile_id": "nw.common.d3d12.fl12_0.sm6_0.v1",
        "requirements_digest": "requirements",
    }
    observed = {
        "artifact_key": "a",
        "package_sha256": "b",
        "manifest_sha256": "diagnostic-manifest",
        "executable_sha256": "diagnostic",
        "profile_id": "nw.common.d3d12.fl12_0.sm6_0.v1",
        "requirements_digest": "requirements",
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


def test_diagnostic_identity_requires_its_own_manifest_binding(
    tmp_path: Path, monkeypatch
) -> None:
    from types import SimpleNamespace
    import hashlib

    stem = correctness.nw05_export._profile_file_stem(
        correctness.nw07_contract.PROFILE_ID
    )
    package_bytes = b"same-common-package"
    manifest = {
        "profile_id": correctness.nw07_contract.PROFILE_ID,
        "requirements_digest": correctness.nw07_contract.REQUIREMENTS_DIGEST,
        "artifact_identity": "sha256:v1:common-artifact",
        "retained": {"dependencies": ["kernel32.dll", "vcruntime140d.dll"]},
    }
    manifest["manifest_sha256"] = "sha256:v1:" + hashlib.sha256(
        correctness.nwp.canonical_json_bytes(manifest)
    ).hexdigest()
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    executable_bytes = b"MZ-diagnostic"
    (tmp_path / f"{stem}.nwp").write_bytes(package_bytes)
    (tmp_path / f"{stem}.manifest.json").write_bytes(manifest_bytes)
    (tmp_path / f"{stem}.exe").write_bytes(executable_bytes)

    capability = SimpleNamespace(
        profile_id=correctness.nw07_contract.PROFILE_ID,
        requirements_digest=correctness.nw07_contract.REQUIREMENTS_DIGEST,
    )
    inspection = SimpleNamespace(
        valid=True, package=SimpleNamespace(capability=capability)
    )
    monkeypatch.setattr(
        correctness.nwp, "inspect_package_bytes", lambda *_args, **_kwargs: inspection
    )
    monkeypatch.setattr(
        correctness.nw05_export,
        "extract_embedded_package",
        lambda _payload: (package_bytes, manifest_bytes),
    )
    monkeypatch.setattr(
        correctness.nw05_export,
        "_pe_runtime_dependencies",
        lambda _payload: ["kernel32.dll", "vcruntime140d.dll"],
    )
    identity = correctness._diagnostic_identity(tmp_path)
    assert identity["package_sha256"] == hashlib.sha256(package_bytes).hexdigest()
    assert identity["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert identity["artifact_key"] == "sha256:v1:common-artifact"

    monkeypatch.setattr(
        correctness.nw05_export,
        "extract_embedded_package",
        lambda _payload: (package_bytes, b"different-manifest"),
    )
    with pytest.raises(ValueError, match="diagnostic manifest"):
        correctness._diagnostic_identity(tmp_path)
