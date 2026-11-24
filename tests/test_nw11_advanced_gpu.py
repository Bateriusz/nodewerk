from __future__ import annotations

import copy
import json
import sys
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
        "artifact_path": (
            "bin/common_baseline.exe" if role == "baseline" else "bin/optional_variant.exe"
        ),
        "capability_decision": {
            "status": capability_status,
            "marketing_name_used_for_eligibility": False,
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
    }


def _experiment(experiment_id: str = contract.EXPERIMENT_MESH_ID) -> dict:
    return experiment.load_experiment(
        REPO_ROOT / contract.EXPERIMENT_PATH_BY_ID[experiment_id]
    )


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
    assert common["evidence_status"] == "verified"


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
        )
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
    document_evidence = _evidence(root, manifest, document, [ada_baseline, ada_variant])
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
    both = _evidence(
        root,
        manifest,
        document,
        [ada_baseline, ada_variant, blackwell_baseline, blackwell_variant],
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
        [_run("baseline", measurement=_measurement(gpu=10.0)), _run("variant", measurement=_measurement(gpu=7.5))],
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

    root, _manifest = _build_bundle(tmp_path)
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
    reports = []
    for role, gpu, artifact in (
        ("baseline", 10.0, "bin/common_baseline.exe"),
        ("variant", 7.5, "bin/optional_variant.exe"),
    ):
        for run_index in range(contract.PERF_RUN_COUNT):
            report = _run(role, measurement=_measurement(gpu=gpu))
            report["device_key"] = eligibility["device_key"]
            report["environment_key"] = eligibility["environment_key"]
            report["artifact_path"] = artifact
            report["capability_decision"] = eligibility
            path = tmp_path / f"{role}-{run_index}.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            reports.append(path)
    evidence_path = tmp_path / "collected.json"
    arguments = [
        sys.executable,
        str(REPO_ROOT / "tools" / "nw11" / "collect-evidence.py"),
        "--bundle-root",
        str(root),
        "--expected-commit",
        COMMIT,
        "--out",
        str(evidence_path),
    ]
    for report in reports:
        arguments.extend(["--run-report", str(report)])
    subprocess.run(arguments, check=True)
    collected = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert len(collected["runs"]) == 2
    assert collected["runs"][0]["process_run_count"] == contract.PERF_RUN_COUNT
    assert collected["raw_per_sample_results_preserved"] is True

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
    assert "pip install" not in launcher
    assert "cmake" not in launcher.lower()
    assert "visual studio" not in launcher.lower()
    verify_position = launcher.index('"bundle", "--bundle-root"')
    measure_position = launcher.index("foreach ($pair in @(")
    assert verify_position < measure_position


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
