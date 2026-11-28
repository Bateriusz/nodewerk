from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw13_evidence as evidence  # noqa: E402
import nw13_artifact_measure as artifact_measure  # noqa: E402
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
    dead.nodes["unreachable"] = type(document.nodes["room_outer"])(
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
    invalidated = pipeline.dependency_local_invalidation(document, ["room_outer"])
    assert {"room_outer", "room_shell", "film", "corridor_render", "hotel_screen"} <= invalidated
    assert {"door", "arch", "room_inner"}.isdisjoint(invalidated)


def test_admission_rejects_excessive_steps_instances_geometry_and_memory() -> None:
    document = pipeline.load_graph(GRAPH)
    production = pipeline.optimize_document(document)
    assert production.reachable
    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.admission_check(document, budget={"max_nodes": 1})
    assert any(item["code"] == "pipeline_node_budget_exceeded" for item in exc.value.diagnostics)

    oversized = copy.deepcopy(document)
    oversized.nodes["oversized"] = type(document.nodes["room_outer"])(
        "oversized", "nw.scene.instances", "1.0.0", "initialization", "cpu",
        params={"count": ["4097"]},
    )
    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.optimize_document(oversized)
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
    assert schedule["history"] == ["scene_feedback"]
    assert all(frame["history"] == ["scene_feedback"] for frame in schedule["timeline"])
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
    changed_frequency.nodes["film"].frequency = "initialization"
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
    # No build or smoke ran, so the bundle must not claim one.
    assert bundle["independent_builds"] is False
    assert bundle["common"]["distribution_bytes"] is None
    assert bundle["common"]["shader_bytes"] is None
    assert bundle["common"]["measurement_status"] == "unavailable"
    assert bundle["common"]["smoke"] == {"executed": False, "status": "unavailable", "headless": None}

    failed = evidence.export_artifacts(production, capabilities=capabilities, requested_features=["dxr_inline_ray_query"], optional_build_fails=True)
    assert failed["common"]["smoke"]["status"] == "unavailable"
    assert failed["fallback"]["smoke"]["status"] == "unavailable"
    assert any(item["code"] == "nw13_optional_build_failed" for item in failed["diagnostics"])


def test_export_artifacts_only_records_supplied_measurements_and_smoke() -> None:
    production = {"id": "nw13.hotel_corridor", "revision": "abc123"}
    observation = {
        "distribution_bytes": 123456,
        "shader_bytes": 4321,
        "sections": ["CAP ", "RES ", "SHDR"],
        "imports": ["d3d12.dll", "dxgi.dll"],
        "smoke": {"executed": True, "status": "pass", "headless": True, "returncode": 0},
        "startup_ms": 12.5,
    }
    bundle = evidence.export_artifacts(
        production,
        capabilities={"raytracing_tier": 1.1, "highest_shader_model": "6_5"},
        requested_features=["dxr_inline_ray_query"],
        common_observation=observation,
        optional_observations={"dxr_inline_ray_query": {**observation, "distribution_bytes": 130000}},
        independent_builds=True,
    )
    assert evidence.validate_bundle(bundle) == []
    assert bundle["independent_builds"] is True
    assert bundle["common"]["distribution_bytes"] == 123456
    assert bundle["common"]["smoke"]["status"] == "pass"
    assert bundle["common"]["measurement_status"] == "measured"
    optional = bundle["optional"][0]
    assert optional["distribution_bytes"] == 130000
    assert optional["measurement_status"] == "measured"

    inconsistent = evidence.seal_bundle(
        {
            **bundle,
            "common": {**bundle["common"], "smoke": {"executed": False, "status": "pass", "headless": True}},
        }
    )
    assert any("executed smoke" in item["message"] for item in evidence.validate_bundle(inconsistent))
    unmeasured = evidence.export_artifacts(production, independent_builds=True)
    assert any("independent_builds" in item["message"] for item in evidence.validate_bundle(unmeasured))

    large = {**observation, "distribution_bytes": 1_200_000, "shader_bytes": 240_000}
    informational = evidence.export_artifacts(
        production, common_observation=large, fallback_observation=large,
        capabilities={"raytracing_tier": 1.1, "highest_shader_model": "6_5"},
        requested_features=["dxr_inline_ray_query"], optional_observations={"dxr_inline_ray_query": large},
    )
    assert evidence.validate_bundle(informational) == []
    for artifact in (informational["common"], informational["fallback"], *informational["optional"]):
        assert artifact["distribution_bytes"] == 1_200_000 and artifact["shader_bytes"] == 240_000
        assert set(artifact["budget_checks"]).isdisjoint({"distribution_bytes", "shader_bytes"})
        assert artifact["within_budgets"] is None
    historical = evidence.seal_bundle({**informational, "common": {**informational["common"],
        "budget_checks": {"distribution_bytes": {"status": "exceeded", "limit": 262144}}}})
    assert evidence.validate_bundle(historical) == []
    invalid_memory = evidence.export_artifacts(
        production, common_observation={**large, "committed_memory_bytes": 65 * 1024 * 1024},
    )
    assert invalid_memory["common"]["within_budgets"] is False
    assert any(item["code"] == "nw13_budget_exceeded" and "committed_memory_bytes" in item["message"]
               for item in evidence.validate_bundle(invalid_memory))


def test_smoke_observation_records_real_execution(tmp_path: Path) -> None:
    # Windows CreateProcess cannot start a .py file directly. Launch the
    # interpreter explicitly, without shell association or chmod semantics.
    script = tmp_path / "smoke with space.py"
    script.write_text(
        "import sys\n"
        "assert sys.argv[1:] == ['--headless']\n"
        "print('qualified child stdout')\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    passed = evidence.smoke_observation(sys.executable, [str(script), "--headless"])
    assert passed["executed"] is True
    assert passed["status"] == "pass"
    assert passed["headless"] is True
    assert passed["returncode"] == 0
    assert passed["stdout_sha256"] == evidence.sha256_text("qualified child stdout\n")
    failing = tmp_path / "failing.py"
    failing.write_text("raise SystemExit(3)\n", encoding="utf-8")
    failed = evidence.smoke_observation(sys.executable, [str(failing), "--headless"])
    assert failed["executed"] is True
    assert failed["status"] == "fail"
    assert failed["returncode"] == 3


def test_artifact_measurement_without_toolchain_records_unavailable(tmp_path: Path) -> None:
    result = artifact_measure.write_evidence(
        repo_root=REPO_ROOT,
        production_path=REPO_ROOT / "fixtures" / "nw13" / "hotel_corridor.production.json",
        output_dir=tmp_path,
    )
    bundle = result["bundle"]
    ledger = result["ledger"]
    assert evidence.validate_bundle(bundle) == []
    assert bundle["independent_builds"] is False
    assert bundle["common"]["measurement_status"] == "unavailable"
    assert bundle["common"]["smoke"]["executed"] is False
    assert bundle["common"]["distribution_bytes"] is None
    for arm in ledger["arms"]:
        assert arm["available"] is False
        assert arm["metrics"]["distribution_bytes"] is None
        assert arm["metrics"]["shader_bytes"] is None
    assert (tmp_path / "artifact-bundle.v1.json").exists()
    assert (tmp_path / "matched-arms.v1.json").exists()


def test_unreachable_optional_paths_add_zero_release_weight() -> None:
    unreachable = evidence.unreachable_incremental(False)
    assert unreachable["measurement_status"] == "unavailable"
    assert unreachable["additional_release_code_bytes"] is None
    assert unreachable["additional_shader_bytes"] is None
    assert unreachable["additional_imports"] is None
    assert unreachable["additional_package_sections"] is None
    measured = evidence.unreachable_incremental(
        False,
        observations={
            "additional_release_code_bytes": 0,
            "additional_shader_bytes": 0,
            "additional_imports": [],
            "additional_package_sections": [],
            "additional_dependencies": [],
        },
    )
    assert measured["measurement_status"] == "measured"
    assert measured["additional_release_code_bytes"] == 0
    assert measured["additional_shader_bytes"] == 0
    assert measured["additional_imports"] == []
    assert measured["additional_package_sections"] == []


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
    assert bundle["revision"] is bundle["common"]["revision"] is None
    assert bundle["implementation_base"] == "1c9e1559c8d43530c7b213461c6719b6aa43ae35"
    assert bundle["source_state"] == "tracked_expectations_pending_native"
    assert bundle["independent_builds"] is False
    assert bundle["common"]["distribution_bytes"] is None
    assert bundle["common"]["shader_bytes"] is None
    assert bundle["common"]["smoke"]["executed"] is False
    assert bundle["common"]["smoke"]["status"] == "unavailable"
    assert bundle["common"]["within_budgets"] is None
    assert set(bundle["common"]["budget_checks"]) == {"committed_memory_bytes"}
    assert bundle["common"]["size_accounting"] is None
    assert bundle["fallback"]["smoke"]["executed"] is False
    for artifact in bundle["optional"]:
        assert artifact["distribution_bytes"] is None
        assert artifact["shader_bytes"] is None
        assert artifact["smoke"]["status"] == "unavailable"
    ledger = json.loads((REPO_ROOT / "benchmarks" / "nw13" / "matched-arms.v1.json").read_text(encoding="utf-8"))
    assert ledger["unavailable_observations_explicit"] is True
    assert ledger["revision"] is None
    for arm in ledger["arms"]:
        assert arm["available"] is False
        assert "gpu_time_us" in arm["unavailable"]
        assert "distribution_bytes" in arm["unavailable"]
        assert "shader_bytes" in arm["unavailable"]
        assert arm["metrics"]["distribution_bytes"] is None
        assert arm["metrics"]["shader_bytes"] is None
    status = json.loads((REPO_ROOT / "benchmarks" / "nw13" / "evidence-status.v1.json").read_text(encoding="utf-8"))
    assert status["pending_is_not_pass"] is True
    assert status["revision"] is None
    assert status["implementation_base"] == bundle["implementation_base"]
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



@pytest.mark.parametrize("status,returncode", [("fail", 70), ("error", None), ("pass", 70)])
def test_native_smoke_gate_rejects_executed_failure(status, returncode) -> None:
    bundle = evidence.export_artifacts(
        {"id": "test", "revision": "abcdef"},
        common_observation={"distribution_bytes": 100, "shader_bytes": 50, "smoke": {
            "executed": True, "status": status, "headless": True, "returncode": returncode,
        }},
    )
    assert bundle["common"]["measurement_status"] == "failed"
    assert any(item["code"] == "nw13_smoke_failed" for item in evidence.validate_bundle(bundle))
