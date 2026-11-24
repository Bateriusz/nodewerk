#!/usr/bin/env python3
"""NW-11 evidence verification, staleness rejection, and per-device decisions.

Evidence never becomes verified merely because it exists.  It must bind to the
exact tested commit and executable/package hashes in a verified bundle, carry
the capability snapshot produced before measurement, keep raw samples, and
evaluate each device/environment independently.  Pending and unsupported
coverage are preserved as such and never count as passing.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from nw11_contract import (  # type: ignore[import-not-found]
    DEVICE_TIERS,
    EVIDENCE_STATUSES,
    EXPERIMENT_PATH_BY_ID,
    IMPLEMENTATION_BASE,
    TASK,
    canonical_json_bytes,
    digest_value,
)
from nw11_experiment import ExperimentError, evaluate_experiment, validate_experiment

EVIDENCE_FORMAT = "nw11-validation-evidence"
EVIDENCE_VERSION = 1
DECISIONS_FORMAT = "nw11-decisions"
DECISIONS_VERSION = 1


class EvidenceError(ValueError):
    """Raised for malformed, unbound, or internally inconsistent evidence."""


class StaleEvidenceError(EvidenceError):
    """Raised when evidence does not bind the expected commit or artifact hashes."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be a JSON object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} must be a non-empty string")
    return value


def classify_measurement(record: Mapping[str, Any]) -> str:
    """Map a raw run record to one of the frozen NW-11 evidence statuses."""
    eligibility = str(record.get("eligibility_status") or record.get("status") or "")
    if eligibility not in {
        "eligible",
        "measured",
        "pending",
        "unsupported",
        "failed",
        "accepted",
        "rejected",
    }:
        raise EvidenceError(
            f"run evidence status '{eligibility}' is not a known NW-11 status"
        )
    if eligibility == "unsupported":
        return "unsupported"
    if eligibility == "failed":
        return "failed"
    if eligibility == "pending":
        return "pending"
    if record.get("initialization_status") == "failed" or record.get(
        "execution_status"
    ) == "failed":
        return "failed"
    if eligibility in {"eligible", "measured"}:
        decision = str(record.get("decision_status") or "")
        if decision in {"accepted", "rejected"}:
            return decision
        return "pending"
    if eligibility in {"accepted", "rejected"}:
        return eligibility
    raise EvidenceError(f"run evidence status '{eligibility}' is malformed")


def _artifact_hashes(manifest: Mapping[str, Any], roles: set[str]) -> dict[str, str]:
    result = {}
    for artifact in manifest.get("artifacts", []):
        if artifact.get("role") in roles:
            result[str(artifact["path"])] = str(artifact["sha256"])
    return result


ROLE_ARTIFACT_ROLE = {
    "baseline": "common_baseline",
    "variant": "optional_variant",
    "fallback": "fallback_common",
}


def _role_artifact(manifest: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    artifact_role = ROLE_ARTIFACT_ROLE.get(role)
    if artifact_role is None:
        raise EvidenceError(f"unknown measured run role '{role}'")
    for artifact in manifest.get("artifacts", []):
        if artifact.get("role") == artifact_role:
            return artifact
    raise EvidenceError(f"bundle has no artifact for measured role '{role}'")


def _expected_distribution_bytes(manifest: Mapping[str, Any], role: str) -> int:
    accounting = _mapping(manifest.get("accounting"), "bundle.accounting")
    key = {
        "baseline": "baseline_distribution_bytes",
        "variant": "variant_distribution_bytes",
        "fallback": "fallback_distribution_bytes",
    }.get(role)
    if key is None or key not in accounting:
        raise EvidenceError(f"bundle accounting has no distribution bytes for '{role}'")
    return int(accounting[key])


def _expected_shader_ids(manifest: Mapping[str, Any], role: str) -> list[str]:
    inventory = manifest.get("shader_inventory")
    if not isinstance(inventory, Mapping):
        raise EvidenceError("bundle has no shader inventory")
    roles = inventory.get("roles")
    if not isinstance(roles, Mapping) or role not in roles:
        raise EvidenceError(f"bundle shader inventory has no '{role}' role")
    ids = roles[role]
    if not isinstance(ids, list) or not ids:
        raise EvidenceError(f"bundle shader inventory role '{role}' is empty")
    return sorted(str(item) for item in ids)


def _validate_evidence_shape(record: Mapping[str, Any]) -> None:
    if record.get("raw_per_sample_results_preserved") is not True:
        raise EvidenceError(
            "evidence must preserve raw per-sample results"
        )
    if record.get("operator_supplied_values") is not False:
        raise EvidenceError(
            "evidence must be produced without operator-supplied measured values"
        )


def _validate_evidence_schema(record: Mapping[str, Any], schema_root: Path) -> None:
    schema_path = Path(schema_root) / "schemas" / "nw11-evidence.schema.json"
    if not schema_path.is_file():
        raise EvidenceError("the evidence schema is missing from the bundle")
    try:
        import schema_validator  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - import guard
        raise EvidenceError(f"the evidence schema validator is unavailable: {exc}") from exc
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        schema_validator.validate_instance(record, schema, schema)
    except Exception as exc:
        raise EvidenceError(f"evidence failed schema validation: {exc}") from exc


def _validate_run_measurement(
    run: Mapping[str, Any],
    *,
    role: str,
    manifest: Mapping[str, Any],
    document: Mapping[str, Any],
) -> None:
    measurement = _mapping(run.get("measurement"), "run.measurement")
    artifact = _role_artifact(manifest, role)
    if run.get("artifact_path") != artifact.get("path"):
        raise EvidenceError(
            f"{role} run is not bound to the bundle's {artifact.get('role')} artifact"
        )
    expected_bytes = _expected_distribution_bytes(manifest, role)
    if int(measurement.get("distribution_bytes", -1)) != expected_bytes:
        raise EvidenceError(
            f"{role} run distribution bytes disagree with the verified bundle manifest"
        )
    expected_shaders = _expected_shader_ids(manifest, role)
    reported_ids = measurement.get("shader_ids")
    if not isinstance(reported_ids, list):
        raise EvidenceError(f"{role} run did not report its linked shader identities")
    if sorted(str(item) for item in reported_ids) != expected_shaders:
        raise EvidenceError(
            f"{role} run shader identities disagree with the verified bundle inventory"
        )
    if int(measurement.get("shader_count", -1)) != len(expected_shaders):
        raise EvidenceError(
            f"{role} run shader count disagrees with the verified bundle inventory"
        )
    image = _mapping(measurement.get("image"), "run.measurement.image")
    reference = _mapping(document.get("reference"), "experiment.reference")
    criteria_image = _mapping(
        _mapping(document.get("criteria"), "experiment.criteria").get("image"),
        "experiment.criteria.image",
    )
    if list(image.get("fixed_ticks") or []) != list(reference.get("fixed_ticks") or []):
        raise EvidenceError(f"{role} run image ticks are not the frozen reference ticks")
    expected_kind = criteria_image.get("reference_kind")
    if image.get("reference_kind") != expected_kind:
        raise EvidenceError(
            f"{role} run image reference kind does not match the frozen experiment"
        )
    if role in {"baseline", "fallback"}:
        if float(image.get("max_abs_channel_error", 1.0)) > float(
            criteria_image.get("max_abs_channel_error", 0.0)
        ):
            raise EvidenceError(
                f"{role} run does not match the fixed-quality reference image"
            )
        if float(image.get("rmse", 1.0)) > float(
            criteria_image.get("rmse", 0.0)
        ):
            raise EvidenceError(
                f"{role} run RMSE exceeds the fixed-quality reference criterion"
            )


def validate_evidence(
    evidence: Mapping[str, Any],
    *,
    bundle_manifest: Mapping[str, Any],
    experiment: Mapping[str, Any],
    bundle_manifest_sha256: str,
    expected_commit: str | None = None,
    schema_root: Path | str | None = None,
) -> dict[str, Any]:
    record = _mapping(evidence, "evidence")
    if schema_root is not None:
        _validate_evidence_schema(record, Path(schema_root))
    if record.get("format") != EVIDENCE_FORMAT:
        raise EvidenceError("evidence format is not nw11-validation-evidence")
    if record.get("version") != EVIDENCE_VERSION:
        raise EvidenceError("evidence version is not 1")
    if record.get("task") != TASK:
        raise EvidenceError("evidence task is not NW-11")
    if record.get("implementation_base") != IMPLEMENTATION_BASE:
        raise EvidenceError("evidence is not bound to the NW-11 implementation base")

    document = validate_experiment(experiment)
    commit = _string(record.get("commit"), "evidence.commit")
    manifest_commit = _string(bundle_manifest.get("commit"), "bundle.commit")
    if commit != manifest_commit:
        raise StaleEvidenceError(
            "evidence commit does not match the bundle; regenerate evidence"
        )
    if expected_commit is not None and commit != expected_commit:
        raise StaleEvidenceError(
            "evidence was generated for a different commit; regenerate evidence"
        )
    if record.get("experiment_id") != document["experiment_id"]:
        raise EvidenceError("evidence experiment does not match the bundle experiment")
    if record.get("profile_id") != document["variant_profile_id"]:
        raise EvidenceError("evidence profile does not match the optional profile")
    if record.get("fallback_artifact_id") != document["fallback_artifact_id"]:
        raise EvidenceError("evidence fallback identity does not match the experiment")
    if record.get("bundle_id") != bundle_manifest.get("bundle_id"):
        raise EvidenceError("evidence does not bind the verified bundle identity")
    if record.get("bundle_manifest_sha256") != bundle_manifest_sha256:
        raise StaleEvidenceError(
            "evidence does not bind the current bundle manifest bytes; regenerate evidence"
        )
    if record.get("capability_profile_sha256") != bundle_manifest.get(
        "capability_profile", {}
    ).get("sha256"):
        raise EvidenceError("evidence does not bind the bundle capability profile")

    recorded_hashes = _mapping(record.get("artifact_hashes"), "artifact_hashes")
    expected_hashes = _artifact_hashes(
        bundle_manifest,
        {
            "common_baseline",
            "optional_variant",
            "fallback_common",
        },
    )
    if not expected_hashes:
        raise EvidenceError("bundle has no measured artifact identities")
    for path, expected in expected_hashes.items():
        if recorded_hashes.get(path) != expected:
            raise StaleEvidenceError(
                f"evidence does not bind the current hash of '{path}'; regenerate evidence"
            )

    runs = record.get("runs")
    if not isinstance(runs, list) or not runs:
        raise EvidenceError("evidence must preserve at least one device run record")
    _validate_evidence_shape(record)

    roles_seen: set[str] = set()
    for run in runs:
        entry = _mapping(run, "evidence run")
        role = str(entry.get("role"))
        if role not in ROLE_ARTIFACT_ROLE:
            raise EvidenceError(f"run role '{role}' is not baseline, variant, or fallback")
        roles_seen.add(role)
        status = classify_measurement(entry)
        if status not in EVIDENCE_STATUSES:
            raise EvidenceError("run evidence status is not a known NW-11 status")
        eligibility = _mapping(entry.get("capability_decision"), "capability_decision")
        if eligibility.get("marketing_name_used_for_eligibility") is not False:
            raise EvidenceError(
                "evidence must not derive eligibility from a GPU marketing name"
            )
        if status == "eligible" or status == "accepted" or status == "rejected":
            if eligibility.get("status") not in {"eligible", "measured"}:
                raise EvidenceError("measured run has no eligible capability decision")
        if status == "unsupported" and eligibility.get("status") != "unsupported":
            raise EvidenceError("unsupported run does not preserve its unsupported decision")
        if status == "failed" and not entry.get("diagnostics"):
            raise EvidenceError("failed run must preserve a diagnostic")
        device_key = _string(entry.get("device_key"), "run.device_key")
        environment_key = _string(entry.get("environment_key"), "run.environment_key")
        if not device_key or not environment_key:
            raise EvidenceError("run must identify both device and environment")
        if eligibility.get("device_key") != device_key:
            raise EvidenceError(
                "run device key disagrees with the capability decision it was launched under"
            )
        if eligibility.get("environment_key") != environment_key:
            raise EvidenceError(
                "run environment key disagrees with the capability decision it was launched under"
            )
        decision_adapter = eligibility.get("adapter") or {}
        decision_luid = (
            decision_adapter.get("adapter_luid")
            if isinstance(decision_adapter, Mapping)
            else None
        )
        if decision_luid is not None:
            observed_adapter = entry.get("adapter") or {}
            observed_luid = (
                observed_adapter.get("adapter_luid")
                if isinstance(observed_adapter, Mapping)
                else None
            )
            if observed_luid != decision_luid:
                raise EvidenceError(
                    "run adapter LUID disagrees with the probe-selected adapter; "
                    "the measured device cannot be attributed to another GPU"
                )
        if entry.get("device_tier") not in DEVICE_TIERS:
            raise EvidenceError(f"unknown device tier '{entry.get('device_tier')}'")
        if entry.get("measurement") is not None:
            _validate_run_measurement(
                entry, role=role, manifest=bundle_manifest, document=document
            )
        elif str(entry.get("status")) in {"eligible", "measured"} or status in {
            "accepted",
            "rejected",
        }:
            raise EvidenceError("measured run has no preserved measurement")

    if "baseline" not in roles_seen or "variant" not in roles_seen:
        raise EvidenceError("evidence must contain matched baseline and variant runs")
    if "fallback" not in roles_seen:
        raise EvidenceError(
            "the separately exported common fallback artifact was never executed"
        )

    return dict(record)


def assert_fresh(
    evidence: Mapping[str, Any],
    *,
    expected_commit: str,
    bundle_manifest: Mapping[str, Any],
    bundle_manifest_sha256: str,
) -> None:
    if evidence.get("commit") != expected_commit:
        raise StaleEvidenceError("evidence commit is stale")
    if evidence.get("bundle_manifest_sha256") != bundle_manifest_sha256:
        raise StaleEvidenceError("evidence bundle manifest hash is stale")
    for path, digest in _artifact_hashes(
        bundle_manifest, {"common_baseline", "optional_variant", "fallback_common"}
    ).items():
        if _mapping(evidence.get("artifact_hashes"), "artifact_hashes").get(path) != digest:
            raise StaleEvidenceError(f"evidence hash for '{path}' is stale")


def _pending_decision(
    experiment: Mapping[str, Any], device_tier: str, message: str
) -> dict[str, Any]:
    return {
        "device_tier": device_tier,
        "device_key": None,
        "environment_key": None,
        "status": "pending",
        "satisfies_completion": False,
        "reason": message,
        "metrics": None,
    }


def compute_decisions(
    evidence: Mapping[str, Any],
    experiment: Mapping[str, Any],
) -> dict[str, Any]:
    """Compute decisions per device tier while isolating every environment.

    A single device/environment result never establishes coverage for another
    device tier or another driver/OS combination.
    """
    document = validate_experiment(experiment)
    runs = list(_mapping(evidence, "evidence").get("runs") or [])
    by_tier: dict[str, list[Mapping[str, Any]]] = {tier: [] for tier in DEVICE_TIERS}
    tier_by_device: dict[str, str] = {}
    for run in runs:
        entry = _mapping(run, "evidence run")
        tier = str(entry.get("device_tier"))
        if tier not in by_tier:
            raise EvidenceError(f"run references unknown device tier '{tier}'")
        device_key = _string(entry.get("device_key"), "run.device_key")
        previous = tier_by_device.setdefault(device_key, tier)
        if previous != tier:
            raise EvidenceError(
                "the same device key was attributed to two device tiers; "
                "per-device decisions cannot be reused across tiers"
            )
        by_tier[tier].append(entry)

    decisions: list[dict[str, Any]] = []
    for tier in DEVICE_TIERS:
        tier_runs = by_tier[tier]
        if not tier_runs:
            decisions.append(
                _pending_decision(
                    document,
                    tier,
                    "no matched physical baseline/variant run recorded for this device tier",
                )
            )
            continue
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for run in tier_runs:
            key = (
                _string(run.get("device_key"), "run.device_key"),
                _string(run.get("environment_key"), "run.environment_key"),
            )
            grouped.setdefault(key, []).append(run)
        for (device_key, environment_key), group in sorted(grouped.items()):
            decision = _decide_group(
                document, group, device_key=device_key, environment_key=environment_key
            )
            decision["device_tier"] = tier
            decisions.append(decision)
    return {
        "experiment_id": document["experiment_id"],
        "variant_profile_id": document["variant_profile_id"],
        "fallback_artifact_id": document["fallback_artifact_id"],
        "decisions": decisions,
        "per_device_decision_isolation": True,
        "pending_is_not_pass": True,
    }


def _decide_group(
    experiment: Mapping[str, Any],
    group: list[Mapping[str, Any]],
    *,
    device_key: str,
    environment_key: str,
) -> dict[str, Any]:
    if len(group) < 2:
        return {
            "device_key": device_key,
            "environment_key": environment_key,
            "status": "pending",
            "satisfies_completion": False,
            "reason": "a matched baseline and variant pair is required",
            "metrics": None,
        }
    statuses = {
        "eligible" if str(run.get("status")) == "measured" else str(run.get("status"))
        for run in group
    }
    if "failed" in statuses:
        return {
            "device_key": device_key,
            "environment_key": environment_key,
            "status": "failed",
            "satisfies_completion": False,
            "reason": "feature initialization or execution failed; no effect was substituted",
            "metrics": None,
            "diagnostics": [
                diagnostic
                for run in group
                for diagnostic in (run.get("diagnostics") or [])
            ],
        }
    if "unsupported" in statuses:
        return {
            "device_key": device_key,
            "environment_key": environment_key,
            "status": "unsupported",
            "satisfies_completion": False,
            "reason": "required D3D12 capability is absent; the optional profile did not run",
            "metrics": None,
        }
    if statuses != {"eligible"} and statuses != {"measured"}:
        return {
            "device_key": device_key,
            "environment_key": environment_key,
            "status": "pending",
            "satisfies_completion": False,
            "reason": "baseline and variant coverage are not both measured",
            "metrics": None,
        }

    baseline = next((run for run in group if run.get("role") == "baseline"), None)
    variant = next((run for run in group if run.get("role") == "variant"), None)
    fallback = next((run for run in group if run.get("role") == "fallback"), None)
    if baseline is None or variant is None:
        return {
            "device_key": device_key,
            "environment_key": environment_key,
            "status": "pending",
            "satisfies_completion": False,
            "reason": "group does not contain one baseline and one variant run",
            "metrics": None,
        }
    if fallback is None:
        return {
            "device_key": device_key,
            "environment_key": environment_key,
            "status": "pending",
            "satisfies_completion": False,
            "reason": "the separately exported common fallback was not executed",
            "metrics": None,
            "fallback_tested": False,
        }
    try:
        evaluation = evaluate_experiment(
            experiment,
            {
                "device_key": device_key,
                "environment_key": environment_key,
                "baseline": baseline.get("measurement"),
                "variant": variant.get("measurement"),
            },
            device_key=device_key,
            environment_key=environment_key,
        )
    except ExperimentError as exc:
        raise EvidenceError(f"invalid matched measurement: {exc}") from exc
    if int(fallback["measurement"]["distribution_bytes"]) > int(
        experiment["artifact_budget"]["complete_distribution_bytes_ceiling"]
    ):
        evaluation["reasons"].append({
            "code": "NW11-FALLBACK-DISTRIBUTION-CEILING",
            "message": "fallback complete distribution exceeds the preserved 256 KiB ceiling",
        })
        evaluation["status"] = "rejected"
        evaluation["satisfies_completion"] = False
    evaluation["device_tier"] = None
    evaluation["reason"] = (
        "matched p99 GPU-time, end-to-end, distribution, and fixed-quality criteria passed"
        if evaluation["status"] == "accepted"
        else "; ".join(reason["message"] for reason in evaluation["reasons"])
    )
    evaluation["metrics"] = evaluation["aggregate"]
    evaluation["fallback_tested"] = True
    return evaluation


def build_decision_document(
    *,
    evidence: Mapping[str, Any],
    experiment: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    bundle_manifest_sha256: str,
    expected_commit: str | None = None,
    schema_root: Path | str | None = None,
) -> dict[str, Any]:
    validate_evidence(
        evidence,
        bundle_manifest=bundle_manifest,
        experiment=experiment,
        bundle_manifest_sha256=bundle_manifest_sha256,
        expected_commit=expected_commit,
        schema_root=schema_root,
    )
    computed = compute_decisions(evidence, experiment)
    decisions = computed["decisions"]
    if not decisions:
        raise EvidenceError("decision document contains no device decisions")
    order = list(DEVICE_TIERS)
    decisions.sort(key=lambda item: (order.index(item["device_tier"]), item["device_key"] or ""))
    aggregate_status = (
        "accepted"
        if decisions and all(item["status"] == "accepted" for item in decisions)
        else "pending"
        if any(item["status"] == "pending" for item in decisions)
        else "unsupported"
        if any(item["status"] == "unsupported" for item in decisions)
        else "failed"
        if any(item["status"] == "failed" for item in decisions)
        else "rejected"
    )
    document = {
        "format": DECISIONS_FORMAT,
        "version": DECISIONS_VERSION,
        "task": TASK,
        "implementation_base": IMPLEMENTATION_BASE,
        "commit": evidence.get("commit"),
        "bundle_id": bundle_manifest.get("bundle_id"),
        "bundle_manifest_sha256": bundle_manifest_sha256,
        "experiment_id": computed["experiment_id"],
        "variant_profile_id": computed["variant_profile_id"],
        "fallback_artifact_id": computed["fallback_artifact_id"],
        "evidence_file": evidence.get("evidence_file"),
        "devices": decisions,
        "aggregate_status": aggregate_status,
        "fallback_tested": bool(decisions)
        and all(item.get("fallback_tested") is True for item in decisions),
        "satisfies_completion": all(
            item["status"] == "accepted" for item in decisions
        ),
        "per_device_decision_isolation": True,
        "pending_is_not_pass": True,
        "unsupported_is_not_failure_or_pass": True,
    }
    identity = {
        key: document[key]
        for key in (
            "commit",
            "bundle_id",
            "bundle_manifest_sha256",
            "experiment_id",
            "variant_profile_id",
            "fallback_artifact_id",
            "evidence_file",
            "devices",
            "aggregate_status",
            "fallback_tested",
            "satisfies_completion",
        )
    }
    document["decision_id"] = digest_value(identity)
    return document


def validate_decision_document(document: Mapping[str, Any]) -> dict[str, Any]:
    record = _mapping(document, "decision document")
    if record.get("format") != DECISIONS_FORMAT:
        raise EvidenceError("decision document has the wrong format")
    if record.get("version") != DECISIONS_VERSION:
        raise EvidenceError("decision document version is not 1")
    if record.get("task") != TASK:
        raise EvidenceError("decision document task is not NW-11")
    if record.get("implementation_base") != IMPLEMENTATION_BASE:
        raise EvidenceError("decision document is not bound to the NW-11 base")
    devices = record.get("devices")
    if not isinstance(devices, list) or not devices:
        raise EvidenceError("decision document has no device decisions")
    for device in devices:
        entry = _mapping(device, "device decision")
        if entry.get("status") not in EVIDENCE_STATUSES:
            raise EvidenceError("device decision status is not a known NW-11 status")
        if entry.get("status") == "pending" and entry.get("satisfies_completion") is not False:
            raise EvidenceError("pending coverage must not satisfy completion")
        if entry.get("status") == "unsupported" and entry.get("satisfies_completion") is not False:
            raise EvidenceError("unsupported coverage must not satisfy completion")
    aggregate = record.get("aggregate_status")
    if aggregate not in EVIDENCE_STATUSES:
        raise EvidenceError("aggregate decision status is not a known NW-11 status")
    if record.get("satisfies_completion") is True:
        if not all(item.get("status") == "accepted" for item in devices):
            raise EvidenceError(
                "completion requires accepted evidence for every qualified device"
            )
        if record.get("fallback_tested") is not True:
            raise EvidenceError(
                "completion requires the separate common fallback to be executed"
            )
    identity = {
        key: record[key]
        for key in (
            "commit",
            "bundle_id",
            "bundle_manifest_sha256",
            "experiment_id",
            "variant_profile_id",
            "fallback_artifact_id",
            "evidence_file",
            "devices",
            "aggregate_status",
            "fallback_tested",
            "satisfies_completion",
        )
        if key in record
    }
    if record.get("decision_id") != digest_value(identity):
        raise EvidenceError("decision_id does not match the decision document identity")
    return dict(record)


def load_json(path: Path | str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"malformed JSON evidence: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError("evidence document is not a JSON object")
    return value


__all__ = [
    "EVIDENCE_FORMAT",
    "EVIDENCE_VERSION",
    "DECISIONS_FORMAT",
    "DECISIONS_VERSION",
    "EvidenceError",
    "StaleEvidenceError",
    "classify_measurement",
    "validate_evidence",
    "assert_fresh",
    "compute_decisions",
    "build_decision_document",
    "validate_decision_document",
    "load_json",
]
