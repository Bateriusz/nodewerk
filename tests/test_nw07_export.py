from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw07_contract as contract  # noqa: E402
import nw07_export as export  # noqa: E402


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


def _template(directory: Path, size: int = 256) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    template = directory / "template.exe"
    template.write_bytes(b"MZ" + b"\x00" * size)
    return template


def _export(tmp_path: Path) -> dict:
    template = _template(tmp_path / "toolchain")
    return export.export_production(
        tmp_path / "artifact",
        player_template=template,
        dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
        report_path=tmp_path / "report.json",
        allow_test_payloads=True,
    )


def test_export_is_one_self_contained_release_closure(tmp_path: Path) -> None:
    report = _export(tmp_path)
    assert report["success"] is True
    assert report["release_grade"] is False
    assert report["test_payload"] is True
    artifact = report["artifact"]
    assert artifact["profile_id"] == contract.PROFILE_ID
    assert artifact["requirements_digest"] == contract.REQUIREMENTS_DIGEST
    assert artifact["init_opcodes"].count("init.dispatch") == 5
    assert "init.upload_resource" not in artifact["init_opcodes"]
    assert artifact["frame_opcodes"].count("frame.execute_indirect") == 1
    assert artifact["frame_opcodes"].count("frame.dispatch") == 2
    assert artifact["frame_opcodes"][-1] == "frame.present"
    assert "frame.copy_buffer" not in artifact["frame_opcodes"]
    assert artifact["has_readback_resource"] is False
    assert artifact["dependency_closure"]["runtime_dxc_dependency"] is False
    assert artifact["dependency_closure"]["runtime_slang_dependency"] is False
    assert artifact["dependency_closure"]["forbidden"] == []
    assert artifact["dependency_closure"]["gpu_name_predicates"] == []
    assert "nodewerk-studio" in " ".join(
        artifact["dependency_closure"]["authoring_infrastructure_excluded"]
    )
    size = artifact["size_accounting"]
    assert size["all_required_bytes_accounted"] is True
    assert size["complete_distribution_bytes"] == artifact["executable_bytes"]
    assert size["required_non_system_bytes"] == artifact["executable_bytes"]
    assert size["system_dependency_bytes"] == 0
    assert size["embedded_package_bytes"] == artifact["package_bytes"]
    assert size["embedded_manifest_bytes"] == artifact["manifest_bytes"]
    assert size["within_ceiling"] is True
    assert size["stretch_mandatory_gate"] is False
    assert report["distribution"]["single_executable"] is True
    assert report["distribution"]["runtime_dxc_required"] is False
    assert report["distribution"]["editor_required"] is False
    assert report["distribution"]["developer_runtime_required"] is False


def test_export_reports_stretch_without_gating_it(tmp_path: Path) -> None:
    report = _export(tmp_path)
    distribution = report["distribution"]
    assert distribution["stretch_ceiling"] == 131072
    assert distribution["stretch_mandatory_gate"] is False
    assert distribution["stretch_status"] in {
        "measured_within_stretch",
        "measured_above_stretch",
    }
    # The hard ceiling remains 262,144 regardless of the stretch result.
    assert distribution["ceiling"] == 262144
    assert distribution["within_ceiling"] is True


def test_complete_distribution_ceiling_fails_closed(tmp_path: Path) -> None:
    template = _template(tmp_path / "toolchain", size=262_144)
    with pytest.raises(export.Nw07ExportError):
        export.export_production(
            tmp_path / "artifact",
            player_template=template,
            dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
            allow_test_payloads=True,
        )


def test_failed_size_gate_leaves_previous_artifact_untouched(tmp_path: Path) -> None:
    first = _export(tmp_path)
    artifact_dir = tmp_path / "artifact"
    stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    previous_exe = (artifact_dir / f"{stem}.exe").read_bytes()
    huge_template = _template(tmp_path / "toolchain", size=262_144)
    with pytest.raises(export.Nw07ExportError):
        export.export_production(
            artifact_dir,
            player_template=huge_template,
            dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
            allow_test_payloads=True,
        )
    assert (artifact_dir / f"{stem}.exe").read_bytes() == previous_exe
    assert first["distribution"]["within_ceiling"] is True


def test_two_clean_directory_exports_have_matching_hashes(tmp_path: Path) -> None:
    template = _template(tmp_path / "toolchain")
    report = export.export_reproducibility(
        tmp_path / "repro",
        player_template=template,
        dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
        allow_test_payloads=True,
    )
    assert report["matching"] is True
    assert report["fields"]["package_sha256"]
    assert report["fields"]["manifest_sha256"]
    assert report["fields"]["executable_sha256"]
    assert report["build_inputs_digest"].startswith("sha256:v1:")


def test_published_artifact_identity_tracks_modified_executable(tmp_path: Path) -> None:
    _export(tmp_path)
    artifact_dir = tmp_path / "artifact"
    stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    exe = artifact_dir / f"{stem}.exe"
    before = export.inspect_artifact_directory(artifact_dir)["executable_sha256"]
    data = bytearray(exe.read_bytes())
    data[0] ^= 0xFF
    exe.write_bytes(bytes(data))
    after = export.inspect_artifact_directory(artifact_dir)["executable_sha256"]
    assert before != after


def test_published_artifact_identity_rejects_modified_package(tmp_path: Path) -> None:
    _export(tmp_path)
    artifact_dir = tmp_path / "artifact"
    stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    package = artifact_dir / f"{stem}.nwp"
    data = bytearray(package.read_bytes())
    data[-1] ^= 0xFF
    package.write_bytes(bytes(data))
    with pytest.raises(export.Nw07ExportError):
        export.inspect_artifact_directory(artifact_dir)


def test_release_report_schema_validates(tmp_path: Path) -> None:
    _export(tmp_path)
    report_path = tmp_path / "report.json"
    from schema_validator import validate_file_against_schema

    validate_file_against_schema(
        report_path, REPO_ROOT / "schemas" / "nw07-export-report.schema.json"
    )


def test_export_report_contains_pinned_build_inputs_and_toolchain(tmp_path: Path) -> None:
    report = _export(tmp_path)
    inputs = report["build_inputs"]
    assert inputs["files"]["fixtures/nw07/production_loop.nwgraph"]
    assert inputs["files"]["tools/nw07_export.py"]
    assert inputs["digest"].startswith("sha256:v1:")
    assert inputs["toolchain"]["pinned_dxc_version"] == nwp.PINNED_DXC_VERSION
    assert inputs["toolchain"]["toolchain_manifest_sha256"]
    assert report["graph"]["fixed_tick_samples"]
    assert report["graph"]["loop_boundary"]["seamless_modulo_turn"] is True
