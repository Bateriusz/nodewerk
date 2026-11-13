from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from argparse import Namespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw05_export  # noqa: E402
import nwp_package as nwp  # noqa: E402
import verify_nw05  # noqa: E402


def _fixture(tmp_path: Path) -> tuple[Path, Path, bytes, str]:
    if os.name == "nt":
        pytest.skip("shell telemetry fixture is POSIX-only")
    package = nw04_fixture.build_compute_display_package()
    package_bytes = nwp.encode_package(package)
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    stem = nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)
    (artifact / f"{stem}.nwp").write_bytes(package_bytes)
    artifact_key = "sha256:v1:evidence-key"
    manifest = json.dumps({"artifact_identity": artifact_key}).encode("utf-8")
    player = tmp_path / "player"
    script = tmp_path / "player.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo NW05_LINK_MANIFEST_VERIFIED=1\n"
        "echo NW05_IMAGE_BYTES=1024\n"
        "echo NW05_IMAGE_NONZERO_BYTES=512\n"
        "echo NW05_IMAGE_FNV64=0123456789abcdef\n"
        "echo NW05_ADAPTER=\"desktop adapter\"\n"
        "echo NW05_DRIVER=1.0\n"
        "echo NW05_VENDOR_ID=1\n"
        "echo NW05_DEVICE_ID=1\n"
        "echo NW05_ALIASED_PLAN=1\n"
        "echo NW05_ALIAS_BARRIERS=2\n"
        "echo NW05_UAV_BARRIERS=1\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    player.write_bytes(
        nw05_export.embed_package(
            script.read_bytes(), package_bytes, manifest
        )
    )
    player.chmod(0o755)
    return artifact, player, package_bytes, artifact_key


def _record(
    artifact_key: str,
    package_sha: str,
    executable_sha: str,
    *,
    matching: bool = True,
) -> dict:
    other_key = artifact_key if matching else "sha256:v1:other"
    qualification = {
        "status": "verified",
        "artifact_key": other_key,
        "package_sha256": package_sha,
        "executable_sha256": executable_sha,
        "adapter_description": "desktop adapter",
        "hardware_identity_verified": True,
    }
    execution = {
        "hardware_tier": "ada",
        "execution_status": "passed",
        "hardware_identity_verified": True,
        "aliased_plan_realized": True,
        "alias_barrier_count": 2,
        "alias_activations": 2,
        "validation_enabled": True,
        "output_matches_non_aliased": True,
        "details": {
            "device": "RTX",
            "adapter_description": "desktop adapter",
            "driver_version": "1.0",
            "capabilities": {"wave_ops": True},
        },
    }
    blackwell_execution = dict(execution, hardware_tier="blackwell")
    return {
        "verification_status": "verified",
        "toolchain_manifest_sha256": "sha256:v1:test-toolchain",
        "qualifications": {
            "ada": dict(qualification),
            "blackwell": dict(qualification),
        },
        "executions": [execution, blackwell_execution],
    }


def test_verified_evidence_must_match_embedded_artifact(tmp_path: Path) -> None:
    artifact, player, package_bytes, artifact_key = _fixture(tmp_path)
    package_sha = hashlib.sha256(package_bytes).hexdigest()
    executable_sha = hashlib.sha256(player.read_bytes()).hexdigest()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            _record(
                artifact_key,
                package_sha,
                executable_sha,
                matching=False,
            )
        ),
        encoding="utf-8",
    )
    checks = verify_nw05._windows_checks(
        Namespace(
            fixture=str(player),
            package=str(artifact),
            evidence=str(evidence),
        )
    )
    assert checks[0].status == "passed"
    assert checks[1].status == "failed"
    assert "does not match" in checks[1].detail


def test_matching_verified_evidence_and_corrupt_json_are_distinct(
    tmp_path: Path,
) -> None:
    artifact, player, package_bytes, artifact_key = _fixture(tmp_path)
    package_sha = hashlib.sha256(package_bytes).hexdigest()
    executable_sha = hashlib.sha256(player.read_bytes()).hexdigest()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(_record(artifact_key, package_sha, executable_sha)),
        encoding="utf-8",
    )
    checks = verify_nw05._windows_checks(
        Namespace(
            fixture=str(player),
            package=str(artifact),
            evidence=str(evidence),
        )
    )
    assert checks[0].status == "passed"
    assert checks[1].status == "passed"

    evidence.write_text("{not json", encoding="utf-8")
    checks = verify_nw05._windows_checks(
        Namespace(
            fixture=str(player),
            package=str(artifact),
            evidence=str(evidence),
        )
    )
    assert checks[1].status == "failed"
    assert "not valid JSON" in checks[1].detail
