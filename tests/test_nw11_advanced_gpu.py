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
import nw11_measurements as measurements  # noqa: E402
import nw11_host as host  # noqa: E402
from nw11_host import MEASURED_APPLICATIONS  # noqa: E402
import nw11_graph_scene as graph_scene  # noqa: E402
import nw11_pe_dependencies as pe_dependencies  # noqa: E402
import nw11_pack_python_runtime as python_runtime  # noqa: E402
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


def _host_observation(device_id: int = 0x2805, *, hypervisor: bool = False) -> dict:
    return {
        "cim": {"system": {"Manufacturer": "Example Computers", "Model": "Desktop",
                           "HypervisorPresent": hypervisor, "PCSystemType": 1,
                           "TotalPhysicalMemory": 32 * 1024**3, "NumberOfLogicalProcessors": 16},
                "processor": [{"Name": "Desktop CPU", "NumberOfCores": 8}],
                "motherboard": [{"Manufacturer": "Example", "Product": "Desktop board"}]},
        "power_plan": "Power Scheme GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
        "pcie_power": "Current AC setting: 0x00000000\nCurrent DC setting: 0x00000000",
        "nvidia_power_profiles": {name: {"value": 1, "location": 1} for name in MEASURED_APPLICATIONS},
        "display_devices": [{"VendorID": "0x10DE", "DeviceID": f"0x{device_id:04x}",
                             "SubSysID": "0x000010DE", "DriverModel": "WDDM 3.2"}],
        "nvidia_smi_xml": f"""<nvidia_smi_log><driver_version>610.88</driver_version><gpu>
            <uuid>GPU-example</uuid><pci><pci_device_id>0x{device_id:04x}10DE</pci_device_id>
            <pci_sub_system_id>0x000010DE</pci_sub_system_id><pci_bus_id>00000000:01:00.0</pci_bus_id></pci>
            <performance_state>P0</performance_state>
            <gpu_power_readings><enforced_power_limit>165 W</enforced_power_limit></gpu_power_readings>
            <clocks><graphics_clock>2200 MHz</graphics_clock><mem_clock>7200 MHz</mem_clock></clocks>
            <max_clocks><graphics_clock>2600 MHz</graphics_clock><mem_clock>9000 MHz</mem_clock></max_clocks>
            <temperature><gpu_temp>38 C</gpu_temp></temperature>
          </gpu></nvidia_smi_log>""",
    }


def _observation(
    *,
    device_id: int = 0x2805,
    mesh: str = "D3D12_MESH_SHADER_TIER_1",
    ray: int = 11,
    shader_model: str = "D3D12_SHADER_MODEL_6_7",
    initialization: str = "ok",
) -> dict:
    return {
        "adapter": {
            "vendor_id": 0x10DE,
            "device_id": device_id,
            "subsystem_id": 0x10DE,
            "adapter_name": "metadata only",
            "adapter_luid": "00000000:0000ABCD",
        },
        "release_default_adapter_luid": "00000000:0000ABCD",
        "driver_version": "610.88",
        "os_build": "10.0.26300.9818",
        "toolchain_identity": "nw11-test-toolchain",
        "runtime": {"native_architecture": "x64", "d3d12_dll_version": "10.0.26300.9818",
                    "dxgi_dll_version": "10.0.26300.9818"},
        "format_queries": {
            "DXGI_FORMAT_R8G8B8A8_UNORM": {"hresult": 0, "support1": 0x2004220, "support2": 0},
            "DXGI_FORMAT_R32G32B32A32_FLOAT": {"hresult": 0, "support1": 0x4121, "support2": 0x80},
            "DXGI_FORMAT_R32_UINT": {"hresult": 0, "support1": 0x2000105, "support2": 0},
            "DXGI_FORMAT_D32_FLOAT": {"hresult": 0, "support1": 0x10020, "support2": 0},
        },
        "features": {
            **dict.fromkeys(contract.PRODUCTION_FORMATS, "production_usage_supported"),
            "d3d_feature_level": "D3D_FEATURE_LEVEL_12_1",
            "resource_binding_tier": "D3D12_RESOURCE_BINDING_TIER_2",
            "resource_heap_tier": "D3D12_RESOURCE_HEAP_TIER_2",
            "highest_shader_model": shader_model,
            "highest_shader_model_mesh": shader_model,
            "highest_shader_model_ray": shader_model,
            "mesh_shader_tier": mesh,
            "raytracing_tier": ray,
        },
        "host_observation": _host_observation(device_id),
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
    process = {
        "gpu_time_samples": [gpu + (index % 10) * 0.0001 for index in range(count)],
        "cpu_submit_samples": [cpu] * count,
        "frame_time_samples": [frame + (index % 10) * 0.0001 for index in range(count)],
        "process_start_to_first_present_ms": cold_init,
        "displayed_presents": count,
        "dropped_presents": 0,
        "distribution_bytes": distribution,
        "shader_count": shaders,
        "shader_ids": list(shader_ids or []),
        "peak_engine_committed_gpu_bytes": vram,
    }
    return {
        **{key: process[key] for key in ("distribution_bytes", "shader_count", "shader_ids",
                                          "peak_engine_committed_gpu_bytes")},
        "cold_initialization_ms": cold_init,
        "process_runs": [copy.deepcopy(process) for _ in range(contract.PERF_RUN_COUNT)],
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
            "device_id": int(device_key.split(":")[-1], 16),
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
        "raw_inputs": {"test-fixture.json": "0" * 64},
        "canonical_release_correctness": {},
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
        "optional_eligibility": dict(
            next((item for item in runs if item["role"] == "variant"), runs[0])["capability_decision"]
        ),
        "common_eligibility": dict(
            next(item for item in runs if item["role"] in {"baseline", "fallback"})["capability_decision"]
        ),
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
    assert common["artifact_budget"]["distribution_size_policy"] == "gated"
    assert common["artifact_budget"]["incremental_distribution_bytes_ceiling"] == 0
    assert mesh["fallback_artifact_id"] == contract.MESH_FALLBACK_ARTIFACT_ID
    assert ray["fallback_artifact_id"] == contract.RAY_FALLBACK_ARTIFACT_ID
    assert mesh["fallback_artifact_id"] != ray["fallback_artifact_id"]
    assert mesh["artifact_budget"]["incremental_distribution_bytes_ceiling"] == 32768
    assert ray["artifact_budget"]["incremental_distribution_bytes_ceiling"] == 65536
    for profile in (common, mesh, ray):
        names = {entry["name"] for entry in profile["dependencies"]}
        assert {"gdi32", "psapi", "advapi32", "version"} <= names
        assert all(
            entry["counted"] is True and entry["distributed_bytes"] == 0
            for entry in profile["dependencies"]
        )
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
    assert document["fallback_artifact_id"] == (
        contract.MESH_FALLBACK_ARTIFACT_ID
        if experiment_id == contract.EXPERIMENT_MESH_ID
        else contract.RAY_FALLBACK_ARTIFACT_ID
    )
    assert (
        document["artifact_budget"]["distribution_size_policy"] == "gated"
    )
    assert document["artifact_budget"]["complete_distribution_bytes_ceiling"] <= 262144
    assert (
        document["artifact_budget"]["incremental_distribution_bytes_ceiling"]
        <= (32768 if experiment_id == contract.EXPERIMENT_MESH_ID else 65536)
    )
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
    older = _observation()
    older["features"]["d3d_feature_level"] = "D3D_FEATURE_LEVEL_11_1"
    common = capability.profile_manifest_template(contract.COMMON_PROFILE_ID)
    shortfall = capability.evaluate_observation(common, older)
    assert shortfall["status"] == "unsupported"
    assert any(item.get("query_id") == "d3d_feature_level" for item in shortfall["diagnostics"])


def test_nw11_derive_profile_emits_the_bundle_schema(tmp_path: Path) -> None:
    output = tmp_path / "profile.json"
    assert verify.main(["derive-profile", "--profile-id", contract.MESH_PROFILE_ID,
                        "--json-out", str(output)]) == verify.EXIT_OK
    schema = json.loads((REPO_ROOT / "schemas/nw11-capability-profile.schema.json").read_text())
    result = capability.load_profile(output)
    validate_instance(result, schema, schema)
    assert set(result["shader_targets"]) == set(contract.MESH_SHADER_TARGETS)


def test_nw11_initialization_failure_is_preserved_without_substitution() -> None:
    mesh = capability.load_profile(REPO_ROOT / contract.PROFILE_PATH_BY_ID[contract.MESH_PROFILE_ID])
    decision = capability.evaluate_observation(
        mesh, _observation(initialization="failed")
    )
    assert decision["status"] == "failed"
    assert decision["satisfies_optional_profile"] is False
    assert decision["diagnostics"][0]["code"] == "NW11-INITIALIZATION-FAILED"


def test_nw11_production_format_preflight_uses_observed_support_masks() -> None:
    profile = capability.profile_manifest_template(contract.COMMON_PROFILE_ID)
    observation = _observation()
    required = "DXGI_FORMAT_R32G32B32A32_FLOAT"
    assert capability.evaluate_observation(profile, observation)["status"] == "eligible"
    observation["format_queries"][required]["support2"] = 0
    unsupported = capability.evaluate_observation(profile, observation)
    assert unsupported["status"] == "unsupported"
    assert unsupported["diagnostics"][0]["query_id"] == required
    observation["format_queries"][required]["hresult"] = -2147024809
    assert capability.evaluate_observation(profile, observation)["status"] == "pending"
    del observation["format_queries"][required]
    assert capability.evaluate_observation(profile, observation)["status"] == "pending"


def test_nw11_physical_tier_and_environment_bind_host_provenance(tmp_path: Path) -> None:
    profile = capability.profile_manifest_template(contract.COMMON_PROFILE_ID)
    observation = _observation()
    physical = capability.evaluate_observation(profile, observation)
    assert physical["physical_device_tier"] == "desktop_ada_representative"
    assert physical["physical_provenance"]["host"]["cpu"][0]["physical_cores"] == 8
    observation["host_observation"]["nvidia_smi_xml"] = (
        observation["host_observation"]["nvidia_smi_xml"].replace("38 C", "55 C"))
    hotter = capability.evaluate_observation(profile, observation)
    assert hotter["environment_key"] != physical["environment_key"]
    root, manifest = _build_bundle(tmp_path)
    observation["host_observation"]["nvidia_power_profiles"]["optional_variant.exe"]["value"] = 5
    common, optional = measurements.evaluate_profiles(root, manifest, observation)
    assert common["status"] == "eligible"
    assert optional["status"] == "pending"
    assert optional["diagnostics"][0]["code"] == "NW11-POWER-PROFILE-MISMATCH"
    observation["host_observation"] = _host_observation(hypervisor=True)
    hypervisor = capability.evaluate_observation(profile, observation)
    assert hypervisor["physical_device_tier"] is None
    assert hypervisor["status"] == "eligible"  # Capabilities and coverage are independent.


def test_nw11_bundle_is_deterministic_and_self_contained(tmp_path: Path) -> None:
    first, first_manifest = _build_bundle(tmp_path, output="first")
    second, second_manifest = _build_bundle(tmp_path, output="second")
    assert (first / bundle.MANIFEST_NAME).read_bytes() == (
        second / bundle.MANIFEST_NAME
    ).read_bytes()
    assert first_manifest["bundle_id"] == second_manifest["bundle_id"]
    verified = bundle.verify_bundle(first, expected_commit=COMMIT, fixture_only=True)
    assert verified["status"] == "fixture_only"
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
    assert first_manifest["accounting"]["distribution_size_policy"] == "gated"
    fallback = next(
        artifact
        for artifact in first_manifest["artifacts"]
        if artifact["role"] == "fallback_common"
    )
    assert fallback["profile_id"] == contract.COMMON_PROFILE_ID
    assert (
        fallback["metadata"]["fallback_artifact_id"]
        == first_manifest["fallback_artifact_id"]
    )
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
    with pytest.raises(bundle.BundleError, match="unrecorded payload"):
        bundle.verify_bundle(first, expected_commit=COMMIT, fixture_only=True)
    (cache / "nw11_contract.cpython-312.pyc").unlink()
    (first / "bin" / "unrecorded.dll").write_bytes(b"unrecorded-payload")
    with pytest.raises(bundle.BundleError, match="unrecorded payload"):
        bundle.verify_bundle(first, expected_commit=COMMIT, fixture_only=True)


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
        bundle.verify_bundle(root, fixture_only=True)


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
    assert result["satisfies_completion"] is True
    assert result["aggregate"]["baseline"]["process_runs"][0]["raw_gpu_time_samples"]


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
    assert len(decisions["devices"]) == 1
    assert decisions["devices"][0]["device_tier"] == "desktop_ada_representative"
    assert decisions["devices"][0]["status"] == "accepted"
    assert decisions["aggregate_status"] == "accepted"
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
    blackwell_evidence = _evidence(
        root,
        manifest,
        document,
        [blackwell_baseline, blackwell_variant, blackwell_fallback],
    )
    blackwell_decisions = evidence.build_decision_document(
        evidence=blackwell_evidence,
        experiment=document,
        bundle_manifest=manifest,
        bundle_manifest_sha256=blackwell_evidence["bundle_manifest_sha256"],
        expected_commit=COMMIT,
    )
    assert len(blackwell_decisions["devices"]) == 1
    assert blackwell_decisions["devices"][0]["device_tier"] == "desktop_blackwell_representative"
    assert blackwell_decisions["devices"][0]["status"] == "accepted"
    assert blackwell_decisions["aggregate_status"] == "accepted"
    assert blackwell_decisions["satisfies_completion"] is False
    blackwell_evidence["runs"][1]["measurement"] = _measurement(gpu=9.5)
    blackwell_evidence = _evidence(root, manifest, document, blackwell_evidence["runs"])
    rejected = evidence.build_decision_document(
        evidence=blackwell_evidence, experiment=document, bundle_manifest=manifest,
        bundle_manifest_sha256=blackwell_evidence["bundle_manifest_sha256"],
    )
    both = evidence.aggregate_verified_decisions([decisions, rejected])
    assert [item["status"] for item in both["devices"]] == ["accepted", "rejected"]
    assert both["satisfies_completion"] is False
    failed = evidence.aggregate_verified_decisions(
        [decisions, blackwell_decisions], cross_device_equivalence={"status": "failed"})
    assert failed["aggregate_status"] == "failed"
    assert [item["status"] for item in failed["devices"]] == ["accepted", "accepted"]
    assert failed["satisfies_completion"] is False
    with pytest.raises(evidence.EvidenceError, match="comparison rows"):
        evidence.aggregate_verified_decisions([decisions, rejected],
                                             cross_device_equivalence={"status": "verified"})
    for item in (decisions, rejected):
        item["devices"][0]["canonical_release_correctness"] = {"numeric_loop_boundary": {"status": "verified"}}
        item["decision_id"] = evidence.decision_id(item)
    comparison = {"status": "verified", "tolerance": nw07_contract.IMAGE_ABSOLUTE_TOLERANCE,
                  "rows": {str(tick): {"maximum_error": 0, "maximum_relative_error": 0}
                           for tick in contract.REFERENCE_FIXED_TICKS}}
    crossed = evidence.aggregate_verified_decisions([decisions, rejected], cross_device_equivalence=comparison)
    assert crossed["satisfies_completion"] is True
    for bad_row in (None, {"maximum_error": 1, "maximum_relative_error": 0},
                    {"maximum_error": 0, "maximum_relative_error": float("inf")},
                    {"maximum_error": 0}):
        malformed = copy.deepcopy(comparison)
        malformed["rows"]["0"] = bad_row
        with pytest.raises(evidence.EvidenceError, match="comparison"):
            evidence.validate_decision_document(dict(crossed, cross_device_equivalence=malformed))


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
    with pytest.raises(evidence.EvidenceError, match="same device/environment"):
        evidence.build_decision_document(
            evidence=mismatched,
            experiment=document,
            bundle_manifest=manifest,
            bundle_manifest_sha256=mismatched["bundle_manifest_sha256"],
            expected_commit=COMMIT,
        )


def test_nw11_stale_evidence_and_artifact_hashes_are_rejected(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(bundle, "verify_bundle", lambda *a, **k: {"manifest": manifest})
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


def test_nw11_runtime_floors_and_future_dxr_tier() -> None:
    ray = capability.profile_manifest_template(contract.RAY_QUERY_PROFILE_ID)
    observation = _observation(ray=12)
    observation["driver_version"] = "32.0.16.1088"
    result = capability.evaluate_observation(ray, observation)
    assert result["status"] == "eligible"
    assert result["queried"]["raytracing_tier"] == 12
    assert result["physical_device_tier"] == "desktop_ada_representative"
    observation["driver_version"] = "unavailable"
    assert capability.evaluate_observation(ray, observation)["status"] == "pending"
    observation["os_build"] = "10.0.19045"
    assert capability.evaluate_observation(ray, observation)["status"] == "unsupported"
    observation["os_build"] = "10.0.22631"
    observation["driver_version"] = "32.0.16.1087"
    assert capability.evaluate_observation(ray, observation)["status"] == "unsupported"


def test_nw11_host_output_decoding_is_independent_of_cp1250(monkeypatch) -> None:
    monkeypatch.setattr(host, "_windows_code_pages", lambda: ("cp852", "cp1250"))
    payload = b"Power Scheme GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c " + bytes([0x98])
    decoded = host._decode_process_output(payload)
    assert decoded.endswith("ś")


def test_nw11_launcher_is_one_command_and_verifies_before_measuring() -> None:
    launcher = (REPO_ROOT / "tools/nw11/run-validation.ps1").read_text()
    assert launcher.index('"bundle", "--bundle-root"') < launcher.index("& $python -I -B -S @runnerArgs")
    assert launcher.index("New-Item -ItemType Directory -Path $temporaryPath") < launcher.index("Remove-Item")
    assert '$Manifest.bundle_id' not in launcher
    assert '"summary.json"' in launcher
    assert 'pip install' not in launcher
    assert 'cmake' not in launcher.lower()


def test_nw11_evidence_rejects_adapter_luid_mismatch(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    runs = [
        _run("baseline", measurement=_measurement(gpu=10.0)),
        _run("variant", measurement=_measurement(gpu=7.5)),
        _run("fallback", measurement=_measurement(gpu=10.0)),
    ]
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


def test_nw11_bundle_distribution_rejection_reaches_decision(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    document = _experiment()
    runs = [
        _run("baseline", measurement=_measurement(gpu=10.0)),
        _run("variant", measurement=_measurement(gpu=7.5)),
        _run("fallback", measurement=_measurement(gpu=10.0)),
    ]
    evidence_document = _evidence(root, manifest, document, runs)
    manifest_sha = contract.sha256_bytes(
        (root / bundle.MANIFEST_NAME).read_bytes()
    )
    decisions = evidence.build_decision_document(
        evidence=evidence_document,
        experiment=document,
        bundle_manifest=manifest,
        bundle_manifest_sha256=manifest_sha,
        schema_root=root,
        bundle_accounting={"adoption_budget_status": "rejected"},
    )
    assert decisions["bundle_adoption_budget_status"] == "rejected"
    assert decisions["aggregate_status"] == "rejected"
    assert decisions["satisfies_completion"] is False
    assert all(item["status"] == "rejected" for item in decisions["devices"])


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
    assert "CommittedWorldSpaceGeometryNormal" not in ray_source
    assert "ReflectionRadiance(reflected, hit)" in ray_source
    assert "1e-6" not in ray_source
    assert "hit_marker" not in ray_source

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
    _, manifest = _build_bundle(tmp_path)
    assert set(measurements.role_paths(manifest, {"status": "pending"})) == {"baseline", "fallback"}


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
    assert accounting["variant_distribution_bytes"] == accounting["optional_distribution_bytes"]
    shader_entries = [item for item in manifest["artifacts"] if item["role"] == "shader"]
    assert shader_entries
    assert all(item["distributed_bytes"] == 0 for item in shader_entries)
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
    runner = (REPO_ROOT / "tools/nw11/validate-device.py").read_text()
    assert "adapter_luid" in probe
    assert '"--adapter-luid"' in main
    assert "NW11_ADAPTER_LUID" in harness
    from nw11_specialize_player import specialize
    production = specialize(REPO_ROOT / "src/player/package_player.cpp",
        REPO_ROOT / "src/nw11/production/package_player.overlay.json")
    assert "NW11_ADAPTER_LUID" in production
    assert "adapter_desc.AdapterLuid" in production
    assert '"--adapter-luid", decision["adapter"]["adapter_luid"]' in runner
    assert 'contract.sha256_file(path) != entry["sha256"]' in runner


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
    smoke = workflow.split("  windows-runtime-smoke:", 1)[1].split("  build-validation-bundles:", 1)[0]
    warp_launches = [line for line in smoke.splitlines() if "--allow-software" in line]
    assert warp_launches and all("--headless" in line for line in warp_launches)
    assert "nw11_toolchain_identity.py" in workflow
    assert "nw11_shader_payloads.py" in workflow
    assert "nw11_pe_dependencies.py" in workflow
    assert "--toolchain-identity" in workflow
    assert "--shader-payloads" in workflow
    assert "--dependency-report" in workflow
    assert "adoption_budget_status" in workflow
    assert "within_complete_ceiling" in workflow
    assert "within_incremental_budget" in workflow
    assert "::warning::" in workflow


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


def test_nw11_pe_dependency_closure_keeps_optional_and_probe_imports_out_of_common(
    tmp_path: Path, monkeypatch,
) -> None:
    common, variant, probe, vendor = (
        "bin/common_baseline.exe", "bin/optional_variant.exe", "bin/probe.exe", "bin/vendor.dll"
    )
    imports = {
        "common_baseline.exe": ["KERNEL32.dll"],
        "optional_variant.exe": ["VENDOR.dll"],
        "probe.exe": ["version.dll"],
        "vendor.dll": ["KERNEL32.dll"],
    }
    monkeypatch.setattr(pe_dependencies, "imports", lambda path: list(imports[Path(path).name]))
    with pytest.raises(pe_dependencies.PeDependencyError, match="unclassified DLL vendor.dll"):
        pe_dependencies.deployment_closure(tmp_path, [common, variant, probe], [])
    (tmp_path / "bin").mkdir()
    (tmp_path / vendor).write_bytes(b"counted bytes")
    executables, entries = pe_dependencies.deployment_closure(tmp_path, [common, variant, probe], [vendor])
    assert executables[common] == ["kernel32.dll"]
    assert executables[variant] == ["kernel32.dll", "vendor.dll"]
    ledger = {item["name"]: item for item in entries}
    assert ledger["version.dll"]["used_by"] == [probe]
    assert not ledger["version.dll"]["in_common_closure"]
    assert ledger["vendor.dll"]["sha256"] == contract.sha256_file(tmp_path / vendor)
    assert ledger["vendor.dll"]["distributed_bytes"] == len(b"counted bytes")


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
    verified = bundle.verify_bundle(target, expected_commit=COMMIT, fixture_only=True)
    assert verified["status"] == "fixture_only"


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

    weakened = copy.deepcopy(document)
    weakened["artifact_budget"]["distribution_size_policy"] = "report_only"
    with pytest.raises(experiment.ExperimentError):
        experiment.validate_experiment(weakened)

    weakened = copy.deepcopy(document)
    weakened["criteria"]["guardrails"]["complete_distribution_bytes_ceiling"] = 10_000_000
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
    from nw11_specialize_player import specialize
    production = specialize(REPO_ROOT / "src/player/package_player.cpp",
        REPO_ROOT / "src/nw11/production/package_player.overlay.json")
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
    begin = production.index("instrumentation_.BeginFrame(frame_tick)")
    submitted = production.index("->ExecuteCommandLists(", begin)
    present = production.index("->Present(", submitted)
    end = production.index("instrumentation_.EndFrame();", submitted)
    retirement = production.index("const ExitCode retirement = retire_submitted_work();", end)
    wait = production.index("WaitForFenceValue(", retirement)
    assert begin < submitted < present < end < retirement < wait


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


def test_nw11_collector_requires_three_fresh_processes_per_role() -> None:
    baseline = _measurement()
    baseline["process_runs"] = baseline["process_runs"][:1]
    with pytest.raises(experiment.ExperimentError, match="3 independent"):
        experiment.summarize_process_runs(baseline, "baseline", _experiment()["protocol"])


def test_nw11_failed_run_cannot_be_hidden_in_aggregate() -> None:
    base, variant = _measurement(), _measurement(gpu=7.5)
    # Seven spikes fail p99 within one run but could disappear across 1,800 samples.
    base["process_runs"][0]["gpu_time_samples"][:7] = [24.0] * 7
    result = experiment.evaluate_experiment(
        _experiment(), {"device_key": "pci:10de:2805", "environment_key": "host",
                        "baseline": base, "variant": variant},
        device_key="pci:10de:2805", environment_key="host",
    )
    assert result["status"] == "rejected"
    assert "NW11-NW07-REFERENCE-GATE" in {reason["code"] for reason in result["reasons"]}


def test_nw11_capture_comparator_computes_metrics_from_pixels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contract, "OUTPUT_WIDTH", 2)
    monkeypatch.setattr(contract, "OUTPUT_HEIGHT", 2)
    monkeypatch.setattr(contract, "REFERENCE_FIXED_TICKS", (0, 1))
    monkeypatch.setattr(contract, "LOOP_TICKS", 2)
    comparator = _load_comparator()
    ticks = [0, 1, 2]
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
    assert result["fixed_ticks"] == ticks[:-1]
    assert result["loop_boundary"]["variant"]["rmse"] == 0


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
        bundle.verify_bundle(root, fixture_only=True)


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
    with pytest.raises(evidence.EvidenceError, match="PCI identity"):
        evidence.compute_decisions(evidence_document, document)


def test_nw11_production_windows_runtime_preserves_platform_definitions() -> None:
    cmake = (REPO_ROOT / "src/nw11/CMakeLists.txt").read_text(encoding="utf-8")
    assert "target_compile_definitions(nw11_production_runtime PUBLIC" in cmake
    assert "WIN32_LEAN_AND_MEAN NOMINMAX" in cmake
    assert "NW10_COMPACT_PACKAGE=0" in cmake
    probe = (REPO_ROOT / "src/nw11/capability_probe.cpp").read_text(encoding="utf-8")
    assert 'queried(has_options7, MeshTierName' in probe
    assert 'JsonNumber(static_cast<unsigned>(options5.RaytracingTier))' in probe
    assert ' : "null";' in probe


def _canonical_shader_package() -> bytes:
    import hashlib
    import nwp_package as nwp

    shaders = [nwp.ShaderRecord(6, "SceneVS", "vs_6_0", b"canonical-VS", 1, "graphics.nw06.instanced"),
               nwp.ShaderRecord(7, "ScenePS", "ps_6_0", b"canonical-PS", 1, "graphics.nw06.instanced")]
    payloads = {b"CAP ": b"unchanged-capability", b"FRAM": b"frame",
                b"INIT": b"initialization", b"PARM": b"parameters", b"RES ": b"resources",
                b"ROOT": b"roots", b"SHDR": nwp._encode_shaders(shaders)}
    index = bytearray()
    cursor = nwp.HEADER_SIZE + nwp.SECTION_ENTRY_SIZE * len(payloads)
    for name, data in payloads.items():
        index.extend(struct.pack("<4sIIQQI", name, 0, 2 if name == b"SHDR" else 1, cursor, len(data), 0))
        cursor += len(data)
    header = struct.pack("<8sHHIIIQQIIQQ", nwp.MAGIC, 1, 0, nwp.HEADER_SIZE, 1, 0,
                         nwp.HEADER_SIZE, cursor + nwp.CHECKSUM_SIZE, len(payloads),
                         nwp.SECTION_ENTRY_SIZE, cursor, 0)
    body = header + bytes(index) + b"".join(payloads.values())
    return body + hashlib.sha256(body).digest()


def test_nw11_native_package_and_dxil_binding_fail_closed() -> None:
    import nw05_export
    import nw11_production_binding as binding

    payloads = {name: b"DXBC" + name.encode() for name in binding.PAYLOADS}
    package = _canonical_shader_package()
    specialized, removed = binding.specialize_package(package, "mesh")
    assert removed[0]["entry_point"] == "SceneVS"
    assert removed[0]["blob_sha256"] == contract.sha256_bytes(b"canonical-VS")
    assert b"canonical-VS" not in specialized and b"canonical-PS" in specialized
    for kind in ("ray", "analytic"):
        reflection, removed_ps = binding.specialize_package(package, kind)
        assert removed_ps[0]["entry_point"] == "ScenePS"
        assert b"canonical-PS" not in reflection and b"canonical-VS" in reflection
    assert binding.specialize_package(package, "common") == (package, [])
    template = b"MZ" + b"".join(data for name, data in payloads.items() if binding.PAYLOADS[name] == "mesh")

    def image(code: bytes, commit: str = COMMIT, packaged: bytes = specialized) -> bytes:
        overlay = json.dumps({"format": "nw11-production-specialization.v1",
            "tested_commit": commit, "specialization": "mesh",
            "canonical_package_sha256": contract.sha256_bytes(package),
            "specialized_package_sha256": contract.sha256_bytes(specialized),
            "displaced_canonical_shaders": removed,
            "canonical_link_manifest_sha256": "f" * 64,
            "template_sha256": contract.sha256_bytes(code)}).encode()
        return nw05_export.embed_package(code, packaged, overlay)

    def inspect(executable: bytes):
        return binding.inspect(executable, commit=COMMIT, specialization="mesh",
            package_sha256=contract.sha256_bytes(package), manifest_sha256="f" * 64,
            payloads=payloads, canonical_package=package)

    assert len(inspect(image(template))["linked_specialization_payloads"]) == 2
    with pytest.raises(ValueError):
        inspect(image(template, "0" * 40))
    with pytest.raises(ValueError):
        inspect(image(template + payloads["nw11_deform_cs_dxil.dxil"]))
    with pytest.raises(ValueError):
        inspect(image(template, packaged=package))
    with pytest.raises(ValueError):
        inspect(image(b"MZ"))


def test_nw11_production_verifier_rejects_fixture_only_bundle(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    assert bundle.verify_bundle(root, fixture_only=True)["status"] == "fixture_only"
    with pytest.raises(bundle.BundleError, match="required native artifacts"):
        bundle.verify_bundle(root, expected_commit=COMMIT)
    assert verify.main(["bundle", "--bundle-root", str(root)]) == verify.EXIT_IDENTITY


def test_nw11_specialization_is_isolated_from_frozen_player(tmp_path: Path) -> None:
    from nw11_specialize_player import specialize
    source = REPO_ROOT / "src/player/package_player.cpp"
    overlay = REPO_ROOT / "src/nw11/production/package_player.overlay.json"
    original = source.read_text(encoding="utf-8")
    result = specialize(source, overlay)
    assert "NW11_PRODUCTION_EXPERIMENT" not in original
    assert "NW11_PRODUCTION_EXPERIMENT" in result
    # The generated translation unit reproduces the previously native-tested
    # specialization, rather than silently replacing the common renderer.
    assert result.count("instrumentation_.BeginFrame(") == original.count("instrumentation_.BeginFrame(")
    assert "peak_engine_committed_gpu_bytes" in result
    changed = tmp_path / "player.cpp"
    changed.write_text(original + "\n// upstream change\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rebase"):
        specialize(changed, overlay)


def test_nw11_distribution_budget_is_gated(tmp_path: Path) -> None:
    files = _source_artifacts(tmp_path)
    complete = tmp_path / "complete-oversize"
    files["variant.exe"].write_bytes(b"MZ" + b"x" * 300_000)
    complete_manifest = build_bundle.build(
        output_directory=complete,
        experiment_id=contract.EXPERIMENT_MESH_ID,
        commit=COMMIT,
        baseline_executable=files["baseline.exe"],
        variant_executable=files["variant.exe"],
        fallback_executable=files["fallback.exe"],
        capability_probe=files["probe.exe"],
        repo_root=REPO_ROOT,
    )
    assert complete_manifest["accounting"]["within_complete_ceiling"] is False
    assert (
        complete_manifest["accounting"]["adoption_budget_status"] == "rejected"
    )
    assert (
        bundle.verify_bundle(complete, expected_commit=COMMIT, fixture_only=True)["accounting"][
            "adoption_budget_status"
        ]
        == "rejected"
    )

    files["variant.exe"].write_bytes(b"MZ" + b"x" * 40_000)
    incremental = tmp_path / "incremental-oversize"
    incremental_manifest = build_bundle.build(
        output_directory=incremental,
        experiment_id=contract.EXPERIMENT_MESH_ID,
        commit=COMMIT,
        baseline_executable=files["baseline.exe"],
        variant_executable=files["variant.exe"],
        fallback_executable=files["fallback.exe"],
        capability_probe=files["probe.exe"],
        repo_root=REPO_ROOT,
    )
    assert incremental_manifest["accounting"]["within_incremental_budget"] is False
    assert (
        incremental_manifest["accounting"]["adoption_budget_status"] == "rejected"
    )

    result = experiment.evaluate_experiment(
        _experiment(),
        {
            "device_key": "device",
            "environment_key": "environment",
            "baseline": _measurement(gpu=10.0, distribution=5_000),
            "variant": _measurement(gpu=7.5, distribution=45_000),
        },
        device_key="device",
        environment_key="environment",
    )
    assert result["status"] == "rejected"
    assert "NW11-INCREMENTAL-BUDGET" in {
        reason["code"] for reason in result["reasons"]
    }


def test_nw11_python_runtime_archive_is_deterministic_and_self_contained(tmp_path: Path) -> None:
    root = tmp_path / "python"
    (root / "Lib/json").mkdir(parents=True)
    (root / "Lib/encodings").mkdir(parents=True)
    (root / "DLLs").mkdir()
    (root / "python.exe").write_bytes(b"MZ-python")
    (root / "python312.dll").write_bytes(b"dll")
    (root / "LICENSE.txt").write_text("Python license", encoding="utf-8")
    (root / "Lib/json/__init__.py").write_text("# json", encoding="utf-8")
    (root / "Lib/encodings/__init__.py").write_text("# encodings", encoding="utf-8")
    (root / "DLLs/_hashlib.pyd").write_bytes(b"pyd")
    first = tmp_path / "a.zip"
    second = tmp_path / "b.zip"
    python_runtime.pack(root, first)
    python_runtime.pack(root, second)
    assert first.read_bytes() == second.read_bytes()
    import zipfile
    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
    assert {"python.exe", "python312.dll", "LICENSE.txt", "Lib/json/__init__.py", "Lib/encodings/__init__.py", "DLLs/_hashlib.pyd"} <= names


def test_nw11_validation_bundle_uses_only_bundled_python_runtime() -> None:
    launcher = (REPO_ROOT / "tools/nw11/run-validation.ps1").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github/workflows/nw11-advanced-gpu.yml").read_text(encoding="utf-8")
    builder = (REPO_ROOT / "tools/nw11_build_bundle.py").read_text(encoding="utf-8")
    assert "runtime/python-runtime.zip" in launcher
    assert "Expand-Archive" in launcher
    assert 'Get-Command $candidate' not in launcher
    assert "--python-runtime-zip" in workflow
    assert "production validation bundles require native proofs and validation runtimes" in builder
    assert (launcher.index("trusted CI SHA-256") < launcher.index("Unexpected bundle file")
            < launcher.index("Bundle artifact mismatch") < launcher.index("& $python -I -B -S"))
    assert r"\.py[co]$" in launcher
    assert "common_fallback_verified -eq $true" in workflow


def test_nw11_common_captures_require_cross_device_images(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(nw07_contract, "OUTPUT_WIDTH", 2)
    monkeypatch.setattr(nw07_contract, "OUTPUT_HEIGHT", 2)
    records = []
    for tier in contract.DEVICE_TIERS:
        unit = tmp_path / tier
        capture = unit / "canonical-release"
        capture.mkdir(parents=True)
        rows = []
        for tick in contract.REFERENCE_FIXED_TICKS:
            path = capture / f"{tick}.rgba"
            path.write_bytes(array("f", [0.25] * 16).tobytes())
            rows.append({"tick": tick, "variant_sha256": contract.sha256_file(path)})
        records.append((tier, unit, {"per_tick": rows, "numeric_loop_boundary": {"status": "verified"}}))
    assert measurements.cross_device_captures(records)["status"] == "verified"
    _, folder, correctness = records[-1]
    corruption = folder / "canonical-release/0.rgba"
    corruption.write_bytes(array("f", [0.5] * 16).tobytes())
    correctness["per_tick"][0]["variant_sha256"] = contract.sha256_file(corruption)
    assert measurements.cross_device_captures(records)["status"] == "failed"
    correctness["numeric_loop_boundary"]["status"] = "pending"
    assert measurements.cross_device_captures(records)["status"] == "pending"


def test_nw11_unsupported_optional_requires_common_and_fallback(tmp_path: Path) -> None:
    root, manifest = _build_bundle(tmp_path)
    observation = _observation(mesh="D3D12_MESH_SHADER_TIER_NOT_SUPPORTED")
    common, optional = measurements.evaluate_profiles(root, manifest, observation)
    assert common["status"] == "eligible"
    assert optional["status"] == "unsupported"
    assert set(measurements.role_paths(manifest, optional)) == {"baseline", "fallback"}
    data = _evidence(root, manifest, _experiment(), [_run("baseline"), _run("fallback")])
    data["optional_eligibility"] = dict(data["common_eligibility"], status="unsupported")
    result = evidence.build_decision_document(
        evidence=data, experiment=_experiment(), bundle_manifest=manifest,
        bundle_manifest_sha256=data["bundle_manifest_sha256"], schema_root=root,
    )
    assert result["devices"][0]["status"] == "unsupported"
    assert result["devices"][0]["fallback_tested"] is True
    data["runs"].pop()
    with pytest.raises(evidence.EvidenceError, match="fallback"):
        evidence.build_decision_document(
            evidence=data, experiment=_experiment(), bundle_manifest=manifest,
            bundle_manifest_sha256=data["bundle_manifest_sha256"], schema_root=root,
        )


def test_nw11_regenerates_decisions_from_bound_raw_files(tmp_path: Path, monkeypatch) -> None:
    root, manifest = _build_bundle(tmp_path)
    output = tmp_path / "physical-fixture"
    output.mkdir()
    from types import SimpleNamespace
    comparator = _load_comparator()
    comparator.contract = SimpleNamespace(**(vars(contract) | {"OUTPUT_WIDTH": 2, "OUTPUT_HEIGHT": 2}))
    monkeypatch.setattr(measurements, "_comparison_tool", lambda: comparator.compare)
    monkeypatch.setattr(nw07_contract, "OUTPUT_WIDTH", 2)
    monkeypatch.setattr(nw07_contract, "OUTPUT_HEIGHT", 2)

    def put(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def captures(folder: Path, identity: dict) -> dict:
        ticks = [*contract.REFERENCE_FIXED_TICKS, contract.LOOP_TICKS]
        images = []
        for tick in ticks:
            path = folder / f"{tick}.rgba"
            path.write_bytes(array("f", [0.25] * 16).tobytes())
            images.append({"tick": tick, "path": path.name, "width": 2, "height": 2,
                           "format": "rgba32f", "sha256": contract.sha256_file(path)})
        return {"format": "nw11-capture-set", "version": 1,
                "fixed_ticks": ticks, "images": images, **identity}

    for name in ("canonical_reference", "canonical_release"):
        relative = f"bin/{name}.exe"
        (root / relative).write_bytes(name.encode())
        manifest["artifacts"].append(bundle.make_entry(root, relative, role=bundle.ROLE_TOOL,
                                                     metadata={"package_sha256": "c" * 64}))
    package_sha = "c" * 64
    for entry in manifest["artifacts"]:
        if entry["role"] in measurements.ARTIFACT_ROLES.values():
            entry["metadata"]["package_sha256"] = package_sha
    observation = _observation()
    snapshot = output / "capability-snapshot.json"
    put(snapshot, observation)
    put(output / "host-observation.json", observation["host_observation"])
    common, optional = measurements.evaluate_profiles(root, manifest, observation)
    put(root / "manifests/canonical-loop.json", {
        "graph_sha256": contract.BASE_GRAPH_SHA256,
        "loop_boundary": {"seamless_modulo_turn": True,
                          "start": {"tick": 0, "wrapped_tick": 0, "camera_angle_radians": 0, "twist_degrees": 0},
                          "boundary": {"tick": contract.LOOP_TICKS, "wrapped_tick": 0,
                                       "camera_angle_radians": 0, "twist_degrees": 0}},
    })
    put(output / "common-eligibility.json", common)
    put(output / "eligibility.json", optional)
    probe_sha = contract.sha256_file(root / "bin/nw11_capability_probe.exe")
    session = {"commit": COMMIT, "bundle_id": manifest["bundle_id"]}
    put(output / "probe-execution.json", {
        **session, "process_id": 100, "start_filetime": 10, "end_filetime": 20,
        "exit_code": 0, "snapshot_sha256": contract.sha256_file(snapshot), "probe_sha256": probe_sha,
    })

    sequence = 0

    def process(folder: Path, role: str, relative: str, mode: str, *,
                has_capture: bool = False) -> None:
        nonlocal sequence
        sequence += 1
        folder.mkdir(parents=True)
        start = sequence * 1_000_000_000
        decision = optional if role == "variant" else common
        artifact = next(item for item in manifest["artifacts"] if item["path"] == relative)
        identity = {"launch_id": f"{sequence:032x}", "process_id": 100 + sequence,
                    "executable_sha256": artifact["sha256"]}
        command = [relative, "--runs", "1", "--warmup-seconds", "5" if mode == "warm" else "0",
                   "--run-seconds", "60" if mode == "warm" else "1", "--fixed-quality", contract.QUALITY_PROFILE_ID,
                   "--role", role, "--artifact-path", relative, "--launch-id", identity["launch_id"],
                   "--commit", COMMIT, "--device-tier", decision["physical_device_tier"],
                   "--device-key", decision["device_key"], "--environment-key", decision["environment_key"],
                   "--adapter-luid", decision["adapter"]["adapter_luid"]]
        if mode == "canonical_reference":
            command.append("--capture-display")
        put(folder / "launch.json", {
            **session, **identity, "mode": mode, "role": role, "artifact_path": relative, "command": command,
            "start_filetime": start, "end_filetime": start + 900_000_000,
            "start_qpc_ms": 50, "end_qpc_ms": 70000, "exit_code": 0,
        })
        if role in measurements.ROLE_ORDER:
            def point(begin):
                return {"start_qpc_ms": begin, "end_qpc_ms": begin + 5,
                        "observation": observation["host_observation"]}
            put(folder / "operating-conditions.json",
                {**identity, "before": point(10), "after": point(70001),
                 "window": [point(value) for value in (10000, 25000, 40000, 55000)] if mode == "warm" else []})
        ids = manifest["shader_inventory"]["roles"].get(role, [])
        report_identity = {**identity, "process_creation_filetime": start + 1,
                           "tested_commit": COMMIT, "package_sha256": package_sha}
        put(folder / "report.json", {
            **report_identity, "role": role, "artifact_path": relative, "status": "eligible",
            "device_tier": decision["physical_device_tier"], "device_key": decision["device_key"],
            "environment_key": decision["environment_key"], "capability_decision": decision,
            "format": "nw11-raw-run.v1", "adapter": {k: v for k, v in observation["adapter"].items() if k != "subsystem_id"},
            "timing": {"warmup_start_qpc_ms": 100, "measurement_start_qpc_ms": 5100,
                       "measurement_end_qpc_ms": 65100},
            "measurement": {"presented_frames": 602, "process_start_to_first_present_ms": 900, "gpu_time_samples": [],
                            "cpu_submit_samples": [5.0] * 601, "shader_count": len(ids),
                            "shader_ids": ids, "peak_engine_committed_gpu_bytes": 300000000,
                            "distribution_bytes": manifest["accounting"].get(f"{role}_distribution_bytes", 0)},
        })
        if has_capture:
            put(folder / "captures.json", captures(folder, report_identity))
        if mode == "warm":
            rows = ["ProcessID,CPUStartQPCTime,SyncInterval,SwapChainAddress,GPUTime,DisplayedTime,DisplayLatency"]
            gpu = 7.5 if role == "variant" else 10.0
            rows.extend(f"{identity['process_id']},{5200 + index * 90},0,abc,{gpu},16,25"
                        for index in range(600))
            rows.append(f"{identity['process_id']},60000,0,abc,{gpu},NA,NA")
            (folder / "presentmon.csv").write_text("\n".join(rows) + "\n")

    roles = measurements.role_paths(manifest, optional)
    for role, relative in roles.items():
        process(output / "raw" / role / "startup", role, relative, "startup")
    audit_start = 3_950_000_000
    put(output / "reboot-audit.json", {**session, "collected_by_process_id": 90,
        "start_filetime": audit_start, "end_filetime": audit_start + 1,
        "observation": {"unavailable": "No access to audit log"}})
    for role, relative in roles.items():
        folder = output / "raw" / role
        for index in range(1, 4):
            process(folder / f"run-{index}", role, relative, "warm", has_capture=index == 3)

    process(output / "canonical-reference", "canonical_reference", "bin/canonical_reference.exe",
            "canonical_reference", has_capture=True)
    release = output / "canonical-release"
    release.mkdir()
    put(release / "captures.json", captures(release, {}))
    for index, tick in enumerate((*contract.REFERENCE_FIXED_TICKS, contract.LOOP_TICKS), 1):
        player_log = release / f"{tick}.log"
        player_log.write_text(f"NW05_EXPLICIT_TICK={tick}\n", encoding="utf-8")
        display = release / f"{tick}-display.json"
        put(display, {"source": "dwm_composed_client", "process_id": 1000 + index, "observed_qpc_ms": 920})
        put(release / f"{tick}-execution.json", {
            "display_observation_sha256": contract.sha256_file(display), "process_started_qpc_ms": 100,
            "capture_probe_sha256": probe_sha,
            "reference_sha256": contract.sha256_file(output / "canonical-reference" / f"{tick}.rgba"),
            "player_log_sha256": contract.sha256_file(player_log),
            **session, "executable_sha256": contract.sha256_file(root / "bin/canonical_release.exe"),
            "process_id": 1000 + index, "tick": tick, "start_filetime": 2000 + index,
            "end_filetime": 3000 + index, "player_exit_code": 0, "capture_exit_code": 0,
            "command": ["canonical_release.exe", "--tick", str(tick), "--frames", "1"],
        })

    windowed_report = output / "raw/baseline/run-1/report.json"
    report = measurements.load_json(windowed_report)
    put(windowed_report, {**report, "measurement": {**report["measurement"], "presented_frames": 0}})
    with pytest.raises(evidence.EvidenceError, match="No successful windowed Present"):
        measurements.collect(root, manifest, output, "evidence.json")
    put(windowed_report, report)

    record = measurements.collect(root, manifest, output, "evidence.json")
    assert [len(run["measurement"]["process_runs"]) for run in record["runs"]] == [3, 3, 3]
    assert record["runs"][0]["measurement"]["process_runs"][0]["dropped_presents"] == 1
    put(output / "evidence.json", record)
    monkeypatch.setattr(bundle, "verify_bundle", lambda *a, **k: {"manifest": manifest, "accounting": manifest["accounting"]})
    arguments = ["evidence", "--bundle-root", str(root), "--evidence", str(output / "evidence.json"),
                 "--json-out", str(output / "decisions.json")]
    assert verify.main(arguments) == verify.EXIT_OK
    decision = json.loads((output / "decisions.json").read_text())
    assert decision["devices"][0]["canonical_release_correctness"]["loop_boundary"]["variant"]["rmse"] == 0
    assert decision["devices"][0]["status"] == "pending"
    assert record["runs"][0]["measurement"]["cold_initialization_ms"] is None
    assert record["runs"][0]["measurement"]["unqualified_startup_ms"] == 900
    assert decision["devices"][0]["canonical_release_correctness"]["numeric_loop_boundary"]["status"] == "verified"
    assert not decision["satisfies_completion"]

    audit_path = output / "reboot-audit.json"
    audit = measurements.load_json(audit_path)
    boot = 400_000_000
    audit["observation"] = {
        "boot_type": "0", "boot_record": 3, "boot_time": boot, "system_started": boot,
        "oldest_security": boot - 100, "previous_process_audit": boot - 10, "disruptions": [],
        "process_creation_audit_flags": 1,
        "launches": [],
    }
    for index, role in enumerate(roles, 1):
        launch = measurements.load_json(output / "raw" / role / "startup/launch.json")
        audit["observation"]["launches"].append({
            "record_id": index * 12, "time": launch["start_filetime"] + 1,
            "path": launch["command"][0], "pid": hex(launch["process_id"]),
        })
    put(audit_path, audit)
    cold = measurements.collect(root, manifest, output, "evidence.json")
    assert [run["measurement"]["cold_initialization_ms"] for run in cold["runs"]] == [900] * 3
    put(output / "evidence.json", cold)
    assert verify.main([*arguments, "--require-accepted"]) == verify.EXIT_OK

    first = audit["observation"]["launches"][0]
    audit["observation"]["launches"].append(dict(first, record_id=1, time=boot + 100, pid="0xbeef"))
    put(audit_path, audit)
    pre_run = measurements.collect(root, manifest, output, "evidence.json")
    assert pre_run["runs"][0]["measurement"]["cold_initialization_ms"] is None
    audit["observation"]["launches"].pop()
    put(audit_path, audit)

    settings_path = output / "raw/variant/run-2/operating-conditions.json"
    original = measurements.load_json(settings_path)
    settings = copy.deepcopy(original)
    settings["window"][1]["observation"]["power_plan"] = "Power Scheme GUID: 12345678-1234-1234-1234-123456789abc"
    settings["window"][1]["observation"]["nvidia_smi_xml"] = (
        settings["window"][1]["observation"]["nvidia_smi_xml"].replace("165 W", "150 W"))
    put(settings_path, settings)
    changed = measurements.collect(root, manifest, output, "evidence.json")
    telemetry = changed["runs"][1]["measurement"]["operating_conditions"][2]
    assert telemetry["status"] == "pending"
    assert set(telemetry["reasons"]) == {"windows_power_plan changed after preflight",
                                         "GPU enforced power limit changed after preflight"}
    assert telemetry["samples"][2]["environment"]["temperature_c"] == 38
    put(output / "evidence.json", changed)
    assert verify.main([*arguments, "--require-accepted"]) == verify.EXIT_PENDING
    put(settings_path, original)

    # Editing only the derived aggregate must not change a decision.
    cold["runs"][0]["measurement"]["process_runs"][0]["gpu_time_samples"][0] = 3
    put(output / "evidence.json", cold)
    assert verify.main(arguments) == verify.EXIT_MALFORMED
    # Reusing an earlier launch under a new role-run path also fails raw regeneration.
    role = output / "raw" / "baseline"
    (role / "run-2/launch.json").write_bytes((role / "run-1/launch.json").read_bytes())
    (role / "run-2/report.json").write_bytes((role / "run-1/report.json").read_bytes())
    assert verify.main(arguments) == verify.EXIT_STALE
    with pytest.raises(evidence.EvidenceError, match="reused process identity"):
        measurements.collect(root, manifest, output, "evidence.json")


def test_nw11_native_inventory_counts_compiled_shaders_in_package_and_pe() -> None:
    from nw11_production_binding import shader_inventory

    def container(part: bytes, content: bytes) -> bytes:
        size = 44 + len(content)
        return (b"DXBC" + bytes(16) + struct.pack("<IIII", 1, size, 1, 36)
                + part + struct.pack("<I", len(content)) + content)

    raster = container(b"DXIL", b"retained raster shader")
    mesh = container(b"DXIL", b"mesh shader")
    signature = container(b"RTS0", b"signature")
    observed = shader_inventory(b"MZ" + signature + mesh + b"NWP-package" + raster)
    assert [item["bytes"] for item in observed] == [len(mesh), len(raster)]
    assert all(item["id"].endswith(contract.sha256_bytes(payload))
               for item, payload in zip(observed, (mesh, raster)))


def test_nw11_reflection_fixture_distinguishes_known_hits_and_misses() -> None:
    document = _experiment(contract.EXPERIMENT_RAY_ID)
    fixture = document["criteria"]["image"]["fixture"]
    assert fixture["miss_segment_length"] < fixture["outward_offset"] < fixture["hit_segment_length"]
    shader = (REPO_ROOT / "shaders/nw11/reflection/nw11_reflection_fixture.hlsli").read_text()
    assert "ReflectionOffset = 0.02" in shader
    assert "ReflectionOffset + 0.001" in shader
    assert "ReflectionOffset - 0.005" in shader
    assert "hit ? float3(0.92, 0.16, 0.05) : AnalyticEnvironment" in shader


def test_nw11_canonical_release_binding_reads_frozen_manifest_digest() -> None:
    import nw05_export
    import nw11_production_binding as binding
    manifest = b'{"release_manifest": true}'
    link = bytes.fromhex(contract.sha256_bytes(manifest))
    image = nw05_export.embed_package(b"MZtest release", b"canonical package", link)
    assert binding.embedded_payloads(image) == (b"MZtest release", b"canonical package", link)
