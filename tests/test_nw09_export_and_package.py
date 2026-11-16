from __future__ import annotations

import json
from dataclasses import replace
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw04_fixture  # noqa: E402
import nw05_export  # noqa: E402
import nw09_export  # noqa: E402
import nw09_temporal as temporal  # noqa: E402
import nwp_package as nwp  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402


PARTICLES = REPO_ROOT / "fixtures" / "nw09" / "particles_seeded.nwgraph"
FEEDBACK = REPO_ROOT / "fixtures" / "nw09" / "feedback_rect.nwgraph"
STATELESS = REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph"
BASE_COMMIT = "60d106fde9d5b70f1d53aae89096595eeeb7b8c1"


@pytest.fixture(autouse=True)
def _test_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")


def _mock_dxc(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dxc = directory / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
        "fo.parent.mkdir(parents=True, exist_ok=True)\n"
        f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\n",
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    return dxc


def _template(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    template = directory / "template.exe"
    template.write_bytes(b"MZ" + b"\x00" * 256)
    return template


def _settings(tmp_path: Path) -> nw05_export.ExportSettings:
    return nw05_export.ExportSettings(
        byte_ceiling=16 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        player_template=str(_template(tmp_path / "toolchain")),
    )


def _export(tmp_path: Path, graph: Path, name: str):
    return nw05_export.export_graph(
        graph,
        tmp_path / name,
        settings=_settings(tmp_path),
        dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
        allow_test_payloads=True,
    )


@pytest.mark.parametrize("fixture", [PARTICLES, FEEDBACK])
def test_stateful_export_uses_explicit_versioned_extension(
    tmp_path: Path, fixture: Path
) -> None:
    result = _export(tmp_path, fixture, "artifact")
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    package = artifact.package
    assert package.version_minor == nwp.VERSION_MINOR_STATEFUL
    assert package.history
    assert all(
        record.current_resource_id != record.previous_resource_id
        for record in package.history
    )
    bytes_value = artifact.package_bytes
    assert struct.unpack_from("<H", bytes_value, 10)[0] == 1
    assert b"HIST" in bytes_value
    decoded = nwp.decode_package(bytes_value)
    assert nwp.encode_package(decoded) == bytes_value
    assert result.report["fixed_step"]["timeline_ticks_per_simulation_tick"] == 400
    assert result.report["frames_in_flight"] == 2
    assert result.report["pending_retirement_bytes"] >= (
        sum(record.state_bytes for record in package.history) * 2
    )
    scheduled = {
        resource.resource_id: resource
        for resource in artifact.logical_schedule.resources
    }
    for record in package.history:
        current = scheduled[record.current_resource_id]
        previous = scheduled[record.previous_resource_id]
        assert current.resource_id != previous.resource_id
        assert current.lifetime.persistent is True
        assert previous.lifetime.persistent is True
        assert current.alias_eligible is False
        assert previous.alias_eligible is False
        assert artifact.logical_schedule.frames_in_flight == 2


def test_packaged_release_checkpoints_report_byte_cost(tmp_path: Path) -> None:
    report = nw09_export.export_stateful_graph(
        PARTICLES,
        tmp_path / "stateful",
        allow_test_payloads=True,
        packaged_checkpoint_ticks=(0, 4000),
        report_path=tmp_path / "stateful" / "report.json",
    )
    assert report["packaged_checkpoint_byte_cost"] > 0
    assert len(report["packaged_checkpoints"]) == 2
    for item in report["packaged_checkpoints"]:
        assert item["byte_cost"] > 0
        assert item["portable_gpu_state_claim"] is False
    package = nwp.decode_package(Path(report["package_path"]).read_bytes())
    assert package.release_checkpoints
    assert package.version_minor == nwp.VERSION_MINOR_STATEFUL


def test_packaged_checkpoint_is_runtime_compatible(tmp_path: Path) -> None:
    report = nw09_export.export_stateful_graph(
        PARTICLES,
        tmp_path / "stateful",
        allow_test_payloads=True,
        packaged_checkpoint_ticks=(0, 4000),
    )
    package = nwp.decode_package(Path(report["package_path"]).read_bytes())
    runtime = temporal.TemporalRuntime(nw03.load_graph(PARTICLES))
    for record in package.release_checkpoints:
        checkpoint = temporal.Checkpoint.from_dict(json.loads(record.payload))
        runtime.advance_to(checkpoint.tick)
        compatibility = runtime.checkpoint_compatibility(checkpoint)
        assert compatibility.compatible, compatibility.reason
        assert checkpoint.semantic_digest != "deferred-to-runtime-semantic-digest"
        assert checkpoint.state_layout_hash.startswith("sha256:v1:")


def test_stateful_package_executes_initial_upload_and_history_dispatch(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path, PARTICLES, "artifact")
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    assert package.history
    record = package.history[0]
    assert record.initial_state
    assert len(record.initial_state) == record.state_bytes
    for resource_id in (record.current_resource_id, record.previous_resource_id):
        resource = next(
            item for item in package.resources if item.resource_id == resource_id
        )
        assert resource.format == "DXGI_FORMAT_R32_UINT"
        assert resource.size_bytes == record.state_bytes
    uploads = [
        instruction
        for instruction in package.initialization
        if instruction.opcode == "init.upload_history_state"
    ]
    assert len(uploads) == 2
    assert any(
        instruction.opcode == "frame.dispatch" for instruction in package.frame
    )


def test_stateful_export_does_not_use_synthetic_fixture_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("stateful export must compile the supplied document")

    monkeypatch.setattr(
        nw04_fixture, "build_compute_display_package", forbidden, raising=False
    )
    result = _export(tmp_path, FEEDBACK, "artifact")
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    assert package.history
    assert package.shaders
    assert any(
        instruction.opcode == "init.upload_history_state"
        for instruction in package.initialization
    )


def test_export_settings_can_request_packaged_checkpoints(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path), packaged_checkpoint_ticks=(0, 4000)
    )
    result = nw05_export.export_graph(
        PARTICLES,
        tmp_path / "artifact",
        settings=settings,
        dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
        allow_test_payloads=True,
    )
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    assert artifact.package.release_checkpoints
    assert result.report["checkpoint_byte_cost"] > 0
    assert artifact.size_report["stateful"]["checkpoint_byte_cost"] > 0


def test_presentation_geometry_survives_stateful_export(tmp_path: Path) -> None:
    result = _export(tmp_path, FEEDBACK, "artifact")
    assert nw09_export.present_geometry(
        nw03.load_graph(FEEDBACK)
    ) == (320, 180)
    assert result.report["present_geometry"] == {"width": 320, "height": 180}
    manifest = json.loads(
        (tmp_path / "artifact" / "link-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["present_geometry"] == {"width": 320, "height": 180}
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    geometry = {
        parameter.name: struct.unpack("<I", parameter.value)[0]
        for parameter in package.parameters
        if parameter.name in {"nw05.present.width", "nw05.present.height"}
    }
    assert geometry == {"nw05.present.width": 320, "nw05.present.height": 180}


def test_history_free_export_keeps_v1_baseline_and_links_no_history(tmp_path: Path) -> None:
    first = _export(tmp_path, STATELESS, "first")
    second = _export(tmp_path, STATELESS, "second")
    a = first.artifacts[nwp.COMMON_PROFILE_ID].package
    b = second.artifacts[nwp.COMMON_PROFILE_ID].package
    assert a.version_minor == 0
    assert not a.history and not a.release_checkpoints
    assert nwp.encode_package(a) == nwp.encode_package(b)
    payload = first.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    assert b"HIST" not in payload
    assert b"CKPT" not in payload
    manifest = json.loads(
        (tmp_path / "first" / "link-manifest.json").read_text(encoding="utf-8")
    )
    retained = json.dumps(manifest.get("retained", {})).lower()
    assert "nw09" not in retained
    assert "checkpoint" not in retained
    assert "nw09_temporal" not in retained


def _base_export(tmp_path: Path) -> bytes:
    archive = tmp_path / "base"
    archive.mkdir()
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", BASE_COMMIT],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("base commit is unavailable in this checkout")
    subprocess.run(
        ["tar", "-x", "-C", str(archive)],
        input=proc.stdout,
        check=True,
    )
    script = archive / "export_base.py"
    container_hex = nw04_fixture.make_dxil_container().hex()
    script.write_text(
        "\n".join(
            [
                "import sys, pathlib",
                "sys.path.insert(0, str(pathlib.Path(__file__).parent / 'tools'))",
                "import nw05_export, nwp_package",
                "root = pathlib.Path(__file__).parent",
                "dxc = root / 'mock_dxc.py'",
                f"container = bytes.fromhex({container_hex!r})",
                "mock = 'import sys\\nfrom pathlib import Path\\n'",
                "mock += \"if '--version' in sys.argv:\\n print('dxcompiler.dll: 1.8 - 1.8.2407.12')\\n sys.exit(0)\\n\"",
                "mock += \"fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\\n\"",
                "mock += \"fo.write_bytes(bytes.fromhex('\" + container.hex() + \"'))\\n\"",
                "dxc.write_text(mock)",
                "template = root / 'template.exe'",
                "template.write_bytes(b'MZ' + b'\\x00' * 256)",
                "result = nw05_export.export_graph(",
                "    root / 'fixtures' / 'nw05' / 'procedural_image.nwgraph',",
                "    root / 'artifact',",
                "    settings=nw05_export.ExportSettings(player_template=str(template)),",
                "    dxc_executable=str(dxc),",
                "    allow_test_payloads=True,",
                ")",
                "print(nwp_package.sha256_hex(result.artifacts[nwp_package.COMMON_PROFILE_ID].package_bytes))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["NODEWERK_NW03_TEST_DIGEST"] = "1"
    env["PYTHONPATH"] = str(archive / "tools")
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().encode("ascii")


def test_stateless_export_is_byte_identical_to_base_commit(tmp_path: Path) -> None:
    base_hex = _base_export(tmp_path)
    result = _export(tmp_path / "current", STATELESS, "artifact")
    current = result.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    assert hashlib_hex(current).encode("ascii") == base_hex


def hashlib_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_cross_device_evidence_is_pending_and_schema_valid() -> None:
    validate_file_against_schema(
        REPO_ROOT / "benchmarks" / "nw09" / "hardware-evidence.json",
        REPO_ROOT / "schemas" / "nw09-hardware-evidence.schema.json",
    )
    payload = json.loads(
        (REPO_ROOT / "benchmarks" / "nw09" / "hardware-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["status"] == "pending_unverified"
    assert payload["physical_coverage_available"] is False
    assert payload["portable_gpu_state_replay"] is False
    assert {row["tier"] for row in payload["devices"]} == {
        "desktop_ada",
        "desktop_blackwell",
    }
    assert all(
        row["status"] == "pending_unverified" for row in payload["devices"]
    )


def test_checkpoint_and_stateful_report_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = temporal.TemporalRuntime(nw03.load_graph(PARTICLES))
    runtime.advance_to(400 * 4)
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(
        json.dumps(runtime.checkpoint().to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    validate_file_against_schema(
        checkpoint_path,
        REPO_ROOT / "schemas" / "nw09-checkpoint.schema.json",
    )
    report = nw09_export.export_stateful_graph(
        PARTICLES,
        tmp_path / "stateful",
        allow_test_payloads=True,
        report_path=tmp_path / "stateful" / "report.json",
    )
    validate_file_against_schema(
        tmp_path / "stateful" / "report.json",
        REPO_ROOT / "schemas" / "nw09-export-report.schema.json",
    )
    assert report["package_version"] == {"major": 1, "minor": 1}

