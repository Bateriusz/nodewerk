from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw05_evidence  # noqa: E402
import nw05_export  # noqa: E402
import nwp_package as nwp  # noqa: E402


def _artifact_directory(tmp_path: Path) -> tuple[Path, bytes, bytes]:
    package = nw04_fixture.build_compute_display_package()
    package_bytes = nwp.encode_package(package)
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / f"{nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)}.nwp").write_bytes(
        package_bytes
    )
    manifest = json.dumps(
        {"artifact_identity": "sha256:v1:test-artifact"}
    ).encode("utf-8")
    return directory, package_bytes, manifest


def _player(tmp_path: Path, name: str, binary: Path, package: bytes, manifest: bytes) -> Path:
    player = tmp_path / name
    player.write_bytes(nw05_export.embed_package(binary.read_bytes(), package, manifest))
    player.chmod(0o755)
    return player


def test_record_execution_requires_real_pass_and_preserves_failures(
    tmp_path: Path,
) -> None:
    true_binary = shutil.which("true")
    false_binary = shutil.which("false")
    if true_binary is None or false_binary is None:
        pytest.skip("POSIX true/false executables are unavailable")
    directory, package_bytes, manifest = _artifact_directory(tmp_path)
    passing = _player(tmp_path, "passing", Path(true_binary), package_bytes, manifest)
    failing = _player(tmp_path, "failing", Path(false_binary), package_bytes, manifest)
    record_path = tmp_path / "evidence.json"
    record = nw05_evidence.record_execution(
        passing,
        directory,
        tier="ada",
        status="passed",
        exit_code=0,
        details={
            "device": "RTX 4090",
            "adapter_description": "NVIDIA GeForce RTX 4090",
            "driver_version": "1.2.3",
            "capabilities": {"wave_ops": True},
        },
        output_path=record_path,
    )
    assert record["qualifications"]["ada"]["status"] == "verified"
    assert record["qualifications"]["ada"]["artifact_key"] == "sha256:v1:test-artifact"

    record = nw05_evidence.record_execution(
        passing,
        directory,
        tier="blackwell",
        status="passed",
        exit_code=0,
        details={"adapter_description": "missing telemetry"},
        output_path=record_path,
    )
    # Missing device/driver/capabilities telemetry cannot fabricate a pass.
    assert record["qualifications"]["blackwell"]["status"] == "failed"
    assert record["verification_status"] == "executed_failure"

    record = nw05_evidence.record_execution(
        failing,
        directory,
        tier="blackwell",
        status="failed",
        exit_code=1,
        details={},
        output_path=record_path,
    )
    assert record["qualifications"]["blackwell"]["status"] == "failed"
    # A later pass on the other tier must not erase the executed failure.
    record = nw05_evidence.record_execution(
        passing,
        directory,
        tier="ada",
        status="passed",
        exit_code=0,
        details={
            "device": "RTX 4090",
            "adapter_description": "NVIDIA GeForce RTX 4090",
            "driver_version": "1.2.3",
            "capabilities": {"wave_ops": True},
        },
        output_path=record_path,
    )
    assert record["verification_status"] == "executed_failure"


def test_record_execution_rejects_status_mismatch(tmp_path: Path) -> None:
    true_binary = shutil.which("true")
    if true_binary is None:
        pytest.skip("POSIX true executable is unavailable")
    directory, package_bytes, manifest = _artifact_directory(tmp_path)
    passing = _player(tmp_path, "passing", Path(true_binary), package_bytes, manifest)
    record = nw05_evidence.record_execution(
        passing,
        directory,
        tier="ada",
        status="failed",
        exit_code=1,
        details={},
        output_path=tmp_path / "evidence.json",
    )
    assert record["qualifications"]["ada"]["status"] == "failed"
