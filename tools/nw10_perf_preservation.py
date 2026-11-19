#!/usr/bin/env python3
"""Collect and validate matched NW-10 before/after performance preservation.

The frozen nw07_perf_ref_v1 capture remains the immutable reference.  The
"after" artifact is allowed to have a different artifact identity only inside
this NW-10 preservation comparison.  Host, board, OS/driver, toolchain,
quality, cache, measurement tools and run protocol must remain matched.

Public output is whitelist-based and intentionally omits raw samples, local
paths, PIDs and process/collector stdout/stderr.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_capture  # noqa: E402
import nw07_contract  # noqa: E402
import nw07_evidence  # noqa: E402
import nw10_contract  # noqa: E402


FORMAT = "nw10-performance-preservation"
VERSION = 1
RELATIVE_ALERT_PERCENT = 10.0
_ALLOWED_AFTER_VALIDATION_REASONS = {
    "capture does not reference the exact final artifact",
}
_WINDOWS_USER_PATH = re.compile(r"(?i)[A-Z]:\\Users\\[^\\\"\\s]+")
_UNIX_HOME_PATH = re.compile(r"(?i)/home/[^/\"\s]+")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def _capture_from_record(record: Mapping[str, Any], capture_id: str) -> dict[str, Any]:
    captures = record.get("captures")
    if not isinstance(captures, list):
        raise ValueError("evidence record has no captures list")
    matches = [
        item
        for item in captures
        if isinstance(item, Mapping) and item.get("capture_id") == capture_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one capture {capture_id!r}, found {len(matches)}"
        )
    return copy.deepcopy(dict(matches[0]))


def _run_summaries(capture: Mapping[str, Any]) -> list[dict[str, Any]]:
    validation = capture.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    runs = validation.get("runs")
    if not isinstance(runs, list):
        return []
    result: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        gpu = run.get("gpu_frame_time_ms")
        cpu = run.get("cpu_submit_ms")
        displayed = run.get("displayed_frame_interval_ms")
        result.append(
            {
                "run_index": run.get("run_index"),
                "status": run.get("status"),
                "duration_seconds": (
                    (run.get("timing") or {}).get("duration_seconds")
                    if isinstance(run.get("timing"), Mapping)
                    else None
                ),
                "sample_density_hz": run.get("sample_density_hz"),
                "gpu_p99_ms": gpu.get("p99_ms") if isinstance(gpu, Mapping) else None,
                "gpu_sample_count": (
                    gpu.get("sample_count") if isinstance(gpu, Mapping) else None
                ),
                "cpu_p99_ms": cpu.get("p99_ms") if isinstance(cpu, Mapping) else None,
                "cpu_sample_count": (
                    cpu.get("sample_count") if isinstance(cpu, Mapping) else None
                ),
                "displayed_p99_ms": (
                    displayed.get("p99_ms")
                    if isinstance(displayed, Mapping)
                    else None
                ),
                "displayed_sample_count": (
                    displayed.get("sample_count")
                    if isinstance(displayed, Mapping)
                    else None
                ),
            }
        )
    return result


def _worst(runs: list[dict[str, Any]], key: str) -> float:
    values = [float(run[key]) for run in runs if run.get(key) is not None]
    if len(values) != nw07_contract.PERF_RUN_COUNT:
        raise ValueError(f"missing {key} values from one or more performance runs")
    return max(values)


def _technical_reference(reference: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section_name, fields in {
        "board": (
            "model", "vendor_id", "device_id", "subsystem_vendor_id",
            "subsystem_device_id", "vram_mib", "architecture",
        ),
        "host": (
            "cpu_model", "physical_cores", "logical_processors",
            "motherboard", "memory",
        ),
        "environment": (
            "os", "execution_kind", "os_build", "driver_version",
            "wddm_driver_version", "windows_power_plan",
            "nvidia_power_management_mode", "pcie_power_management",
            "gpu_clocks", "temperature_c",
        ),
        "toolchain": (
            "toolchain_identity", "toolchain_manifest_sha256", "dxc_version",
            "msvc_version", "windows_sdk",
        ),
        "quality": (
            "profile_id", "resolution", "vsync", "presentation_mode",
            "msaa_samples", "filtering", "anisotropy", "texture_format",
            "intermediate_format", "bloom_resolution_scale",
            "tonemap_operator", "tone_exposure", "tone_gamma",
            "feature_level", "shader_model",
        ),
        "cache": (
            "clean_start_application_pipeline_cache", "accepted_states",
            "cold_preparation", "warm_preparation",
        ),
    }.items():
        section = reference.get(section_name)
        if not isinstance(section, Mapping):
            raise ValueError(f"reference section {section_name!r} is missing")
        result[section_name] = {
            field: copy.deepcopy(section.get(field)) for field in fields
        }
    return result


def _artifact_summary(capture: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_key": capture.get("artifact_key"),
        "package_sha256": capture.get("package_sha256"),
        "manifest_sha256": capture.get("manifest_sha256"),
        "executable_sha256": capture.get("executable_sha256"),
        "complete_distribution_bytes": capture.get("executable_bytes"),
    }


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _scan_public_payload(value: Mapping[str, Any], forbid_tokens: list[str]) -> None:
    strings = list(_walk_strings(value))
    for item in strings:
        if _WINDOWS_USER_PATH.search(item) or _UNIX_HOME_PATH.search(item):
            raise ValueError("public evidence contains a local user-profile path")
    folded = "\n".join(strings).casefold()
    for token in forbid_tokens:
        token = token.strip()
        if token and token.casefold() in folded:
            raise ValueError(
                f"public evidence contains forbidden privacy token {token!r}"
            )


def validate_preservation(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    perf_contract: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    *,
    forbid_tokens: list[str] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    contract_validation = nw07_evidence.validate_perf_contract(perf_contract)
    if contract_validation.get("status") != "frozen":
        reasons.append("nw07_perf_ref_v1 is not a fully bound frozen contract")

    binding_id = perf_contract.get("binding_id")
    binding_digest = perf_contract.get("binding_digest")
    before_validation = before.get("validation")
    after_validation = after.get("validation")
    before_validation = (
        before_validation if isinstance(before_validation, Mapping) else {}
    )
    after_validation = after_validation if isinstance(after_validation, Mapping) else {}

    if before_validation.get("status") != "verified":
        reasons.append("before capture is not verified nw07_perf_ref_v1 evidence")
    for label, capture in (("before", before), ("after", after)):
        if capture.get("reference_binding_id") != binding_id:
            reasons.append(f"{label} capture names a different reference binding id")
        if capture.get("reference_binding_digest") != binding_digest:
            reasons.append(f"{label} capture names a different reference binding digest")
        if capture.get("contract_id") != nw10_contract.PERF_CONTRACT_ID:
            reasons.append(f"{label} capture does not name nw07_perf_ref_v1")
        if capture.get("profile_id") != nw07_contract.PROFILE_ID:
            reasons.append(f"{label} capture changed the common profile")
        if capture.get("requirements_digest") != nw07_contract.REQUIREMENTS_DIGEST:
            reasons.append(f"{label} capture changed the requirements digest")
        if capture.get("present_sync_interval") != 0:
            reasons.append(f"{label} capture is not uncapped")
        if capture.get("protocol") != before.get("protocol") and label == "after":
            reasons.append("before/after performance protocols do not match")
        if capture.get("tools") != before.get("tools") and label == "after":
            reasons.append("before/after measurement tools do not match")

    expected_reference = perf_contract.get("reference")
    if not isinstance(expected_reference, Mapping):
        reasons.append("bound performance contract has no reference object")
        expected_reference = {}
    for label, capture in (("before", before), ("after", after)):
        reference = capture.get("reference")
        if reference != expected_reference:
            reasons.append(f"{label} capture does not carry the exact frozen reference")

    before_environment = before.get("execution_environment")
    after_environment = after.get("execution_environment")
    if before_environment != after_environment:
        reasons.append("before/after execution-environment declarations differ")
    if not isinstance(after_environment, Mapping) or after_environment.get("kind") not in {
        "physical",
        "documented_gpu_passthrough",
    }:
        reasons.append("after capture is not physical or documented GPU passthrough")

    allowed_after_reasons = set(after_validation.get("reasons") or [])
    unexpected = sorted(allowed_after_reasons - _ALLOWED_AFTER_VALIDATION_REASONS)
    if unexpected:
        reasons.append(
            "after standard validation has non-artifact mismatches: "
            + "; ".join(unexpected)
        )
    if after_validation.get("status") not in {"verified", "unverified"}:
        reasons.append("after capture did not complete frozen run validation")
    after_runs = _run_summaries(after)
    before_runs = _run_summaries(before)
    if len(before_runs) != nw07_contract.PERF_RUN_COUNT:
        reasons.append("before capture does not contain three validated runs")
    if len(after_runs) != nw07_contract.PERF_RUN_COUNT:
        reasons.append("after capture does not contain three validated runs")
    if any(run.get("status") != "passed" for run in after_runs):
        reasons.append("one or more after performance runs failed")
    if any(run.get("status") != "passed" for run in before_runs):
        reasons.append("one or more before performance runs failed")

    candidate_artifact = candidate_report.get("artifact")
    candidate_artifact = (
        candidate_artifact if isinstance(candidate_artifact, Mapping) else {}
    )
    after_artifact = _artifact_summary(after)
    for key, value in candidate_artifact.items():
        if key in after_artifact and after_artifact.get(key) != value:
            reasons.append(f"after capture does not match candidate artifact field {key}")
    if candidate_report.get("comparison_role") != "after_common_artifact":
        reasons.append("candidate report is not the NW-10 after common artifact")
    if candidate_report.get("performance_contract") != nw10_contract.PERF_CONTRACT_ID:
        reasons.append("candidate report changed the performance contract")
    if candidate_report.get("profile_id") != nw07_contract.PROFILE_ID:
        reasons.append("candidate report changed the common profile")
    if before.get("package_sha256") != after.get("package_sha256"):
        reasons.append(
            "before/after canonical package hashes differ; common content is not matched"
        )

    try:
        before_gpu = _worst(before_runs, "gpu_p99_ms")
        before_cpu = _worst(before_runs, "cpu_p99_ms")
        after_gpu = _worst(after_runs, "gpu_p99_ms")
        after_cpu = _worst(after_runs, "cpu_p99_ms")
    except ValueError as exc:
        reasons.append(str(exc))
        before_gpu = before_cpu = after_gpu = after_cpu = float("nan")

    if after_gpu == after_gpu and not (
        after_gpu < nw07_contract.FRAME_TIME_THRESHOLD_MS
    ):
        reasons.append("after GPU p99 does not preserve the strict <16.67 ms gate")
    if after_cpu == after_cpu and not (
        after_cpu < nw07_contract.FRAME_TIME_THRESHOLD_MS
    ):
        reasons.append("after CPU-submit p99 does not preserve the strict <16.67 ms gate")

    def delta_percent(after_value: float, before_value: float) -> float | None:
        if before_value <= 0 or before_value != before_value or after_value != after_value:
            return None
        return (after_value - before_value) * 100.0 / before_value

    gpu_delta = delta_percent(after_gpu, before_gpu)
    cpu_delta = delta_percent(after_cpu, before_cpu)
    relative_alert = any(
        value is not None and value > RELATIVE_ALERT_PERCENT
        for value in (gpu_delta, cpu_delta)
    )

    public = {
        "format": FORMAT,
        "version": VERSION,
        "task": nw10_contract.TASK,
        "performance_contract": nw10_contract.PERF_CONTRACT_ID,
        "performance_protocol": nw10_contract.PERF_PROTOCOL_ID,
        "reference_binding_id": binding_id,
        "reference_binding_digest": binding_digest,
        "status": "failed" if reasons else "verified_preserved",
        "reasons": reasons,
        "matched_environment": not any(
            "environment" in reason or "reference" in reason or "tools" in reason
            for reason in reasons
        ),
        "reference_environment": _technical_reference(expected_reference),
        "before": {
            "capture_id": before.get("capture_id"),
            "artifact": _artifact_summary(before),
            "runs": before_runs,
            "worst_gpu_p99_ms": before_gpu,
            "worst_cpu_submit_p99_ms": before_cpu,
        },
        "after": {
            "capture_id": after.get("capture_id"),
            "artifact": after_artifact,
            "runs": after_runs,
            "worst_gpu_p99_ms": after_gpu,
            "worst_cpu_submit_p99_ms": after_cpu,
        },
        "comparison": {
            "same_canonical_package_sha256": (
                before.get("package_sha256") == after.get("package_sha256")
            ),
            "gpu_worst_p99_delta_percent": gpu_delta,
            "cpu_worst_p99_delta_percent": cpu_delta,
            "relative_alert_threshold_percent": RELATIVE_ALERT_PERCENT,
            "relative_regression_alert": relative_alert,
            "absolute_gate_operator": "<",
            "absolute_gate_ms": nw07_contract.FRAME_TIME_THRESHOLD_MS,
            "faster_substitute_used": False,
        },
        "privacy": {
            "raw_samples_included": False,
            "process_ids_included": False,
            "stdout_stderr_included": False,
            "local_paths_included": False,
            "whitelist_generated": True,
        },
    }
    _scan_public_payload(public, list(forbid_tokens or []))
    return public


def _canonical_text_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("unable to resolve a valid current git HEAD")
    return value


def _validate_public_for_publish(
    public: Mapping[str, Any],
    *,
    forbid_tokens: list[str] | None = None,
) -> None:
    _scan_public_payload(public, list(forbid_tokens or []))
    if public.get("format") != FORMAT or public.get("version") != VERSION:
        raise ValueError("public evidence has an unexpected format/version")
    if public.get("task") != nw10_contract.TASK:
        raise ValueError("public evidence is not NW-10")
    if public.get("performance_contract") != nw10_contract.PERF_CONTRACT_ID:
        raise ValueError("public evidence changed the performance contract")
    if public.get("performance_protocol") != nw10_contract.PERF_PROTOCOL_ID:
        raise ValueError("public evidence changed the performance protocol")
    if public.get("status") != "verified_preserved":
        raise ValueError("public evidence is not verified_preserved")
    if list(public.get("reasons") or []):
        raise ValueError("verified public evidence still contains failure reasons")
    if public.get("matched_environment") is not True:
        raise ValueError("public evidence is not a matched before/after environment")

    comparison = public.get("comparison")
    if not isinstance(comparison, Mapping):
        raise ValueError("public evidence has no comparison object")
    if comparison.get("same_canonical_package_sha256") is not True:
        raise ValueError("before/after canonical package identity is not preserved")
    if comparison.get("faster_substitute_used") is not False:
        raise ValueError("a faster substitute cannot certify preservation")
    if comparison.get("absolute_gate_operator") != "<":
        raise ValueError("performance gate operator is not strict '<'")
    if float(comparison.get("absolute_gate_ms", -1)) != float(
        nw07_contract.FRAME_TIME_THRESHOLD_MS
    ):
        raise ValueError("performance gate threshold changed")

    privacy = public.get("privacy")
    if not isinstance(privacy, Mapping):
        raise ValueError("public evidence has no privacy declaration")
    expected_privacy = {
        "raw_samples_included": False,
        "process_ids_included": False,
        "stdout_stderr_included": False,
        "local_paths_included": False,
        "whitelist_generated": True,
    }
    for key, expected in expected_privacy.items():
        if privacy.get(key) is not expected:
            raise ValueError(f"public evidence privacy flag {key!r} is invalid")

    for label in ("before", "after"):
        section = public.get(label)
        if not isinstance(section, Mapping):
            raise ValueError(f"public evidence is missing {label!r}")
        gpu = float(section.get("worst_gpu_p99_ms", float("inf")))
        cpu = float(section.get("worst_cpu_submit_p99_ms", float("inf")))
        if not gpu < nw07_contract.FRAME_TIME_THRESHOLD_MS:
            raise ValueError(f"{label} GPU p99 does not pass the frozen gate")
        if not cpu < nw07_contract.FRAME_TIME_THRESHOLD_MS:
            raise ValueError(f"{label} CPU-submit p99 does not pass the frozen gate")
        runs = section.get("runs")
        if not isinstance(runs, list) or len(runs) != nw07_contract.PERF_RUN_COUNT:
            raise ValueError(f"{label} evidence does not contain three run summaries")
        if any(
            not isinstance(run, Mapping) or run.get("status") != "passed"
            for run in runs
        ):
            raise ValueError(f"{label} evidence contains a non-passing run")


def _carry_compact_feasibility_file(
    status: dict[str, Any], files: list[dict[str, Any]]
) -> None:
    """Keep a preserved compact-feasibility component in the status ledger.

    Performance publication deliberately preserves an existing
    ``compact_feasibility`` entry, but the evidence-status validator requires
    every preserved component to have a matching ``files`` row.  Carry the
    entry's path forward (refreshing its digest from the retained record when
    it is available) so performance-only republication cannot invalidate a
    verified compact result.
    """
    entry = status.get("compact_feasibility")
    if not isinstance(entry, dict):
        return
    relative = entry.get("path")
    digest = entry.get("sha256")
    if not isinstance(relative, str) or not isinstance(digest, str):
        return
    normalized = relative.replace("\\", "/")
    if not normalized.startswith("benchmarks/nw10/"):
        return
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        return
    digest = digest.lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return
    candidate = REPO_ROOT / normalized
    try:
        if candidate.is_file():
            digest = _canonical_text_sha256(candidate)
            entry["sha256"] = digest
    except OSError:
        # A missing file is left to the evidence validator; do not fabricate
        # a digest for a record that is not present in this checkout.
        pass
    files[:] = [
        item
        for item in files
        if not (
            isinstance(item, Mapping)
            and str(item.get("path", "")).replace("\\", "/") == normalized
        )
    ]
    files.append({"path": normalized, "sha256": digest})


def publish_public_evidence(
    source: Path,
    destination: Path,
    status_path: Path,
    *,
    forbid_tokens: list[str] | None = None,
    implementation_head: str | None = None,
) -> dict[str, Any]:
    public = _load_json(source)
    _validate_public_for_publish(public, forbid_tokens=list(forbid_tokens or []))

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(public, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    status = _load_json(status_path)
    if status.get("format") != "nw10-evidence-status":
        raise ValueError("NW-10 evidence-status file has an unexpected format")
    head = (implementation_head or _git_head()).strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise ValueError("implementation head must be a 40-character lowercase hex SHA")
    status["source_head_sha"] = head
    current_evidence_status = str(status.get("evidence_status", ""))
    if current_evidence_status in {
        "",
        "pending_physical_windows_capture",
        "performance_verified_correctness_pending",
    }:
        compact = status.get("compact_feasibility")
        compact_verified = (
            isinstance(compact, Mapping)
            and compact.get("status") == "verified_windows_runtime"
        )
        status["evidence_status"] = (
            "performance_and_compact_verified_correctness_pending"
            if compact_verified
            else "performance_verified_correctness_pending"
        )
    # A later evidence stage may have advanced the aggregate state beyond
    # performance/compact. Re-running performance publication must never
    # downgrade that progress.
    status["physical_evidence"] = "performance_verified"
    status["reference_capture"] = "verified_reference_capture"
    status["frame_time"] = "verified_preserved"
    status["performance_preservation"] = {
        "status": "verified_preserved",
        "path": destination.relative_to(REPO_ROOT).as_posix(),
        "sha256": _canonical_text_sha256(destination),
        "reference_binding_id": public.get("reference_binding_id"),
    }
    files = status.setdefault("files", [])
    if not isinstance(files, list):
        raise ValueError("NW-10 evidence-status files field is not a list")
    destination_rel = destination.relative_to(REPO_ROOT).as_posix()
    files[:] = [
        item
        for item in files
        if not (
            isinstance(item, Mapping)
            and item.get("path") == destination_rel
        )
    ]
    files.append(
        {
            "path": destination_rel,
            "sha256": _canonical_text_sha256(destination),
        }
    )
    _carry_compact_feasibility_file(status, files)
    files.sort(key=lambda item: str(item.get("path", "")) if isinstance(item, Mapping) else "")
    status_path.write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return status


def collect_after(args: argparse.Namespace) -> dict[str, Any]:
    before_record = _load_json(Path(args.before_evidence))
    before = _capture_from_record(before_record, args.before_capture_id)
    perf_contract = _load_json(Path(args.contract))
    candidate_report = _load_json(Path(args.candidate_report))

    namespace = argparse.Namespace(
        package=args.package,
        executable=args.executable,
        contract=args.contract,
        samples_dir=args.samples_dir,
        reference=args.reference,
        collector_command=args.collector_command,
        player_args=args.player_args,
        capture_id=args.capture_id,
        execution_kind=args.execution_kind,
        passthrough_documentation=args.passthrough_documentation,
        evidence=args.raw_evidence,
    )
    after = nw07_capture.collect_perf(namespace)
    raw_record = {
        "format": "nw10-performance-preservation-raw",
        "version": 1,
        "captures": [after],
    }
    raw_path = Path(args.raw_evidence)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(raw_record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    public = validate_preservation(
        before,
        after,
        perf_contract,
        candidate_report,
        forbid_tokens=list(args.forbid_token or []),
    )
    public_path = Path(args.public_evidence)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(
        json.dumps(public, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect-after")
    collect.add_argument("--package", required=True)
    collect.add_argument("--executable", required=True)
    collect.add_argument("--contract", required=True)
    collect.add_argument("--reference", required=True)
    collect.add_argument("--before-evidence", required=True)
    collect.add_argument("--before-capture-id", required=True)
    collect.add_argument("--candidate-report", required=True)
    collect.add_argument("--samples-dir", required=True)
    collect.add_argument("--collector-command", required=True)
    collect.add_argument("--player-args", default="--frames 0")
    collect.add_argument("--capture-id", required=True)
    collect.add_argument("--execution-kind", choices=("physical", "documented_gpu_passthrough"), default="physical")
    collect.add_argument("--passthrough-documentation", default="")
    collect.add_argument("--raw-evidence", required=True)
    collect.add_argument("--public-evidence", required=True)
    collect.add_argument("--forbid-token", action="append", default=[])

    publish = sub.add_parser("publish")
    publish.add_argument("--public-evidence", required=True)
    publish.add_argument(
        "--destination",
        default=str(
            REPO_ROOT
            / "benchmarks"
            / "nw10"
            / "performance-preservation-rtx4060ti.v1.json"
        ),
    )
    publish.add_argument(
        "--status-file",
        default=str(REPO_ROOT / "benchmarks" / "nw10" / "evidence-status.v1.json"),
    )
    publish.add_argument("--forbid-token", action="append", default=[])
    publish.add_argument("--implementation-head")

    compare = sub.add_parser("compare")
    compare.add_argument("--before-evidence", required=True)
    compare.add_argument("--before-capture-id", required=True)
    compare.add_argument("--after-evidence", required=True)
    compare.add_argument("--after-capture-id", required=True)
    compare.add_argument("--contract", required=True)
    compare.add_argument("--candidate-report", required=True)
    compare.add_argument("--output", required=True)
    compare.add_argument("--forbid-token", action="append", default=[])

    args = parser.parse_args(argv)
    try:
        if args.command == "collect-after":
            public = collect_after(args)
        elif args.command == "publish":
            status = publish_public_evidence(
                Path(args.public_evidence),
                Path(args.destination),
                Path(args.status_file),
                forbid_tokens=list(args.forbid_token or []),
                implementation_head=args.implementation_head,
            )
            print(
                json.dumps(
                    {
                        "status": "published",
                        "destination": args.destination,
                        "evidence_status": status.get("evidence_status"),
                        "reference_capture": status.get("reference_capture"),
                        "frame_time": status.get("frame_time"),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        else:
            before = _capture_from_record(
                _load_json(Path(args.before_evidence)), args.before_capture_id
            )
            after = _capture_from_record(
                _load_json(Path(args.after_evidence)), args.after_capture_id
            )
            public = validate_preservation(
                before,
                after,
                _load_json(Path(args.contract)),
                _load_json(Path(args.candidate_report)),
                forbid_tokens=list(args.forbid_token or []),
            )
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(public, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except Exception as exc:
        print(
            json.dumps(
                {"format": FORMAT, "status": "failed", "error": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": public["status"],
                "matched_environment": public["matched_environment"],
                "before": {
                    "gpu": public["before"]["worst_gpu_p99_ms"],
                    "cpu": public["before"]["worst_cpu_submit_p99_ms"],
                },
                "after": {
                    "gpu": public["after"]["worst_gpu_p99_ms"],
                    "cpu": public["after"]["worst_cpu_submit_p99_ms"],
                },
                "relative_regression_alert": public["comparison"][
                    "relative_regression_alert"
                ],
                "public_evidence": (
                    args.public_evidence
                    if args.command == "collect-after"
                    else args.output
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if public["status"] == "verified_preserved" else 2


if __name__ == "__main__":
    raise SystemExit(main())
