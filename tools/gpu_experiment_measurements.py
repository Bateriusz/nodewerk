"""Reconstruct physical NW-11 evidence from exact process and capture inputs."""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
import runpy
from typing import Any

import nw11_bundle as bundle
import nw11_capability as capability
import nw11_contract as contract
import validation_cold_start as nw11_cold_start
import validation_gpu_telemetry as nw11_host
import nw11_evidence as evidence
from presentation_metrics import extract_samples

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


def evaluate_profiles(root: Path, manifest: dict, observation: dict,
                      host: dict | None = None) -> tuple[dict, dict]:
    common = capability.load_profile(root / "manifests/common-capability-profile.json")
    optional = capability.load_profile(root / manifest["capability_profile"]["path"])
    if host is not None:
        observation = {**observation, "host_observation": host}
    common_result = capability.evaluate_observation(common, observation)
    optional_result = capability.evaluate_observation(optional, observation)
    provenance = common_result.get("physical_provenance")
    if provenance and optional_result["status"] == "eligible":
        modes = provenance["environment"]["nvidia_power_management_mode"]
        if modes["common_baseline.exe"]["value"] != modes["optional_variant.exe"]["value"]:
            optional_result = {
                **optional_result, "status": "pending", "satisfies_optional_profile": False,
                "diagnostics": [{"code": "NW11-POWER-PROFILE-MISMATCH",
                                 "message": "Baseline and variant effective NVIDIA power modes differ"}],
            }
    return common_result, optional_result


def validate_release_adapter(observation: dict) -> None:
    # The canonical release and every experiment explicitly select this LUID.
    # The adapter that would be selected without an override is metadata only.
    if not re.fullmatch(r"[0-9a-fA-F]{8}:[0-9a-fA-F]{8}", str(observation["adapter"].get("adapter_luid") or "")):
        raise evidence.EvidenceError("The selected adapter LUID is missing or malformed")


def role_paths(manifest: dict, optional: dict) -> dict[str, str]:
    return {role: next(entry["path"] for entry in manifest["artifacts"]
                       if entry["role"] == ARTIFACT_ROLES[role])
            for role in ROLE_ORDER if role != "variant" or optional["status"] == "eligible"}


def warm_run_schedule(settings: dict, roles: dict) -> list[tuple[str, int]]:
    """One predeclared order, used by the launcher and by evidence reconstruction."""
    if settings.get("version") in (2, 3):
        order = settings["validation_policy"]["physical"]["warm_run_order"]
        return [(role, index) for index, row in enumerate(order, 1)
                for role in row if role in roles]
    return [(role, index) for role in roles
            for index in range(1, settings["protocol"]["runs"] + 1)]


def validate_warm_run_order(settings: dict, roles: dict, observed: list[tuple[str, int, dict]]) -> None:
    ordered = sorted(observed, key=lambda item: item[2]["start_filetime"])
    if [(role, index) for role, index, _ in ordered] != warm_run_schedule(settings, roles):
        raise evidence.EvidenceError("Warm process sequence differs from the declared matched-run schedule")
    if any(left[2]["end_filetime"] > right[2]["start_filetime"]
           for left, right in zip(ordered, ordered[1:])):
        raise evidence.EvidenceError("Warm processes overlap across experiment roles")


def matched_start_conditions(settings: dict, baseline: dict, variant: dict) -> list[str]:
    """Compare only pre-launch samples, not workload-induced operating differences."""
    if settings.get("version") not in (2, 3):
        return []
    policy = settings["validation_policy"]["physical"]
    try:
        left = baseline["samples"][0]["environment"]
        right = variant["samples"][0]["environment"]
        if baseline["status"] != "matched" or variant["status"] != "matched":
            return ["starting conditions unavailable or not qualified"]
        reasons = []
        if abs(left["temperature_c"] - right["temperature_c"]) > policy["max_start_temperature_delta_c"]:
            reasons.append("matched run starting GPU temperatures differ beyond tolerance")
        for name in ("graphics_clock", "mem_clock"):
            first, second = left["gpu_clocks"][name], right["gpu_clocks"][name]
            if abs(first["observed_mhz"] - second["observed_mhz"]) > (
                    first["maximum_mhz"] * policy["max_start_clock_delta_fraction_of_max"]):
                reasons.append(f"matched run starting {name} differs beyond tolerance")
        return reasons
    except (KeyError, TypeError, IndexError):
        return ["starting temperature or clocks unavailable"]


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
    if mode == "canonical_reference" and expected_package is None:
        expected_package = next(item["metadata"]["canonical_package_sha256"] for item in manifest["artifacts"]
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
    presented = samples.get("presented_frames")
    if type(presented) is not int or presented < 1:
        raise evidence.EvidenceError(f"No successful windowed Present in {folder}")
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


def operating_conditions(folder: Path, launch: dict, report: dict, host: dict,
                         adapter: dict, initial: dict) -> dict:
    raw = load_json(folder / "operating-conditions.json")
    for key in ("launch_id", "process_id", "executable_sha256"):
        _equal(raw.get(key), launch[key], f"operating conditions {key}")
    timing = report["timing"]
    windows = raw["window"]
    if not isinstance(windows, list):
        raise evidence.EvidenceError("Operating sample windows must be a list")
    start, end = launch["start_qpc_ms"], launch["end_qpc_ms"]
    conditions = [(raw["before"], 0, start), (raw["after"], end, float("inf"))]
    reasons = []
    if launch["mode"] == "warm":
        if len(windows) != 4:
            reasons.append("four in-window operating samples are required")
        conditions.extend((row, timing["measurement_start_qpc_ms"], timing["measurement_end_qpc_ms"])
                          for row in windows)
    elif windows != []:
        raise evidence.EvidenceError("Unexpected operating samples for a startup launch")
    stable = ("windows_power_plan", "pcie_power_management", "gpu_uuid", "gpu_pci_bus",
              "nvidia_driver_reported_version", "nvidia_power_management_mode")
    telemetry = []
    for sample, earliest, latest in conditions:
        a, b = sample["start_qpc_ms"], sample["end_qpc_ms"]
        if (type(a) not in (int, float) or type(b) not in (int, float)
                or not math.isfinite(a) or not math.isfinite(b) or not earliest <= a <= b <= latest):
            reasons.append("operating sample did not coincide with its declared process interval")
            continue
        operating = sample["observation"]
        required = {"power_plan", "pcie_power", "nvidia_smi_xml", "nvidia_power_profiles"}
        observed = (nw11_host.qualify(host | operating, adapter)
                    if isinstance(operating, dict) and required <= operating.keys() else None)
        if observed is None:
            reasons.append("operating controls or clock and thermal telemetry are unavailable")
            continue
        environment = observed["environment"]
        for key in stable:
            if environment[key] != initial[key]:
                reasons.append(f"{key} changed after preflight")
        if environment["gpu_power_state"]["enforced_limit_w"] != initial["gpu_power_state"]["enforced_limit_w"]:
            reasons.append("GPU enforced power limit changed after preflight")
        if any(clock["maximum_mhz"] != initial["gpu_clocks"][name]["maximum_mhz"]
               for name, clock in environment["gpu_clocks"].items()):
            reasons.append("GPU maximum clocks changed after preflight")
        telemetry.append({"start_qpc_ms": a, "end_qpc_ms": b, "environment": environment})
    if any(first["end_qpc_ms"] > second["start_qpc_ms"] for first, second in zip(windows, windows[1:])):
        reasons.append("operating measurement windows overlap or are out of order")
    return {"status": "pending" if reasons else "matched", "reasons": sorted(set(reasons)),
            "samples": sorted(telemetry, key=lambda sample: sample["start_qpc_ms"])}


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


def numeric_loop(root: Path, release_start: Path, release_boundary: Path) -> dict:
    from nw07_evidence import _validate_tolerance_section, compare_rgba32f_files

    graph = load_json(root / "manifests/canonical-loop.json")
    _equal(graph["graph_sha256"], contract.BASE_GRAPH_SHA256, "NW-07 loop graph")
    loop = graph["loop_boundary"]
    start, boundary = loop["start"], loop["boundary"]
    if (loop["seamless_modulo_turn"] is not True
            or [start["tick"], boundary["tick"]] != list(contract.REFERENCE_LOOP_BOUNDARY_TICKS)
            or start["wrapped_tick"] != 0 or boundary["wrapped_tick"] != 0):
        raise evidence.EvidenceError("Invalid NW-07 loop state provenance")
    state_delta = {
        "camera_angle_modulo_turn": abs(math.remainder(boundary["camera_angle_radians"] -
                                                      start["camera_angle_radians"], math.tau)),
        "twist_degrees": abs(boundary["twist_degrees"] - start["twist_degrees"]),
    }
    if any(not math.isfinite(error) or error > contract.NUMERIC_ABSOLUTE_TOLERANCE
           for error in state_delta.values()):
        raise evidence.EvidenceError("NW-07 deterministic scene state diverged at the loop boundary")
    numeric = {**compare_rgba32f_files(release_boundary, release_start),
               "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
               "relative_tolerance": contract.NUMERIC_RELATIVE_TOLERANCE}
    state, reason = _validate_tolerance_section(
        numeric, kind="numeric", absolute_tolerance=contract.NUMERIC_ABSOLUTE_TOLERANCE,
        relative_tolerance=contract.NUMERIC_RELATIVE_TOLERANCE, relative_can_satisfy=True)
    if state != "passed":
        raise evidence.EvidenceError(f"Canonical numeric loop comparison failed: {reason}")
    return {"status": "verified", "numeric": numeric, "scene_state_error": state_delta,
            "ticks": list(contract.REFERENCE_LOOP_BOUNDARY_TICKS),
            "start_sha256": contract.sha256_file(release_start),
            "boundary_sha256": contract.sha256_file(release_boundary)}


def cross_device_captures(records: list[tuple[str, Path, dict]]) -> dict:
    from nw07_evidence import _cross_device_capture_equivalence
    from nw07_contract import CORRECTNESS_COVERAGE_TARGETS, FIXED_TICK_CORRECTNESS_ROLE
    from os.path import commonpath

    ordered = {tier: (folder, correction) for tier, folder, correction in records}
    if len(records) != 2 or set(ordered) != set(contract.DEVICE_TIERS):
        return {"status": "pending", "reason": "Both devices must retain independent release captures"}
    root = Path(commonpath([folder.resolve() for _, folder, _ in records]))
    series = []
    for tier, platform in zip(contract.DEVICE_TIERS, ("ada", "blackwell")):
        folder, correction = ordered[tier]
        if correction.get("numeric_loop_boundary", {}).get("status") != "verified":
            return {"status": "pending", "reason": "Both devices need verified numeric loop results"}
        capture_dir = (folder / "canonical-release").resolve()
        artifacts = []
        for row in correction["per_tick"]:
            raw = capture_dir / f"{row['tick']}.rgba"
            if contract.sha256_file(raw) != row["variant_sha256"]:
                raise evidence.StaleEvidenceError("Cross-device canonical capture hash changed")
            artifacts.append({"tick": row["tick"], "path": raw.relative_to(root).as_posix()})
        series.append({"coverage_target": CORRECTNESS_COVERAGE_TARGETS[platform],
                       "evidence_role": FIXED_TICK_CORRECTNESS_ROLE, "capture_artifacts": artifacts})
    status, result = _cross_device_capture_equivalence(series, artifact_root=root)
    return {"status": status, **result}


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
            "executable_sha256": release_sha, "adapter_luid": common["adapter"]["adapter_luid"],
        }.items():
            _equal(execution.get(key), value, f"canonical capture {key}")
        command = execution["command"]
        identity = (execution["process_id"], execution["start_filetime"])
        if (identity in launch_ids or not isinstance(identity[0], int) or identity[0] <= 0
                or execution["end_filetime"] <= execution["start_filetime"]
                or command[1:] != ["--tick", str(tick), "--frames", "1", "--borderless"]
                or Path(command[0].replace("\\", "/")).name != "canonical_release.exe"):
            raise evidence.EvidenceError(f"Invalid canonical release execution at {tick}")
        display_record = release / f"{tick}-display.json"
        _equal(contract.sha256_file(display_record), execution["display_observation_sha256"],
               "canonical display observation hash")
        _equal(contract.sha256_file(root / "bin/nw11_capability_probe.exe"),
               execution["capture_probe_sha256"], "canonical display observer executable")
        _equal(contract.sha256_file(reference_folder / f"{tick}.rgba"),
               execution["reference_sha256"], "canonical display observer reference")
        displayed = load_json(display_record)
        _equal(displayed.get("source"), "dwm_composed_client", "canonical display observation source")
        _equal(displayed.get("process_id"), identity[0], "canonical display observation PID")
        stamp = displayed.get("observed_qpc_ms")
        start = execution["process_started_qpc_ms"]
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or not start <= stamp <= start + contract.PROCESS_START_TO_FIRST_PRESENT_MS):
            raise evidence.EvidenceError("Canonical scene was not observed on the desktop within the startup ceiling")
        log = release / f"{tick}.log"
        _equal(contract.sha256_file(log), execution["player_log_sha256"], "canonical release process log")
        log_lines = log.read_text(encoding="utf-8").splitlines()
        selected = [line.partition("=")[2].upper() for line in log_lines
                    if line.startswith("NODEWERK_SELECTED_ADAPTER_LUID=")]
        if selected != [common["adapter"]["adapter_luid"].upper()]:
            raise evidence.EvidenceError("Canonical release did not select the measured adapter")
        if f"NW05_EXPLICIT_TICK={tick}" not in log_lines:
            raise evidence.EvidenceError(f"Canonical executable did not report the requested tick {tick}")
        launch_ids.add(identity)
    result = compare(baseline_manifest=_capture(reference_folder, reference),
                     variant_manifest=release / "captures.json", reference_kind="raster_reference")
    image_sha = {item["tick"]: item.get("sha256") for item in captures["images"]}
    for item in result["per_tick"]:
        _equal(item["variant_sha256"], image_sha.get(item["tick"]), "canonical release capture hash")
    _require_image(result, "canonical release/reference")
    result["numeric_loop_boundary"] = numeric_loop(root, release / "0.rgba", release / f"{contract.LOOP_TICKS}.rgba")
    return result


def validate_canonical_capture(root: Path, directory: Path, manifest: dict,
                               common: dict, protocol: dict) -> dict:
    """Run the complete canonical image check before the expensive warm measurements.

    Final aggregation repeats this check with all measured launch identities.
    """
    return _canonical_capture(root, directory, manifest, common, set(), _comparison_tool(), protocol)


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
    host_path = directory / "host-observation.json"
    host = load_json(host_path)
    common, optional = evaluate_profiles(root, manifest, observation, host)
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
    warm_launches = []
    requires_cold = settings["version"] < 3
    if requires_cold:
        audit = load_json(directory / "reboot-audit.json")
        for key in ("commit", "bundle_id"):
            _equal(audit.get(key), manifest[key], f"reboot audit {key}")
        if not 0 < audit["start_filetime"] <= audit["end_filetime"] or audit["collected_by_process_id"] <= 0:
            raise evidence.EvidenceError("Invalid reboot audit collection identity")
    for role, relative in roles.items():
        path = directory / "raw" / role
        decision = optional if role == "variant" else common
        initial = common["physical_provenance"]["environment"]
        processes = []
        previous_end = probe["end_filetime"]
        if requires_cold:
            startup_launch, startup = _validate_launch(path / "startup", manifest, decision,
                                                       relative, "startup", role, launch_ids, protocol)
            if not probe["end_filetime"] <= startup_launch["start_filetime"] < startup_launch["end_filetime"] <= audit["start_filetime"]:
                raise evidence.EvidenceError(f"Invalid reboot evidence timing for {role}")
            preparation = nw11_cold_start.qualify(audit["observation"], startup_launch, startup,
                                                  audit["start_filetime"], audit["end_filetime"])
            startup_conditions = operating_conditions(path / "startup", startup_launch, startup,
                                                       host, observation["adapter"], initial)
            previous_end = audit["end_filetime"]
        elif (path / "startup").exists():
            raise evidence.EvidenceError("Unscheduled startup launch in functional experiment")
        for index in range(1, protocol["runs"] + 1):
            folder = path / f"run-{index}"
            launch, report = _validate_launch(folder, manifest, decision, relative, "warm", role, launch_ids, protocol)
            if launch["start_filetime"] < previous_end:
                raise evidence.EvidenceError("Warm run started before the preceding process exited")
            previous_end = launch["end_filetime"]
            warm_launches.append((role, index, launch))
            processes.append({
                **_warm_run(folder, launch, report, protocol),
                "operating_conditions": operating_conditions(folder, launch, report,
                                                              host, observation["adapter"], initial),
            })
        captures[role] = _capture(folder, report)
        operating = [run["operating_conditions"] for run in processes]
        startup_metrics = {}
        peaks = [run["peak_engine_committed_gpu_bytes"] for run in processes]
        if requires_cold:
            startup_ms = startup["measurement"]["process_start_to_first_present_ms"]
            cold = preparation["status"] == "cold_driver_cache" and startup_conditions["status"] == "matched"
            operating.insert(0, startup_conditions)
            peaks.append(startup["measurement"]["peak_engine_committed_gpu_bytes"])
            startup_metrics = {
                "cold_initialization_ms": startup_ms if cold else None,
                "unqualified_startup_ms": None if cold else startup_ms,
                "cache_preparation": preparation,
            }
        metrics = {
            "process_runs": processes,
            **startup_metrics,
            "operating_conditions": operating,
            "operating_conditions_matched": all(item["status"] == "matched" for item in operating),
            "peak_engine_committed_gpu_bytes": max(peaks),
            **{key: report["measurement"][key] for key in ("shader_count", "shader_ids", "distribution_bytes")},
        }
        runs[role] = {**{key: report[key] for key in ("role", "device_tier", "device_key", "environment_key",
                                                      "status", "artifact_path", "adapter", "capability_decision")},
                      "measurement": metrics}
    validate_warm_run_order(settings, roles, warm_launches)
    if "variant" in roles:
        baseline_runs = runs["baseline"]["measurement"]["process_runs"]
        variant_runs = runs["variant"]["measurement"]["process_runs"]
        for index, (left, right) in enumerate(zip(baseline_runs, variant_runs), 1):
            reasons = matched_start_conditions(settings, left["operating_conditions"],
                                               right["operating_conditions"])
            if reasons:
                for run in (left, right):
                    conditions = run["operating_conditions"]
                    conditions["status"] = "pending"
                    conditions["reasons"] = sorted(set(conditions["reasons"] + [
                        f"matched run {index}: {reason}" for reason in reasons]))
                for role in ("baseline", "variant"):
                    runs[role]["measurement"]["operating_conditions_matched"] = False
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
    inputs[host_path.name] = contract.sha256_file(host_path)
    for name in ("common-eligibility.json", "eligibility.json", "probe-execution.json"):
        inputs[name] = contract.sha256_file(directory / name)
    if requires_cold:
        name = "reboot-audit.json"
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
