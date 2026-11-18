#!/usr/bin/env python3
"""Publish privacy-safe NW-10 physical evidence into tracked summaries.

This tool is deliberately publication-only. Capture tools keep raw files under
build/ or in transfer bundles; this publisher validates their identities,
removes raw/local-path material, writes tracked summaries, and advances the
NW-10 evidence state machine only when all required inputs are verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = Path("benchmarks/nw10/evidence-status.v1.json")
CORRECTNESS_PATH = Path("benchmarks/nw10/common-artifact-correctness.v1.json")
SYSTEM_PATH = Path("benchmarks/nw10/system-validation.v1.json")
INVALIDATED_GRADIENT_SHA256 = (
    "76c13bcc21da3a17b10b912f5e8ff4270f64749c74b347f174b519cbdd8c96bb"
)
STARTUP_CEILING_MS = 2000.0
MEMORY_CEILING_BYTES = 536_870_912


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _json_strings(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
    elif isinstance(value, str):
        yield value


def _assert_privacy(value: Mapping[str, Any], forbid_tokens: list[str]) -> None:
    hits: list[str] = []
    tokens = [token for token in forbid_tokens if token]
    for text in _json_strings(value):
        lower = text.casefold()
        if re.search(r"(?i)^[a-z]:\\users\\", text) or lower.startswith("/home/"):
            hits.append(text)
            continue
        if any(token.casefold() in lower for token in tokens):
            hits.append(text)
    if hits:
        raise ValueError(
            "privacy audit found forbidden public strings: "
            + "; ".join(sorted(set(hits)))
        )


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("unable to resolve current git HEAD")
    value = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("current git HEAD is malformed")
    return value


def _artifact_from_intro_report(report: Mapping[str, Any]) -> dict[str, Any]:
    artifact = report.get("canonical_artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("intro report has no canonical_artifact")
    required = (
        "executable_sha256",
        "executable_bytes",
        "package_sha256",
        "manifest_sha256",
    )
    missing = [name for name in required if artifact.get(name) in (None, "")]
    if missing:
        raise ValueError("intro canonical artifact is incomplete: " + ", ".join(missing))
    result = {
        "executable_sha256": str(artifact["executable_sha256"]).lower(),
        "executable_bytes": int(artifact["executable_bytes"]),
        "package_sha256": str(artifact["package_sha256"]).lower(),
        "manifest_sha256": str(artifact["manifest_sha256"]).lower(),
    }
    for name in ("executable_sha256", "package_sha256", "manifest_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", result[name]):
            raise ValueError(f"intro artifact {name} is malformed")
    if result["executable_sha256"] == INVALIDATED_GRADIENT_SHA256:
        raise ValueError("refusing to publish evidence for invalidated gradient artifact")
    if result["executable_bytes"] <= 0:
        raise ValueError("intro executable size is invalid")
    return result


def _load_clean_bundle(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    bundle_sha = _sha256_bytes(raw)
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        if "bundle-manifest.json" not in names:
            raise ValueError("clean-system result bundle has no bundle-manifest.json")
        manifest = json.loads(archive.read("bundle-manifest.json").decode("utf-8-sig"))
        if manifest.get("format") != "nw10-clean-system-result-bundle":
            raise ValueError("unexpected clean-system result bundle format")
        if int(manifest.get("version", -1)) != 1:
            raise ValueError("unexpected clean-system result bundle version")
        rows = manifest.get("files")
        if not isinstance(rows, list):
            raise ValueError("clean-system bundle manifest files are absent")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("clean-system bundle manifest row is invalid")
            rel = PurePosixPath(str(row.get("path", "")))
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError("clean-system bundle contains unsafe path")
            name = rel.as_posix()
            if name not in names:
                raise ValueError(f"clean-system bundle file is missing: {name}")
            data = archive.read(name)
            if len(data) != int(row.get("bytes", -1)):
                raise ValueError(f"clean-system bundle size mismatch: {name}")
            if _sha256_bytes(data) != str(row.get("sha256", "")).lower():
                raise ValueError(f"clean-system bundle hash mismatch: {name}")
        if "clean-system-report.json" not in names:
            raise ValueError("clean-system result bundle has no report")
        report = json.loads(
            archive.read("clean-system-report.json").decode("utf-8-sig")
        )
    if not isinstance(report, dict):
        raise ValueError("clean-system report is not a JSON object")
    return report, bundle_sha


def _clean_summary(
    report: Mapping[str, Any],
    *,
    bundle_sha256: str,
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    if report.get("verification_status") != "verified":
        raise ValueError("clean-system result is not verified")
    host = report.get("host")
    deployment = report.get("deployment")
    scenarios = report.get("scenarios")
    if not isinstance(host, Mapping) or not isinstance(deployment, Mapping):
        raise ValueError("clean-system report host/deployment is missing")
    if not isinstance(scenarios, list) or len(scenarios) != 6:
        raise ValueError("clean-system report does not contain six scenarios")
    if host.get("execution_kind") != "physical":
        raise ValueError("clean-system evidence is not physical")
    if host.get("clean_environment") is not True:
        raise ValueError("clean-system host is not clean")
    if host.get("development_dependencies_present_on_path") not in ([], tuple()):
        raise ValueError("clean-system host exposes development dependencies")
    if host.get("environment_cache_variables") not in ([], tuple()):
        raise ValueError("clean-system host exposes cache environment variables")
    if deployment.get("only_runtime_executable") is not True:
        raise ValueError("clean-system deployment is not a single executable")
    if str(deployment.get("executable_sha256", "")).lower() != artifact["executable_sha256"]:
        raise ValueError("clean-system executable does not match final intro artifact")
    if int(deployment.get("executable_bytes", -1)) != artifact["executable_bytes"]:
        raise ValueError("clean-system executable size does not match final intro artifact")
    rows: list[dict[str, Any]] = []
    for row in scenarios:
        if not isinstance(row, Mapping):
            raise ValueError("clean-system scenario row is invalid")
        if row.get("status") != "passed" or row.get("timed_out") is not False:
            raise ValueError(f"clean-system scenario failed: {row.get('scenario')}")
        if row.get("observed_exit_code") != row.get("expected_exit_code"):
            raise ValueError(
                f"clean-system scenario exit mismatch: {row.get('scenario')}"
            )
        rows.append(
            {
                "scenario": row.get("scenario"),
                "expected_exit_code": int(row.get("expected_exit_code")),
                "observed_exit_code": int(row.get("observed_exit_code")),
                "status": "passed",
                "timed_out": False,
            }
        )
    probe = host.get("virtualization_probe")
    if not isinstance(probe, Mapping):
        raise ValueError("clean-system virtualization probe is absent")
    hardware = report.get("artifact", {}).get("hardware")
    if not isinstance(hardware, Mapping):
        # The verified report records adapter identity in scenario/runtime output on
        # older captures. New transferred captures may retain it directly on host.
        hardware = host.get("hardware")
    if not isinstance(hardware, Mapping):
        hardware = {}
    return {
        "status": "verified",
        "execution_kind": "physical",
        "captured_at_utc": str(report.get("captured_at_utc", "")),
        "source_result_bundle_sha256": bundle_sha256,
        "host": {
            "manufacturer": probe.get("manufacturer"),
            "model": probe.get("model"),
            "architecture": host.get("architecture"),
            "windows_build": host.get("windows_build"),
            "hypervisor_present": probe.get("hypervisor_present"),
            "virtualization_based_security_status": probe.get(
                "virtualization_based_security_status"
            ),
            "virtualization_indicators": host.get("virtualization_indicators", []),
            "clean_environment": True,
        },
        "hardware": {
            "adapter_description": hardware.get("adapter_description", "unavailable"),
            "adapter_generation": hardware.get("adapter_generation", "unavailable"),
            "vendor_id": hardware.get("vendor_id", 0),
            "device_id": hardware.get("device_id", 0),
            "driver_version": hardware.get("driver_version", "unavailable"),
        },
        "development_dependencies_present_on_path": [],
        "environment_cache_variables": [],
        "deployment": {
            "only_runtime_executable": True,
            "sidecars": list(deployment.get("sidecars") or []),
            "executable_sha256": artifact["executable_sha256"],
            "executable_bytes": artifact["executable_bytes"],
            "forbidden_runtime_dependencies": list(
                deployment.get("forbidden_runtime_dependencies") or []
            ),
        },
        "scenarios": rows,
    }


def _refresh_status(status: dict[str, Any]) -> dict[str, Any]:
    perf = (
        isinstance(status.get("performance_preservation"), Mapping)
        and status["performance_preservation"].get("status") == "verified_preserved"
    )
    correctness = (
        isinstance(status.get("correctness_evidence"), Mapping)
        and status["correctness_evidence"].get("status") == "verified"
    )
    system = (
        isinstance(status.get("system_validation"), Mapping)
        and status["system_validation"].get("status") == "verified"
    )
    compact = (
        isinstance(status.get("compact_feasibility"), Mapping)
        and status["compact_feasibility"].get("status") == "verified_windows_runtime"
    )
    pe = (
        isinstance(status.get("pe_ledger"), Mapping)
        and status["pe_ledger"].get("status") == "verified"
    )

    status["reference_capture"] = (
        "verified_reference_capture" if perf else "pending_reference_capture"
    )
    status["frame_time"] = "verified_preserved" if perf else "pending_unverified"
    status["ada_correctness"] = "verified" if correctness else "pending_unverified"
    status["blackwell_correctness"] = "verified" if correctness else "pending_unverified"
    status["clean_system"] = "verified" if system else "pending_unverified"
    status["startup"] = "verified" if system else "pending_unverified"
    status["memory"] = "verified" if system else "pending_unverified"

    if perf and correctness and system:
        status["physical_evidence"] = "performance_correctness_and_system_verified"
    elif perf and correctness:
        status["physical_evidence"] = "performance_and_correctness_verified"
    elif perf:
        status["physical_evidence"] = "performance_verified"
    else:
        status["physical_evidence"] = "pending"

    if perf and correctness and system and compact and pe:
        status["evidence_status"] = "verified_complete"
    elif perf and correctness and system:
        status["evidence_status"] = (
            "performance_correctness_and_system_verified_packaging_pending"
        )
    elif perf and correctness:
        status["evidence_status"] = "performance_and_correctness_verified_system_pending"
    elif perf:
        status["evidence_status"] = "performance_verified_correctness_pending"
    else:
        status["evidence_status"] = "pending_physical_windows_capture"
    return status


def publish_correctness(args: argparse.Namespace) -> dict[str, Any]:
    source = _load(Path(args.comparison))
    if source.get("format") != "nw10-common-artifact-correctness":
        raise ValueError("unexpected correctness comparison format")
    if source.get("status") != "verified":
        raise ValueError("correctness comparison is not verified")
    privacy = source.get("privacy")
    if not isinstance(privacy, Mapping):
        raise ValueError("correctness comparison has no privacy declaration")
    if privacy.get("raw_capture_bytes_embedded") is not False:
        raise ValueError("correctness comparison embeds raw capture bytes")
    if privacy.get("local_paths_included") is not False:
        raise ValueError("correctness comparison exposes local paths")

    common = source.get("common_artifact")
    ada = source.get("ada")
    blackwell = source.get("blackwell")
    comparison = source.get("comparison")
    if not all(isinstance(item, Mapping) for item in (common, ada, blackwell, comparison)):
        raise ValueError("correctness comparison structure is incomplete")
    per_tick = comparison.get("per_tick")
    if not isinstance(per_tick, list) or not per_tick:
        raise ValueError("correctness comparison has no per-tick rows")
    if any(row.get("status") != "passed" for row in per_tick if isinstance(row, Mapping)):
        raise ValueError("correctness comparison contains failed fixed ticks")
    ada_loop = ada.get("loop_boundary")
    blackwell_loop = blackwell.get("loop_boundary")
    if not isinstance(ada_loop, Mapping) or not isinstance(blackwell_loop, Mapping):
        raise ValueError("correctness comparison has no loop-boundary results")
    if ada_loop.get("status") != "passed" or blackwell_loop.get("status") != "passed":
        raise ValueError("correctness loop-boundary check failed")

    def hardware_summary(row: Mapping[str, Any], tier: str) -> dict[str, Any]:
        hardware = row.get("hardware")
        if not isinstance(hardware, Mapping):
            raise ValueError(f"{tier} correctness hardware is missing")
        result = {
            "adapter_description": hardware.get("adapter_description"),
            "classified_tier": tier,
            "device_form_factor": hardware.get("device_form_factor", "desktop"),
        }
        if hardware.get("driver_version"):
            result["driver_version"] = hardware.get("driver_version")
        return result

    public = {
        "format": "nw10-common-artifact-correctness-summary",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "capture_protocol": source.get("capture_protocol"),
        "common_artifact": {
            "package_sha256": str(common.get("package_sha256", "")).lower()
        },
        "ada": {"status": "verified", "hardware": hardware_summary(ada, "ada")},
        "blackwell": {
            "status": "verified",
            "hardware": hardware_summary(blackwell, "blackwell"),
        },
        "comparison": {
            "fixed_ticks": [int(row["tick"]) for row in per_tick],
            "fixed_tick_status": "passed",
            "loop_boundary_ticks": [0, 960000],
            "ada_loop_boundary_status": "passed",
            "blackwell_loop_boundary_status": "passed",
            "worst_absolute_error": float(comparison.get("worst_absolute_error", 0.0)),
            "worst_relative_error": float(
                comparison.get("worst_relative_error", 0.0)
            ),
            "absolute_tolerance": float(comparison.get("absolute_tolerance")),
            "relative_tolerance": float(comparison.get("relative_tolerance")),
            "absolute_required": bool(comparison.get("absolute_required")),
        },
        "privacy": {
            "raw_capture_bytes_embedded": False,
            "local_paths_included": False,
            "stdout_stderr_included": False,
        },
        "source": {
            "mode": "compare",
            "execution_kind": "physical_cross_hardware",
            "tool": "tools/nw10/run-correctness-evidence.ps1",
            "implementation_head": args.implementation_head or _git_head(),
            "raw_capture_manifests_tracked": False,
        },
    }
    package_sha = public["common_artifact"]["package_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", package_sha):
        raise ValueError("correctness package SHA-256 is malformed")
    _assert_privacy(public, list(args.forbid_token or []))

    destination = REPO_ROOT / args.destination
    _write(destination, public)
    status_path = REPO_ROOT / args.status_file
    status = _load(status_path)
    status["correctness_evidence"] = {
        "status": "verified",
        "path": args.destination.replace("\\", "/"),
        "sha256": _text_sha256(destination),
        "package_sha256": package_sha,
        "worst_absolute_error": public["comparison"]["worst_absolute_error"],
        "worst_relative_error": public["comparison"]["worst_relative_error"],
    }
    _refresh_status(status)
    _assert_privacy(status, list(args.forbid_token or []))
    _write(status_path, status)
    return public


def publish_system(args: argparse.Namespace) -> dict[str, Any]:
    intro = _load(Path(args.intro_report))
    artifact = _artifact_from_intro_report(intro)
    clean_report, clean_bundle_sha = _load_clean_bundle(
        Path(args.clean_system_bundle)
    )
    clean = _clean_summary(
        clean_report, bundle_sha256=clean_bundle_sha, artifact=artifact
    )
    metrics = _load(Path(args.metrics))
    metadata = metrics.get("metadata")
    measurements = metrics.get("measurements")
    if not isinstance(metadata, Mapping) or not isinstance(measurements, Mapping):
        raise ValueError("metrics JSON requires metadata and measurements objects")
    startup_ms = float(measurements.get("process_start_to_first_present_ms"))
    peak_gpu = int(measurements.get("peak_engine_committed_gpu_bytes"))
    if startup_ms < 0 or startup_ms > STARTUP_CEILING_MS:
        raise ValueError("startup measurement exceeds the NW-10 ceiling")
    if peak_gpu < 0 or peak_gpu > MEMORY_CEILING_BYTES:
        raise ValueError("GPU memory measurement exceeds the NW-10 ceiling")
    post_init = measurements.get("post_initialization_allocation_violation")
    if post_init is None:
        raise ValueError(
            "metrics JSON must retain post_initialization_allocation_violation"
        )
    if int(post_init) != 0:
        raise ValueError("post-initialization allocation violation is nonzero")
    cache_kind = str(measurements.get("cache_kind", ""))
    if cache_kind not in {"cold_driver_cache", "warm"}:
        raise ValueError("metrics JSON has invalid cache_kind")
    adapter = str(metadata.get("adapter_description", ""))
    driver = str(metadata.get("driver_version", ""))
    if not adapter or not driver:
        raise ValueError("metrics JSON has incomplete hardware identity")

    startup = {
        "status": "verified",
        "protocol": "nw07-startup.v1",
        "execution_kind": "physical",
        "hardware": {
            "adapter_description": adapter,
            "driver_version": driver,
        },
        "cache_state": {
            "kind": cache_kind,
            "application_pipeline_cache_used": False,
        },
        "warm_preparation": str(measurements.get("warm_preparation", "")),
        "cold_driver_cache_preparation": str(
            measurements.get("cold_driver_cache_preparation", "")
        ),
        "measurement_method": str(
            measurements.get(
                "startup_measurement_method",
                "fresh physical metrics collector bound to the exact NW-10 artifact",
            )
        ),
        "process_start_to_first_present_ms": startup_ms,
        "ceiling_ms": STARTUP_CEILING_MS,
    }
    for key in ("lower_bound_ms", "process_creation_window_ms"):
        if measurements.get(key) is not None:
            startup[key] = float(measurements[key])

    memory = {
        "status": "verified",
        "execution_kind": "physical",
        "hardware": {
            "adapter_description": adapter,
            "driver_version": driver,
        },
        "package_sha256": artifact["package_sha256"],
        "peak_engine_committed_gpu_bytes": peak_gpu,
        "ceiling_bytes": MEMORY_CEILING_BYTES,
        "post_initialization_allocation_violation": 0,
        "measurement_source": str(measurements.get("memory_source", "")),
        "resolution": {"width": 1920, "height": 1080},
    }
    optional_ints = {
        "engine_resource_bytes": "engine_resource_bytes",
        "engine_upload_bytes": "engine_upload_bytes",
        "engine_cumulative_committed_bytes": "engine_cumulative_committed_bytes",
        "frames": "frames",
    }
    for source_key, public_key in optional_ints.items():
        if measurements.get(source_key) is not None:
            memory[public_key] = int(measurements[source_key])
    if measurements.get("memory_accounting_scope"):
        memory["accounting_scope"] = str(measurements["memory_accounting_scope"])

    public = {
        "format": "nw10-system-validation",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "final_artifact": artifact,
        "startup": startup,
        "memory": memory,
        "clean_system": clean,
        "privacy": {
            "local_paths_included": False,
            "raw_stdout_stderr_included": False,
            "user_profile_paths_included": False,
            "raw_result_bundle_tracked": False,
        },
    }
    _assert_privacy(public, list(args.forbid_token or []))

    destination = REPO_ROOT / args.destination
    _write(destination, public)
    status_path = REPO_ROOT / args.status_file
    status = _load(status_path)
    status["system_validation"] = {
        "status": "verified",
        "path": args.destination.replace("\\", "/"),
        "sha256": _text_sha256(destination),
        "final_executable_sha256": artifact["executable_sha256"],
        "package_sha256": artifact["package_sha256"],
    }
    _refresh_status(status)
    _assert_privacy(status, list(args.forbid_token or []))
    _write(status_path, status)
    return public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    correctness = sub.add_parser("correctness")
    correctness.add_argument("--comparison", required=True)
    correctness.add_argument(
        "--destination",
        default=CORRECTNESS_PATH.as_posix(),
    )
    correctness.add_argument("--status-file", default=STATUS_PATH.as_posix())
    correctness.add_argument("--implementation-head", default="")
    correctness.add_argument("--forbid-token", action="append", default=[])

    system = sub.add_parser("system")
    system.add_argument("--clean-system-bundle", required=True)
    system.add_argument("--metrics", required=True)
    system.add_argument("--intro-report", required=True)
    system.add_argument("--destination", default=SYSTEM_PATH.as_posix())
    system.add_argument("--status-file", default=STATUS_PATH.as_posix())
    system.add_argument("--forbid-token", action="append", default=[])

    args = parser.parse_args(argv)
    try:
        if args.command == "correctness":
            public = publish_correctness(args)
        else:
            public = publish_system(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "format": "nw10-physical-evidence-publisher",
                    "status": "failed",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": public.get("status"),
                "format": public.get("format"),
                "destination": args.destination,
                "status_file": args.status_file,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
