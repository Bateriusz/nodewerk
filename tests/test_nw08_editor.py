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
import nw08_preview as preview  # noqa: E402
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


def test_identical_assignment_is_a_noop_not_an_edit() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    baseline = session.semantic_digest
    assert session.can_undo is False
    result = session.set_parameter("texture", "seed", "1297036693")
    assert result.valid is True
    assert result.message == "no change"
    assert result.semantic_digest == baseline
    assert result.previous_semantic_digest == baseline
    assert result.changed_nodes == ()
    assert result.recomputed_nodes == ()
    assert session.semantic_digest == baseline
    assert session.can_undo is False

    # Reconnecting an identical edge is likewise a no-op.
    same = session.connect("texture_gpu", "texture", "texture", "out")
    assert same.valid is True
    assert same.message == "no change"
    assert same.recomputed_nodes == ()
    assert session.can_undo is False


def test_gui_curve_keyframe_binding_edits_canonical_keys_parameter() -> None:
    from nw08_gui import CurveEditorBinding

    application = studio.StudioApplication.create()
    binding = CurveEditorBinding(application)
    node_id = "camera"
    baseline = application.session.semantic_digest
    assert binding.supports(node_id)

    added = binding.add(node_id, 120_000, 0.75)
    assert added.valid
    assert application.session.semantic_digest != baseline
    points = binding.points(node_id)
    assert editor.CurvePoint(120_000, 0.75) in points

    moved = binding.update(node_id, 120_000, 180_000, 1.25)
    assert moved.valid
    points = binding.points(node_id)
    assert editor.CurvePoint(180_000, 1.25) in points
    assert editor.CurvePoint(120_000, 0.75) not in points

    removed = binding.remove(node_id, 180_000)
    assert removed.valid
    assert editor.CurvePoint(180_000, 1.25) not in binding.points(node_id)
    assert "keys" in application.session.parameter_names(node_id)


def test_semantic_digest_uses_nw02_identity_when_helper_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nw03_compiler

    native = "sha256:v1:" + "a" * 64
    monkeypatch.setattr(nw03_compiler, "_native_nw02_digest", lambda _doc: native)
    session = editor.GraphEditorSession.create_nw07_proof()
    assert session.semantic_digest == native
    assert session.semantic_digest_provenance == "nw02"
    assert session.semantic_digest_canonical is True


def test_semantic_digest_fallback_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nw03_compiler

    monkeypatch.setattr(nw03_compiler, "_native_nw02_digest", lambda _doc: None)
    session = editor.GraphEditorSession.create_nw07_proof()
    assert session.semantic_digest_provenance == "python-fallback"
    assert session.semantic_digest_canonical is False
    assert session.semantic_digest_status.reason


def test_semantic_digest_does_not_swallow_helper_defects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nw03_compiler

    def _broken(_document):
        raise RuntimeError("native helper crashed")

    monkeypatch.setattr(nw03_compiler, "_native_nw02_digest", _broken)
    with pytest.raises(RuntimeError):
        editor.GraphEditorSession.create_nw07_proof()


def test_studio_application_uses_actual_or_unavailable_host_identity() -> None:
    import nw08_cache

    application = studio.StudioApplication.create()
    generation = application.report()["preview"]["device"]["generation"]
    if os.name != "nt":
        assert generation["adapter"]["vendor_id"] == 0
        assert generation["adapter"]["device_id"] == 0
        assert application.report()["preview"]["device"]["adapter_available"] is False

    explicit = studio.StudioApplication.create(
        adapter=nw08_cache.AdapterDeviceIdentity(
            vendor_id=0x1002,
            device_id=0x744C,
            subsystem_id=7,
            adapter_luid="explicit-luid",
        ),
        driver_version="31.0.1234.5",
        os_build="10.0.26100.1",
        toolchain=nw08_cache.ToolchainIdentity(os_build="10.0.26100.1"),
    )
    explicit_generation = explicit.report()["preview"]["device"]["generation"]
    assert explicit_generation["adapter"]["vendor_id"] == 0x1002
    assert explicit_generation["adapter"]["device_id"] == 0x744C
    assert explicit_generation["driver_version"] == "31.0.1234.5"
    assert explicit_generation["os_build"] == "10.0.26100.1"
    assert explicit.report()["preview"]["device"]["adapter_available"] is True


def test_studio_configure_export_drives_the_existing_export_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    dxc = _mock_dxc(tmp_path / "toolchain")
    template = _template(tmp_path / "toolchain")
    application = studio.StudioApplication.create(allow_test_payloads=True)
    application.configure_export(
        dxc_executable=str(dxc),
        player_template=str(template),
        cache_dir=str(tmp_path / "compiler-cache"),
        optimization="O2",
    )
    report = application.export_report(tmp_path / "configured-export")
    assert report["package_bytes"] > 0
    assert (tmp_path / "configured-export" / "nw_common_d3d12_fl12_0_sm6_0_v1.nwp").is_file()
    assert (
        application.report()["export_configuration"]["dxc_executable"] == str(dxc)
    )


def test_configure_export_refreshes_live_backend_and_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nw08_test_toolchain

    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    toolchain = nw08_test_toolchain.create_toolchain(tmp_path / "toolchain")
    host = tmp_path / "preview-host.exe"
    host.write_bytes(b"MZ")
    application = studio.StudioApplication.create(
        player_executable=host, preview_mode="package-host"
    )
    assert application.pipeline.toolchain.dxc_version == "unavailable"
    application.configure_export(
        dxc_executable=toolchain["dxc"],
        player_template=toolchain["player_template"],
        cache_dir=str(tmp_path / "cache"),
        optimization="O1",
    )
    assert application.pipeline.toolchain.dxc_version == "1.8.2407.12"
    assert application.pipeline.toolchain.optimization == "O1"
    backend = application.pipeline.backend
    assert isinstance(backend, preview.WindowsRuntimePreviewBackend)
    assert backend._exporter.dxc_executable == toolchain["dxc"]
    assert backend._exporter.player_template == toolchain["player_template"]
    assert backend._exporter.cache_dir == str(tmp_path / "cache")
    assert backend._exporter.optimization == "O1"


def test_reopen_preserves_preview_and_export_configuration(
    tmp_path: Path,
) -> None:
    import nw08_test_toolchain

    toolchain = nw08_test_toolchain.create_toolchain(tmp_path / "toolchain")
    host = tmp_path / "preview-host.exe"
    host.write_bytes(b"MZ")
    application = studio.StudioApplication.create(
        player_executable=host,
        preview_mode="embedded-player",
        execution_kind="simulated",
        dxc_executable=toolchain["dxc"],
        player_template=toolchain["player_template"],
        cache_dir=str(tmp_path / "cache"),
        optimization="O3",
        export_settings={"fusion_enabled": False},
    )
    reopened = application.reopen()
    assert reopened.preview_mode == "embedded-player"
    assert reopened.execution_kind == "simulated"
    assert reopened.export_settings == {"fusion_enabled": False}
    assert reopened.dxc_executable == toolchain["dxc"]
    assert reopened.player_template == toolchain["player_template"]
    assert reopened.cache_dir == str(tmp_path / "cache")
    assert reopened.optimization == "O3"
    assert isinstance(reopened.pipeline.backend, preview.WindowsRuntimePreviewBackend)


def test_resolution_reconfiguration_invalidates_inflight_jobs() -> None:
    application = studio.StudioApplication.create()
    token = application.pipeline.submit_preview(tick=1)
    application.configure_preview_resolution(1280, 720)
    decision = application.pipeline.execute(token)
    assert decision.accepted is False
    assert decision.reason == "canceled"
    assert application.report()["export_configuration"]["width"] == 1280
    assert application.report()["export_configuration"]["height"] == 720


def test_open_graph_failure_keeps_previous_application_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tkinter.filedialog as filedialog

    from nw08_gui import StudioWindow

    class Status:
        def __init__(self) -> None:
            self.text = ""

        def configure(self, **kwargs):
            self.text = kwargs.get("text", "")

    class Application:
        explicit_backend = False

        def __init__(self) -> None:
            self.closed = False

        def reopen(self, _path):
            raise editor.EditorError("invalid graph")

        def close(self) -> None:
            self.closed = True

    class Dispatcher:
        def cancel(self) -> bool:
            return False

    class Actions:
        def close(self) -> None:
            return None

    window = object.__new__(StudioWindow)
    # Headless fixture bypasses the real Tk constructor: initialize transport state.
    window._audio_playing = False
    window._audio_pending = None
    window.application = Application()
    window.dispatcher = Dispatcher()
    window.actions = Actions()
    window.status = Status()
    monkeypatch.setattr(
        filedialog, "askopenfilename", lambda **_kwargs: "bad.nwgraph"
    )
    window.open_graph()
    assert window.application.closed is False
    assert "open failed" in window.status.text


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
    # The presented output depends on fullscreen, so rewiring the shader
    # invalidates both the edited node and its downstream present node.
    assert connected.changed_nodes == ("fullscreen", "screen")
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
    assert inputs["source_inputs_recorded"] is True
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

