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
import nw11_experiment as experiment  # noqa: E402

EXPECTED_ROLE_ARTIFACT = {
    "baseline": "common_baseline",
    "variant": "optional_variant",
    "fallback": "fallback_common",
}


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
    if len(reports) != contract.PERF_RUN_COUNT:
        raise evidence.EvidenceError(
            f"role '{role}' requires exactly {contract.PERF_RUN_COUNT} fresh-process "
            f"reports, observed {len(reports)}"
        )
    identity_keys = ("device_tier", "device_key", "environment_key", "status", "artifact_path")
    first = reports[0]
    for key in identity_keys:
        if any(report.get(key) != first.get(key) for report in reports[1:]):
            raise evidence.EvidenceError(
                f"matched {role} runs disagree on '{key}'; observations are not comparable"
            )
    adapter_identity = [report.get("adapter") for report in reports]
    if any(item != adapter_identity[0] for item in adapter_identity[1:]):
        raise evidence.EvidenceError(
            f"matched {role} runs disagree on the measured adapter; "
            "per-device decisions cannot mix adapters"
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
            if (
                not isinstance(values, list)
                or len(values) < contract.MIN_RAW_SAMPLES_PER_RUN
            ):
                raise evidence.EvidenceError(
                    f"{role} raw report does not preserve at least "
                    f"{contract.MIN_RAW_SAMPLES_PER_RUN} raw '{metric}' samples per run"
                )
            samples.extend(values)
        merged_measurement[metric] = samples
    for metric in (
        "distribution_bytes",
        "shader_count",
    ):
        values = [report["measurement"].get(metric) for report in reports]
        if any(value != values[0] for value in values[1:]):
            raise evidence.EvidenceError(
                f"matched {role} runs disagree on '{metric}'"
            )
        merged_measurement[metric] = values[0]
    memory = [report["measurement"].get("peak_engine_committed_gpu_bytes") for report in reports]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in memory):
        raise evidence.EvidenceError(f"{role} has an invalid observed peak GPU memory measurement")
    merged_measurement["peak_engine_committed_gpu_bytes"] = max(memory)
    shader_ids = [report["measurement"].get("shader_ids") for report in reports]
    if any(
        not isinstance(value, list) or list(value) != list(shader_ids[0])
        for value in shader_ids
    ):
        raise evidence.EvidenceError(
            f"matched {role} runs disagree on linked shader identities"
        )
    merged_measurement["shader_ids"] = list(shader_ids[0])
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
    merged["adapter"] = adapter_identity[0]
    merged["role"] = role
    merged["capability_decision"] = capability_decisions[0]
    merged["measurement"] = merged_measurement
    merged["raw_report_files"] = [report["raw_report_file"] for report in reports]
    merged["process_run_count"] = len(reports)
    return merged


def _expected_artifact(manifest: Mapping[str, Any], role: str) -> str:
    artifact_role = EXPECTED_ROLE_ARTIFACT[role]
    for artifact in manifest.get("artifacts", []):
        if artifact.get("role") == artifact_role:
            return str(artifact["path"])
    raise evidence.EvidenceError(f"bundle lacks the '{artifact_role}' artifact")


def _expected_distribution(manifest: Mapping[str, Any], role: str) -> int:
    accounting = manifest["accounting"]
    key = {
        "baseline": "baseline_distribution_bytes",
        "variant": "variant_distribution_bytes",
        "fallback": "fallback_distribution_bytes",
    }[role]
    return int(accounting[key])


def _expected_shader_ids(manifest: Mapping[str, Any], role: str) -> list[str]:
    inventory = manifest.get("shader_inventory")
    if not isinstance(inventory, Mapping):
        raise evidence.EvidenceError("verified bundle has no shader inventory")
    roles = inventory.get("roles")
    if not isinstance(roles, Mapping) or role not in roles:
        raise evidence.EvidenceError(
            f"verified bundle shader inventory has no '{role}' role"
        )
    return sorted(str(item) for item in roles[role])


def _load_optional_json(path: str | None, label: str) -> dict[str, Any] | None:
    if not path:
        return None
    return _load(Path(path))


def collect(
    *,
    bundle_root: Path | str,
    run_reports: list[Path | str],
    expected_commit: str | None,
    evidence_file: str | None = None,
    eligibility_file: str | None = None,
    common_eligibility_file: str | None = None,
    image_result_file: str | None = None,
    fallback_image_result_file: str | None = None,
    device_tier: str | None = None,
) -> dict[str, Any]:
    root = Path(bundle_root)
    verified = bundle.verify_bundle(root, expected_commit=expected_commit)
    manifest = verified["manifest"]
    experiment_document = _load(
        root / str(manifest["experiment_definition"]["path"])
    )
    experiment.validate_experiment(experiment_document)
    eligibility = _load_optional_json(eligibility_file, "eligibility")
    common_eligibility = _load_optional_json(
        common_eligibility_file, "common eligibility"
    )
    image_result = _load_optional_json(image_result_file, "image result")
    fallback_image_result = _load_optional_json(
        fallback_image_result_file, "fallback image result"
    )
    if eligibility is None or common_eligibility is None:
        raise evidence.EvidenceError(
            "collector requires the capability decisions produced before execution"
        )
    if image_result is None or fallback_image_result is None:
        raise evidence.EvidenceError(
            "collector requires the independent fixed-tick image comparisons"
        )
    if eligibility.get("status") != "eligible":
        raise evidence.EvidenceError(
            "optional capability decision is not eligible; no variant may be measured"
        )
    if common_eligibility.get("status") != "eligible":
        raise evidence.EvidenceError(
            "common capability decision is not eligible; baseline cannot be measured"
        )
    manifest_sha = contract.sha256_bytes((root / bundle.MANIFEST_NAME).read_bytes())
    artifact_hashes = {
        artifact["path"]: artifact["sha256"]
        for artifact in manifest["artifacts"]
        if artifact["role"]
        in {"common_baseline", "optional_variant", "fallback_common"}
    }
    by_role: dict[str, list[dict[str, Any]]] = {
        "baseline": [],
        "variant": [],
        "fallback": [],
    }
    for path in run_reports:
        record = _load(Path(path))
        role = record.get("role")
        if role not in EXPECTED_ROLE_ARTIFACT:
            raise evidence.EvidenceError(
                f"raw run report '{path}' has unsupported role '{role}'"
            )
        artifact_path = record.get("artifact_path")
        expected_artifact = _expected_artifact(manifest, role)
        if artifact_path != expected_artifact or artifact_path not in artifact_hashes:
            raise evidence.EvidenceError(
                f"raw run report '{path}' is not bound to the bundle's "
                f"'{EXPECTED_ROLE_ARTIFACT[role]}' artifact"
            )
        capability_decision = record.get("capability_decision")
        if not isinstance(capability_decision, Mapping):
            raise evidence.EvidenceError(
                f"raw run report '{path}' has no capability decision"
            )
        expected_decision = eligibility if role == "variant" else common_eligibility
        if dict(capability_decision) != dict(expected_decision):
            raise evidence.EvidenceError(
                f"raw run report '{path}' capability decision does not match the "
                "decision the launcher obtained from the probe"
            )
        decision_adapter = capability_decision.get("adapter") or {}
        decision_luid = (
            decision_adapter.get("adapter_luid")
            if isinstance(decision_adapter, Mapping)
            else None
        )
        if decision_luid is not None:
            observed_adapter = record.get("adapter") or {}
            observed_luid = (
                observed_adapter.get("adapter_luid")
                if isinstance(observed_adapter, Mapping)
                else None
            )
            if observed_luid != decision_luid:
                raise evidence.EvidenceError(
                    f"raw run report '{path}' adapter LUID does not match the "
                    "probe-selected adapter"
                )
        measurement = record.get("measurement")
        if not isinstance(measurement, Mapping):
            raise evidence.EvidenceError(
                f"raw run report '{path}' has no measurement object"
            )
        if int(measurement.get("distribution_bytes", -1)) != _expected_distribution(
            manifest, role
        ):
            raise evidence.EvidenceError(
                f"raw run report '{path}' distribution bytes disagree with the "
                "verified bundle manifest"
            )
        expected_ids = _expected_shader_ids(manifest, role)
        reported_ids = measurement.get("shader_ids")
        if not isinstance(reported_ids, list) or sorted(
            str(item) for item in reported_ids
        ) != expected_ids:
            raise evidence.EvidenceError(
                f"raw run report '{path}' shader identities disagree with the "
                "verified bundle inventory"
            )
        if int(measurement.get("shader_count", -1)) != len(expected_ids):
            raise evidence.EvidenceError(
                f"raw run report '{path}' shader count disagrees with the "
                "verified bundle inventory"
            )
        if record.get("device_key") != expected_decision.get("device_key"):
            raise evidence.EvidenceError(
                f"raw run report '{path}' device key disagrees with the probe decision"
            )
        if record.get("environment_key") != expected_decision.get("environment_key"):
            raise evidence.EvidenceError(
                f"raw run report '{path}' environment key disagrees with the probe decision"
            )
        if device_tier is not None and record.get("device_tier") != device_tier:
            raise evidence.EvidenceError(
                f"raw run report '{path}' device tier disagrees with the "
                "qualification tier declared by the launcher"
            )
        record["raw_report_file"] = Path(path).name
        by_role[role].append(record)
    for role in EXPECTED_ROLE_ARTIFACT:
        if not by_role[role]:
            raise evidence.EvidenceError(
                f"no raw run reports for required role '{role}'"
            )
    runs = [_merge_role_reports(role, reports) for role, reports in by_role.items()]
    if image_result is not None:
        criteria = experiment_document["criteria"]
        expected_kind = criteria["image"]["reference_kind"]
        expected_ticks = list(criteria["image"]["fixed_ticks"])
        if image_result.get("reference_kind") != expected_kind:
            raise evidence.EvidenceError(
                "independent image comparison used the wrong reference kind"
            )
        if list(image_result.get("fixed_ticks") or []) != expected_ticks:
            raise evidence.EvidenceError(
                "independent image comparison did not cover the frozen fixed ticks"
            )
        variant_run = next(run for run in runs if run["role"] == "variant")
        variant_run["measurement"]["image"] = {
            "max_abs_channel_error": float(
                image_result["max_abs_channel_error"]
            ),
            "rmse": float(image_result["rmse"]),
            "reference_kind": expected_kind,
            "fixed_ticks": expected_ticks,
        }
    if fallback_image_result is not None:
        criteria = experiment_document["criteria"]
        expected_kind = criteria["image"]["reference_kind"]
        expected_ticks = list(criteria["image"]["fixed_ticks"])
        if fallback_image_result.get("reference_kind") != expected_kind:
            raise evidence.EvidenceError(
                "fallback image comparison used the wrong reference kind"
            )
        if list(fallback_image_result.get("fixed_ticks") or []) != expected_ticks:
            raise evidence.EvidenceError(
                "fallback image comparison did not cover the frozen fixed ticks"
            )
        # The baseline is the fixed-quality reference, so its own metric must
        # also come from the repository-owned capture comparison rather than
        # from a self-reported value in the measured binary.  The separately
        # built common fallback exercises the same common pipeline, so the
        # baseline-vs-fallback comparison is the independent baseline check.
        baseline_run = next(run for run in runs if run["role"] == "baseline")
        baseline_run["measurement"]["image"] = {
            "max_abs_channel_error": float(
                fallback_image_result["max_abs_channel_error"]
            ),
            "rmse": float(fallback_image_result["rmse"]),
            "reference_kind": expected_kind,
            "fixed_ticks": expected_ticks,
        }
        fallback_run = next(run for run in runs if run["role"] == "fallback")
        fallback_run["measurement"]["image"] = {
            "max_abs_channel_error": float(
                fallback_image_result["max_abs_channel_error"]
            ),
            "rmse": float(fallback_image_result["rmse"]),
            "reference_kind": expected_kind,
            "fixed_ticks": expected_ticks,
        }
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
    parser.add_argument("--eligibility")
    parser.add_argument("--common-eligibility")
    parser.add_argument("--image-result")
    parser.add_argument("--fallback-image-result")
    parser.add_argument("--device-tier")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    reports = list(args.run_report) + list(args.run_reports)
    try:
        document = collect(
            bundle_root=args.bundle_root,
            run_reports=reports,
            expected_commit=args.expected_commit,
            evidence_file=args.evidence_name,
            eligibility_file=args.eligibility,
            common_eligibility_file=args.common_eligibility,
            image_result_file=args.image_result,
            fallback_image_result_file=args.fallback_image_result,
            device_tier=args.device_tier,
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
