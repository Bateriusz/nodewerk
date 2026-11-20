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
import nw10_publish_evidence as publisher  # noqa: E402


def _capture_record(
    tier: str,
    status: str,
    *,
    package_sha256: str = "same",
    packaging_mode: str = "unpacked",
    diagnostic_sha256: str | None = None,
    encoded_container_sha256: str | None = None,
) -> dict:
    adapter = (
        "NVIDIA GeForce RTX 4060 Ti"
        if tier == "ada"
        else "NVIDIA GeForce RTX 5070 Ti"
    )
    diagnostic_sha256 = diagnostic_sha256 or (
        "d" * 64 if tier == "ada" else "e" * 64
    )
    return {
        "format": correctness.FORMAT,
        "version": 1,
        "task": "NW-10",
        "tier": tier,
        "status": status,
        "execution_kind": "physical",
        "packaging_mode": packaging_mode,
        "capture_protocol": "nw07-correctness-capture.v1",
        "common_artifact": {"package_sha256": package_sha256},
        "diagnostic_sibling": {
            "executable_sha256": diagnostic_sha256,
            "executable_bytes": 1024,
            "manifest_sha256": "f" * 64,
            "separate_from_final_executable": True,
            "same_common_package": True,
            "release_manifest_reused": False,
            "packaging_mode": packaging_mode,
            "encoded_container_sha256": encoded_container_sha256,
            "encoded_container_bytes": 512 if packaging_mode == "packed" else None,
            "codec_id": (
                "nwc.codec.mszip.v1" if packaging_mode == "packed" else None
            ),
            "layout_id": (
                "nwc.layout.soa.v1" if packaging_mode == "packed" else None
            ),
            "verification_file": (
                "packed-diagnostic.exe" if packaging_mode == "packed" else None
            ),
        },
        "hardware": {
            "adapter_description": adapter,
            "driver_version": "1.2.3",
            "vendor_id": 4318,
            "device_id": 1,
            "classified_tier": tier,
            "device_form_factor": "desktop",
            "device_form_factor_source": "win32_system_enclosure_and_battery",
        },
        "captures": [
            {
                "tick": tick,
                "file": f"tick_{tick}.rgba32f",
                "bytes": 4,
                "sha256": "ignored",
            }
            for tick in correctness.ALL_TICKS
        ],
        "loop_boundary": {"status": "captured_pending_numeric_validation"},
        "runtime": {
            "status": "passed",
            "capture_exit_code": 0,
            "frames_per_tick": 1,
            "present_sync_interval": 0,
        },
    }


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


def test_device_form_factor_is_measured_from_chassis_and_battery(monkeypatch) -> None:
    monkeypatch.setattr(correctness.platform, "system", lambda: "Windows")

    def run(command, **_kwargs):
        return correctness.subprocess.CompletedProcess(
            command, 0, '{"ChassisTypes":[3],"BatteryPresent":false}', ""
        )

    monkeypatch.setattr(correctness.subprocess, "run", run)
    probe = correctness._windows_device_form_factor_probe()
    assert probe["device_form_factor"] == "desktop"
    assert probe["device_form_factor_source"] == "win32_system_enclosure_and_battery"

    monkeypatch.setattr(
        correctness.subprocess,
        "run",
        lambda command, **_kwargs: correctness.subprocess.CompletedProcess(
            command, 0, '{"ChassisTypes":[10],"BatteryPresent":true}', ""
        ),
    )
    assert correctness._windows_device_form_factor_probe()["device_form_factor"] == "laptop"


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
    ada = _capture_record("ada", "captured_reference", package_sha256="a")
    blackwell = _capture_record(
        "blackwell", "captured_candidate", package_sha256="b"
    )
    a = tmp_path / "ada.json"
    b = tmp_path / "blackwell.json"
    a.write_text(json.dumps(ada), encoding="utf-8")
    b.write_text(json.dumps(blackwell), encoding="utf-8")
    with pytest.raises(ValueError, match="same common artifact"):
        monkeypatch.setattr(
            correctness, "_verify_capture_manifest", lambda *_args: None
        )
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


def test_verify_capture_manifest_rejects_transferred_file_corruption(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    rows = []
    for tick in correctness.ALL_TICKS:
        path = captures / f"tick_{tick}.rgba32f"
        payload = (str(tick).encode("ascii") + b"x")
        path.write_bytes(payload)
        rows.append(
            {
                "tick": tick,
                "file": path.name,
                "bytes": len(payload),
                "sha256": correctness._sha256(path),
            }
        )
    record = {"captures": rows}
    correctness._verify_capture_manifest(record, captures)
    (captures / f"tick_{correctness.ALL_TICKS[2]}.rgba32f").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="size mismatch|SHA-256 mismatch"):
        correctness._verify_capture_manifest(record, captures)


def test_compare_recomputes_loop_boundaries_from_raw_captures(
    tmp_path: Path, monkeypatch
) -> None:
    ada_record = tmp_path / "ada.json"
    blackwell_record = tmp_path / "blackwell.json"
    ada_record.write_text(
        json.dumps(_capture_record("ada", "captured_reference")),
        encoding="utf-8",
    )
    blackwell_record.write_text(
        json.dumps(_capture_record("blackwell", "captured_candidate")),
        encoding="utf-8",
    )
    monkeypatch.setattr(correctness, "_verify_capture_manifest", lambda *_args: None)
    calls = []
    def fake_compare(left: Path, right: Path) -> dict[str, float]:
        calls.append((left.name, right.name))
        return {"maximum_error": 0.0, "maximum_relative_error": 0.0}
    monkeypatch.setattr(correctness, "_compare_rgba32f", fake_compare)
    result = correctness.compare(
        ada_record,
        blackwell_record,
        ada_capture_dir=tmp_path / "ada",
        blackwell_capture_dir=tmp_path / "blackwell",
        output=tmp_path / "out.json",
    )
    assert result["status"] == "verified"
    assert result["ada"]["loop_boundary"]["status"] == "passed"
    assert result["blackwell"]["loop_boundary"]["status"] == "passed"
    assert result["packed_unpacked_comparison"]["status"] == "pending_physical"
    assert result["source"]["mode"] == "compare"
    assert result["source"]["execution_kind"] == "physical_cross_hardware"
    assert len(result["source"]["implementation_head"]) == 40
    assert result["comparison"]["fixed_tick_status"] == "passed"
    assert result["comparison"]["ada_loop_boundary_status"] == "passed"
    assert result["comparison"]["blackwell_loop_boundary_status"] == "passed"
    assert (
        f"tick_{correctness.nw07_contract.LOOP_BOUNDARY_TICKS[1]}.rgba32f"
        in {name for pair in calls for name in pair}
    )


def test_packed_executable_verification_recomputes_container_identity(
    tmp_path: Path, monkeypatch
) -> None:
    decoded = b"canonical package bytes"
    payload = b"compact container bytes"
    executable_bytes = b"captured packed executable"
    executable = tmp_path / "packed-diagnostic.exe"
    executable.write_bytes(executable_bytes)
    record_path = tmp_path / "capture.json"
    record = _capture_record(
        "ada",
        "captured_reference",
        package_sha256=correctness.hashlib.sha256(decoded).hexdigest(),
        packaging_mode="packed",
        diagnostic_sha256=correctness.hashlib.sha256(executable_bytes).hexdigest(),
        encoded_container_sha256=correctness.hashlib.sha256(payload).hexdigest(),
    )
    record["diagnostic_sibling"]["encoded_container_bytes"] = len(payload)

    monkeypatch.setattr(
        correctness.nw05_export,
        "extract_embedded_package",
        lambda _data: (payload, b"manifest"),
    )
    monkeypatch.setattr(correctness.compact, "is_compact_container", lambda _data: True)
    monkeypatch.setattr(
        correctness.compact,
        "parse_header",
        lambda _data: {
            "codec_id": "nwc.codec.mszip.v1",
            "layout_id": "nwc.layout.soa.v1",
        },
    )
    monkeypatch.setattr(correctness.compact, "decode_compact", lambda _data: decoded)

    correctness._verify_packed_executable(record, record_path=record_path)

    record["diagnostic_sibling"]["encoded_container_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="encoded-container SHA-256"):
        correctness._verify_packed_executable(record, record_path=record_path)


def test_packed_result_bundle_manifest_binds_verification_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    captures = root / "captures"
    captures.mkdir(parents=True)
    executable = root / "packed-diagnostic.exe"
    executable.write_bytes(b"packed executable")
    record = _capture_record(
        "ada",
        "captured_reference",
        packaging_mode="packed",
        diagnostic_sha256=correctness._sha256(executable),
        encoded_container_sha256="2" * 64,
    )
    for capture in record["captures"]:
        path = captures / capture["file"]
        path.write_bytes(b"data")
        capture["bytes"] = path.stat().st_size
        capture["sha256"] = correctness._sha256(path)
    record_path = root / "capture.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    files = []
    for path in [record_path, executable, *sorted(captures.iterdir())]:
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": correctness._sha256(path),
            }
        )
    (root / "bundle-manifest.json").write_text(
        json.dumps(
            {
                "format": "nw10-correctness-result-bundle",
                "version": 1,
                "task": "NW-10",
                "tier": "ada",
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    correctness._verify_result_bundle_manifest(
        record,
        record_path=record_path,
        capture_dir=captures,
    )

    executable.write_bytes(b"tampered executable")
    with pytest.raises(ValueError, match="size mismatch|SHA-256 mismatch"):
        correctness._verify_result_bundle_manifest(
            record,
            record_path=record_path,
            capture_dir=captures,
        )


def test_direct_packed_capture_writes_comparable_result_bundle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    captures = root / "captures"
    captures.mkdir(parents=True)
    executable = root / "packed-diagnostic.exe"
    executable.write_bytes(b"packed executable")
    record = _capture_record(
        "ada",
        "captured_reference",
        packaging_mode="packed",
        diagnostic_sha256=correctness._sha256(executable),
        encoded_container_sha256="2" * 64,
    )
    for capture in record["captures"]:
        path = captures / capture["file"]
        path.write_bytes(b"data")
        capture["bytes"] = path.stat().st_size
        capture["sha256"] = correctness._sha256(path)
    record_path = root / "capture.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    correctness._write_result_bundle_manifest(
        root,
        tier="ada",
        record_path=record_path,
        capture_dir=captures,
        verification_file="packed-diagnostic.exe",
    )
    correctness._verify_result_bundle_manifest(
        record,
        record_path=record_path,
        capture_dir=captures,
    )

    manifest = json.loads(
        (root / "bundle-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["format"] == "nw10-correctness-result-bundle"
    assert {row["path"] for row in manifest["files"]} == {
        "capture.json",
        "packed-diagnostic.exe",
        *{f"captures/{capture['file']}" for capture in record["captures"]},
    }


def test_packed_comparison_requires_manifests_and_packed_mode(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(correctness, "_verify_capture_manifest", lambda *_args: None)
    monkeypatch.setattr(
        correctness, "_verify_packed_executable", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        correctness, "_verify_result_bundle_manifest", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        correctness,
        "_compare_rgba32f",
        lambda *_args: {"maximum_error": 0.0, "maximum_relative_error": 0.0},
    )
    package = "a" * 64
    ada = _capture_record("ada", "captured_reference", package_sha256=package)
    blackwell = _capture_record(
        "blackwell", "captured_candidate", package_sha256=package
    )
    ada_record = tmp_path / "ada.json"
    blackwell_record = tmp_path / "blackwell.json"
    ada_record.write_text(json.dumps(ada), encoding="utf-8")
    blackwell_record.write_text(json.dumps(blackwell), encoding="utf-8")
    packed_ada = _capture_record(
        "ada",
        "captured_reference",
        package_sha256=package,
        packaging_mode="packed",
        diagnostic_sha256="1" * 64,
        encoded_container_sha256="2" * 64,
    )
    packed_blackwell = _capture_record(
        "blackwell",
        "captured_candidate",
        package_sha256=package,
        packaging_mode="packed",
        diagnostic_sha256="3" * 64,
        encoded_container_sha256="4" * 64,
    )
    packed_ada_record = tmp_path / "packed-ada.json"
    packed_blackwell_record = tmp_path / "packed-blackwell.json"
    (tmp_path / "packed-ada").mkdir()
    (tmp_path / "packed-blackwell").mkdir()
    packed_ada_record.write_text(json.dumps(packed_ada), encoding="utf-8")
    packed_blackwell_record.write_text(
        json.dumps(packed_blackwell), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="requires both hardware capture"):
        correctness.compare(
            ada_record,
            blackwell_record,
            ada_capture_dir=tmp_path / "ada",
            blackwell_capture_dir=tmp_path / "blackwell",
            output=tmp_path / "out.json",
            packed_ada_capture_dir=tmp_path / "packed-ada",
        )

    result = correctness.compare(
        ada_record,
        blackwell_record,
        ada_capture_dir=tmp_path / "ada",
        blackwell_capture_dir=tmp_path / "blackwell",
        output=tmp_path / "out.json",
        packed_ada_capture_dir=tmp_path / "packed-ada",
        packed_blackwell_capture_dir=tmp_path / "packed-blackwell",
        packed_ada_record=packed_ada_record,
        packed_blackwell_record=packed_blackwell_record,
        packed_package_sha256=package,
        unpacked_package_sha256=package,
    )
    assert result["packed_unpacked_comparison"]["status"] == "verified"
    assert (
        result["packed_unpacked_comparison"][
            "direct_separate_packed_gpu_capture_recorded"
        ]
        is True
    )
    assert result["packed_unpacked_comparison"]["runtime"]["ada"][
        "packed_executable_sha256"
    ] == "1" * 64

    # The compare output must be directly consumable by the publisher; a
    # separately hand-authored summary is not the documented path.
    monkeypatch.setattr(publisher, "REPO_ROOT", tmp_path)
    status_path = tmp_path / "benchmarks" / "nw10" / "evidence-status.v1.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {
                "format": "nw10-evidence-status",
                "version": 1,
                "task": "NW-10",
                "evidence_status": "pending_physical_windows_capture",
                "contract_evidence": "verified_by_pytest",
                "physical_evidence": "pending",
                "reference_capture": "pending_reference_capture",
                "ada_correctness": "pending_unverified",
                "blackwell_correctness": "pending_unverified",
                "clean_system": "pending_unverified",
                "startup": "pending_unverified",
                "memory": "pending_unverified",
                "frame_time": "pending_unverified",
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    import argparse

    published = publisher.publish_correctness(
        argparse.Namespace(
            comparison=str(tmp_path / "out.json"),
            destination="benchmarks/nw10/common-artifact-correctness.v1.json",
            status_file="benchmarks/nw10/evidence-status.v1.json",
            implementation_head=result["source"]["implementation_head"],
            forbid_token=[],
        )
    )
    assert published["status"] == "verified"
    assert published["packed_unpacked_comparison"]["status"] == "verified"

    packed_blackwell["packaging_mode"] = "unpacked"
    packed_blackwell_record.write_text(
        json.dumps(packed_blackwell), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="not the declared packed artifact"):
        correctness.compare(
            ada_record,
            blackwell_record,
            ada_capture_dir=tmp_path / "ada",
            blackwell_capture_dir=tmp_path / "blackwell",
            output=tmp_path / "out2.json",
            packed_ada_capture_dir=tmp_path / "packed-ada",
            packed_blackwell_capture_dir=tmp_path / "packed-blackwell",
            packed_ada_record=packed_ada_record,
            packed_blackwell_record=packed_blackwell_record,
            packed_package_sha256=package,
            unpacked_package_sha256=package,
        )
