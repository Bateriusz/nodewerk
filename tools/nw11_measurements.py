"""Reconstruct physical NW-11 evidence from exact process and capture inputs."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import runpy
from typing import Any

import nw11_bundle as bundle
import nw11_capability as capability
import nw11_contract as contract
import nw11_evidence as evidence
from nw07_presentmon_collector import extract_samples

ROLE_ORDER = ("baseline", "variant", "fallback")
ARTIFACT_ROLES = dict(zip(ROLE_ORDER, ("common_baseline", "optional_variant", "fallback_common")))


def load_json(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise evidence.EvidenceError(f"Invalid raw JSON {path}: {exc}") from exc
    if not isinstance(record, dict):
        raise evidence.EvidenceError(f"Raw JSON is not an object: {path}")
    return record


def evaluate_profiles(root: Path, manifest: dict, observation: dict) -> tuple[dict, dict]:
    common = capability.load_profile(root / "manifests/common-capability-profile.json")
    optional = capability.load_profile(root / manifest["capability_profile"]["path"])
    return (capability.evaluate_observation(common, observation),
            capability.evaluate_observation(optional, observation))


def validate_release_adapter(observation: dict) -> None:
    if (not observation["adapter"].get("adapter_luid")
            or observation.get("release_default_adapter_luid") != observation["adapter"]["adapter_luid"]):
        raise evidence.EvidenceError("The canonical release would select a different adapter")


def role_paths(manifest: dict, optional: dict) -> dict[str, str]:
    return {role: next(entry["path"] for entry in manifest["artifacts"]
                       if entry["role"] == ARTIFACT_ROLES[role])
            for role in ROLE_ORDER if role != "variant" or optional["status"] == "eligible"}


def _equal(observed: Any, expected: Any, label: str) -> None:
    if observed != expected:
        raise evidence.EvidenceError(f"{label} disagrees with the bound execution")


def validate_renderer_command(launch: dict, protocol: dict, decision: dict) -> None:
    """Check recorded settings so changed launch arguments cannot qualify."""
    command = launch["command"]
    if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
        raise evidence.EvidenceError("Invalid recorded renderer command")
    leaf = lambda path: Path(path.replace("\\", "/")).name
    _equal(leaf(command[0]), leaf(launch["artifact_path"]), "renderer executable")
    paired = {}
    display_capture = False
    arguments = iter(command[1:])
    for flag in arguments:
        if flag == "--capture-display":
            if display_capture:
                raise evidence.EvidenceError("Duplicate display capture flag")
            display_capture = True
            continue
        value = next(arguments, None)
        if flag in paired or value is None or value.startswith("--"):
            raise evidence.EvidenceError("Invalid or duplicate renderer arguments")
        paired[flag] = value
    warm = launch["mode"] == "warm"
    required = {
        "--runs": "1", "--warmup-seconds": str(protocol["warmup_seconds"] if warm else 0),
        "--run-seconds": str(protocol["run_seconds"] if warm else 1),
        "--fixed-quality": contract.QUALITY_PROFILE_ID, "--role": launch["role"],
        "--artifact-path": launch["artifact_path"], "--launch-id": launch["launch_id"],
        "--commit": launch["commit"], "--device-tier": decision["physical_device_tier"],
        "--device-key": decision["device_key"], "--environment-key": decision["environment_key"],
        "--adapter-luid": decision["adapter"]["adapter_luid"],
    }
    for flag, value in required.items():
        _equal(paired.get(flag), value, flag)
    allowed = required.keys() | {
        "--benchmark-json", "--capability-decision", "--distribution-bytes",
        "--shader-ids", "--capture-dir", "--capture-manifest",
    }
    if not paired.keys() <= allowed:
        raise evidence.EvidenceError("Recorded renderer arguments changed the frozen mode")
    _equal(display_capture, launch["mode"] == "canonical_reference", "canonical capture mode")


def _validate_launch(folder: Path, manifest: dict, decision: dict, relative: str,
                     mode: str, role: str, launch_ids: set, protocol: dict) -> tuple[dict, dict]:
    launch = load_json(folder / "launch.json")
    report = load_json(folder / "report.json")
    validate_renderer_command(launch, protocol, decision)
    artifact = next(item for item in manifest["artifacts"] if item["path"] == relative)
    for key, expected in {
        "commit": manifest["commit"], "bundle_id": manifest["bundle_id"], "mode": mode,
        "artifact_path": relative, "executable_sha256": artifact["sha256"],
        "exit_code": 0, "role": role,
    }.items():
        _equal(launch.get(key), expected, f"{folder.name} launch {key}")
    for key, expected in {
        "format": "nw11-raw-run.v1", "status": "eligible", "artifact_path": relative,
        "tested_commit": manifest["commit"], "role": role,
        "executable_sha256": artifact["sha256"], "capability_decision": decision,
        "device_tier": decision["physical_device_tier"], "device_key": decision["device_key"],
        "environment_key": decision["environment_key"],
        "adapter": {key: decision["adapter"][key] for key in ("vendor_id", "device_id", "adapter_luid")}
        | {"adapter_name": decision["adapter"]["adapter_name_metadata_only"]},
    }.items():
        _equal(report.get(key), expected, f"{folder.name} report {key}")
    expected_package = artifact["metadata"].get("package_sha256")
    if mode == "canonical_reference":
        expected_package = next(item["metadata"]["package_sha256"] for item in manifest["artifacts"]
                                if item["role"] == "common_baseline")
    _equal(report.get("package_sha256"), expected_package, "package_sha256")
    for key in ("launch_id", "process_id"):
        _equal(report.get(key), launch.get(key), key)
    identity = (report["process_id"], report["process_creation_filetime"])
    marker = launch["launch_id"]
    if (not isinstance(marker, str) or len(marker) != 32 or marker in launch_ids
            or identity in launch_ids or type(identity[0]) is not int or identity[0] <= 0
            or not launch["start_filetime"] <= identity[1] <= launch["end_filetime"]):
        raise evidence.EvidenceError(f"Invalid or reused process identity in {folder}")
    launch_ids.update((marker, identity))
    samples = report["measurement"]
    peak = samples["peak_engine_committed_gpu_bytes"]
    if type(peak) is not int or peak <= 0:
        raise evidence.EvidenceError(f"Invalid production allocation high-water mark in {folder}")
    startup = samples["process_start_to_first_present_ms"]
    if (isinstance(startup, bool) or not isinstance(startup, (int, float))
            or not math.isfinite(startup)
            or not 0 < startup <= (launch["end_filetime"] - identity[1]) / 10000):
        raise evidence.EvidenceError(f"Invalid startup timing in {folder}")
    if role in ROLE_ORDER:
        ids = manifest["shader_inventory"]["roles"][role]
        _equal(samples["shader_ids"], ids, "runtime shader identities")
        _equal(samples["shader_count"], len(ids), "runtime shader count")
        _equal(samples["distribution_bytes"], manifest["accounting"][f"{role}_distribution_bytes"],
               "distribution bytes")
    return launch, report


def presentation_samples(path: Path, launch: dict, timing: dict, minimum: int) -> dict:
    begin, end = timing["measurement_start_qpc_ms"], timing["measurement_end_qpc_ms"]
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("ProcessID") != str(launch["process_id"]):
                continue
            stamp = float(row["CPUStartQPCTime"])
            if begin <= stamp <= end:
                if row.get("SyncInterval") != "0":
                    raise evidence.EvidenceError("PresentMon observed a capped or overridden sync interval")
                if "DisplayedTime" not in row or "DisplayLatency" not in row:
                    raise evidence.EvidenceError("PresentMon display telemetry is missing")
                rows.append(row)
    if len({row["SwapChainAddress"] for row in rows}) != 1:
        raise evidence.EvidenceError("Expected exactly one measured PresentMon swap chain")
    gpu, _, displayed = extract_samples(rows, process_id=launch["process_id"])
    # Finite displayed intervals track missed screen refreshes. Rows with no
    # DisplayLatency explicitly describe presents that were never displayed.
    dropped = sum(row["DisplayLatency"].strip().upper() in {"NA", "N/A", "NAN", ""} for row in rows)
    if len(displayed) < minimum or any(value <= 0 for value in displayed):
        raise evidence.EvidenceError("Insufficient PresentMon displayed-frame intervals")
    if len(gpu) < minimum or any(value <= 0 for value in gpu):
        raise evidence.EvidenceError("Insufficient PresentMon GPU frame timing")
    return {"gpu_time_samples": gpu, "frame_time_samples": displayed, "presents": len(rows),
            "dropped_presents": dropped, "displayed_presents": len(rows) - dropped}


def _warm_run(folder: Path, launch: dict, report: dict, protocol: dict) -> dict:
    timing = report["timing"]
    warmup = timing["warmup_start_qpc_ms"]
    begin, end = timing["measurement_start_qpc_ms"], timing["measurement_end_qpc_ms"]
    elapsed_filetime_ms = (launch["end_filetime"] - launch["start_filetime"]) / 10000
    if (elapsed_filetime_ms + 1000 < end - warmup
            or not launch["start_qpc_ms"] <= warmup < begin < end <= launch["end_qpc_ms"]
            or begin - warmup < protocol["warmup_seconds"] * 1000
            or end - begin < protocol["run_seconds"] * 1000):
        raise evidence.EvidenceError(f"Invalid measurement interval in {folder}")
    samples = report["measurement"]
    minimum = protocol["min_raw_samples_per_run"]
    cpu = samples["cpu_submit_samples"]
    if (not isinstance(cpu, list) or len(cpu) < minimum
            or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in cpu)):
        raise evidence.EvidenceError(f"Missing per-process CPU submission samples in {folder}")
    display = presentation_samples(folder / "presentmon.csv", launch, timing, minimum)
    if abs(display["presents"] - len(cpu)) > 2:
        raise evidence.EvidenceError("ETW present count disagrees with the production frame count")
    return {**samples, **display, "launch": launch, "timing": timing}


def _capture(folder: Path, report: dict) -> Path:
    path = folder / "captures.json"
    capture = load_json(path)
    for key in ("launch_id", "process_id", "process_creation_filetime", "tested_commit",
                "executable_sha256", "package_sha256"):
        _equal(capture.get(key), report[key], f"capture {key}")
    return path


def _comparison_tool() -> Any:
    here = Path(__file__).resolve().parent
    path = here / "compare-captures.py"
    if not path.exists():
        path = here / "nw11/compare-captures.py"
    return runpy.run_path(str(path))["compare"]


def _canonical_capture(root: Path, directory: Path, manifest: dict, common: dict,
                       launch_ids: set, compare: Any, protocol: dict) -> dict:
    reference_folder = directory / "canonical-reference"
    _, reference = _validate_launch(reference_folder, manifest, common,
                                    "bin/canonical_reference.exe", "canonical_reference",
                                    "canonical_reference", launch_ids, protocol)
    release = directory / "canonical-release"
    release_sha = contract.sha256_file(root / "bin/canonical_release.exe")
    captures = load_json(release / "captures.json")
    for tick in (*contract.REFERENCE_FIXED_TICKS, contract.LOOP_TICKS):
        execution = load_json(release / f"{tick}-execution.json")
        for key, value in {
            "tick": tick, "player_exit_code": 0, "capture_exit_code": 0,
            "commit": manifest["commit"], "bundle_id": manifest["bundle_id"],
            "executable_sha256": release_sha,
        }.items():
            _equal(execution.get(key), value, f"canonical capture {key}")
        command = execution["command"]
        identity = (execution["process_id"], execution["start_filetime"])
        if (identity in launch_ids or not isinstance(identity[0], int) or identity[0] <= 0
                or execution["end_filetime"] <= execution["start_filetime"]
                or command[1:] != ["--tick", str(tick), "--frames", "1"]
                or Path(command[0].replace("\\", "/")).name != "canonical_release.exe"):
            raise evidence.EvidenceError(f"Invalid canonical release execution at {tick}")
        log = release / f"{tick}.log"
        _equal(contract.sha256_file(log), execution["player_log_sha256"], "canonical release process log")
        if f"NW05_EXPLICIT_TICK={tick}" not in log.read_text(encoding="utf-8").splitlines():
            raise evidence.EvidenceError(f"Canonical executable did not report the requested tick {tick}")
        launch_ids.add(identity)
    result = compare(baseline_manifest=_capture(reference_folder, reference),
                     variant_manifest=release / "captures.json", reference_kind="raster_reference")
    image_sha = {item["tick"]: item.get("sha256") for item in captures["images"]}
    for item in result["per_tick"]:
        _equal(item["variant_sha256"], image_sha.get(item["tick"]), "canonical release capture hash")
    _require_image(result, "canonical release/reference")
    return result


def _require_image(image: dict, label: str) -> None:
    if (image["max_abs_channel_error"] > contract.IMAGE_ABSOLUTE_TOLERANCE
            or image["rmse"] > contract.IMAGE_RMSE_TOLERANCE
            or any(value["rmse"] > contract.IMAGE_RMSE_TOLERANCE
                   for value in image["loop_boundary"].values())):
        raise evidence.EvidenceError(f"{label} image comparison exceeded the frozen tolerance")


def collect(root: Path, manifest: dict, directory: Path, evidence_file: str) -> dict:
    """Rebuild all decisions from retained probe, executable reports, ETW CSVs and pixels."""
    snapshot = directory / "capability-snapshot.json"
    probe_path = directory / "probe-execution.json"
    probe = load_json(probe_path)
    for key, value in {
        "commit": manifest["commit"], "bundle_id": manifest["bundle_id"], "exit_code": 0,
        "probe_sha256": contract.sha256_file(root / "bin/nw11_capability_probe.exe"),
        "snapshot_sha256": contract.sha256_file(snapshot),
    }.items():
        _equal(probe.get(key), value, f"probe {key}")
    if probe["process_id"] <= 0 or probe["start_filetime"] >= probe["end_filetime"]:
        raise evidence.EvidenceError("Invalid probe process timing")
    observation = load_json(snapshot)
    validate_release_adapter(observation)
    common, optional = evaluate_profiles(root, manifest, observation)
    for name, decision in (("common-eligibility.json", common), ("eligibility.json", optional)):
        _equal(load_json(directory / name), decision, name)
    if (common["status"] != "eligible" or not common["physical_device_tier"]
            or not common["adapter"]["adapter_luid"] or optional["status"] not in
            {"eligible", "unsupported", "pending"}):
        raise evidence.EvidenceError("Probe did not qualify this representative physical board")
    settings = load_json(root / manifest["experiment_definition"]["path"])
    protocol = settings["protocol"]
    reference = settings["criteria"]["image"]["reference_kind"]
    roles = role_paths(manifest, optional)
    _equal({entry.name for entry in (directory / "raw").iterdir()}, set(roles), "executed roles")
    launch_ids: set = set()
    runs = {}
    captures = {}
    for role, relative in roles.items():
        path = directory / "raw" / role
        decision = optional if role == "variant" else common
        startup_launch, startup = _validate_launch(path / "startup", manifest, decision,
                                                   relative, "startup", role, launch_ids, protocol)
        reset = load_json(path / "cache-execution.json")
        preparation = load_json(path / "cache-preparation.json")
        if (probe["end_filetime"] > reset["start_filetime"]
                or reset["probe_sha256"] != contract.sha256_file(root / "bin/nw11_capability_probe.exe")
                or reset["result_sha256"] != contract.sha256_file(path / "cache-preparation.json")
                or not reset["start_filetime"] < reset["end_filetime"] <= startup_launch["start_filetime"]):
            raise evidence.EvidenceError(f"Invalid cache preparation for {role}")
        cleared = reset["exit_code"] == 0
        if cleared:
            _equal(preparation, {"format": "nw11-driver-cache-preparation.v1", "status": "cleared",
                                 "method": "D3D12 ShaderCacheControl(CLEAR)",
                                 "adapter_luid": decision["adapter"]["adapter_luid"]}, "cache reset")
        processes = []
        previous_end = startup_launch["end_filetime"]
        for index in range(1, protocol["runs"] + 1):
            folder = path / f"run-{index}"
            launch, report = _validate_launch(folder, manifest, decision, relative, "warm", role, launch_ids, protocol)
            if launch["start_filetime"] < previous_end:
                raise evidence.EvidenceError("Warm run started before the preceding process exited")
            previous_end = launch["end_filetime"]
            processes.append(_warm_run(folder, launch, report, protocol))
        captures[role] = _capture(folder, report)
        metrics = {
            "process_runs": processes,
            "cold_initialization_ms": startup["measurement"]["process_start_to_first_present_ms"] if cleared else None,
            "cache_preparation": preparation,
            "peak_engine_committed_gpu_bytes": max(startup["measurement"]["peak_engine_committed_gpu_bytes"],
                                                   *(run["peak_engine_committed_gpu_bytes"] for run in processes)),
            **{key: report["measurement"][key] for key in ("shader_count", "shader_ids", "distribution_bytes")},
        }
        runs[role] = {**{key: report[key] for key in ("role", "device_tier", "device_key", "environment_key",
                                                      "status", "artifact_path", "adapter", "capability_decision")},
                      "measurement": metrics}
    compare = _comparison_tool()
    canonical = _canonical_capture(root, directory, manifest, common, launch_ids, compare, protocol)
    fallback = compare(baseline_manifest=captures["baseline"], variant_manifest=captures["fallback"],
                       reference_kind=reference)
    _require_image(fallback, "common fallback")
    runs["baseline"]["measurement"]["image"] = fallback
    runs["fallback"]["measurement"]["image"] = fallback
    if "variant" in roles:
        runs["variant"]["measurement"]["image"] = compare(
            baseline_manifest=captures["baseline"], variant_manifest=captures["variant"], reference_kind=reference)
    inputs = {path.relative_to(directory).as_posix(): contract.sha256_file(path)
              for folder in ("raw", "canonical-release", "canonical-reference")
              for path in sorted((directory / folder).rglob("*"))
              if path.is_file() and path.suffix in {".json", ".csv", ".rgba", ".log"}}
    inputs[snapshot.name] = contract.sha256_file(snapshot)
    for name in ("common-eligibility.json", "eligibility.json", "probe-execution.json"):
        inputs[name] = contract.sha256_file(directory / name)
    return {
        "format": evidence.EVIDENCE_FORMAT, "version": evidence.EVIDENCE_VERSION,
        "task": contract.TASK, "implementation_base": contract.IMPLEMENTATION_BASE,
        **{key: manifest[key] for key in ("commit", "bundle_id", "experiment_id", "profile_id", "fallback_artifact_id")},
        "bundle_manifest_sha256": contract.sha256_file(root / bundle.MANIFEST_NAME),
        "capability_profile_sha256": manifest["capability_profile"]["sha256"],
        "artifact_hashes": {item["path"]: item["sha256"] for item in manifest["artifacts"]
                            if item["role"] in ARTIFACT_ROLES.values()
                            or item["path"] in {"bin/canonical_release.exe", "bin/canonical_reference.exe",
                                                "bin/nw11_capability_probe.exe", "runtime/presentmon.exe"}},
        "optional_eligibility": optional, "common_eligibility": common,
        "runs": list(runs.values()), "canonical_release_correctness": canonical,
        "raw_inputs": inputs, "evidence_file": evidence_file,
        "generated_by": "tools/nw11/collect-evidence.py",
        "raw_per_sample_results_preserved": True, "operator_supplied_values": False,
    }
