from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw13_evidence as evidence  # noqa: E402
import nw13_pipeline as pipeline  # noqa: E402
import nw04_fixture  # noqa: E402
import nw05_export  # noqa: E402


GRAPH = REPO_ROOT / "fixtures" / "nw13" / "hotel_corridor.nwgraph"


def test_shared_optimizer_reachability_partitions_and_semantic_hash() -> None:
    document = pipeline.load_graph(GRAPH)
    production = pipeline.optimize_document(document)
    assert "hotel_screen" in production.reachable
    assert "corridor_render" in production.reachable
    assert production.partitions["initialization"]
    assert production.partitions["frame"]
    assert production.semantic_hash.startswith("sha256:v1:")
    again = pipeline.optimize_document(pipeline.load_graph(GRAPH))
    assert again.semantic_hash == production.semantic_hash
    assert again.node_keys == production.node_keys

    dead = copy.deepcopy(document)
    dead.nodes["unreachable"] = type(document.nodes["wall_texture"])(
        node_id="unreachable",
        operator_id="nw.math.scalar_const",
        version="1.0.0",
        frequency="frame",
        residency="cpu",
        params={"value": ["7"]},
    )
    optimized = pipeline.optimize_document(dead)
    assert "unreachable" in optimized.dead_nodes
    assert "unreachable" not in optimized.reachable


def test_dependency_local_cache_invalidation_only_affects_dependents() -> None:
    document = pipeline.load_graph(GRAPH)
    invalidated = pipeline.dependency_local_invalidation(document, ["wall_texture"])
    assert "wall_texture" in invalidated
    assert "wall_texture_gpu" in invalidated
    assert "wall_material" in invalidated
    assert "corridor_render" in invalidated
    assert "hotel_screen" in invalidated
    assert "door_mesh" not in invalidated
    assert "lamp_light" not in invalidated
    assert "corridor_camera" not in invalidated


def test_admission_rejects_excessive_steps_instances_geometry_and_memory() -> None:
    document = pipeline.load_graph(GRAPH)
    production = pipeline.optimize_document(document)
    assert production.reachable
    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.admission_check(document, budget={"max_nodes": 1})
    assert any(item["code"] == "pipeline_node_budget_exceeded" for item in exc.value.diagnostics)

    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.admission_check(document, budget={"max_instances": 1})
    assert any(item["code"] == "pipeline_instance_budget_exceeded" for item in exc.value.diagnostics)

    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.admission_check(document, budget={"max_evaluation_steps": 1})
    assert any(item["code"] == "pipeline_evaluation_budget_exceeded" for item in exc.value.diagnostics)

    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.admission_check(document, budget={"max_memory_bytes": 1})
    assert any(item["code"] == "pipeline_memory_budget_exceeded" for item in exc.value.diagnostics)


def test_schedule_keeps_static_work_in_initialization_and_identifies_frame_work() -> None:
    production = pipeline.optimize_document(pipeline.load_graph(GRAPH))
    schedule = pipeline.build_schedule(production, start_tick=0, end_tick=400 * 10)
    assert schedule["steps"] == 10
    assert schedule["initialization"] == list(production.partitions["initialization"]) + list(production.partitions.get("compile-time", ()))
    assert schedule["frame"]
    assert schedule["timeline"][0]["initialization"]
    assert schedule["timeline"][1]["initialization"] == []
    assert schedule["timeline"][-1]["frame_dependent"] is True
    assert all(frame["history"] == [] for frame in schedule["timeline"])
    capped = pipeline.build_schedule(production, start_tick=0, end_tick=400 * 10, work_cap_steps=3)
    assert len(capped["timeline"]) == 3
    assert capped["pending_steps"] == 7


def test_versioning_and_explicit_migration() -> None:
    document = {"format": "nw13.pipeline", "version": 0, "graph": "fixtures/nw13/hotel_corridor.nwgraph"}
    migrated = pipeline.migrate_v0(document)
    assert migrated["version"] == 1
    assert migrated["migration"]["rule"]
    assert migrated["profile"]["id"] == "nw.common.d3d12.fl12_0.sm6_0.v1"
    with pytest.raises(pipeline.PipelineError):
        pipeline.migrate_v0({"format": "nw13.pipeline", "version": 99})


def test_frequency_residency_profile_and_seed_identities_change_hash() -> None:
    document = pipeline.load_graph(GRAPH)
    baseline = pipeline.semantic_hash(document)
    changed_frequency = copy.deepcopy(document)
    changed_frequency.nodes["wall_material"].frequency = "initialization"
    assert pipeline.semantic_hash(changed_frequency) != baseline
    changed_residency = copy.deepcopy(document)
    changed_residency.nodes["hotel_screen"].residency = "cpu"
    assert pipeline.semantic_hash(changed_residency) != baseline
    changed_profile = copy.deepcopy(document)
    changed_profile.profile_id = "nw.optional.d3d12.ray_query.ps6_5.v1"
    assert pipeline.semantic_hash(changed_profile) != baseline


def test_optional_lowering_eligibility_and_diagnostics() -> None:
    common = evidence.select_lowering("common", {})
    assert common["profile_id"] == evidence.COMMON_PROFILE_ID
    ray = evidence.select_lowering("dxr_inline_ray_query", {"raytracing_tier": 1.1, "highest_shader_model": "6_5"})
    assert ray["profile_id"] == evidence.RAY_PROFILE_ID
    assert ray["budgets"]["max_bounces"] == 1
    with pytest.raises(evidence.EvidenceError) as exc:
        evidence.select_lowering("dxr_inline_ray_query", {"raytracing_tier": None, "highest_shader_model": "6_0"})
    assert any(item["code"] == "nw13_optional_unsupported" for item in exc.value.diagnostics)
    with pytest.raises(evidence.EvidenceError):
        evidence.select_lowering("ser", {"highest_shader_model": "6_9"})


def test_lowering_matrix_records_special_cases_independently() -> None:
    matrix = evidence.optional_lowering_matrix()
    assert matrix["common_surface"]["rtx_required"] is False
    assert matrix["mesh_shader"]["status"] == "pending_unavailable"
    assert matrix["mesh_shader"]["decision"] == "defer"
    assert matrix["ser"]["status"] == "pending_unavailable"
    assert matrix["omm"]["status"] == "not_applicable"
    assert matrix["micromesh"]["status"] == "pending_unavailable"
    assert matrix["lighting_denoising_inference"]["excluded"]["online_nrc"] == "excluded"
    assert matrix["lighting_denoising_inference"]["excluded"]["asset_specific_fitting"] == "excluded"
    assert matrix["lighting_denoising_inference"]["pretrained_inference"]["frame_generation"]["requires"]


def test_work_graph_experiment_requires_measured_criterion() -> None:
    pending = evidence.work_graph_experiment(conventional_metric=10.0, work_graph_metric=None)
    assert pending["decision"] == "defer"
    assert pending["satisfies_completion"] is False
    adopted = evidence.work_graph_experiment(conventional_metric=10.0, work_graph_metric=9.0)
    assert adopted["decision"] == "adopt"
    assert adopted["satisfies_completion"] is True
    rejected = evidence.work_graph_experiment(conventional_metric=10.0, work_graph_metric=10.1)
    assert rejected["decision"] == "reject"


def test_export_artifacts_bind_provenance_and_independent_fallback() -> None:
    production = {"id": "nw13.hotel_corridor", "revision": "abc123"}
    capabilities = {"raytracing_tier": 1.1, "highest_shader_model": "6_5"}
    bundle = evidence.export_artifacts(production, capabilities=capabilities, requested_features=["dxr_inline_ray_query", "ser"])
    assert evidence.validate_bundle(bundle) == []
    assert bundle["common"]["profile_id"] == evidence.COMMON_PROFILE_ID
    assert bundle["common"]["revision"] == "abc123"
    assert bundle["fallback"]["artifact_id"] == "nw13.common.fallback"
    assert any(entry["artifact_id"].endswith("dxr_inline_ray_query") for entry in bundle["optional"])
    assert any(item["code"] == "nw13_optional_unsupported" for item in bundle["diagnostics"])

    failed = evidence.export_artifacts(production, capabilities=capabilities, requested_features=["dxr_inline_ray_query"], optional_build_fails=True)
    assert failed["common"]["smoke"]["status"] == "pass"
    assert failed["fallback"]["smoke"]["status"] == "pass"
    assert any(item["code"] == "nw13_optional_build_failed" for item in failed["diagnostics"])


def test_unreachable_optional_paths_add_zero_release_weight() -> None:
    unreachable = evidence.unreachable_incremental(False)
    assert unreachable["additional_release_code_bytes"] == 0
    assert unreachable["additional_shader_bytes"] == 0
    assert unreachable["additional_imports"] == []
    assert unreachable["additional_package_sections"] == []
    reachable = evidence.unreachable_incremental(True)
    assert reachable["additional_release_code_bytes"] > 0


def test_matched_arms_mark_unavailable_observations_explicitly() -> None:
    ledger = evidence.matched_arms(
        revision="abc123",
        common={"revision": "abc123", "distribution_bytes": 100, "shader_bytes": 20, "startup_ms": 1.0, "committed_memory_bytes": 1000, "vram_bytes": None, "cpu_submit_us": 2.0, "gpu_time_us": 3.0, "image_max_abs_error": 0.0, "image_rmse": 0.0},
        optional={"revision": "abc123", "distribution_bytes": 110, "shader_bytes": 25, "startup_ms": 1.1, "committed_memory_bytes": 1100, "vram_bytes": 2048, "cpu_submit_us": 2.1, "gpu_time_us": 2.9, "image_max_abs_error": 0.001, "image_rmse": 0.0005},
    )
    assert ledger["same_revision"] is True
    common = ledger["arms"][0]
    assert common["available"] is False
    assert "vram_bytes" in common["unavailable"]
    optional = ledger["arms"][1]
    assert optional["available"] is True


def test_recorded_lowering_and_artifact_evidence_is_internally_consistent() -> None:
    optional = json.loads((REPO_ROOT / "benchmarks" / "nw13" / "optional-lowering.v1.json").read_text(encoding="utf-8"))
    assert optional["per_device_decisions"]["mesh_shader"]["decision"] == "defer"
    assert optional["per_device_decisions"]["ray_query"]["decision"] == "capability_qualified_optional"
    assert optional["lighting_denoising_inference"]["excluded"]["online_nrc"] == "excluded"
    work_graph = json.loads((REPO_ROOT / "benchmarks" / "nw13" / "work-graph-experiment.v1.json").read_text(encoding="utf-8"))
    assert work_graph["decision"] == "defer"
    assert work_graph["satisfies_completion"] is False
    bundle = json.loads((REPO_ROOT / "benchmarks" / "nw13" / "artifact-bundle.v1.json").read_text(encoding="utf-8"))
    assert evidence.validate_bundle(bundle) == []
    assert bundle["common"]["revision"] == "1c9e1559c8d43530c7b213461c6719b6aa43ae35"
    ledger = json.loads((REPO_ROOT / "benchmarks" / "nw13" / "matched-arms.v1.json").read_text(encoding="utf-8"))
    assert ledger["unavailable_observations_explicit"] is True
    for arm in ledger["arms"]:
        assert arm["available"] is False
        assert "gpu_time_us" in arm["unavailable"]
    status = json.loads((REPO_ROOT / "benchmarks" / "nw13" / "evidence-status.v1.json").read_text(encoding="utf-8"))
    assert status["pending_is_not_pass"] is True
    assert status["observations"]["performance"]["status"] == "unavailable"
    assert status["optional_decisions"]["omm"] == "omit"


def test_production_graph_exports_reproducibly_through_shared_offline_compiler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    template = tmp_path / "template.exe"
    template.write_bytes(b"MZ" + b"\x00" * 4096)
    dxc = tmp_path / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        f"Path(sys.argv[sys.argv.index('-Fo') + 1]).write_bytes(bytes.fromhex({container.hex()!r}))\n",
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    settings = nw05_export.ExportSettings(
        byte_ceiling=4 * 1024 * 1024,
        memory_budget=256 * 1024 * 1024,
        player_template=str(template),
    )
    first = nw05_export.export_graph(
        GRAPH,
        tmp_path / "first",
        settings=settings,
        report_path=tmp_path / "first.json",
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    second = nw05_export.export_graph(
        GRAPH,
        tmp_path / "second",
        settings=settings,
        report_path=tmp_path / "second.json",
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    assert first.report["success"] is True
    assert second.report["success"] is True
    assert first.common_artifact_key == second.common_artifact_key
    common = first.artifacts[nw13_evidence_common_id()]
    assert common.profile_id == evidence.COMMON_PROFILE_ID
    assert common.emitted_requirements["required_init_instructions"]
    assert "frame.present" in common.emitted_requirements["required_frame_instructions"]
    assert common.artifact_key.startswith("sha256:v1:")


def nw13_evidence_common_id() -> str:
    return evidence.COMMON_PROFILE_ID

