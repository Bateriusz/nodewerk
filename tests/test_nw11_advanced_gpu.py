from __future__ import annotations

import copy
import importlib.util
import json
import struct
import sys
from array import array
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_contract  # noqa: E402
import nw07_export  # noqa: E402
import nw11_bundle as bundle  # noqa: E402
import nw11_build_bundle as build_bundle  # noqa: E402
import nw11_capability as capability  # noqa: E402
import nw11_contract as contract  # noqa: E402
import nw11_evidence as evidence  # noqa: E402
import nw11_experiment as experiment  # noqa: E402
import nw11_followup as followup  # noqa: E402
import nw11_graph_scene as graph_scene  # noqa: E402
import nw11_pe_dependencies as pe_dependencies  # noqa: E402
import nw11_shader_payloads as shader_payloads  # noqa: E402
import nw11_toolchain_identity as toolchain_identity  # noqa: E402
import nw11_verify as verify  # noqa: E402
from schema_validator import validate_instance  # noqa: E402

COMMIT = contract.IMPLEMENTATION_BASE


def _source_artifacts(tmp_path: Path) -> dict[str, Path]:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    files = {}
    for name, payload in (
        ("baseline.exe", b"nw11-baseline-bytes"),
        ("variant.exe", b"nw11-variant-bytes"),
        ("fallback.exe", b"nw11-fallback-bytes"),
        ("probe.exe", b"MZ-nw11-probe"),
    ):
        path = source / name
        path.write_bytes(payload)
        files[name] = path
    return files


def _build_bundle(
    tmp_path: Path,
    experiment_id: str = contract.EXPERIMENT_MESH_ID,
    *,
    output: str = "bundle",
) -> tuple[Path, dict]:
    files = _source_artifacts(tmp_path)
    target = tmp_path / output
    manifest = build_bundle.build(
        output_directory=target,
        experiment_id=experiment_id,
        commit=COMMIT,
        baseline_executable=files["baseline.exe"],
        variant_executable=files["variant.exe"],
        fallback_executable=files["fallback.exe"],
        capability_probe=files["probe.exe"],
        repo_root=REPO_ROOT,
    )
    return target, manifest


def _observation(
    *,
    device_id: int = 0x2805,
    mesh: str = "D3D12_MESH_SHADER_TIER_1",
    ray: str = "D3D12_RAYTRACING_TIER_1_1",
    shader_model: str = "D3D12_SHADER_MODEL_6_7",
    initialization: str = "ok",
) -> dict:
    return {
        "adapter": {
            "vendor_id": 0x10DE,
            "device_id": device_id,
            "adapter_name": "metadata only",
            "adapter_luid": "00000000:0000ABCD",
        },
        "driver_version": "610.88",
        "os_build": "10.0.26300.9818",
        "toolchain_identity": "nw11-test-toolchain",
        "features": {
            "d3d_feature_level": "D3D_FEATURE_LEVEL_12_1",
            "resource_binding_tier": "D3D12_RESOURCE_BINDING_TIER_2",
            "resource_heap_tier": "D3D12_RESOURCE_HEAP_TIER_2",
            "highest_shader_model": shader_model,
            "highest_shader_model_mesh": shader_model,
            "highest_shader_model_ray": shader_model,
            "mesh_shader_tier": mesh,
            "raytracing_tier": ray,
        },
        "initialization": {"status": initialization, "stage": "test"},
    }


def _measurement(
    *,
    gpu: float = 10.0,
    frame: float = 16.0,
    cpu: float = 5.0,
    distribution: int = 3000,
    shaders: int = 10,
    shader_ids: list[str] | None = None,
    cold_init: float = 900.0,
    vram: int = 300_000_000,
    image_error: float = 1e-5,
) -> dict:
    count = contract.MIN_RAW_SAMPLES_PER_RUN
    return {
        "gpu_time_samples": [gpu + (index % 10) * 0.0001 for index in range(count)],
        "cpu_submit_samples": [cpu] * count,
        "frame_time_samples": [frame + (index % 10) * 0.0001 for index in range(count)],
        "cold_initialization_ms": cold_init,
        "distribution_bytes": distribution,
        "shader_count": shaders,
        "shader_ids": list(shader_ids or []),
        "peak_engine_committed_gpu_bytes": vram,
        "image": {
            "max_abs_channel_error": image_error,
            "rmse": image_error / 2.0,
            "reference_kind": "raster_reference",
            "fixed_ticks": [0, 240000, 480000, 720000, 959999],
        },
    }


def _run(
    role: str,
    *,
    device_key: str = "pci:10de:2805",
    environment_key: str = "sha256:v1:" + "a" * 64,
    tier: str = "desktop_ada_representative",
    status: str = "eligible",
    measurement: dict | None = None,
    capability_status: str = "eligible",
) -> dict:
    return {
        "role": role,
        "device_tier": tier,
        "device_key": device_key,
        "environment_key": environment_key,
        "status": status,
        "artifact_path": {
            "baseline": "bin/common_baseline.exe",
            "variant": "bin/optional_variant.exe",
            "fallback": "bin/fallback_common.exe",
        }[role],
        "capability_decision": {
            "status": capability_status,
            "marketing_name_used_for_eligibility": False,
            "device_key": device_key,
            "environment_key": environment_key,
            "adapter": {"adapter_luid": "00000000:0000ABCD"},
        },
        "adapter": {
            "adapter_name": "metadata only",
            "adapter_luid": "00000000:0000ABCD",
            "vendor_id": 0x10DE,
            "device_id": 0x2805,
        },
        "measurement": measurement or _measurement(),
    }


def _evidence(
    bundle_root: Path,
    manifest: dict,
    experiment_document: dict,
    runs: list[dict],
) -> dict:
    manifest_sha = contract.sha256_bytes(
        (bundle_root / bundle.MANIFEST_NAME).read_bytes()
    )
    runs = [dict(item) for item in runs]
    accounting = manifest["accounting"]
    inventory = manifest["shader_inventory"]["roles"]
    distributed = {
        "baseline": accounting["baseline_distribution_bytes"],
        "variant": accounting["variant_distribution_bytes"],
        "fallback": accounting["fallback_distribution_bytes"],
    }
    for item in runs:
        role = item["role"]
        measurement = dict(item["measurement"])
        measurement["distribution_bytes"] = distributed[role]
        measurement["shader_ids"] = list(inventory[role])
        measurement["shader_count"] = len(inventory[role])
        image = dict(measurement["image"])
        image["reference_kind"] = experiment_document["criteria"]["image"][
            "reference_kind"
        ]
        image["fixed_ticks"] = list(
            experiment_document["reference"]["fixed_ticks"]
        )
        measurement["image"] = image
        item["measurement"] = measurement
        item["device_key"] = item["capability_decision"]["device_key"]
        item["environment_key"] = item["capability_decision"]["environment_key"]
    return {
        "format": evidence.EVIDENCE_FORMAT,
        "version": evidence.EVIDENCE_VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "commit": manifest["commit"],
        "bundle_id": manifest["bundle_id"],
        "bundle_manifest_sha256": manifest_sha,
        "experiment_id": experiment_document["experiment_id"],
        "profile_id": experiment_document["variant_profile_id"],
        "fallback_artifact_id": experiment_document["fallback_artifact_id"],
        "capability_profile_sha256": manifest["capability_profile"]["sha256"],
        "artifact_hashes": {
            item["path"]: item["sha256"]
            for item in manifest["artifacts"]
            if item["role"] in {"common_baseline", "optional_variant", "fallback_common"}
        },
        "runs": runs,
        "evidence_file": "evidence.json",
        "raw_per_sample_results_preserved": True,
        "operator_supplied_values": False,
    }


def _experiment(experiment_id: str = contract.EXPERIMENT_MESH_ID) -> dict:
    return experiment.load_experiment(
        REPO_ROOT / contract.EXPERIMENT_PATH_BY_ID[experiment_id]
    )


def _write_image_result(path: Path, reference_kind: str = "raster_reference") -> Path:
    path.write_text(
        json.dumps(
            {
                "format": "nw11-image-result",
                "version": 1,
                "task": contract.TASK,
                "implementation_base": contract.IMPLEMENTATION_BASE,
                "reference_kind": reference_kind,
                "fixed_ticks": list(contract.REFERENCE_FIXED_TICKS),
                "max_abs_channel_error": 0.0,
                "rmse": 0.0,
            }
        ),
        encoding="utf-8",
    )
    return path


def _load_collector():
    path = REPO_ROOT / "tools" / "nw11" / "collect-evidence.py"
    spec = importlib.util.spec_from_file_location("nw11_collect_evidence", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_comparator():
    path = REPO_ROOT / "tools" / "nw11" / "compare-captures.py"
    spec = importlib.util.spec_from_file_location("nw11_compare_captures", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_nw11_profiles_are_frozen_and_digest_bound() -> None:
    common = capability.load_profile(REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.COMMON_PROFILE_ID])
    mesh = capability.load_profile(REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.MESH_PROFILE_ID])
    ray = capability.load_profile(REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.RAY_QUERY_PROFILE_ID])

    assert common["requirements_digest"] == contract.COMMON_REQUIREMENTS_DIGEST
    assert common["fallback_artifact_id"] is None
    assert common["artifact_budget"]["incremental_distribution_bytes_ceiling"] == 0
    assert mesh["fallback_artifact_id"] == contract.COMMON_FALLBACK_ARTIFACT_ID
    assert ray["fallback_artifact_id"] == contract.COMMON_FALLBACK_ARTIFACT_ID
    assert mesh["requirements_digest"] == capability.compute_requirements_digest(mesh)
    assert ray["requirements_digest"] == capability.compute_requirements_digest(ray)
    assert mesh["requirements_digest"] != ray["requirements_digest"]
    assert mesh["shader_targets"] == list(contract.MESH_SHADER_TARGETS)
    assert ray["shader_targets"] == list(contract.RAY_QUERY_SHADER_TARGETS)
    assert any(
        query["query_id"] == "mesh_shader_tier"
        for query in mesh["queried_capabilities"]
    )
    assert any(
        query["query_id"] == "raytracing_tier"
        for query in ray["queried_capabilities"]
    )
    assert mesh["eligibility"]["gpu_marketing_name_used"] is False
    assert mesh["fallback_artifact"]["optional_payloads"] == []
    assert ray["runtime"]["agility_sdk"]["required"] is False
    assert mesh["evidence_status"] == "pending"
    assert common["requirements_digest"] == capability.compute_requirements_digest(common)
    assert common["evidence_status"] == "pending"
    assert all(
        device["status"] == "pending" for device in common["device_coverage"]
    )


@pytest.mark.parametrize(
    "experiment_id", [contract.EXPERIMENT_MESH_ID, contract.EXPERIMENT_RAY_ID]
)
def test_nw11_experiments_freeze_the_nw07_workload_and_success_criterion(
    experiment_id: str,
) -> None:
    document = _experiment(experiment_id)
    experiment.validate_experiment(document, repo_root=REPO_ROOT)
    assert document["status"] == "frozen"
    assert document["base_graph"]["sha256"] == contract.BASE_GRAPH_SHA256
    assert document["workload"]["torus_instances"] == 256
    assert document["workload"]["loop_ticks"] == 960000
    assert document["protocol"]["warmup_seconds"] == 5
    assert document["protocol"]["runs"] == 3
    assert document["protocol"]["run_seconds"] == 60
    assert document["protocol"]["application_pipeline_cache"] is False
    assert document["protocol"]["matched_environment_required"] is True
    assert document["criteria"]["primary"]["relative_reduction_percent_min"] == 10.0
    assert document["criteria"]["image"]["fixed_quality"] is True
    assert document["criteria"]["image"]["max_abs_channel_error"] <= 0.0001
    assert document["graph_derivation"]["new_operators"] == []
    assert document["graph_derivation"]["second_production_backend"] is False
    assert document["fallback_artifact_id"] == contract.COMMON_FALLBACK_ARTIFACT_ID
    for shader in document["pipeline"]["variant_shaders"]:
        assert (REPO_ROOT / shader["path"]).is_file()


def test_nw11_preserves_nw01_policy_and_nw07_common_bytes() -> None:
    capability_manifest = json.loads(
        (REPO_ROOT / "benchmarks" / "capability-manifest.v1.json").read_text(encoding="utf-8")
    )
    excluded = capability_manifest["excluded_baseline_features"]
    assert excluded["mesh_shaders"] is False
    assert excluded["ray_tracing"] is False
    assert excluded["runtime_shader_compilation"] is False
    assert excluded["mandatory_agility_sdk_redistribution"] is False

    config = nw07_export.load_canonical_config()
    pinned = nw07_export.verify_pinned_inputs(config)
    assert pinned["frozen"] is True
    assert pinned["matches_frozen_digest"] is True
    assert all(
        not path.startswith(("src/nw11", "shaders/nw11", "tools/nw11"))
        for path in config["pinned_inputs"]
    )
    assert nw07_contract.PROFILE_ID == contract.COMMON_PROFILE_ID
    assert nw07_contract.REQUIREMENTS_DIGEST == contract.COMMON_REQUIREMENTS_DIGEST


def test_nw11_malformed_profiles_and_experiments_fail_closed() -> None:
    mesh = json.loads(
        (REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.MESH_PROFILE_ID]).read_text(
            encoding="utf-8"
        )
    )
    tampered = copy.deepcopy(mesh)
    tampered["requirements_digest"] = "sha256:v1:" + "0" * 64
    with pytest.raises(capability.CapabilityProfileError):
        capability.validate_profile(tampered)

    tampered = copy.deepcopy(mesh)
    tampered["queried_capabilities"] = tampered["queried_capabilities"][:-1]
    with pytest.raises(capability.CapabilityProfileError):
        capability.validate_profile(tampered)

    tampered = copy.deepcopy(mesh)
    tampered["eligibility"]["gpu_marketing_name_used"] = True
    with pytest.raises(capability.CapabilityProfileError):
        capability.validate_profile(tampered)

    document = _experiment()
    weakened = copy.deepcopy(document)
    weakened["criteria"]["primary"]["relative_reduction_percent_min"] = 1.0
    with pytest.raises(experiment.ExperimentError):
        experiment.validate_experiment(weakened)

    weakened = copy.deepcopy(document)
    weakened["graph_derivation"]["second_production_backend"] = True
    with pytest.raises(experiment.ExperimentError):
        experiment.validate_experiment(weakened)

    weakened = copy.deepcopy(document)
    weakened["graph_derivation"]["new_operators"] = ["nw.render.second_backend"]
    with pytest.raises(experiment.ExperimentError):
        experiment.validate_experiment(weakened)

    weakened = copy.deepcopy(document)
    weakened["pipeline"]["variant_shaders"][0]["target"] = "vs_6_0"
    with pytest.raises(experiment.ExperimentError):
        experiment.validate_experiment(weakened)


def test_nw11_capability_absence_is_explicitly_unsupported() -> None:
    mesh = capability.load_profile(REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.MESH_PROFILE_ID])
    decision = capability.evaluate_observation(
        mesh, _observation(mesh="D3D12_MESH_SHADER_TIER_NOT_SUPPORTED")
    )
    assert decision["status"] == "unsupported"
    assert decision["satisfies_optional_profile"] is False
    assert decision["marketing_name_used_for_eligibility"] is False
    assert decision["diagnostics"][0]["code"] == "NW11-CAPABILITY-UNSUPPORTED"

    missing = _observation()
    del missing["features"]["mesh_shader_tier"]
    pending = capability.evaluate_observation(mesh, missing)
    assert pending["status"] == "pending"
    assert pending["satisfies_optional_profile"] is False

    ray = capability.load_profile(REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.RAY_QUERY_PROFILE_ID])
    eligible = capability.evaluate_observation(ray, _observation())
    assert eligible["status"] == "eligible"
    assert eligible["satisfies_optional_profile"] is True


def test_nw11_initialization_failure_is_preserved_without_substitution() -> None:
    mesh = capability.load_profile(REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.MESH_PROFILE_ID])
    decision = capability.evaluate_observation(
        mesh, _observation(initialization="failed")
    )
    assert decision["status"] == "failed"
    assert decision["satisfies_optional_profile"] is False
    assert decision["diagnostics"][0]["code"] == "NW11-INITIALIZATION-FAILED"


def test_nw11_bundle_is_deterministic_and_self_contained(tmp_path: Path) -> None:
    first, first_manifest = _build_bundle(tmp_path, output="first")
    second, second_manifest = _build_bundle(tmp_path, output="second")
    assert (first / bundle.MANIFEST_NAME).read_bytes() == (
        second / bundle.MANIFEST_NAME
    ).read_bytes()
    assert first_manifest["bundle_id"] == second_manifest["bundle_id"]
    verified = bundle.verify_bundle(first, expected_commit=COMMIT)
    assert verified["status"] == "verified"
    paths = {item["path"] for item in first_manifest["artifacts"]}
    for required in (
        "run-validation.ps1",
        "bin/common_baseline.exe",
        "bin/optional_variant.exe",
        "bin/fallback_common.exe",
        "bin/nw11_capability_probe.exe",
        "manifests/capability-profile.json",
        "manifests/experiment.json",
        "tools/nw11/nw11_verify.py",
        "tools/nw11/collect-evidence.py",
    ):
        assert required in paths
    assert first_manifest["verification"]["operator_supplied_values_allowed"] is False
    assert first_manifest["accounting"]["within_incremental_budget"] is True
    fallback = next(
        artifact
        for artifact in first_manifest["artifacts"]
        if artifact["role"] == "fallback_common"
    )
    assert fallback["profile_id"] == contract.COMMON_PROFILE_ID
    assert set(fallback["shader_targets"]) <= set(contract.COMMON_SHADER_TARGETS)
    assert fallback["optional"] is False
    manifest_schema = json.loads(
        (REPO_ROOT / "schemas" / "nw11-bundle-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validate_instance(json.loads((first / bundle.MANIFEST_NAME).read_text()), manifest_schema, manifest_schema)
    cache = first / "tools" / "nw11" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "nw11_contract.cpython-312.pyc").write_bytes(b"interpreter-cache")
    bundle.verify_bundle(first, expected_commit=COMMIT)
    (first / "bin" / "unrecorded.dll").write_bytes(b"unrecorded-payload")
    with pytest.raises(bundle.BundleError, match="unrecorded payload"):
        bundle.verify_bundle(first, expected_commit=COMMIT)


def test_nw11_zero_cost_stripping_rejects_optional_payload_in_common(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    tampered = copy.deepcopy(manifest)
    for artifact in tampered["artifacts"]:
        if artifact["role"] == "common_baseline":
            artifact["shader_targets"] = ["vs_6_0", "ps_6_0", "ms_6_5"]
    identity = {
        key: tampered[key]
        for key in (
            "repository",
            "commit",
            "experiment_id",
            "profile_id",
            "fallback_artifact_id",
            "bundle_version",
            "artifacts",
            "capability_profile",
            "experiment_definition",
            "toolchain",
            "required_capabilities",
            "dependency_ledger",
            "accounting",
            "shader_inventory",
            "shader_payloads",
            "dependency_report",
            "canonical_production",
        )
        if key in tampered
    }
    tampered["bundle_id"] = contract.digest_value(identity)
    bundle.write_manifest(root, tampered)
    with pytest.raises(bundle.BundleError, match="optional shader targets"):
        bundle.verify_bundle(root)


def test_nw11_malformed_bundle_and_stale_commit_fail_closed(tmp_path: Path) -> None:
    root, _ = _build_bundle(tmp_path)
    (root / bundle.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert verify.main(["bundle", "--bundle-root", str(root)]) == verify.EXIT_IDENTITY

    root, _ = _build_bundle(tmp_path, output="second")
    with pytest.raises(bundle.BundleError, match="expected tested commit"):
        bundle.verify_bundle(root, expected_commit="0" * 40)


@pytest.mark.parametrize(
    ("measurement", "expected"),
    (
        ({}, "accepted"),
        ({"variant": _measurement(gpu=9.5)}, "rejected"),
        ({"variant": _measurement(gpu=7.5, frame=17.0)}, "rejected"),
        ({"variant": _measurement(gpu=7.5, distribution=2_000_000)}, "rejected"),
        ({"variant": _measurement(gpu=7.5, image_error=0.01)}, "rejected"),
    ),
)
def test_nw11_matched_run_evaluation_rejects_regressions(
    measurement: dict, expected: str, tmp_path: Path
) -> None:
    _root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    payload = {
        "device_key": "pci:10de:2805",
        "environment_key": "sha256:v1:" + "a" * 64,
        "baseline": _measurement(gpu=10.0),
        "variant": measurement.get("variant", _measurement(gpu=7.5)),
    }
    if "variant" not in measurement:
        payload["variant"] = _measurement(gpu=7.5, frame=15.5)
    result = experiment.evaluate_experiment(
        document,
        payload,
        device_key="pci:10de:2805",
        environment_key="sha256:v1:" + "a" * 64,
    )
    assert result["status"] == expected
    assert result["satisfies_completion"] is (expected == "accepted")
    assert result["raw_per_sample_results"]["baseline"]["gpu_time_samples"]


def test_nw11_evaluation_never_crosses_device_or_environment(
    tmp_path: Path,
) -> None:
    document = _experiment()
    payload = {
        "device_key": "pci:10de:2805",
        "environment_key": "sha256:v1:" + "a" * 64,
        "baseline": _measurement(gpu=10.0),
        "variant": _measurement(gpu=7.5),
    }
    with pytest.raises(experiment.ExperimentError, match="device does not match"):
        experiment.evaluate_experiment(
            document,
            payload,
            device_key="pci:10de:2c05",
            environment_key="sha256:v1:" + "a" * 64,
        )
    with pytest.raises(experiment.ExperimentError, match="environment does not match"):
        experiment.evaluate_experiment(
            document,
            payload,
            device_key="pci:10de:2805",
            environment_key="sha256:v1:" + "b" * 64,
        )


def test_nw11_per_device_decisions_are_isolated(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    ada_baseline = _run("baseline", measurement=_measurement(gpu=10.0))
    ada_variant = _run("variant", measurement=_measurement(gpu=7.5))
    ada_fallback = _run("fallback", measurement=_measurement(gpu=10.0))
    document_evidence = _evidence(
        root, manifest, document, [ada_baseline, ada_variant, ada_fallback]
    )
    decisions = evidence.build_decision_document(
        evidence=document_evidence,
        experiment=document,
        bundle_manifest=manifest,
        bundle_manifest_sha256=document_evidence["bundle_manifest_sha256"],
        expected_commit=COMMIT,
    )
    by_tier = {item["device_tier"]: item for item in decisions["devices"]}
    assert by_tier["desktop_ada_representative"]["status"] == "accepted"
    assert by_tier["desktop_blackwell_representative"]["status"] == "pending"
    assert decisions["aggregate_status"] == "pending"
    assert decisions["satisfies_completion"] is False

    blackwell_baseline = _run(
        "baseline",
        device_key="pci:10de:2c05",
        environment_key="sha256:v1:" + "c" * 64,
        tier="desktop_blackwell_representative",
        measurement=_measurement(gpu=10.0),
    )
    blackwell_variant = _run(
        "variant",
        device_key="pci:10de:2c05",
        environment_key="sha256:v1:" + "c" * 64,
        tier="desktop_blackwell_representative",
        measurement=_measurement(gpu=7.5),
    )
    blackwell_fallback = _run(
        "fallback",
        device_key="pci:10de:2c05",
        environment_key="sha256:v1:" + "c" * 64,
        tier="desktop_blackwell_representative",
        measurement=_measurement(gpu=10.0),
    )
    both = _evidence(
        root,
        manifest,
        document,
        [
            ada_baseline,
            ada_variant,
            ada_fallback,
            blackwell_baseline,
            blackwell_variant,
            blackwell_fallback,
        ],
    )
    complete = evidence.build_decision_document(
        evidence=both,
        experiment=document,
        bundle_manifest=manifest,
        bundle_manifest_sha256=both["bundle_manifest_sha256"],
        expected_commit=COMMIT,
    )
    assert complete["aggregate_status"] == "accepted"
    assert complete["satisfies_completion"] is True
    decision_schema = json.loads(
        (REPO_ROOT / "schemas" / "nw11-decision.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validate_instance(complete, decision_schema, decision_schema)

    mismatched = _evidence(
        root,
        manifest,
        document,
        [
            ada_baseline,
            _run(
                "variant",
                device_key="pci:10de:2c05",
                environment_key="sha256:v1:" + "c" * 64,
                measurement=_measurement(gpu=7.5),
            ),
            ada_fallback,
        ],
    )
    isolated = evidence.build_decision_document(
        evidence=mismatched,
        experiment=document,
        bundle_manifest=manifest,
        bundle_manifest_sha256=mismatched["bundle_manifest_sha256"],
        expected_commit=COMMIT,
    )
    assert isolated["satisfies_completion"] is False
    assert all(item["status"] == "pending" for item in isolated["devices"])


def test_nw11_stale_evidence_and_artifact_hashes_are_rejected(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    base_evidence = _evidence(
        root,
        manifest,
        document,
        [
            _run("baseline", measurement=_measurement(gpu=10.0)),
            _run("variant", measurement=_measurement(gpu=7.5)),
            _run("fallback", measurement=_measurement(gpu=10.0)),
        ],
    )
    stale = copy.deepcopy(base_evidence)
    stale["commit"] = "1" * 40
    with pytest.raises(evidence.StaleEvidenceError):
        evidence.validate_evidence(
            stale,
            bundle_manifest=manifest,
            experiment=document,
            bundle_manifest_sha256=base_evidence["bundle_manifest_sha256"],
            expected_commit=COMMIT,
        )

    stale_hash = copy.deepcopy(base_evidence)
    stale_hash["artifact_hashes"]["bin/common_baseline.exe"] = "0" * 64
    with pytest.raises(evidence.StaleEvidenceError):
        evidence.validate_evidence(
            stale_hash,
            bundle_manifest=manifest,
            experiment=document,
            bundle_manifest_sha256=base_evidence["bundle_manifest_sha256"],
            expected_commit=COMMIT,
        )

    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(stale), encoding="utf-8")
    assert verify.main(
        [
            "evidence",
            "--bundle-root",
            str(root),
            "--evidence",
            str(evidence_path),
            "--expected-commit",
            COMMIT,
            "--require-accepted",
        ]
    ) == verify.EXIT_STALE


def test_nw11_capability_cli_exit_codes(tmp_path: Path) -> None:
    mesh_profile = REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.MESH_PROFILE_ID]
    unsupported = tmp_path / "unsupported.json"
    unsupported.write_text(
        json.dumps(_observation(mesh="D3D12_MESH_SHADER_TIER_NOT_SUPPORTED")),
        encoding="utf-8",
    )
    assert verify.main(
        [
            "capability",
            "--profile",
            str(mesh_profile),
            "--observation",
            str(unsupported),
        ]
    ) == verify.EXIT_UNSUPPORTED

    failed = tmp_path / "failed.json"
    failed.write_text(json.dumps(_observation(initialization="failed")), encoding="utf-8")
    assert verify.main(
        [
            "capability",
            "--profile",
            str(mesh_profile),
            "--observation",
            str(failed),
        ]
    ) == verify.EXIT_EXECUTION_FAILED


def test_nw11_collected_evidence_preserves_raw_runs_and_pending_coverage(
    tmp_path: Path,
) -> None:
    import subprocess

    root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    observation_path = tmp_path / "observation.json"
    observation_path.write_text(json.dumps(_observation()), encoding="utf-8")
    eligibility_path = tmp_path / "eligibility.json"
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "nw11_verify.py"),
            "capability",
            "--profile",
            str(root / "manifests/capability-profile.json"),
            "--observation",
            str(observation_path),
            "--json-out",
            str(eligibility_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    eligibility = json.loads(eligibility_path.read_text(encoding="utf-8"))
    common_eligibility_path = tmp_path / "common-eligibility.json"
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "nw11_verify.py"),
            "capability",
            "--profile",
            str(root / "manifests/common-capability-profile.json"),
            "--observation",
            str(observation_path),
            "--json-out",
            str(common_eligibility_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    common_eligibility = json.loads(
        common_eligibility_path.read_text(encoding="utf-8")
    )
    accounting = manifest["accounting"]
    inventory = manifest["shader_inventory"]["roles"]
    reports = []
    for role, gpu, artifact, decision, distribution in (
        (
            "baseline",
            10.0,
            "bin/common_baseline.exe",
            common_eligibility,
            accounting["baseline_distribution_bytes"],
        ),
        (
            "variant",
            7.5,
            "bin/optional_variant.exe",
            eligibility,
            accounting["variant_distribution_bytes"],
        ),
        (
            "fallback",
            10.0,
            "bin/fallback_common.exe",
            common_eligibility,
            accounting["fallback_distribution_bytes"],
        ),
    ):
        for run_index in range(contract.PERF_RUN_COUNT):
            measurement = _measurement(
                gpu=gpu,
                distribution=distribution,
                shaders=len(inventory[role]),
                shader_ids=inventory[role],
            )
            measurement["image"]["reference_kind"] = document["criteria"]["image"][
                "reference_kind"
            ]
            if role == "baseline":
                # A measured binary must never be able to self-report a
                # passing fixed-quality result.  The collector must replace
                # this with the independent capture comparison.
                measurement["image"]["max_abs_channel_error"] = 5.0
                measurement["image"]["rmse"] = 5.0
            report = _run(role, measurement=measurement)
            report["device_key"] = eligibility["device_key"]
            report["environment_key"] = eligibility["environment_key"]
            report["artifact_path"] = artifact
            report["capability_decision"] = decision
            path = tmp_path / f"{role}-{run_index}.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            reports.append(path)
    evidence_path = tmp_path / "collected.json"
    image_result = _write_image_result(
        tmp_path / "image-result.json",
        document["criteria"]["image"]["reference_kind"],
    )
    fallback_image_result = _write_image_result(
        tmp_path / "fallback-image-result.json",
        document["criteria"]["image"]["reference_kind"],
    )
    arguments = [
        sys.executable,
        str(REPO_ROOT / "tools" / "nw11" / "collect-evidence.py"),
        "--bundle-root",
        str(root),
        "--expected-commit",
        COMMIT,
        "--eligibility",
        str(eligibility_path),
        "--common-eligibility",
        str(common_eligibility_path),
        "--image-result",
        str(image_result),
        "--fallback-image-result",
        str(fallback_image_result),
        "--out",
        str(evidence_path),
    ]
    for report in reports:
        arguments.extend(["--run-report", str(report)])
    subprocess.run(arguments, check=True)
    collected = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert len(collected["runs"]) == 3
    assert collected["runs"][0]["process_run_count"] == contract.PERF_RUN_COUNT
    assert collected["raw_per_sample_results_preserved"] is True
    baseline_run = next(
        run for run in collected["runs"] if run["role"] == "baseline"
    )
    assert baseline_run["measurement"]["image"]["max_abs_channel_error"] == 0.0
    assert baseline_run["measurement"]["image"]["rmse"] == 0.0

    decisions_path = tmp_path / "decisions.json"
    code = verify.main(
        [
            "evidence",
            "--bundle-root",
            str(root),
            "--evidence",
            str(evidence_path),
            "--expected-commit",
            COMMIT,
            "--require-accepted",
            "--json-out",
            str(decisions_path),
        ]
    )
    assert code == verify.EXIT_PENDING
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    assert decisions["aggregate_status"] == "pending"
    assert decisions["satisfies_completion"] is False


def test_nw11_launcher_is_one_command_and_verifies_before_measuring() -> None:
    launcher = (REPO_ROOT / "tools" / "nw11" / "run-validation.ps1").read_text(
        encoding="utf-8"
    )
    assert "$BundleRoot" in launcher
    assert "$ExpectedCommit" in launcher
    assert "$VerifyOnly" in launcher
    assert "$DryRun" in launcher
    assert "tools/nw11/nw11_verify.py" in launcher
    assert "collect-evidence.py" in launcher
    assert "capability-snapshot.json" in launcher
    assert "exit 3" in launcher
    assert "& $Python.Command @all | Out-Host" in launcher
    assert '$resolved += @("--runs", "1")' in launcher
    assert "pip install" not in launcher
    assert "cmake" not in launcher.lower()
    assert "visual studio" not in launcher.lower()
    verify_position = launcher.index('"bundle", "--bundle-root"')
    measure_position = launcher.index("foreach ($pair in @(")
    assert verify_position < measure_position


def test_nw11_evidence_rejects_adapter_luid_mismatch(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    runs = [_run("baseline"), _run("variant"), _run("fallback")]
    runs[1]["adapter"]["adapter_luid"] = "00000000:0000FFFF"
    document_evidence = _evidence(root, manifest, document, runs)
    manifest_sha = contract.sha256_bytes(
        (root / bundle.MANIFEST_NAME).read_bytes()
    )
    with pytest.raises(evidence.EvidenceError):
        evidence.build_decision_document(
            evidence=document_evidence,
            experiment=document,
            bundle_manifest=manifest,
            bundle_manifest_sha256=manifest_sha,
            schema_root=root,
        )


def test_nw11_shader_manifest_declares_synchronization_and_lifetimes() -> None:
    manifest = json.loads(
        (REPO_ROOT / "shaders" / "nw11" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["common_profile_excluded"] is True
    assert manifest["optional_sdk_dependencies"] == []
    entries = {entry["shader_id"]: entry for entry in manifest["shaders"]}
    mesh = entries["nw11_torus_meshlet_ms"]
    assert mesh["optional"] is True
    assert mesh["target_profile"] == "ms_6_5"
    assert mesh["synchronization"]["group_barrier_required"] is True
    mesh_source = (REPO_ROOT / mesh["source_path"]).read_text(encoding="utf-8")
    assert "GroupMemoryBarrierWithGroupSync" in mesh_source
    assert "SetMeshOutputCounts" in mesh_source

    ray = entries["nw11_scene_rayquery_ps"]
    assert ray["optional"] is True
    assert ray["target_profile"] == "ps_6_5"
    assert ray["resource_lifetime"]["acceleration_structure_outlives_frame"] is True
    ray_source = (REPO_ROOT / ray["source_path"]).read_text(encoding="utf-8")
    assert "RayQuery<" in ray_source
    assert "TraceRayInline" in ray_source
    assert "CommittedStatus" in ray_source
    assert "CommittedPrimitiveIndex()" in ray_source
    assert "CommittedObjectToWorld3x4()" in ray_source
    assert "CommittedWorldSpaceGeometryNormal" not in ray_source
    assert "NwIndices.Load" in ray_source and "NwVertices.Load" in ray_source

    analytic = entries["nw11_analytic_reflection_ps"]
    assert analytic["optional"] is False
    assert analytic["target_profile"] == "ps_6_0"
    assert bundle.validate_variant_sources(REPO_ROOT)["status"] == "verified"


def test_nw11_follow_up_matrix_is_bounded_and_complete() -> None:
    document = followup.load_matrix(REPO_ROOT / contract.FOLLOWUP_MATRIX_PATH)
    assert [entry["feature"] for entry in document["features"]] == list(
        contract.REQUIRED_FOLLOWUP_FEATURES
    )
    assert all(
        entry["prototypes_in_this_task"] is False for entry in document["features"]
    )
    assert all(
        entry["byte_cost"]["status"] in {"measured", "unmeasured_estimate"}
        for entry in document["features"]
    )
    costs = {entry["item"] for entry in document["vendor_optional_costs"]}
    assert costs == set(contract.VENDOR_OPTIONAL_COST_ITEMS)

    overreaching = copy.deepcopy(document)
    overreaching["features"][0]["prototypes_in_this_task"] = True
    with pytest.raises(followup.FollowupMatrixError):
        followup.validate_matrix(overreaching)

    missing = copy.deepcopy(document)
    missing["features"] = missing["features"][:-1]
    with pytest.raises(followup.FollowupMatrixError):
        followup.validate_matrix(missing)


def test_nw11_common_digest_is_self_consistent_and_templates_validate() -> None:
    common = capability.load_profile(
        REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.COMMON_PROFILE_ID]
    )
    assert capability.compute_requirements_digest(common) == contract.COMMON_REQUIREMENTS_DIGEST
    for profile_id in (
        contract.COMMON_PROFILE_ID,
        contract.MESH_PROFILE_ID,
        contract.RAY_QUERY_PROFILE_ID,
    ):
        template = capability.profile_manifest_template(profile_id)
        capability.validate_profile(template)


def test_nw11_pending_capability_query_never_reaches_optional_execution(
    tmp_path: Path,
) -> None:
    mesh_profile = REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.MESH_PROFILE_ID]
    observation = _observation()
    del observation["features"]["mesh_shader_tier"]
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(observation), encoding="utf-8")
    assert verify.main(
        ["capability", "--profile", str(mesh_profile), "--observation", str(path)]
    ) == verify.EXIT_PENDING
    launcher = (
        REPO_ROOT / "tools" / "nw11" / "run-validation.ps1"
    ).read_text(encoding="utf-8")
    assert "$eligibilityCode -eq 11" in launcher
    assert "$eligibilityCode -eq 3" in launcher
    pending_position = launcher.index("$eligibilityCode -eq 11")
    loop_position = launcher.index("foreach ($pair in @(")
    assert pending_position < loop_position


def test_nw11_malformed_run_status_and_missing_constants_fail_closed(
    tmp_path: Path,
) -> None:
    root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    runs = [
        _run("baseline", measurement=_measurement(gpu=10.0)),
        _run("variant", measurement=_measurement(gpu=7.5)),
        _run("fallback", measurement=_measurement(gpu=10.0)),
    ]
    evidence_document = _evidence(root, manifest, document, runs)
    malformed = copy.deepcopy(evidence_document)
    malformed["runs"][0]["status"] = "definitely-not-a-status"
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_evidence(
            malformed,
            bundle_manifest=manifest,
            experiment=document,
            bundle_manifest_sha256=evidence_document["bundle_manifest_sha256"],
            expected_commit=COMMIT,
            schema_root=root,
        )

    no_constants = copy.deepcopy(evidence_document)
    del no_constants["raw_per_sample_results_preserved"]
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_evidence(
            no_constants,
            bundle_manifest=manifest,
            experiment=document,
            bundle_manifest_sha256=evidence_document["bundle_manifest_sha256"],
            expected_commit=COMMIT,
            schema_root=root,
        )

    operator_values = copy.deepcopy(evidence_document)
    operator_values["operator_supplied_values"] = True
    with pytest.raises(evidence.EvidenceError):
        evidence.validate_evidence(
            operator_values,
            bundle_manifest=manifest,
            experiment=document,
            bundle_manifest_sha256=evidence_document["bundle_manifest_sha256"],
            expected_commit=COMMIT,
            schema_root=root,
        )


def test_nw11_fallback_must_be_executed_and_role_bound(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    without_fallback = _evidence(
        root,
        manifest,
        document,
        [
            _run("baseline", measurement=_measurement(gpu=10.0)),
            _run("variant", measurement=_measurement(gpu=7.5)),
        ],
    )
    with pytest.raises(evidence.EvidenceError, match="fallback"):
        evidence.validate_evidence(
            without_fallback,
            bundle_manifest=manifest,
            experiment=document,
            bundle_manifest_sha256=without_fallback["bundle_manifest_sha256"],
            expected_commit=COMMIT,
        )

    wrong_role = _evidence(
        root,
        manifest,
        document,
        [
            _run("baseline", measurement=_measurement(gpu=10.0)),
            _run("variant", measurement=_measurement(gpu=7.5)),
            _run("fallback", measurement=_measurement(gpu=10.0)),
        ],
    )
    wrong_role["runs"][2]["artifact_path"] = "bin/optional_variant.exe"
    with pytest.raises(evidence.EvidenceError, match="fallback"):
        evidence.validate_evidence(
            wrong_role,
            bundle_manifest=manifest,
            experiment=document,
            bundle_manifest_sha256=wrong_role["bundle_manifest_sha256"],
            expected_commit=COMMIT,
        )


def test_nw11_bundle_accounts_optional_and_shared_shader_bytes(
    tmp_path: Path,
) -> None:
    root, manifest = _build_bundle(tmp_path)
    accounting = manifest["accounting"]
    assert accounting["optional_shader_bytes"] > 0
    assert accounting["optional_shader_count"] >= 2
    assert accounting["variant_distribution_bytes"] >= (
        accounting["optional_distribution_bytes"]
        + accounting["optional_shader_bytes"]
    )
    shader_entries = [
        item for item in manifest["artifacts"] if item["role"] == "shader"
    ]
    assert shader_entries
    assert all(item["distributed_bytes"] == item["bytes"] for item in shader_entries)
    paths = {item["path"] for item in manifest["artifacts"]}
    assert "shaders/nw06/nw06_scene_ps.hlsl" in paths
    assert "shaders/nw06/nw06_bloom_cs.hlsl" in paths
    assert "shaders/nw06/nw06_tonemap_cs.hlsl" in paths
    assert "shaders/display_fixture.hlsl" in paths
    assert "manifests/shader-manifest.json" in paths
    assert (root / "shaders/nw06/nw06_scene_ps.hlsl").is_file()
    for role in ("baseline", "variant", "fallback"):
        assert {"nw06_bloom_cs", "nw06_tonemap_cs", "display_fixture"} <= set(
            manifest["shader_inventory"]["roles"][role]
        )


def test_nw11_mesh_shader_consumes_graph_generated_geometry() -> None:
    source = (
        REPO_ROOT / "shaders/nw11/mesh/nw11_torus_meshlet.ms.hlsl"
    ).read_text(encoding="utf-8")
    assert "NwVertices.Load" in source
    assert "NwIndices" in source
    assert "NW_MAJOR_SEGMENTS" not in source
    assert "0.7" not in source and "0.3 * cos" not in source
    document = graph_scene.lower(
        REPO_ROOT / contract.BASE_GRAPH, repo_root=REPO_ROOT
    )
    assert document["torus"]["major_segments"] == 64
    assert document["torus"]["minor_segments"] == 32
    assert document["torus"]["major_radius"] == 0.7
    assert document["instances"]["count"] == 256


def test_nw11_mesh_shader_indexes_generated_buffers_in_elements() -> None:
    source = (
        REPO_ROOT / "shaders/nw11/mesh/nw11_torus_meshlet.ms.hlsl"
    ).read_text(encoding="utf-8")
    compact = "".join(source.split())
    # Buffer<uint>.Load takes an element index.  The generated vertex record is
    # 32 bytes = eight uint elements, and the index buffer is one uint per
    # triangle corner.
    assert "vertex_index*32u" in compact
    assert "ByteAddressBufferNwIndices" in compact
    assert "NwIndices.Load((quad*6u+corner)*4u)" in compact
    assert "numthreads(NW_VERTS_PER_MESHLET,1,1)" in compact
    assert "SetMeshOutputCounts(NW_VERTS_PER_MESHLET,NW_PRIMS_PER_MESHLET)" in compact
    bundle.validate_variant_sources(REPO_ROOT)


def test_nw11_camera_interpolation_matches_nw06_semantics() -> None:
    document = graph_scene.lower(
        REPO_ROOT / contract.BASE_GRAPH, repo_root=REPO_ROOT
    )
    header = graph_scene.emit_header(document)
    # nw06_scene_vs treats 1 as step/hold and 0 as linear.
    assert "kCameraInterpolation = 0u" in header
    mesh = (
        REPO_ROOT / "shaders/nw11/mesh/nw11_torus_meshlet.ms.hlsl"
    ).read_text(encoding="utf-8")
    assert "SampleCameraCurve" in mesh
    assert "NwCamera.Load(11u)" in mesh


def test_nw11_generated_constants_bind_the_canonical_production_contract() -> None:
    document = graph_scene.lower(
        REPO_ROOT / contract.BASE_GRAPH, repo_root=REPO_ROOT
    )
    canonical = document["canonical_production"]
    assert canonical["path"] == contract.CANONICAL_PRODUCTION_CONFIG
    assert len(canonical["sha256"]) == 64
    assert canonical["quality_profile_id"] == contract.QUALITY_PROFILE_ID
    header = graph_scene.emit_header(document)
    assert "kCanonicalProductionSha256" in header
    assert "kBloomThreshold" in header
    assert "kTonemapOperator" in header


def test_nw11_acceleration_structure_scratch_buffers_are_uav_state() -> None:
    extension = (REPO_ROOT / "src/nw11/production/nw11_production_extension.h").read_text(encoding="utf-8")
    assert "constexpr auto scratch = D3D12_RESOURCE_STATE_UNORDERED_ACCESS" in extension
    for name in ("blas", "tlas"):
        assert f"scratch, ray.{name}_scratch" in extension
        assert f"UavBarrier(list, ray.{name}_scratch.Get())" in extension


def test_nw11_probe_adapter_luid_is_propagated_to_measured_artifacts() -> None:
    probe = (REPO_ROOT / "src/nw11/capability_probe.cpp").read_text(encoding="utf-8")
    harness = (
        REPO_ROOT / "src/nw11/render/nw11_scene_harness.cpp"
    ).read_text(encoding="utf-8")
    main = (REPO_ROOT / "src/nw11/render/nw11_main.cpp").read_text(encoding="utf-8")
    launcher = (REPO_ROOT / "tools/nw11/run-validation.ps1").read_text(encoding="utf-8")
    assert "adapter_luid" in probe
    assert '"--adapter-luid"' in main
    assert "NW11_ADAPTER_LUID" in harness
    production = (REPO_ROOT / "src/player/package_player.cpp").read_text(encoding="utf-8")
    assert "NW11_ADAPTER_LUID" in production
    assert "adapter_desc.AdapterLuid" in production
    assert '$adapterLuid = [string]$adapterRecord.adapter_luid' in launcher
    assert '@("--adapter-luid", $adapterLuid)' in launcher
    assert "Assert-Nw11ArtifactHash $manifest $pair.Artifact" in launcher
    assert "Get-FileHash -LiteralPath $target -Algorithm SHA256" in launcher


def test_nw11_windows_ci_runs_command_path_and_binds_toolchain_payloads() -> None:
    workflow = (
        REPO_ROOT / ".github/workflows/nw11-advanced-gpu.yml"
    ).read_text(encoding="utf-8")
    assert "bootstrap-windows.ps1" in workflow
    assert "enter-build-env.ps1 -ExportGitHubEnv -SkipLlvm" in workflow
    assert "-Ci -SkipLlvm" in workflow
    assert "portable_cache_key.py" in workflow
    assert "-G Ninja" in workflow
    assert "windows-runtime-smoke:" in workflow
    assert "runs-on: windows-2025" in workflow
    assert "--allow-software" in workflow
    assert "nw11_toolchain_identity.py" in workflow
    assert "nw11_shader_payloads.py" in workflow
    assert "nw11_pe_dependencies.py" in workflow
    assert "--toolchain-identity" in workflow
    assert "--shader-payloads" in workflow
    assert "--dependency-report" in workflow


def test_nw11_toolchain_payload_and_dependency_records(
    tmp_path: Path,
) -> None:
    good = {
        "format": "nw11-toolchain-identity",
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "dxc_version": contract.TOOLCHAIN_DXC_MEASURED_VERSION,
    }
    assert toolchain_identity.verify(good)["dxc_version"].startswith(
        contract.TOOLCHAIN_DXC_VERSION
    )
    assert toolchain_identity._dxc_identity("dxcompiler.dll: 1.8 - 1.8.2407.12") == "1.8.2407.12"
    with pytest.raises(toolchain_identity.ToolchainIdentityError):
        toolchain_identity._dxc_identity("dxcompiler.dll: 1.8 - 1.8.2407.99")
    bad = dict(good, dxc_version="1.9.0.0")
    with pytest.raises(toolchain_identity.ToolchainIdentityError):
        toolchain_identity.verify(bad)

    shader_dir = tmp_path / "shaders"
    shader_dir.mkdir()
    (shader_dir / "a.dxil").write_bytes(b"dxil-bytes")
    (shader_dir / "a_dxil.h").write_text("static const unsigned char a[] = {};", encoding="utf-8")
    payloads = shader_payloads.collect(shader_dir)
    assert len(payloads["payloads"]) == 2
    assert all(len(item["sha256"]) == 64 for item in payloads["payloads"])

    assert pe_dependencies.forbidden(["VCRUNTIME140.dll"]) == ["VCRUNTIME140.dll"]
    assert pe_dependencies.forbidden(["d3d12.dll", "KERNEL32.dll"]) == []


def test_nw11_pe_dependency_parser_reads_import_table(tmp_path: Path) -> None:
    data = bytearray(0x400)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    coff = 0x44
    struct.pack_into("<H", data, coff, 0x8664)
    struct.pack_into("<H", data, coff + 2, 1)
    struct.pack_into("<H", data, coff + 16, 0xF0)
    optional = coff + 20
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<I", data, optional + 108, 16)
    directory = optional + 112
    struct.pack_into("<I", data, directory + 8, 0x1000)
    struct.pack_into("<I", data, directory + 12, 40)
    section = optional + 0xF0
    struct.pack_into("<I", data, section + 8, 0x200)
    struct.pack_into("<I", data, section + 12, 0x1000)
    struct.pack_into("<I", data, section + 16, 0x200)
    struct.pack_into("<I", data, section + 20, 0x200)
    struct.pack_into("<I", data, 0x200 + 12, 0x1050)
    data[0x250:0x25D] = b"KERNEL32.dll\0"
    executable = tmp_path / "sample.exe"
    executable.write_bytes(data)
    assert pe_dependencies.imports(executable) == ["KERNEL32.dll"]


def test_nw11_bundle_records_toolchain_shader_and_dependency_manifests(
    tmp_path: Path,
) -> None:
    files = _source_artifacts(tmp_path)
    toolchain = tmp_path / "toolchain.json"
    toolchain.write_text(
        json.dumps(
            {
                "format": "nw11-toolchain-identity",
                "version": 1,
                "task": contract.TASK,
                "implementation_base": contract.IMPLEMENTATION_BASE,
                "dxc_version": contract.TOOLCHAIN_DXC_MEASURED_VERSION,
            }
        ),
        encoding="utf-8",
    )
    payloads = tmp_path / "payloads.json"
    payloads.write_text(
        json.dumps(
            {
                "format": "nw11-shader-payloads",
                "version": 1,
                "task": contract.TASK,
                "implementation_base": contract.IMPLEMENTATION_BASE,
                "payloads": [{"path": "a.dxil", "bytes": 3, "sha256": "a" * 64}],
            }
        ),
        encoding="utf-8",
    )
    deps = tmp_path / "deps.json"
    deps.write_text(
        json.dumps(
            {
                "format": "nw11-pe-dependencies",
                "version": 1,
                "task": contract.TASK,
                "implementation_base": contract.IMPLEMENTATION_BASE,
                "executables": [{"path": "baseline.exe", "dlls": ["d3d12.dll"]}],
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path / "bundle"
    manifest = build_bundle.build(
        output_directory=target,
        experiment_id=contract.EXPERIMENT_MESH_ID,
        commit=COMMIT,
        baseline_executable=files["baseline.exe"],
        variant_executable=files["variant.exe"],
        fallback_executable=files["fallback.exe"],
        capability_probe=files["probe.exe"],
        measured_toolchain=toolchain,
        shader_payloads=payloads,
        dependency_report=deps,
        repo_root=REPO_ROOT,
    )
    assert manifest["toolchain"]["measured"]["dxc_version"]
    assert manifest["shader_payloads"]["payload_count"] == 1
    assert manifest["dependency_report"]["sha256"]
    verified = bundle.verify_bundle(target, expected_commit=COMMIT)
    assert verified["status"] == "verified"


def test_nw11_experiment_runtime_validator_enforces_frozen_guardrails() -> None:
    document = _experiment()
    weakened = copy.deepcopy(document)
    weakened["criteria"]["guardrails"]["cpu_submit_ms_max"] = 100.0
    with pytest.raises(experiment.ExperimentError):
        experiment.validate_experiment(weakened)

    weakened = copy.deepcopy(document)
    weakened["criteria"]["image"]["fixed_ticks"] = [0]
    with pytest.raises(experiment.ExperimentError):
        experiment.validate_experiment(weakened)

    weakened = copy.deepcopy(document)
    weakened["criteria"]["guardrails"][
        "incremental_distribution_bytes_ceiling"
    ] = 10_000_000
    with pytest.raises(experiment.ExperimentError):
        experiment.validate_experiment(weakened)

    weakened = copy.deepcopy(document)
    weakened["criteria"]["guardrails"]["shader_count_increment_ceiling"] = 100
    with pytest.raises(experiment.ExperimentError):
        experiment.validate_experiment(weakened)


def test_nw11_renderer_sources_exist_and_declare_required_d3d12_paths() -> None:
    cmake = (REPO_ROOT / "src/nw11/CMakeLists.txt").read_text(encoding="utf-8")
    for target in (
        "nw11_common_baseline",
        "nw11_common_analytic",
        "nw11_optional_mesh",
        "nw11_optional_ray",
        "nw11_fallback_common",
        "nw11_fallback_analytic",
    ):
        assert target in cmake
    for profile in ("as_6_5", "ms_6_5", "ps_6_5"):
        assert profile in cmake
    harness = (
        REPO_ROOT / "src/nw11/render/nw11_scene_harness.cpp"
    ).read_text(encoding="utf-8")
    production = (REPO_ROOT / "src/player/package_player.cpp").read_text(encoding="utf-8")
    extension = (REPO_ROOT / "src/nw11/production/nw11_production_extension.h").read_text(encoding="utf-8")
    assert "PackagePlayer player(config)" in harness
    assert "LoadEmbeddedPackage" in harness
    assert "readbackPresentedOutput" in harness
    assert "instrumentation().timing()" in harness
    assert "CreateSwapChain" not in harness
    assert "CreateCommandQueue" not in harness
    for token in ("BuildRaytracingAccelerationStructure", "DispatchMesh", "GetResourceAllocationInfo"):
        assert token in extension
    for token in ("ExecuteIndirect", "NW11_PRODUCTION_EXPERIMENT", "BuildRayFrame"):
        assert token in production


def test_nw11_capability_probe_selects_a_real_adapter_safely() -> None:
    source = (REPO_ROOT / "src/nw11/capability_probe.cpp").read_text(
        encoding="utf-8"
    )
    assert "--adapter-luid" in source
    assert "--adapter-index" in source
    assert "DedicatedVideoMemory" in source
    assert "DXGI_ADAPTER_FLAG_SOFTWARE" in source
    # Narrow must allocate the full NUL-inclusive byte count before resizing.
    assert "std::string output(static_cast<size_t>(bytes), '\\0');" in source
    assert "output.resize(static_cast<size_t>(bytes - 1));" in source


def test_nw11_collector_requires_three_fresh_processes_per_role(
    tmp_path: Path,
) -> None:
    collector = _load_collector()
    root, manifest = _build_bundle(tmp_path)
    decision = {
        "status": "eligible",
        "marketing_name_used_for_eligibility": False,
        "device_key": "pci:10de:2805",
        "environment_key": "sha256:v1:" + "a" * 64,
    }
    eligibility_path = tmp_path / "eligibility.json"
    eligibility_path.write_text(json.dumps(decision), encoding="utf-8")
    image_result = _write_image_result(tmp_path / "image-result.json")
    fallback_image_result = _write_image_result(
        tmp_path / "fallback-image-result.json"
    )
    inventory = manifest["shader_inventory"]["roles"]
    accounting = manifest["accounting"]
    reports: list[Path] = []
    for role, artifact, distribution in (
        ("baseline", "bin/common_baseline.exe", accounting["baseline_distribution_bytes"]),
        ("variant", "bin/optional_variant.exe", accounting["variant_distribution_bytes"]),
        ("fallback", "bin/fallback_common.exe", accounting["fallback_distribution_bytes"]),
    ):
        measurement = _measurement(
            distribution=distribution,
            shaders=len(inventory[role]),
            shader_ids=inventory[role],
        )
        report = _run(role, measurement=measurement)
        report["device_key"] = decision["device_key"]
        report["environment_key"] = decision["environment_key"]
        report["artifact_path"] = artifact
        report["capability_decision"] = decision
        path = tmp_path / f"{role}-only.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        reports.append(path)
    with pytest.raises(evidence.EvidenceError, match="exactly"):
        collector.collect(
            bundle_root=root,
            run_reports=reports,
            expected_commit=COMMIT,
            eligibility_file=str(eligibility_path),
            common_eligibility_file=str(eligibility_path),
            image_result_file=str(image_result),
            fallback_image_result_file=str(fallback_image_result),
        )

    # A single report cannot satisfy the repeated-sample protocol even when it
    # contains 600 samples, because the frozen policy requires three fresh
    # processes per role.
    with pytest.raises(evidence.EvidenceError):
        collector.collect(
            bundle_root=root,
            run_reports=reports[:1],
            expected_commit=COMMIT,
            eligibility_file=str(eligibility_path),
            common_eligibility_file=str(eligibility_path),
            image_result_file=str(image_result),
            fallback_image_result_file=str(fallback_image_result),
        )


def test_nw11_capture_comparator_computes_metrics_from_pixels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comparator = _load_comparator()
    monkeypatch.setattr(contract, "OUTPUT_WIDTH", 2)
    monkeypatch.setattr(contract, "OUTPUT_HEIGHT", 2)
    monkeypatch.setattr(contract, "REFERENCE_FIXED_TICKS", (0, 1))
    ticks = [0, 1]
    baseline_dir = tmp_path / "baseline"
    variant_dir = tmp_path / "variant"
    baseline_dir.mkdir()
    variant_dir.mkdir()

    def write_set(directory: Path, offset: float) -> Path:
        images = []
        for tick in ticks:
            values = array("f", [offset] * 16)
            name = f"{tick}.rgba"
            (directory / name).write_bytes(values.tobytes())
            images.append(
                {
                    "tick": tick,
                    "path": name,
                    "width": 2,
                    "height": 2,
                    "format": "rgba32f",
                }
            )
        manifest = directory / "captures.json"
        manifest.write_text(
            json.dumps(
                {
                    "format": "nw11-capture-set",
                    "version": 1,
                    "fixed_ticks": ticks,
                    "images": images,
                }
            ),
            encoding="utf-8",
        )
        return manifest

    baseline_manifest = write_set(baseline_dir, 0.0)
    variant_manifest = write_set(variant_dir, 0.00005)
    result = comparator.compare(
        baseline_manifest=baseline_manifest,
        variant_manifest=variant_manifest,
        reference_kind="raster_reference",
    )
    assert result["max_abs_channel_error"] == pytest.approx(0.00005, abs=1e-7)
    assert result["rmse"] == pytest.approx(0.00005, abs=1e-7)
    assert result["fixed_ticks"] == ticks


def test_nw11_bundle_rejects_tampered_experiment_definition(
    tmp_path: Path,
) -> None:
    root, manifest = _build_bundle(tmp_path)
    experiment_path = root / "manifests/experiment.json"
    document = json.loads(experiment_path.read_text(encoding="utf-8"))
    document["criteria"]["primary"]["relative_reduction_percent_min"] = 1.0
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    experiment_path.write_text(payload, encoding="utf-8", newline="\n")
    data = experiment_path.read_bytes()
    tampered = copy.deepcopy(manifest)
    for artifact in tampered["artifacts"]:
        if artifact["path"] == "manifests/experiment.json":
            artifact["bytes"] = len(data)
            artifact["sha256"] = contract.sha256_bytes(data)
    tampered["experiment_definition"]["bytes"] = len(data)
    tampered["experiment_definition"]["sha256"] = contract.sha256_bytes(data)
    identity = {
        key: tampered[key]
        for key in (
            "repository",
            "commit",
            "experiment_id",
            "profile_id",
            "fallback_artifact_id",
            "bundle_version",
            "artifacts",
            "capability_profile",
            "experiment_definition",
            "toolchain",
            "required_capabilities",
            "dependency_ledger",
            "accounting",
            "shader_inventory",
                "shader_payloads",
                "dependency_report",
                "canonical_production",
        )
        if key in tampered
    }
    tampered["bundle_id"] = contract.digest_value(identity)
    bundle.write_manifest(root, tampered)
    with pytest.raises(bundle.BundleError, match="experiment definition is invalid"):
        bundle.verify_bundle(root)


def test_nw11_same_device_key_cannot_span_device_tiers(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    runs = [
        _run("baseline", measurement=_measurement(gpu=10.0)),
        _run("fallback", measurement=_measurement(gpu=10.0)),
        _run(
            "variant",
            tier="desktop_blackwell_representative",
            measurement=_measurement(gpu=7.5),
        ),
    ]
    evidence_document = _evidence(root, manifest, document, runs)
    with pytest.raises(evidence.EvidenceError, match="two device tiers"):
        evidence.compute_decisions(evidence_document, document)
