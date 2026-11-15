from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw08_editor as editor  # noqa: E402
import nw08_studio as studio  # noqa: E402
import nwp_package as nwp  # noqa: E402


COMMON_PROFILE = "nw.common.d3d12.fl12_0.sm6_0.v1"
COMMON_DIGEST = (
    "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a"
)


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


def test_proof_graph_roundtrip_and_editor_metadata_are_semantic_neutral() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    assert session.document.profile_id == COMMON_PROFILE
    assert session.document.requirements_digest == COMMON_DIGEST
    baseline = session.semantic_digest

    emitted = session.source_text
    parsed, diagnostics = editor.parse_graph_text(emitted, source_name="<roundtrip>")
    assert parsed is not None
    assert not [item for item in diagnostics if item.get("severity") == "error"]
    assert editor.semantic_digest(parsed)[0] == baseline

    session.set_layout("texture", 123.0, 456.0)
    session.select("texture", "camera")
    session.scrub(480_000)
    assert session.semantic_digest == baseline

    emitted_after = session.source_text
    parsed_after, _ = editor.parse_graph_text(emitted_after)
    assert editor.semantic_digest(parsed_after)[0] == baseline


def test_parameter_edit_recomputes_only_affected_branch() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    result = session.set_parameter("texture", "seed", "1297036694")
    assert result.valid
    assert result.semantic_digest != result.previous_semantic_digest
    assert set(result.changed_nodes) == {
        "material",
        "render",
        "screen",
        "texture",
        "texture_gpu",
        "bloom",
        "tonemap",
    }
    assert set(result.recomputed_nodes) == set(result.changed_nodes)
    assert set(result.cached_nodes) == {
        "camera",
        "instances",
        "mesh_gpu",
        "scene_gpu",
        "sun",
        "torus",
        "twist",
    }
    assert "render" in result.affected_passes
    assert "texture_gpu" in result.recomputed_resources
    assert "mesh_gpu" not in result.recomputed_resources

    camera_edit = session.set_parameter("camera", "fov_degrees", "55")
    assert camera_edit.valid
    assert "render" in camera_edit.recomputed_nodes
    assert "texture" in camera_edit.cached_nodes
    assert "torus" in camera_edit.cached_nodes


def test_curve_and_connection_editing_with_undo() -> None:
    proof = editor.GraphEditorSession.create_nw07_proof()
    curve = proof.set_curve_points(
        "camera",
        [editor.CurvePoint(0, 0.0), editor.CurvePoint(480_000, 3.141592653589793)],
    )
    assert curve.valid
    assert "0:0;480000:3.141592653589793" in proof.source_text
    assert proof.curve_points("camera")[1].value == pytest.approx(3.141592653589793)

    session = editor.GraphEditorSession.open(
        REPO_ROOT / "fixtures" / "nw08" / "shader_edit_probe.nwgraph"
    )
    added = session.add_node(
        "shader_alt",
        "nw.shader.expr",
        params={"expression": "vec4(1.0, 0.0, 0.0, 1.0)"},
    )
    assert added.valid
    connected = session.connect("fullscreen", "shader", "shader_alt", "out")
    assert connected.valid
    assert connected.changed_nodes == ("fullscreen",)
    rows = [
        row
        for row in session.connections()
        if row["target_node"] == "fullscreen" and row["target_port"] == "shader"
    ]
    assert rows == [
        {
            "target_node": "fullscreen",
            "target_port": "shader",
            "source_node": "shader_alt",
            "source_port": "out",
        }
    ]
    session.undo()
    rows = [
        row
        for row in session.connections()
        if row["target_node"] == "fullscreen" and row["target_port"] == "shader"
    ]
    assert rows[0]["source_node"] == "shader"
    session.redo()
    rows = [
        row
        for row in session.connections()
        if row["target_node"] == "fullscreen" and row["target_port"] == "shader"
    ]
    assert rows[0]["source_node"] == "shader_alt"


def test_timeline_scrub_is_absolute_and_seek_stable() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    first = session.set_timeline_tick(240_000)
    assert first.valid
    state = session.timeline_state()
    assert state.tick == 240_000
    assert state.duration_ticks == 960_000
    assert state.tick_rate == 48_000
    assert state.camera_angle_radians == pytest.approx(1.5707963267948966)
    session.set_timeline_tick(0)
    session.set_timeline_tick(240_000)
    assert session.timeline_state().camera_angle_radians == pytest.approx(
        state.camera_angle_radians
    )


def test_gui_and_cli_exports_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    toolchain = tmp_path / "toolchain"
    dxc = _mock_dxc(toolchain)
    template = _template(toolchain)
    session = editor.GraphEditorSession.create_nw07_proof()
    session.set_parameter("camera", "fov_degrees", "55")
    emitted = tmp_path / "edited.nwgraph"
    session.save(emitted)

    gui_result = studio.StudioApplication.create(
        emitted, gpu_cache_budget_bytes=1024 * 1024 * 1024
    ).export(
        tmp_path / "gui-export",
        settings={
            "player_template": str(template),
        },
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    cli = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "nodewerkc.py"),
            "export",
            str(emitted),
            str(tmp_path / "cli-export"),
            "--dxc",
            str(dxc),
            "--player-template",
            str(template),
            "--allow-test-payloads",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "NODEWERK_NW03_TEST_DIGEST": "1"},
    )
    assert cli.returncode == 0, cli.stdout + cli.stderr

    stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    gui_artifact = gui_result.artifacts[nwp.COMMON_PROFILE_ID]
    gui_package = (tmp_path / "gui-export" / f"{stem}.nwp").read_bytes()
    cli_package = (tmp_path / "cli-export" / f"{stem}.nwp").read_bytes()
    assert gui_package == cli_package == gui_artifact.package_bytes
    assert hashlib.sha256(gui_package).hexdigest() == hashlib.sha256(
        cli_package
    ).hexdigest()
    gui_manifest = (tmp_path / "gui-export" / f"{stem}.manifest.json").read_bytes()
    cli_manifest = (tmp_path / "cli-export" / f"{stem}.manifest.json").read_bytes()
    assert gui_manifest == cli_manifest
    gui_exe = (tmp_path / "gui-export" / f"{stem}.exe").read_bytes()
    cli_exe = (tmp_path / "cli-export" / f"{stem}.exe").read_bytes()
    assert gui_exe == cli_exe


def test_editor_only_additions_do_not_change_release_inputs() -> None:
    import nw07_contract
    import nw07_export

    inputs = nw07_export.verify_pinned_inputs(
        nw07_export.load_canonical_config()
    )
    assert inputs["matches_frozen_digest"] is True
    pinned = set(nw07_contract.PINNED_BUILD_INPUTS)
    assert not any(path.startswith("tools/nw08_") for path in pinned)
    assert not any(path.startswith("src/studio/") for path in pinned)
    document = json.loads(
        (REPO_ROOT / "benchmarks" / "nw07" / "canonical-production.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert document["build_inputs_digest"] == inputs["frozen_digest"]


def test_layout_and_selection_leave_release_bytes_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    dxc = _mock_dxc(tmp_path / "toolchain")
    template = _template(tmp_path / "toolchain")
    base = editor.GraphEditorSession.create_nw07_proof()
    edited = editor.GraphEditorSession.create_nw07_proof()
    edited.set_layout("texture", 999.0, 444.0)
    edited.select("camera", "render")
    edited.scrub(123_456)
    assert base.semantic_digest == edited.semantic_digest

    first = base.export(
        tmp_path / "base-export",
        settings={"player_template": str(template)},
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    second = edited.export(
        tmp_path / "layout-export",
        settings={"player_template": str(template)},
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    assert (tmp_path / "base-export" / f"{stem}.nwp").read_bytes() == (
        tmp_path / "layout-export" / f"{stem}.nwp"
    ).read_bytes()
    assert (tmp_path / "base-export" / f"{stem}.exe").read_bytes() == (
        tmp_path / "layout-export" / f"{stem}.exe"
    ).read_bytes()
    assert (
        first.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
        == second.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    )


def test_studio_cli_and_nodewerkc_cli_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    dxc = _mock_dxc(tmp_path / "toolchain")
    template = _template(tmp_path / "toolchain")
    session = editor.GraphEditorSession.create_nw07_proof()
    session.set_parameter("camera", "fov_degrees", "55")
    graph = tmp_path / "edited.nwgraph"
    session.save(graph)
    env = {**os.environ, "NODEWERK_NW03_TEST_DIGEST": "1"}
    studio_process = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "nw08_studio.py"),
            "export",
            "--graph",
            str(graph),
            "--output",
            str(tmp_path / "studio-export"),
            "--dxc",
            str(dxc),
            "--player-template",
            str(template),
            "--allow-test-payloads",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert studio_process.returncode == 0, studio_process.stderr
    cli_process = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "nodewerkc.py"),
            "export",
            str(graph),
            str(tmp_path / "nodewerkc-export"),
            "--dxc",
            str(dxc),
            "--player-template",
            str(template),
            "--allow-test-payloads",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert cli_process.returncode == 0, cli_process.stderr
    stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    for suffix in (".nwp", ".exe", ".manifest.json"):
        assert (tmp_path / "studio-export" / f"{stem}{suffix}").read_bytes() == (
            tmp_path / "nodewerkc-export" / f"{stem}{suffix}"
        ).read_bytes()

