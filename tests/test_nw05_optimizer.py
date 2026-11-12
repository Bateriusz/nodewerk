from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as compiler  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402


PROCEDURAL = REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph"
SEMANTICS = REPO_ROOT / "fixtures" / "nw05" / "optimization_semantics.nwgraph"
GOLDEN = REPO_ROOT / "fixtures" / "nw05" / "golden_optimized_ir.json"
GOLDEN_TEXT = REPO_ROOT / "fixtures" / "nw05" / "golden_optimized_ir.txt"


def _ir(path: Path) -> optimizer.OptimizedGraph:
    return optimizer.optimize_graph(compiler.load_graph(path))


def test_typed_lowering_matches_golden_optimized_ir() -> None:
    optimized = _ir(PROCEDURAL)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    payload = optimized.to_dict()
    for key, value in golden.items():
        assert payload[key] == value, key
    assert optimized.outputs[0].kind == "present"
    assert "image" in optimized.reachable_node_ids
    assert "texture_recipe" in optimized.partitions["initialization"]
    assert optimized.to_text().replace("\r\n", "\n") == GOLDEN_TEXT.read_text(
        encoding="utf-8"
    ).replace("\r\n", "\n")


def test_constant_propagation_and_partition_ordering() -> None:
    optimized = _ir(SEMANTICS)
    constants = {
        node.node_id: node.constant
        for node in optimized.nodes
        if node.constant is not None
    }
    assert constants["roughness"]["out"] == (0.0,)
    assert constants["scaled"]["out"] == (1.0, 2.0, 3.0, 10.0)
    assert "roughness" in optimized.partitions["compile-time"]
    assert "scaled" in optimized.partitions["compile-time"]
    assert optimized.partitions["initialization"]
    assert optimized.partitions["frame"]

    nodes = optimized.node_map()
    for partition in optimized.partitions.values():
        positions = {node_id: index for index, node_id in enumerate(partition)}
        for node_id in partition:
            for item in nodes[node_id].inputs:
                if item.source_node in positions:
                    assert positions[item.source_node] < positions[node_id]


def test_dead_node_dead_output_and_cse_elimination() -> None:
    optimized = _ir(SEMANTICS)
    assert "unused_curve" in optimized.dead_nodes
    assert "unused_curve" not in optimized.reachable_node_ids
    cse_pairs = {
        (record.canonical_node, merged)
        for record in optimized.cse
        for merged in record.merged_nodes
    }
    assert ("shade_a", "shade_b") in cse_pairs
    assert "shade_b" not in optimized.reachable_node_ids
    fullscreen_b = optimized.node_map()["fullscreen_b"]
    assert any(
        item.port == "shader" and item.source_node == "shade_a"
        for item in fullscreen_b.inputs
    )


def test_unreachable_additions_do_not_change_release_ir_identity() -> None:
    base = _ir(PROCEDURAL)
    text = PROCEDURAL.read_text(encoding="utf-8")
    text += (
        "\nnode unused_extra nw.math.scalar_const@1.0.0 frequency=frame\n"
        "  param value 99\n"
    )
    document, diagnostics = compiler.parse_graph_text(text, source_name="<extra>")
    assert document is not None
    assert not [item for item in diagnostics if item["severity"] == "error"]
    extended = optimizer.optimize_graph(document)
    assert "unused_extra" in extended.dead_nodes
    assert extended.ir_sha256() == base.ir_sha256()
    assert extended.reachable_node_ids == base.reachable_node_ids


def test_unused_output_does_not_change_retained_nodes() -> None:
    optimized = _ir(SEMANTICS)
    # gbuffer exposes color and normals; fullscreen outputs are independent.
    assert all(node_id != "unused_curve" for node_id in optimized.reachable_node_ids)
    before = set(optimized.reachable_node_ids)
    text = SEMANTICS.read_text(encoding="utf-8")
    text += (
        "\nnode unused_fullscreen nw.render.fullscreen@1.0.0 frequency=frame residency=gpu\n"
        "  input shader shade.out\n"
        "  input texture texture_gpu.out\n"
        "layout unused_fullscreen 900 120\n"
    )
    document, diagnostics = compiler.parse_graph_text(text, source_name="<extra-output>")
    assert document is not None
    assert not [item for item in diagnostics if item["severity"] == "error"]
    extended = optimizer.optimize_graph(document)
    # A terminal render output is reachable by graph semantics, but adding it
    # never introduces a second graph model or authoring data into the package.
    assert before <= set(extended.reachable_node_ids)


def test_unreachable_quantized_operator_does_not_change_release_identity() -> None:
    base = _ir(SEMANTICS)
    text = SEMANTICS.read_text(encoding="utf-8")
    text += (
        "\nnode unused_quantized nw.math.scalar_const@1.0.0 frequency=frame\n"
        "  param value 0.3333333\n"
    )
    document, diagnostics = compiler.parse_graph_text(text, source_name="<quantized-extra>")
    assert document is not None
    assert not [item for item in diagnostics if item["severity"] == "error"]
    extended = optimizer.optimize_graph(document)
    assert "unused_quantized" in extended.dead_nodes
    assert extended.ir_sha256() == base.ir_sha256()
    assert extended.quantization == base.quantization


def test_fusion_is_bounded_and_falls_back_when_unmeasured_or_unsupported() -> None:
    optimized = _ir(PROCEDURAL)
    assert optimized.fusions
    accepted = [record for record in optimized.fusions if record.accepted]
    assert accepted
    for record in accepted:
        assert record.correctness_preserved
        assert record.measured
        assert record.expression_bytes_after <= record.expression_bytes_before
    disabled = optimizer.optimize_graph(
        compiler.load_graph(PROCEDURAL), fusion_enabled=False
    )
    assert all(not record.accepted for record in disabled.fusions)
    assert all(
        record.unfused_fallback == "nw03-root-lowering"
        for record in disabled.fusions
    )
    bounded = optimizer.optimize_graph(
        compiler.load_graph(PROCEDURAL), fusion_max_nodes=0
    )
    assert all(not record.accepted for record in bounded.fusions)


def test_quantization_is_field_specific_tolerance_bounded_and_reversible() -> None:
    document = compiler.load_graph(SEMANTICS)
    quantized = optimizer.quantize_document(document, document.settings)
    assert len(quantized.decisions) == 1
    decision = quantized.decisions[0]
    assert decision.node_id == "gain"
    assert decision.parameter == "value"
    assert decision.within_tolerance
    assert decision.max_absolute_error <= decision.tolerance
    assert quantized.recover_full_precision()["gain.value"] == decision.original
    assert quantized.document.nodes["gain"].params["value"] != document.nodes["gain"].params["value"]
    # The source document retains full precision.
    assert document.nodes["gain"].params["value"] == ["0.123456789"]


def test_quantization_tolerance_violation_fails_closed() -> None:
    document = compiler.load_graph(SEMANTICS)
    tight = optimizer.quantize_document(
        document, document.settings, tolerance_override=0.0
    )
    assert tight.errors
    with pytest.raises(optimizer.OptimizationError):
        optimizer.optimize_graph(
            document, quantization_tolerance=0.0
        )


def test_quantization_can_be_disabled() -> None:
    document = compiler.load_graph(SEMANTICS)
    settings = dict(document.settings)
    settings["quantization_policy"] = "none"
    disabled = optimizer.quantize_document(document, settings)
    assert disabled.decisions == ()
    assert disabled.document.nodes["gain"].params["value"] == ["0.123456789"]


def test_link_manifest_lists_exact_reachability_and_exclusions() -> None:
    optimized = _ir(SEMANTICS)
    manifest = optimizer.build_link_manifest(optimized)
    assert manifest["profile_id"] == optimized.profile_id
    assert "shade_a" in manifest["reachable_content"]["node_ids"]
    assert "shade_b" not in manifest["reachable_content"]["node_ids"]
    assert "nw.proc.value_noise" in manifest["excluded"]["optional_implementations"]
    assert manifest["runtime_requirements"]["runtime_dxc_dependency"] is False
    assert manifest["excluded"]["gpu_name_predicates"] == []
    assert any(
        query["name"] == "ID3D12Device::GetResourceAllocationInfo"
        for query in manifest["feature_queries"]
    )


def test_dead_output_elimination_prunes_unused_multi_output_ports() -> None:
    document = compiler.load_graph(SEMANTICS)
    split = compiler.GraphNode(
        node_id="split_probe",
        operator_id="nw.math.vec4_split",
        version="1.0.0",
        frequency="frame",
        residency="cpu",
        params={},
        inputs={"v": ("shade", "out")},
    )
    document.nodes["split_probe"] = split
    document.nodes["roughness"].inputs["x"] = ("split_probe", "x")
    optimized = optimizer.optimize_graph(document)
    node = optimized.node_map()["split_probe"]
    assert set(node.outputs) == {"x"}
    assert ("split_probe", "y") in optimized.dead_outputs
    assert ("split_probe", "z") in optimized.dead_outputs
    assert ("split_probe", "w") in optimized.dead_outputs
    materialized = optimized.materialize_document()
    assert materialized.nodes["split_probe"].inputs["v"] == ("shade", "out")
    assert materialized.nodes["split_probe"].selected_outputs == ("x",)
    assert materialized.settings["nw05.outputs.split_probe"] == "x"
    full = compiler.GraphNode(
        **{
            **materialized.nodes["split_probe"].__dict__,
            "selected_outputs": ("w", "x", "y", "z"),
        }
    )
    reduced_document = compiler.GraphDocument(
        **{**materialized.__dict__, "nodes": dict(materialized.nodes)}
    )
    full_document = compiler.GraphDocument(
        **{
            **materialized.__dict__,
            "nodes": dict(materialized.nodes, split_probe=full),
        }
    )
    fingerprint_args = dict(
        semantic_digest="sha256:v1:test",
        dxc_identity="dxc:test",
        optimization="O2",
    )
    assert compiler._compiler_fingerprint(
        reduced_document, [], **fingerprint_args
    ) != compiler._compiler_fingerprint(
        full_document, [], **fingerprint_args
    )
    lowered, diagnostics = compiler.lower_to_fused_hlsl(materialized)
    materialized.nodes["shade"].selected_outputs = ("dead_port",)
    dead_lowered, dead_diagnostics = compiler.lower_to_fused_hlsl(
        materialized
    )
    assert "shade" in {shader.specialization_id for shader in lowered}
    assert "shade" not in {
        shader.specialization_id for shader in dead_lowered
    }
    assert not diagnostics
    assert not dead_diagnostics


def test_optimized_document_is_what_nw03_compiles() -> None:
    document = compiler.load_graph(SEMANTICS)
    optimized = optimizer.optimize_graph(document)
    materialized = optimized.materialize_document(
        generation_modes={"roughness": "bake", "shade": "bake"}
    )
    assert "unused_curve" not in materialized.nodes
    assert "shade_b" not in materialized.nodes
    assert materialized.nodes["roughness"].operator_id == "nw.math.scalar_const"
    assert materialized.nodes["roughness"].params["value"] == ["0.0"]
    assert "shade" in materialized.nodes
    assert materialized.settings["nw05.generation.roughness.mode"] == "bake"
    assert materialized.settings["nw05.generation.shade.mode"] == "bake"
    assert any(
        key.startswith("nw05.quantization.gain.value")
        for key in materialized.settings
    )


def test_materialized_folded_constants_have_no_dangling_inputs() -> None:
    optimized = _ir(SEMANTICS)
    materialized = optimized.materialize_document()
    for node in materialized.nodes.values():
        for _port, (source_node, _source_port) in node.inputs.items():
            assert source_node in materialized.nodes
    # The folded value is present as an explicit literal constant.
    assert any(
        node.operator_id == "nw.math.vec4_const"
        and node.node_id.startswith("__nw05_folded_")
        for node in materialized.nodes.values()
    )
    lowered, diagnostics = compiler.lower_to_fused_hlsl(materialized)
    assert lowered
    assert not diagnostics


def test_quantization_field_override_without_bits_uses_default_bits() -> None:
    document = compiler.load_graph(SEMANTICS)
    tolerance_settings = dict(document.settings)
    tolerance_settings["quantization_field.gain.value"] = "tolerance=0.01"
    tolerance_only = optimizer.quantize_document(document, tolerance_settings)
    assert not tolerance_only.errors
    assert tolerance_only.decisions

    step_settings = dict(document.settings)
    step_settings["quantization_tolerance"] = "0.05"
    step_settings["quantization_field.gain.value"] = "step=0.05"
    step_only = optimizer.quantize_document(document, step_settings)
    assert not step_only.errors
    assert step_only.decisions

