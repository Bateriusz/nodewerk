#!/usr/bin/env python3
"""Collect launcher-produced raw run reports into NW-11 evidence JSON.

The launcher, not an operator, produces each raw report by executing the
matched baseline/variant binaries under the frozen protocol.  This collector
only validates the shapes, binds them to the verified bundle identity, and
preserves every raw sample and capability snapshot.  It never accepts a
manually specified hash, capability, or aggregate value.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent))

import nw11_bundle as bundle  # noqa: E402
import nw11_contract as contract  # noqa: E402
import nw11_evidence as evidence  # noqa: E402


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise evidence.EvidenceError(f"cannot read raw run report '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise evidence.EvidenceError(f"raw run report '{path}' is not a JSON object")
    return value


def _merge_role_reports(role: str, reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        raise evidence.EvidenceError(f"no raw run reports for role '{role}'")
    identity_keys = ("device_tier", "device_key", "environment_key", "status", "artifact_path")
    first = reports[0]
    for key in identity_keys:
        if any(report.get(key) != first.get(key) for report in reports[1:]):
            raise evidence.EvidenceError(
                f"matched {role} runs disagree on '{key}'; observations are not comparable"
            )
    capability_decisions = [report.get("capability_decision") for report in reports]
    if any(decision != capability_decisions[0] for decision in capability_decisions[1:]):
        raise evidence.EvidenceError(
            f"matched {role} runs used different capability snapshots"
        )
    merged_measurement: dict[str, Any] = {}
    for metric in ("gpu_time_samples", "cpu_submit_samples", "frame_time_samples"):
        samples: list[Any] = []
        for report in reports:
            measurement = report.get("measurement")
            if not isinstance(measurement, Mapping):
                raise evidence.EvidenceError(
                    f"{role} raw report has no measurement object"
                )
            values = measurement.get(metric)
            if not isinstance(values, list) or not values:
                raise evidence.EvidenceError(
                    f"{role} raw report has no raw '{metric}' samples"
                )
            samples.extend(values)
        merged_measurement[metric] = samples
    for metric in (
        "distribution_bytes",
        "shader_count",
        "peak_engine_committed_gpu_bytes",
    ):
        values = [report["measurement"].get(metric) for report in reports]
        if any(value != values[0] for value in values[1:]):
            raise evidence.EvidenceError(
                f"matched {role} runs disagree on '{metric}'"
            )
        merged_measurement[metric] = values[0]
    cold_initialization = [
        float(report["measurement"].get("cold_initialization_ms")) for report in reports
    ]
    merged_measurement["cold_initialization_ms"] = max(cold_initialization)
    images = [report["measurement"].get("image") for report in reports]
    if not all(isinstance(image, Mapping) for image in images):
        raise evidence.EvidenceError(f"{role} raw report has no fixed-quality image result")
    merged_measurement["image"] = {
        "max_abs_channel_error": max(
            float(image["max_abs_channel_error"]) for image in images
        ),
        "rmse": max(float(image["rmse"]) for image in images),
        "reference_kind": images[0].get("reference_kind"),
        "fixed_ticks": list(images[0].get("fixed_ticks") or []),
    }
    merged = {key: first.get(key) for key in identity_keys}
    merged["capability_decision"] = capability_decisions[0]
    merged["measurement"] = merged_measurement
    merged["raw_report_files"] = [report["raw_report_file"] for report in reports]
    merged["process_run_count"] = len(reports)
    return merged


def collect(
    *,
    bundle_root: Path | str,
    run_reports: list[Path | str],
    expected_commit: str | None,
    evidence_file: str | None = None,
) -> dict[str, Any]:
    root = Path(bundle_root)
    verified = bundle.verify_bundle(root, expected_commit=expected_commit)
    manifest = verified["manifest"]
    manifest_sha = contract.sha256_bytes((root / bundle.MANIFEST_NAME).read_bytes())
    artifact_hashes = {
        artifact["path"]: artifact["sha256"]
        for artifact in manifest["artifacts"]
        if artifact["role"]
        in {"common_baseline", "optional_variant", "fallback_common"}
    }
    by_role: dict[str, list[dict[str, Any]]] = {"baseline": [], "variant": []}
    for path in run_reports:
        record = _load(Path(path))
        role = record.get("role")
        if role not in {"baseline", "variant"}:
            raise evidence.EvidenceError(
                f"raw run report '{path}' has unsupported role '{role}'"
            )
        artifact_path = record.get("artifact_path")
        if artifact_path not in artifact_hashes:
            raise evidence.EvidenceError(
                f"raw run report '{path}' is not bound to a measured bundle artifact"
            )
        capability_decision = record.get("capability_decision")
        if not isinstance(capability_decision, Mapping):
            raise evidence.EvidenceError(
                f"raw run report '{path}' has no capability decision"
            )
        record["raw_report_file"] = Path(path).name
        by_role[role].append(record)
    runs = [_merge_role_reports(role, reports) for role, reports in by_role.items() if reports]
    if not runs:
        raise evidence.EvidenceError("no raw run reports were supplied")
    return {
        "format": evidence.EVIDENCE_FORMAT,
        "version": evidence.EVIDENCE_VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "commit": manifest["commit"],
        "bundle_id": manifest["bundle_id"],
        "bundle_manifest_sha256": manifest_sha,
        "experiment_id": manifest["experiment_id"],
        "profile_id": manifest["profile_id"],
        "fallback_artifact_id": manifest["fallback_artifact_id"],
        "capability_profile_sha256": manifest["capability_profile"]["sha256"],
        "artifact_hashes": artifact_hashes,
        "runs": runs,
        "evidence_file": evidence_file,
        "generated_by": "tools/nw11/collect-evidence.py",
        "raw_per_sample_results_preserved": True,
        "operator_supplied_values": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True)
    parser.add_argument("--expected-commit")
    parser.add_argument("--run-report", action="append", default=[])
    parser.add_argument("--run-reports", nargs="*", default=[])
    parser.add_argument("--evidence-name")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    reports = list(args.run_report) + list(args.run_reports)
    try:
        document = collect(
            bundle_root=args.bundle_root,
            run_reports=reports,
            expected_commit=args.expected_commit,
            evidence_file=args.evidence_name,
        )
    except (bundle.BundleError, evidence.EvidenceError) as exc:
        print(f"[NW11-COLLECT-ERR] {exc}", file=sys.stderr)
        return 2
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
