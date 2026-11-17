from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw07_test_demo as demo  # noqa: E402
import nwp_package as nwp  # noqa: E402


GRAPH = REPO_ROOT / "fixtures" / "nw07-test-demo" / "orbital_vortex.nwgraph"
CANONICAL_NW07 = REPO_ROOT / "fixtures" / "nw07" / "production_loop.nwgraph"


def _uint_parameter(package: nwp.NwpPackage, name: str) -> int:
    parameter = next(item for item in package.parameters if item.name == name)
    assert len(parameter.value) == 4
    return struct.unpack("<I", parameter.value)[0]


def _param_float(node: nw03.GraphNode, name: str) -> float:
    return float(node.params[name][0])


def test_demo_graph_stays_inside_the_existing_nw07_operator_vocabulary() -> None:
    report = demo.validate_demo_graph(GRAPH)
    assert report["branch_only"] is True
    assert report["instance_count"] == 1024
    assert report["tunnel_radius"] == pytest.approx(4.6)
    assert report["loop_ticks"] == 1_152_000
    assert report["loop_seconds"] == 24
    assert (report["render_width"], report["render_height"]) == (1920, 1080)
    assert set(report["operators"]) == set(demo.ALLOWED_OPERATORS)
    assert len(report["demo_shader_specialization"]) == 3
    assert report["nw09_visual_extensions"] == {
        "fixed_step_ticks": 400,
        "fixed_step_hz": 120,
        "seeded_particle_instances": 128,
        "temporal_echo_instances": 128,
        "uses_history_abi": False,
    }

    document = nw03.load_graph(GRAPH)
    optimized = optimizer.optimize_graph(document)
    node_map = optimized.node_map()

    assert _param_float(node_map["torus"], "major_segments") == pytest.approx(24.0)
    assert _param_float(node_map["torus"], "minor_segments") == pytest.approx(8.0)
    assert _param_float(node_map["torus"], "major_radius") == pytest.approx(0.75)
    assert _param_float(node_map["torus"], "minor_radius") == pytest.approx(0.68)
    assert _param_float(node_map["instances"], "placement_radius") == pytest.approx(4.6)
    assert _param_float(node_map["instances"], "scale_min") == pytest.approx(0.06)
    assert _param_float(node_map["instances"], "scale_max") == pytest.approx(0.22)
    assert _param_float(node_map["camera"], "fov_degrees") == pytest.approx(74.0)
    assert _param_float(node_map["twist"], "max_twist_degrees") == pytest.approx(540.0)
    assert _param_float(node_map["bloom"], "resolution_scale") == pytest.approx(0.5)
    assert _param_float(node_map["bloom"], "threshold") == pytest.approx(0.90)
    assert _param_float(node_map["bloom"], "intensity") == pytest.approx(0.28)
    assert _param_float(node_map["tonemap"], "exposure") == pytest.approx(0.84)
    assert node_map["tonemap"].params["operator"][0] == "aces"


def test_demo_compiles_through_existing_package_path_with_local_shader_specialization(
    tmp_path: Path,
) -> None:
    original_shaders = nw05_export.nw06_export.FIXED_SHADERS

    result = demo.export_demo(
        tmp_path / "artifact",
        player_template=None,
        dxc=None,
        build_root=None,
        report_path=tmp_path / "report.json",
        contracts_only=True,
    )

    # The helper must never leak branch-local shader overrides into later
    # canonical exports in the same Python process.
    assert nw05_export.nw06_export.FIXED_SHADERS == original_shaders

    artifact = result.artifacts[demo.PROFILE_ID]
    package = artifact.package

    init_opcodes = [instruction.opcode for instruction in package.initialization]
    frame_opcodes = [instruction.opcode for instruction in package.frame]

    assert init_opcodes.count("init.dispatch") == 5
    assert "init.upload_resource" not in init_opcodes
    assert frame_opcodes.count("frame.execute_indirect") == 1
    assert frame_opcodes.count("frame.dispatch") == 2
    assert frame_opcodes[-1] == "frame.present"
    assert "frame.copy_buffer" not in frame_opcodes

    assert _uint_parameter(package, "nw06.timeline.ticks_per_second") == 48_000
    assert _uint_parameter(package, "nw06.timeline.duration_ticks") == demo.LOOP_TICKS
    assert _uint_parameter(package, "nw06.timeline.frame_step_numerator") == 800
    assert _uint_parameter(package, "nw06.timeline.frame_step_denominator") == 1
    assert _uint_parameter(package, "nw06.timeline.stateless_scene") == 1

    shader_ids = {
        row["specialization_id"] for row in artifact.manifest.get("shaders", [])
    }
    assert "nw07_test_demo_instances_cs" in shader_ids
    assert "nw07_test_demo_scene_vs" in shader_ids
    assert "nw07_test_demo_scene_ps" in shader_ids

    assert artifact.physical_realization.peak_bytes <= 512 * 1024 * 1024
    assert artifact.manifest["timeline"]["duration_ticks"] == demo.LOOP_TICKS
    assert artifact.executable_bytes is None




def test_release_demo_uses_normal_generated_player_after_nw09(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_export_graph(
        graph: Path,
        output: Path,
        *,
        settings: nw05_export.ExportSettings,
        report_path: Path | None = None,
        dxc_executable: str | None = None,
        optimization: str = "O2",
        **_: object,
    ) -> object:
        captured["graph"] = graph
        captured["output"] = output
        captured["settings"] = settings
        captured["report_path"] = report_path
        captured["dxc_executable"] = dxc_executable
        captured["optimization"] = optimization
        return sentinel

    monkeypatch.setattr(nw05_export, "export_graph", fake_export_graph)

    result = demo.export_demo(
        tmp_path / "artifact",
        player_template=tmp_path / "template.exe",
        dxc=tmp_path / "dxc.exe",
        build_root=tmp_path / "generated-player",
        report_path=tmp_path / "report.json",
        contracts_only=False,
    )

    assert result is sentinel
    settings = captured["settings"]
    assert isinstance(settings, nw05_export.ExportSettings)
    assert settings.frozen_embedded_specialization is False
    assert settings.player_template.endswith("template.exe")
    assert settings.specialized_build_root.endswith("generated-player")

def test_demo_does_not_replace_the_canonical_nw07_proof() -> None:
    canonical = nw03.load_graph(CANONICAL_NW07)
    experimental = nw03.load_graph(GRAPH)

    canonical_optimized = optimizer.optimize_graph(canonical)
    experimental_optimized = optimizer.optimize_graph(experimental)

    assert CANONICAL_NW07 != GRAPH
    assert _param_float(canonical_optimized.node_map()["instances"], "count") == pytest.approx(256.0)
    assert _param_float(experimental_optimized.node_map()["instances"], "count") == pytest.approx(1024.0)
    assert canonical.settings["nw07_loop_ticks"] == "960000"
    assert experimental.settings["nw07_loop_ticks"] == "1152000"

    canonical_shader_sources = {
        fixed.shader_id: fixed.source for fixed in nw05_export.nw06_export.FIXED_SHADERS
    }
    assert canonical_shader_sources[2] == "nw06_instances_cs.hlsl"
    assert canonical_shader_sources[6] == "nw06_scene_vs.hlsl"
    assert canonical_shader_sources[7] == "nw06_scene_ps.hlsl"


def test_demo_loop_timeline_closes_exactly_after_one_segment() -> None:
    report = demo.validate_demo_graph(GRAPH)
    keys = report["camera_keys"]

    assert keys[0] == [0, 0.0]
    assert keys[-1][0] == demo.LOOP_TICKS
    assert keys[-1][1] == pytest.approx(6.283185307179586, abs=1e-12)

    # The tunnel VS wraps Tick modulo the final camera key and advances exactly
    # one 96-unit segment over that phase, so tick LOOP_TICKS == tick 0.
    assert demo.LOOP_TICKS == 24 * 48_000


def test_tunnel_specialization_keeps_rings_distinct_and_corridor_open() -> None:
    instances_shader = (
        REPO_ROOT
        / "shaders"
        / "nw07_test_demo"
        / "nw07_test_demo_instances_cs.hlsl"
    ).read_text(encoding="utf-8")
    vertex_shader = (
        REPO_ROOT
        / "shaders"
        / "nw07_test_demo"
        / "nw07_test_demo_scene_vs.hlsl"
    ).read_text(encoding="utf-8")
    pixel_shader = (
        REPO_ROOT
        / "shaders"
        / "nw07_test_demo"
        / "nw07_test_demo_scene_ps.hlsl"
    ).read_text(encoding="utf-8")

    # Structural tunnel and hazard corridor remain bounded and deterministic.
    assert "min(384u" in instances_shader
    assert "lane_u < 0.08" in instances_shader
    assert "lerp(0.56, 0.88" in instances_shader
    assert "float tube = 0.015;" in vertex_shader
    assert "float sway_x = 0.16" in vertex_shader
    assert "float3 instance_center = base_center;" in vertex_shader
    assert "world.z = wrapped_center_z + local_world_z;" in vertex_shader

    # NW-09A visual extensions: 128 seeded particle records and 128 temporal
    # echo records at the canonical 1024-instance budget.
    assert "particle_count = min(128u" in instances_shader
    assert "echo_count = min(128u" in instances_shader
    assert "StoreUniformTransform(offset, scale, tx, ty, tz, seed, 3u);" in instances_shader
    assert "source_seed" in instances_shader
    assert "4u);" in instances_shader

    # Motion is quantized to NW-09's 120 Hz fixed step (400 ticks at 48 kHz).
    assert "NW09_FIXED_STEP_TICKS = 400u" in vertex_shader
    assert "simulation_tick = wrapped_tick / NW09_FIXED_STEP_TICKS" in vertex_shader
    assert "temporal_age_steps = 6u + (instance_seed % 18u)" in vertex_shader
    assert "BuildParticleStreak" in vertex_shader

    # Particle emission and delayed echoes are visually distinct and feed the
    # existing bloom/tonemap path; no new runtime or History ABI is introduced.
    assert "input.instance_flags == 3u" in pixel_shader
    assert "input.instance_flags == 4u" in pixel_shader
    assert "simulation_tick = Tick / NW09_FIXED_STEP_TICKS" in pixel_shader
    assert "clip(coverage - 0.62)" in pixel_shader
