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


def _probe(tier: str = "ada") -> dict:
    name = (
        "NVIDIA GeForce RTX 4090"
        if tier == "ada"
        else "NVIDIA GeForce RTX 5090"
    )
    return {
        "source": "test-probe",
        "adapters": [
            {
                "device": name,
                "adapter_description": name,
                "driver_version": "1.2.3",
                "pci_identifier": "0000:01:00.0",
                "capabilities": {"compute_capability": "8.9"},
                "hardware_tier": tier,
            }
        ],
    }


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


def _telemetry_player(
    tmp_path: Path,
    name: str,
    package: bytes,
    manifest: bytes,
    *,
    exit_code: int = 0,
) -> Path:
    script = tmp_path / f"{name}.sh"
    script.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--force-non-aliased-plan\" ] || "
        "echo \"$*\" | grep -q -- \"--force-non-aliased-plan\"; then\n"
        "  alias=0; barriers=0\n"
        "else\n"
        "  alias=1; barriers=2\n"
        "fi\n"
        f"echo NW05_LINK_MANIFEST_VERIFIED=1\n"
        f"echo NW05_IMAGE_BYTES=1024\n"
        f"echo NW05_IMAGE_NONZERO_BYTES=512\n"
        f"echo NW05_IMAGE_FNV64=0123456789abcdef\n"
        f"echo NW05_ADAPTER=\"{'NVIDIA GeForce RTX 4090' if name.startswith('ada') else 'NVIDIA GeForce RTX 5090'}\"\n"
        f"echo NW05_DRIVER=1.2.3\n"
        f"echo NW05_VENDOR_ID=4318\n"
        f"echo NW05_DEVICE_ID=1\n"
        "echo NW05_ALIASED_PLAN=$alias\n"
        "echo NW05_ALIAS_BARRIERS=$barriers\n"
        "echo NW05_UAV_BARRIERS=1\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return _player(tmp_path, name, script, package, manifest)


def test_record_execution_requires_real_pass_and_preserves_failures(
    tmp_path: Path,
) -> None:
    true_binary = shutil.which("true")
    false_binary = shutil.which("false")
    if true_binary is None or false_binary is None:
        pytest.skip("POSIX true/false executables are unavailable")
    directory, package_bytes, manifest = _artifact_directory(tmp_path)
    passing = _telemetry_player(tmp_path, "ada-passing", package_bytes, manifest)
    failing = _player(tmp_path, "failing", Path(false_binary), package_bytes, manifest)
    record_path = tmp_path / "evidence.json"
    record = nw05_evidence.record_execution(
        _player(tmp_path, "true-no-telemetry", Path(true_binary), package_bytes, manifest),
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
        probe=lambda: _probe("ada"),
    )
    # An executable that produces no alias/output telemetry is never verified.
    assert record["qualifications"]["ada"]["status"] == "failed"

    # Start from a clean record for the genuine telemetry-backed executions.
    record_path.unlink()
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
        probe=lambda: _probe("ada"),
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
        probe=lambda: {"source": "unavailable", "adapters": []},
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
        probe=lambda: _probe("blackwell"),
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
        probe=lambda: _probe("ada"),
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
        probe=lambda: _probe("ada"),
    )
    assert record["qualifications"]["ada"]["status"] == "failed"
