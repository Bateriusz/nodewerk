from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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
    emit_telemetry: bool = True,
) -> Path:
    adapter = (
        "NVIDIA GeForce RTX 4090"
        if name.startswith("ada")
        else "NVIDIA GeForce RTX 5090"
    )
    if os.name == "nt":
        script = tmp_path / f"{name}.cmd"
        body = (
            "@echo off\r\n"
            "set \"ALIAS=1\"\r\n"
            "set \"BARRIERS=2\"\r\n"
            "echo %* | findstr /C:\"--force-non-aliased-plan\" >nul\r\n"
            "if not errorlevel 1 (\r\n"
            "  set \"ALIAS=0\"\r\n"
            "  set \"BARRIERS=0\"\r\n"
            ")\r\n"
        )
        if emit_telemetry:
            body += (
                "echo NW05_LINK_MANIFEST_VERIFIED=1\r\n"
                "echo NW05_IMAGE_BYTES=1024\r\n"
                "echo NW05_IMAGE_NONZERO_BYTES=512\r\n"
                "echo NW05_IMAGE_FNV64=0123456789abcdef\r\n"
                f"echo NW05_ADAPTER={adapter}\r\n"
                "echo NW05_DRIVER=1.2.3\r\n"
                "echo NW05_VENDOR_ID=4318\r\n"
                "echo NW05_DEVICE_ID=1\r\n"
                "echo NW05_ALIASED_PLAN=%ALIAS%\r\n"
                "echo NW05_ALIAS_BARRIERS=%BARRIERS%\r\n"
                "echo NW05_UAV_BARRIERS=1\r\n"
            )
        body += f"exit /b {exit_code}\r\n"
    else:
        script = tmp_path / f"{name}.sh"
        body = (
            "#!/bin/sh\n"
            "if echo \"$*\" | grep -q -- \"--force-non-aliased-plan\"; then\n"
            "  alias=0; barriers=0\n"
            "else\n"
            "  alias=1; barriers=2\n"
            "fi\n"
        )
        if emit_telemetry:
            body += (
                "echo NW05_LINK_MANIFEST_VERIFIED=1\n"
                "echo NW05_IMAGE_BYTES=1024\n"
                "echo NW05_IMAGE_NONZERO_BYTES=512\n"
                "echo NW05_IMAGE_FNV64=0123456789abcdef\n"
                f"echo NW05_ADAPTER=\"{adapter}\"\n"
                "echo NW05_DRIVER=1.2.3\n"
                "echo NW05_VENDOR_ID=4318\n"
                "echo NW05_DEVICE_ID=1\n"
                "echo NW05_ALIASED_PLAN=$alias\n"
                "echo NW05_ALIAS_BARRIERS=$barriers\n"
                "echo NW05_UAV_BARRIERS=1\n"
            )
        body += f"exit {exit_code}\n"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    executable_name = f"{name}.cmd" if os.name == "nt" else name
    return _player(tmp_path, executable_name, script, package, manifest)


def test_record_execution_requires_real_pass_and_preserves_failures(
    tmp_path: Path,
) -> None:
    directory, package_bytes, manifest = _artifact_directory(tmp_path)
    passing = _telemetry_player(tmp_path, "ada-passing", package_bytes, manifest)
    failing = _telemetry_player(
        tmp_path, "failing", package_bytes, manifest, exit_code=1
    )
    record_path = tmp_path / "evidence.json"
    record = nw05_evidence.record_execution(
        _telemetry_player(
            tmp_path,
            "no-telemetry",
            package_bytes,
            manifest,
            emit_telemetry=False,
        ),
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
    directory, package_bytes, manifest = _artifact_directory(tmp_path)
    passing = _telemetry_player(tmp_path, "ada-passing", package_bytes, manifest)
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
