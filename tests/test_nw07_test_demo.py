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
    assert report["instance_count"] == 1536
    assert report["tunnel_radius"] == pytest.approx(4.6)
    assert report["loop_ticks"] == 1_152_000
    assert report["loop_seconds"] == 24
    assert (report["render_width"], report["render_height"]) == (1920, 1080)
    assert set(report["operators"]) == set(demo.ALLOWED_OPERATORS)
    assert len(report["demo_shader_specialization"]) == 3

    document = nw03.load_graph(GRAPH)
    optimized = optimizer.optimize_graph(document)
    node_map = optimized.node_map()

    assert _param_float(node_map["torus"], "major_segments") == pytest.approx(24.0)
    assert _param_float(node_map["torus"], "minor_segments") == pytest.approx(8.0)
    assert _param_float(node_map["torus"], "major_radius") == pytest.approx(0.75)
    assert _param_float(node_map["torus"], "minor_radius") == pytest.approx(0.68)
    assert _param_float(node_map["instances"], "placement_radius") == pytest.approx(4.6)
    assert _param_float(node_map["instances"], "scale_min") == pytest.approx(0.20)
    assert _param_float(node_map["instances"], "scale_max") == pytest.approx(0.70)
    assert _param_float(node_map["camera"], "fov_degrees") == pytest.approx(74.0)
    assert _param_float(node_map["twist"], "max_twist_degrees") == pytest.approx(540.0)
    assert _param_float(node_map["bloom"], "resolution_scale") == pytest.approx(0.5)
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


def test_demo_does_not_replace_the_canonical_nw07_proof() -> None:
    canonical = nw03.load_graph(CANONICAL_NW07)
    experimental = nw03.load_graph(GRAPH)

    canonical_optimized = optimizer.optimize_graph(canonical)
    experimental_optimized = optimizer.optimize_graph(experimental)

    assert CANONICAL_NW07 != GRAPH
    assert _param_float(canonical_optimized.node_map()["instances"], "count") == pytest.approx(256.0)
    assert _param_float(experimental_optimized.node_map()["instances"], "count") == pytest.approx(1536.0)
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
