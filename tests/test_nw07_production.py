from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw06_procedural as procedural  # noqa: E402
import nw07_contract as contract  # noqa: E402
import nw07_export as export  # noqa: E402

GRAPH = REPO_ROOT / "fixtures" / "nw07" / "production_loop.nwgraph"
CONFIG = REPO_ROOT / "benchmarks" / "nw07" / "canonical-production.v1.json"


def test_canonical_graph_contains_the_frozen_nw07_content() -> None:
    document = nw03.load_graph(GRAPH)
    optimized = optimizer.optimize_graph(document)
    used = {node.operator_id for node in optimized.nodes}
    assert contract.required_operations() <= used
    assert {"texture", "texture_gpu", "torus", "mesh_gpu", "instances", "scene_gpu"} <= set(
        optimized.partitions["initialization"]
    )
    assert "render" in optimized.partitions["frame"]
    node_map = optimized.node_map()
    assert node_map["instances"].params["count"][0] == "256.0"
    assert node_map["texture"].params["seed"][0] == "1297036693.0"
    assert node_map["twist"].params["max_twist_degrees"][0] == "180.0"
    assert node_map["bloom"].params["resolution_scale"][0] == "0.5"
    assert node_map["tonemap"].params["operator"][0] == "aces"


def test_canonical_configuration_and_graph_identity() -> None:
    config = export.load_canonical_config(CONFIG)
    result = export.verify_canonical_graph(config)
    assert result["graph_sha256"] == config["graph_sha256"]
    assert result["frame_count_per_loop"] == 1200
    assert result["loop_boundary"]["seamless_modulo_turn"] is True
    assert config["artifact_budget"]["complete_distribution_bytes_ceiling"] == 262144
    assert config["artifact_budget"]["stretch_distribution_bytes_ceiling"] == 131072
    assert config["artifact_budget"]["stretch_is_mandatory_gate"] is False
    assert config["artifact_budget"]["peak_engine_committed_gpu_bytes_ceiling"] == 512 * 1024 * 1024
    assert config["artifact_budget"]["process_start_to_first_present_ms_ceiling"] == 2000.0


def test_fixed_tick_evaluation_is_deterministic_and_stateless() -> None:
    document = nw03.load_graph(GRAPH)
    for tick in contract.REQUIRED_FIXED_TICKS:
        first = export.evaluate_camera_at_tick(document, tick)
        second = export.evaluate_camera_at_tick(document, tick)
        assert first == second
    start = export.evaluate_camera_at_tick(document, 0)
    boundary = export.evaluate_camera_at_tick(document, contract.LOOP_TICKS)
    assert start["camera_angle_modulo_turn"] == pytest.approx(
        boundary["camera_angle_modulo_turn"], abs=contract.NUMERIC_ABSOLUTE_TOLERANCE
    )
    assert boundary["camera_angle_radians"] == pytest.approx(
        start["camera_angle_radians"], abs=contract.NUMERIC_ABSOLUTE_TOLERANCE
    )
    assert boundary["wrapped_tick"] == 0
    assert export.evaluate_camera_at_tick(document, contract.LOOP_TICKS + 1234)[
        "wrapped_tick"
    ] == 1234
    wrapped = export.evaluate_camera_at_tick(
        document, contract.LOOP_TICKS + 1234
    )
    direct = export.evaluate_camera_at_tick(document, 1234)
    assert wrapped["camera_angle_radians"] == pytest.approx(
        direct["camera_angle_radians"], abs=contract.NUMERIC_ABSOLUTE_TOLERANCE
    )
    assert wrapped["twist_degrees"] == pytest.approx(
        direct["twist_degrees"], abs=contract.NUMERIC_ABSOLUTE_TOLERANCE
    )
    assert start["twist_degrees"] == pytest.approx(
        boundary["twist_degrees"], abs=contract.NUMERIC_ABSOLUTE_TOLERANCE
    )
    assert export.evaluate_camera_at_tick(document, 0)["twist_degrees"] == pytest.approx(
        0.0, abs=contract.NUMERIC_ABSOLUTE_TOLERANCE
    )
    assert export.evaluate_camera_at_tick(document, 240_000)[
        "twist_degrees"
    ] == pytest.approx(180.0, abs=contract.NUMERIC_ABSOLUTE_TOLERANCE)
    assert export.evaluate_camera_at_tick(document, 480_000)[
        "twist_degrees"
    ] == pytest.approx(0.0, abs=contract.NUMERIC_ABSOLUTE_TOLERANCE)
    assert export.evaluate_camera_at_tick(document, 720_000)[
        "twist_degrees"
    ] == pytest.approx(-180.0, abs=contract.NUMERIC_ABSOLUTE_TOLERANCE)


def test_seeded_procedural_content_is_deterministic() -> None:
    first_instances = procedural.generate_instances()
    second_instances = procedural.generate_instances()
    assert len(first_instances) == contract.TORUS_INSTANCE_COUNT
    assert [record.encode() for record in first_instances] == [
        record.encode() for record in second_instances
    ]
    first_mesh = procedural.generate_torus_mesh()
    second_mesh = procedural.generate_torus_mesh()
    assert first_mesh.vertex_count == 2048
    assert first_mesh.index_count == 12288
    assert first_mesh.vertices == second_mesh.vertices
    assert first_mesh.indices == second_mesh.indices
    assert procedural.procedural_content_digest() == procedural.procedural_content_digest()
    assert procedural.generate_noise2d(16, 16) == procedural.generate_noise2d(16, 16)


def test_nw07_render_dimensions_are_not_the_material_texture_size() -> None:
    document = nw03.load_graph(GRAPH)
    optimized = optimizer.optimize_graph(document)
    assert nw05_export._graph_render_dimensions(document, optimized) == (1920, 1080)
    # The NW-06 proof keeps deriving its render size from the 256x256 material
    # texture unless it explicitly opts into NW-07 dimensions.
    nw06 = REPO_ROOT / "fixtures" / "nw06" / "procedural_scene.nwgraph"
    nw06_document = nw03.load_graph(nw06)
    nw06_optimized = optimizer.optimize_graph(nw06_document)
    assert nw05_export._graph_render_dimensions(nw06_document, nw06_optimized) == (256, 256)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({}, (256, 256)),
        ({"nw07_output_width": "1280"}, (1280, 1080)),
        ({"nw07_output_height": "720"}, (1920, 720)),
        ({"nw07_output_width": "1280", "nw07_output_height": "720"}, (1280, 720)),
    ),
)
def test_nw07_dimension_settings_override_material_texture_without_mixing_axes(
    overrides: dict[str, str], expected: tuple[int, int]
) -> None:
    document = nw03.load_graph(GRAPH)
    for name in ("nw07_output_width", "nw07_output_height"):
        document.settings.pop(name, None)
    document.settings.update(overrides)
    optimized = optimizer.optimize_graph(document)
    assert nw05_export._graph_render_dimensions(document, optimized) == expected


def test_unknown_nw07_dimension_keys_do_not_override_the_material_texture() -> None:
    document = nw03.load_graph(GRAPH)
    for name in ("nw07_output_width", "nw07_output_height"):
        document.settings.pop(name, None)
    document.settings["nw07_output_widths"] = "1280"
    optimized = optimizer.optimize_graph(document)
    # Exact key matching preserves NW-06's texture-derived 256x256 output when a
    # future graph pass introduces or renames a dimension setting.
    assert nw05_export._graph_render_dimensions(document, optimized) == (256, 256)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda c: c["loop"].__setitem__("ticks", 480000),
        lambda c: c["loop"].__setitem__("duration_seconds", 10),
        lambda c: c["loop"].__setitem__("looping", False),
        lambda c: c["resolution"].__setitem__("width", 1280),
        lambda c: c["content"].__setitem__("torus_instances", 128),
        lambda c: c["artifact_budget"].__setitem__(
            "complete_distribution_bytes_ceiling", 524288
        ),
        lambda c: c["artifact_budget"].__setitem__(
            "stretch_is_mandatory_gate", True
        ),
        lambda c: c["artifact_budget"].__setitem__(
            "peak_engine_committed_gpu_bytes_ceiling", 1024 * 1024 * 1024
        ),
        lambda c: c["artifact_budget"].__setitem__(
            "process_start_to_first_present_ms_ceiling", 4000.0
        ),
        lambda c: c["image_tolerances"]["image"].__setitem__("absolute", 0.01),
        lambda c: c["image_tolerances"]["numeric"].__setitem__("absolute", 0.01),
        lambda c: c.__setitem__("requirements_digest", "sha256:v1:" + "0" * 64),
        lambda c: c["pinned_inputs"].pop(),
        lambda c: c.__setitem__("build_inputs_digest", ""),
    ),
)
def test_canonical_configuration_rejects_weakening(mutation) -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    mutation(config)
    with pytest.raises(export.Nw07ExportError):
        _load_from_record(config)


def _load_from_record(config: dict) -> None:
    """Apply the same validation path used for file-backed configuration."""
    import tempfile

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        export.load_canonical_config(path)


def test_pinned_input_contract_cannot_be_narrowed_outside_the_loader() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    config["pinned_inputs"] = config["pinned_inputs"][:-1]
    with pytest.raises(export.Nw07ExportError):
        export.compute_pinned_inputs(config)


def test_pinned_input_digest_tracks_build_inputs() -> None:
    config = export.load_canonical_config(CONFIG)
    first = export.verify_pinned_inputs(config)
    second = export.verify_pinned_inputs(config)
    assert first["digest"] == second["digest"]
    assert first["inputs"] == second["inputs"]
    assert set(first["inputs"]) == set(config["pinned_inputs"])
    assert first["inputs"]["fixtures/nw07/production_loop.nwgraph"] == config["graph_sha256"]
    assert first["frozen"] is True
    assert first["source_inputs_recorded"] is True
    assert first["matches_frozen_digest"] == (first["digest"] == config["build_inputs_digest"])


def test_pinned_input_hash_is_line_ending_stable(tmp_path: Path) -> None:
    lf = tmp_path / "lf.txt"
    crlf = tmp_path / "crlf.txt"
    lf.write_bytes(b"alpha\nbeta\ngamma\n")
    crlf.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    assert export.sha256_canonical_text_file(lf) == export.sha256_canonical_text_file(
        crlf
    )
    assert export.sha256_file(lf) != export.sha256_file(crlf)


def test_frozen_digest_matches_a_crlf_checkout(tmp_path: Path) -> None:
    config = export.load_canonical_config(CONFIG)
    for relative in config["pinned_inputs"]:
        source = REPO_ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        )
    inputs = export.compute_pinned_inputs(config, repo_root=tmp_path)
    assert export.build_inputs_digest(inputs) == export.verify_pinned_inputs(config)["digest"]
