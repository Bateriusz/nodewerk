from __future__ import annotations

import dataclasses
import json
import os
import shutil
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw05_export  # noqa: E402
import nw09_export  # noqa: E402
import nw09_temporal as temporal  # noqa: E402
import nwp_package as nwp  # noqa: E402


PARTICLES = REPO_ROOT / "fixtures" / "nw09" / "particles_seeded.nwgraph"
FEEDBACK = REPO_ROOT / "fixtures" / "nw09" / "feedback_rect.nwgraph"
TICK = temporal.TIMELINE_TICKS_PER_SIMULATION_TICK


@pytest.fixture(autouse=True)
def _test_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")


def _mock_dxc(directory: Path) -> Path:
    import nw04_fixture

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


def _settings(tmp_path: Path, **overrides) -> nw05_export.ExportSettings:
    template = tmp_path / "toolchain" / "template.exe"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_bytes(b"MZ" + b"\x00" * 256)
    base = nw05_export.ExportSettings(
        byte_ceiling=16 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        player_template=str(template),
    )
    return replace(base, **overrides)


def _export(tmp_path: Path, graph: Path, name: str = "artifact", **settings):
    return nw05_export.export_graph(
        graph,
        tmp_path / name,
        settings=_settings(tmp_path, **settings),
        dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
        allow_test_payloads=True,
    )


_TONEMAP_GRAPH = """nwgraph 1
profile nw.common.d3d12.fl12_0.sm6_0.v1 sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a
setting nw05.present.width 320
setting nw05.present.height 180
setting nw09_loop_ticks 9600
node feedback nw.feedback.image@1.0.0 frequency=history residency=gpu
  param width 16
  param height 9
  param seed 99
  param decay 0.94
  param gain 0.25
  history state-type nw.feedback.image.v1 initial "mode=zero;width=16;height=9;seed=99" reset on-loop step-rate 120
node tone nw.post.tonemap@1.0.0 frequency=frame residency=gpu
  input image feedback.out
  param exposure 1.0
  param gamma 2.2
  param operator aces
node screen nw.render.present@1.0.0 frequency=frame residency=gpu
  input image tone.out
layout feedback 0 0
layout screen 320 0
"""


def _bloom_tonemap_graph_text() -> str:
    return _TONEMAP_GRAPH.replace(
        "node tone nw.post.tonemap@1.0.0 frequency=frame residency=gpu\n"
        "  input image feedback.out\n"
        "  param exposure 1.0\n"
        "  param gamma 2.2\n"
        "  param operator aces\n"
        "node screen nw.render.present@1.0.0 frequency=frame residency=gpu\n"
        "  input image tone.out",
        "node bloom nw.post.bloom@1.0.0 frequency=frame residency=gpu\n"
        "  input image feedback.out\n"
        "  param threshold 0.7\n"
        "  param intensity 0.35\n"
        "  param radius 1.5\n"
        "  param resolution_scale 0.5\n"
        "node tone nw.post.tonemap@1.0.0 frequency=frame residency=gpu\n"
        "  input image bloom.out\n"
        "  param exposure 1.0\n"
        "  param gamma 2.2\n"
        "  param operator aces\n"
        "node screen nw.render.present@1.0.0 frequency=frame residency=gpu\n"
        "  input image tone.out",
    )


def test_stateful_export_preserves_ordinary_downstream_consumers(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "tonemap.nwgraph"
    graph.write_text(_TONEMAP_GRAPH, encoding="utf-8")
    document = nw03.load_graph(graph)
    # Presentation ancestry follows the authored chain instead of falling back
    # to the first History declaration.
    assert nw09_export._present_history_node(
        document, temporal.history_specs(document)
    ) == "feedback"
    chain = nw09_export.presentation_chain(document, "feedback")
    assert [node.node_id for node in chain] == ["tone"]

    result = _export(tmp_path, graph)
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    report = result.report
    assert report["present_state_node"] == "feedback"
    assert [item["operator_id"] for item in report["post_passes"]] == [
        "nw.post.tonemap"
    ]
    assert report["present_shader_id"] == report["post_passes"][0]["shader_id"]
    tonemap_shader = next(
        shader
        for shader in package.shaders
        if shader.entry_point == "nw09_post_main"
    )
    assert tonemap_shader.shader_id == report["present_shader_id"]
    dispatches = [
        instruction
        for instruction in package.frame
        if instruction.opcode == "frame.dispatch"
    ]
    # image conversion then the preserved tonemap
    assert [item.operands[-1].value for item in dispatches] == [
        report["history_image_surface"]["shader_id"],
        report["present_shader_id"],
    ]
    # The authored tonemap dispatch must not be replaced by the display pass.
    assert report["present_resource_id"] == report["post_passes"][0][
        "output_resource_id"
    ]


def test_backward_seek_after_loop_restores_origin_generation() -> None:
    events = (temporal.TemporalEvent("tick-400", 400, {}),)
    runtime = temporal.TemporalRuntime(
        nw03.load_graph(FEEDBACK), events=events
    )
    first = runtime.advance_to(9600)
    assert [event.event_id for event in first.events] == ["tick-400"]
    assert runtime.loop_generation == 1

    assert runtime.seek(0).ok
    assert runtime.loop_generation == 0
    replay = runtime.advance_to(400)
    # The already observed event is deduplicated, and the rebuilt state is
    # exactly what a fresh runtime produces at the same fixed tick.
    assert replay.events == ()
    assert runtime.loop_generation == 0
    fresh = temporal.TemporalRuntime(nw03.load_graph(FEEDBACK), events=events)
    fresh.advance_to(400)
    assert runtime.state_digest() == fresh.state_digest()


def test_malformed_checkpoint_payload_falls_back_before_mutation() -> None:
    runtime = temporal.TemporalRuntime(nw03.load_graph(PARTICLES))
    runtime.advance_to(TICK * 10)
    checkpoint = runtime.checkpoint()
    states = [dict(item) for item in checkpoint.states]
    states[0]["values"] = ["not-a-number"]
    bad_state = dataclasses.replace(checkpoint, states=tuple(states))
    original_tick = runtime.simulation_tick
    with pytest.raises(temporal.CheckpointError):
        runtime.restore_checkpoint(bad_state)
    assert runtime.simulation_tick == original_tick
    result = runtime.seek(TICK * 20, checkpoint=bad_state)
    assert result.ok and result.fallback_to_initial_state
    direct = temporal.TemporalRuntime(nw03.load_graph(PARTICLES))
    direct.advance_to(TICK * 20)
    assert runtime.state_digest() == direct.state_digest()

    bad_cursor = dataclasses.replace(
        checkpoint,
        event_cursor={"loop_generation": 0, "tick": "bad", "sequence": 0},
    )
    runtime = temporal.TemporalRuntime(nw03.load_graph(PARTICLES))
    runtime.advance_to(TICK * 10)
    original_tick = runtime.simulation_tick
    with pytest.raises(temporal.CheckpointError):
        runtime.restore_checkpoint(bad_cursor)
    assert runtime.simulation_tick == original_tick
    result = runtime.seek(TICK * 20, checkpoint=bad_cursor)
    assert result.ok and result.fallback_to_initial_state
    direct = temporal.TemporalRuntime(nw03.load_graph(PARTICLES))
    direct.advance_to(TICK * 20)
    assert runtime.state_digest() == direct.state_digest()


def test_stateful_export_embeds_existing_player_and_enforces_byte_ceiling(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path, FEEDBACK, "artifact")
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    assert artifact.executable_bytes is not None
    assert artifact.executable_name == "common.exe"
    executable_path = tmp_path / "artifact" / "common.exe"
    assert executable_path.is_file()
    package_bytes, manifest_bytes = nw05_export.extract_embedded_package(
        executable_path.read_bytes()
    )
    assert package_bytes == artifact.package_bytes
    assert manifest_bytes == (
        tmp_path / "artifact" / "link-manifest.json"
    ).read_bytes()
    assert artifact.size_report["distribution"]["executable_bytes"] > 0
    assert artifact.size_report["dependency_source"] in {
        "template-pe-imports",
        "package-phase-requirements",
    }

    with pytest.raises(nw05_export.ExportError) as excinfo:
        _export(tmp_path, FEEDBACK, "too-big", byte_ceiling=1)
    assert any(
        item.get("code") == "export_byte_ceiling_exceeded"
        for item in excinfo.value.diagnostics
    )


def test_packaged_checkpoint_byte_cost_matches_encoded_section(
    tmp_path: Path,
) -> None:
    report = nw09_export.export_stateful_graph(
        PARTICLES,
        tmp_path / "stateful",
        allow_test_payloads=True,
        packaged_checkpoint_ticks=(0, 4000),
    )
    package = nwp.decode_package(Path(report["package_path"]).read_bytes())
    records = package.release_checkpoints
    expected_total = (
        len(nwp._encode_checkpoints(records)) + nwp.SECTION_ENTRY_SIZE
    )
    assert report["packaged_checkpoint_byte_cost"] == expected_total
    report_by_id = {
        item["checkpoint_id"]: item
        for item in report["packaged_checkpoints"]
    }
    for record in records:
        item = report_by_id[record.checkpoint_id]
        encoded = (
            len(nwp._encode_checkpoints([record]))
            - nwp._CHECKPOINT_SECTION_HEADER_BYTES
        )
        assert item["encoded_record_bytes"] == encoded
        assert item["byte_cost"] == (
            encoded
            + nwp._CHECKPOINT_SECTION_HEADER_BYTES
            + nwp.SECTION_ENTRY_SIZE
        )
        # The reported cost includes the semantic/layout/capability fields and
        # length prefixes that the old payload+label+64 heuristic omitted.
        assert item["byte_cost"] > (
            len(record.payload) + len(record.label.encode("utf-8")) + 64
        )


def test_simulation_tick_is_published_as_integer_bits() -> None:
    document = nw03.load_graph(FEEDBACK)
    spec = temporal.history_specs(document)[0]
    hlsl = nw09_export._history_hlsl(document, spec)
    assert "asuint(g_params[0].y)" in hlsl
    assert "/ 400.0f" not in hlsl

    # Document the precision failure the integer handoff avoids: at exact
    # simulation tick 671089 the float32 round-trip of the timeline tick
    # reconstructs tick 671088.
    timeline_tick = 671089 * TICK
    rounded = struct.unpack("<f", struct.pack("<f", float(timeline_tick)))[0]
    assert int(rounded) // TICK != 671089


def test_stateful_export_packs_exact_simulation_tick_parameter(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path, FEEDBACK, "artifact")
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    names = [parameter.name for parameter in package.parameters]
    assert names[0] == "time"
    assert names[1] == "nw09.simulation_tick"
    assert (
        package.parameters[1].value == struct.pack("<I", 0)
    )


def test_public_package_player_layout_is_not_macro_conditional() -> None:
    header = (REPO_ROOT / "src" / "player" / "package_player.h").read_text(
        encoding="utf-8"
    )
    # The fixture main and the linked implementation must observe identical
    # PackagePlayer/FrameOp layouts.  Conditional layout guards here were the
    # cause of the ODR/offset mismatch.
    assert "#if !NW05_FROZEN_EMBEDDED_SPECIALIZATION" not in header
    assert "#if NW05_FROZEN_EMBEDDED_SPECIALIZATION" not in header
    assert "SimulationStep," in header
    assert "history_simulation_tick() const" in header


def test_authoring_runtime_rewinds_history_on_backward_seek() -> None:
    # The persistent authoring runtime (studio preview host) must reset the
    # History resources and counter together, then replay through the normal
    # render path instead of failing the backward request.
    host = (
        REPO_ROOT / "src" / "studio" / "nw_studio_preview_host.cpp"
    ).read_text(encoding="utf-8")
    assert "rewindHistoryForAuthoring()" in host
    assert "history_simulation_tick()" in host
    # The host's IsDeviceRemoved seam takes the integer exit code; passing the
    # ExitCode enum directly is a Windows-only compile failure.
    assert "IsDeviceRemoved(rewound)" not in host
    assert "IsDeviceRemoved(exit_code)" in host
    overlay = (
        REPO_ROOT / "tools" / "nw08_authoring_runtime_overlay.py"
    ).read_text(encoding="utf-8")
    assert "ExitCode PackagePlayer::rewindHistoryForAuthoring()" in overlay
    assert "RunInitializationInstructions()" in overlay
    cpp = (REPO_ROOT / "src" / "player" / "package_player.cpp").read_text(
        encoding="utf-8"
    )
    # Initialization reset is the authoritative temporal-resource boundary.
    assert cpp.count("history_simulation_tick_ = 0u;") >= 2


def test_preview_pipeline_scrubs_backward_and_restores_checkpoint() -> None:
    import nw08_editor as editor
    import nw08_preview as preview

    session = editor.GraphEditorSession.open(FEEDBACK)
    backend = temporal.TemporalReferencePreviewBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    controller = temporal.TemporalPreviewSession(session, pipeline)

    assert controller.seek(TICK * 8).ok
    checkpoint = controller.runtime.checkpoint()
    last = controller.last_valid_preview
    forward = controller.seek(TICK * 12)
    assert forward.ok
    backward = controller.seek(0)
    assert backward.ok
    assert controller.last_valid_preview is not None

    restored = temporal.TemporalRuntime(
        session.document, semantic_digest=controller.runtime.semantic_digest
    )
    restored.restore_checkpoint(checkpoint)
    result = controller.seek(TICK * 8, checkpoint=checkpoint)
    assert result.ok
    assert controller.runtime.state_digest() == restored.state_digest()
    assert last is not None


def test_persistent_authoring_session_requests_history_rewind() -> None:
    import nw08_preview as preview

    session = preview.AuthoringRuntimeSession(
        "/tmp/preview-host", mode="package-host"
    )
    commands: list[str] = []
    responses = [
        {"event": "rendered", "ok": True},
        {"event": "rewind", "ok": True},
        {"event": "rendered", "ok": True},
    ]
    session._write = lambda command: commands.append(command)  # type: ignore[assignment]
    session._read_response = lambda *, timeout: responses.pop(0)  # type: ignore[assignment]

    session.render(
        tick=TICK * 8,
        capture_dir=Path("/tmp"),
        stats_path=Path("/tmp/stats.json"),
    )
    session.render(
        tick=0,
        capture_dir=Path("/tmp"),
        stats_path=Path("/tmp/stats.json"),
    )
    assert commands == [f"render {TICK * 8}", "rewind", "render 0"]


def test_stateful_manifest_carries_link_metadata_and_executable_launches(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX launch harness uses a shell-script template")
    template = tmp_path / "script_template"
    template.write_text(
        "#!/bin/sh\nprintf 'NW09_TEMPLATE_LAUNCHED\\n'\nexit 0\n",
        encoding="utf-8",
    )
    template.chmod(0o755)
    settings = nw05_export.ExportSettings(
        byte_ceiling=16 * 1024 * 1024,
        memory_budget=987654321,
        player_template=str(template),
    )
    result = nw05_export.export_graph(
        FEEDBACK,
        tmp_path / "artifact",
        settings=settings,
        dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
        allow_test_payloads=True,
    )
    manifest = json.loads(
        (tmp_path / "artifact" / "link-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    # The generated main calls ParseEmbeddedLinkMetadata at startup.
    assert manifest["link_metadata"] == {
        "wave_ops": False,
        "fp16_shader_ops": False,
        "int64_shader_ops": False,
        "memory_budget_bytes": 987654321,
    }
    player_source = (
        tmp_path / "artifact" / "common.player.cpp"
    ).read_text(encoding="utf-8")
    assert "ParseEmbeddedLinkMetadata" in player_source
    executable = tmp_path / "artifact" / "common.exe"
    executable.chmod(0o755)
    launched = subprocess.run(
        [str(executable)], capture_output=True, text=True, check=False
    )
    assert launched.returncode == 0, launched.stderr
    assert "NW09_TEMPLATE_LAUNCHED" in launched.stdout
    assert result.artifacts[nwp.COMMON_PROFILE_ID].executable_bytes is not None


def test_stateful_generated_player_uses_absolute_48khz_timeline(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path, FEEDBACK, "artifact")
    manifest = json.loads(
        (tmp_path / "artifact" / "link-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["timeline"] == {
        "ticks_per_second": 48_000,
        "frame_step_numerator": 1,
        "frame_step_denominator": 1,
        "duration_ticks": 0,
        "mode": "absolute",
    }
    assert "player.nw09.fixed-step-timeline" in manifest["retained"][
        "runtime_implementations"
    ]
    source = (
        tmp_path / "artifact" / "common.player.cpp"
    ).read_text(encoding="utf-8")
    assert "CheckedTicksFromNanoseconds" in source
    assert "48000u" in source
    assert "player.history_simulation_tick()" in source
    # The presentation-rate-dependent frame-counter loop must not be selected.
    assert (
        "for (std::uint64_t tick = 0; max_frames == 0 || tick < max_frames; ++tick)"
        not in source
    )
    assert result.artifacts[nwp.COMMON_PROFILE_ID].package.history


def test_stateful_post_input_uses_typed_srv_access(tmp_path: Path) -> None:
    graph = tmp_path / "tonemap.nwgraph"
    graph.write_text(_TONEMAP_GRAPH, encoding="utf-8")
    document = nw03.load_graph(graph)
    chain = nw09_export.presentation_chain(document, "feedback")
    assert [node.node_id for node in chain] == ["tone"]
    hlsl = nw09_export._post_pass_hlsl(document, chain[0], 320, 180)
    # The package/player realizes RGBA32F buffer SRVs as typed views, so the
    # shader must use typed Buffer<float4> access rather than ByteAddressBuffer.
    assert "Buffer<float4> nw_input : register(t0);" in hlsl
    assert "ByteAddressBuffer nw_input" not in hlsl
    assert "nw_input.Load(int(pixel))" in hlsl
    # State shaders consume the typed R32_UINT SRV as well.
    spec = temporal.history_specs(document)[0]
    step = nw09_export._history_hlsl(document, spec)
    assert "Buffer<uint> nw_previous : register(t0);" in step
    assert "ByteAddressBuffer nw_previous" not in step

    result = _export(tmp_path, graph, "artifact")
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    post = result.report["post_passes"][0]
    post_input = next(
        resource
        for resource in package.resources
        if resource.resource_id == post["copied_input_resource_id"]
    )
    assert post_input.kind == "buffer"
    assert post_input.usage == "srv"
    assert post_input.format == "DXGI_FORMAT_R32G32B32A32_FLOAT"
    assert post_input.size_bytes == 320 * 180 * 16


def test_stateful_bloom_matches_established_operator_semantics(
    tmp_path: Path,
) -> None:
    text = _bloom_tonemap_graph_text()
    graph = tmp_path / "bloom.nwgraph"
    graph.write_text(text, encoding="utf-8")
    document = nw03.load_graph(graph)
    chain = nw09_export.presentation_chain(document, "feedback")
    assert [node.operator_id for node in chain] == [
        "nw.post.bloom",
        "nw.post.tonemap",
    ]
    bloom = nw09_export._post_pass_hlsl(document, chain[0], 320, 180)
    # Established semantics: floating-point radius, luminance threshold with
    # the NW-06 luma weights, nine taps, intensity scaling, half resolution.
    assert "1.5f" in bloom
    assert "const int radius" not in bloom
    assert "luma > 0.7f" in bloom
    assert "0.2126f" in bloom and "0.7152f" in bloom and "0.0722f" in bloom
    assert "bloom / 9.0f * 0.35f" in bloom
    assert "0.5f" in bloom or "half" in bloom
    assert "Buffer<float4> nw_input" in bloom

    result = _export(tmp_path, graph, "artifact")
    assert [item["operator_id"] for item in result.report["post_passes"]] == [
        "nw.post.bloom",
        "nw.post.tonemap",
    ]

    incompatible = text.replace(
        "param resolution_scale 0.5", "param resolution_scale 0.25"
    )
    bad = tmp_path / "bloom-bad.nwgraph"
    bad.write_text(incompatible, encoding="utf-8")
    with pytest.raises(nw05_export.ExportError):
        _export(tmp_path, bad, "bad-artifact")


def test_stateful_bloom_without_tonemap_fails_closed(
    tmp_path: Path,
) -> None:
    text = _bloom_tonemap_graph_text()
    text = text.replace(
        "node tone nw.post.tonemap@1.0.0 frequency=frame residency=gpu\n"
        "  input image bloom.out\n"
        "  param exposure 1.0\n"
        "  param gamma 2.2\n"
        "  param operator aces\n",
        "",
    ).replace("input image tone.out", "input image bloom.out")
    graph = tmp_path / "bloom-direct-present.nwgraph"
    graph.write_text(text, encoding="utf-8")
    with pytest.raises(nw05_export.ExportError) as excinfo:
        _export(tmp_path, graph, "bloom-direct-present")
    assert "must be consumed directly by nw.post.tonemap" in str(excinfo.value)


def test_stateful_export_rejects_frozen_embedded_specialization(
    tmp_path: Path,
) -> None:
    with pytest.raises(nw05_export.ExportError) as excinfo:
        _export(
            tmp_path,
            FEEDBACK,
            "frozen",
            frozen_embedded_specialization=True,
        )
    assert any(
        item.get("code") == "stateful_frozen_player_incompatible"
        for item in excinfo.value.diagnostics
    )


def test_absolute_timeline_cadences_reach_the_same_fixed_step() -> None:
    # The generated player converts monotonic wall time to the 48,000-Hz
    # absolute timeline. One second is 48,000 timeline ticks (120 simulation
    # ticks) regardless of presentation cadence.
    cadences_ms = [
        [index * 1000 // 30 for index in range(0, 31)],
        [index * 1000 // 60 for index in range(0, 61)],
        [0, 7, 31, 222, 501, 777, 999, 1000],
    ]
    digests: list[str] = []
    for milliseconds in cadences_ms:
        ticks = [ms * 48_000 // 1000 for ms in milliseconds]
        runtime = temporal.TemporalRuntime(nw03.load_graph(FEEDBACK))
        runtime.advance_frames(ticks)
        assert runtime.simulation_tick == 120
        digests.append(runtime.state_digest())
    assert len(set(digests)) == 1


def _native_fixture() -> Path | None:
    override = os.environ.get("NW04_FIXTURE")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    candidates.extend(
        sorted((REPO_ROOT / "build").glob("**/nw_package_player_fixture*"))
    )
    return next(
        (
            candidate
            for candidate in candidates
            if candidate.is_file()
            and candidate.suffix.lower() in ("", ".exe")
            and "CMakeFiles" not in candidate.parts
        ),
        None,
    )


def test_native_tonemap_chain_validation_when_available(
    tmp_path: Path,
) -> None:
    required = os.environ.get("NW09_REQUIRE_NATIVE") == "1"
    fixture = _native_fixture()
    if fixture is None:
        if required:
            pytest.fail("required native NW-04 fixture is not built")
        pytest.skip("native NW-04 fixture is not built on this host")
    dxc = nw03.resolve_dxc()
    if dxc is None:
        if required:
            pytest.fail("required pinned offline DXC is unavailable")
        pytest.skip("offline DXC is unavailable on this host")
    graph = tmp_path / "tonemap.nwgraph"
    graph.write_text(_TONEMAP_GRAPH, encoding="utf-8")
    report = nw09_export.export_stateful_graph(
        graph,
        tmp_path / "stateful",
        allow_test_payloads=False,
        dxc_executable=dxc,
        allow_unpinned_dxc=True,
    )
    result = subprocess.run(
        [
            str(fixture),
            "--package",
            report["package_path"],
            "--allow-software-adapter",
            "--frames",
            "1",
            "--tick-start",
            "3200",
            "--tick-step",
            "1",
            "--width",
            "320",
            "--height",
            "180",
            "--emit-display-image-hash",
            "--validation",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    metrics = {
        key: value
        for key, value in (
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if line.startswith("NW_PLAYER_") and "=" in line
        )
    }
    assert metrics["NW_PLAYER_HISTORY_SIMULATION_TICK"] == "8"
    assert int(metrics["NW_PLAYER_DISPLAY_IMAGE_NONZERO_BYTES"]) > 0


def test_native_bloom_tonemap_chain_validation_when_available(
    tmp_path: Path,
) -> None:
    required = os.environ.get("NW09_REQUIRE_NATIVE") == "1"
    fixture = _native_fixture()
    if fixture is None:
        if required:
            pytest.fail("required native NW-04 fixture is not built")
        pytest.skip("native NW-04 fixture is not built on this host")
    dxc = nw03.resolve_dxc()
    if dxc is None:
        if required:
            pytest.fail("required pinned offline DXC is unavailable")
        pytest.skip("offline DXC is unavailable on this host")
    graph = tmp_path / "bloom-tonemap.nwgraph"
    graph.write_text(_bloom_tonemap_graph_text(), encoding="utf-8")
    report = nw09_export.export_stateful_graph(
        graph,
        tmp_path / "stateful-bloom",
        allow_test_payloads=False,
        dxc_executable=dxc,
        allow_unpinned_dxc=True,
    )
    result = subprocess.run(
        [
            str(fixture),
            "--package",
            report["package_path"],
            "--allow-software-adapter",
            "--frames",
            "1",
            "--tick-start",
            "3200",
            "--tick-step",
            "1",
            "--width",
            "320",
            "--height",
            "180",
            "--emit-display-image-hash",
            "--validation",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    metrics = {
        key: value
        for key, value in (
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if line.startswith("NW_PLAYER_") and "=" in line
        )
    }
    assert metrics["NW_PLAYER_HISTORY_SIMULATION_TICK"] == "8"
    assert int(metrics["NW_PLAYER_DISPLAY_IMAGE_NONZERO_BYTES"]) > 0


def test_real_generated_stateful_player_launches_with_link_metadata(
    tmp_path: Path,
) -> None:
    required = os.environ.get("NW09_REQUIRE_REAL_PLAYER") == "1"
    template_value = os.environ.get("NW05_REAL_PLAYER_TEMPLATE", "")
    if os.name != "nt" or not template_value:
        if required:
            pytest.fail(
                "required real generated-player launch needs Windows and "
                "NW05_REAL_PLAYER_TEMPLATE"
            )
        pytest.skip("real generated-player launch runs in Windows CI")
    template = Path(template_value)
    assert template.is_file()
    dxc = shutil.which("dxc") or shutil.which("dxc.exe")
    assert dxc, "pinned DXC must be on PATH for real generated-player launch"
    settings = nw05_export.ExportSettings(
        byte_ceiling=64 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        player_template=str(template),
    )
    result = nw05_export.export_graph(
        FEEDBACK,
        tmp_path / "stateful",
        settings=settings,
        dxc_executable=dxc,
    )
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    assert artifact.executable_bytes is not None
    executable = tmp_path / "stateful" / artifact.executable_name
    launched = subprocess.run(
        [
            str(executable),
            "--tick",
            "3200",
            "--frames",
            "1",
            "--allow-software-adapter",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert launched.returncode == 0, launched.stderr + launched.stdout
    # This marker is printed only after ParseEmbeddedLinkMetadata succeeds.
    assert "NW05_LINK_MANIFEST_VERIFIED=1" in launched.stdout
    assert "NW05_EXPLICIT_TICK=3200" in launched.stdout
    assert "NW05_HISTORY_SIMULATION_TICK=8" in launched.stdout
    assert "NW05_HISTORY_PENDING_TICKS=0" in launched.stdout
