#!/usr/bin/env python3
"""Bounded follow-up matrix for advanced GPU features outside NW-11's scope.

NW-11 implements exactly two experiments: conventional indexed/indirect
rendering versus mesh shaders, and an analytic reflection baseline versus DXR
ray queries.  Every other advanced feature is recorded here only as a bounded
capability, deployment, licensing, dependency, byte-cost, and content-
hypothesis matrix entry.  No prototype may enter this task.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from nw11_contract import (  # type: ignore[import-not-found]
    IMPLEMENTATION_BASE,
    REQUIRED_FOLLOWUP_FEATURES,
    TASK,
    VENDOR_OPTIONAL_COST_ITEMS,
)

MATRIX_FORMAT = "nw11-follow-up-matrix"
MATRIX_VERSION = 1

ALLOWED_DISPOSITIONS = (
    "follow_up_issue_required",
    "not_applicable",
    "blocked_by_licensing",
    "blocked_by_dependency",
)


class FollowupMatrixError(ValueError):
    """Raised when the bounded follow-up matrix is incomplete or overreaches."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FollowupMatrixError(f"{label} must be a JSON object")
    return value


def _string(value: Any, label: str, *, minimum: int = 1) -> str:
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise FollowupMatrixError(
            f"{label} must be a string of at least {minimum} characters"
        )
    return value.strip()


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise FollowupMatrixError(f"{label} must be a non-empty string array")
    return [item.strip() for item in value]


def _byte_cost(entry: Mapping[str, Any], label: str) -> dict[str, Any]:
    cost = _mapping(entry.get("byte_cost"), f"{label}.byte_cost")
    status = cost.get("status")
    if status == "measured":
        value = cost.get("value_bytes")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise FollowupMatrixError(f"{label}.byte_cost.value_bytes is invalid")
    elif status == "unmeasured_estimate":
        value = cost.get("estimate_bytes")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise FollowupMatrixError(
                f"{label}.byte_cost.estimate_bytes is invalid"
            )
        _string(cost.get("basis"), f"{label}.byte_cost.basis", minimum=10)
    else:
        raise FollowupMatrixError(
            f"{label}.byte_cost.status must be measured or unmeasured_estimate"
        )
    return dict(cost)


def load_matrix(path: Path | str) -> dict[str, Any]:
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_matrix(record)


def validate_matrix(record: Mapping[str, Any]) -> dict[str, Any]:
    document = _mapping(record, "follow-up matrix")
    if document.get("format") != MATRIX_FORMAT:
        raise FollowupMatrixError("follow-up matrix has the wrong format")
    if document.get("version") != MATRIX_VERSION:
        raise FollowupMatrixError("follow-up matrix version is not 1")
    if document.get("task") != TASK:
        raise FollowupMatrixError("follow-up matrix task is not NW-11")
    if document.get("implementation_base") != IMPLEMENTATION_BASE:
        raise FollowupMatrixError(
            "follow-up matrix is not bound to the NW-11 implementation base"
        )
    if document.get("prototypes_allowed_in_this_task") is not False:
        raise FollowupMatrixError(
            "no additional advanced-feature prototype may enter NW-11"
        )

    entries = document.get("features")
    if not isinstance(entries, list):
        raise FollowupMatrixError("features must be an array")
    seen: list[str] = []
    for raw in entries:
        entry = _mapping(raw, "feature entry")
        feature = _string(entry.get("feature"), "feature")
        if feature not in REQUIRED_FOLLOWUP_FEATURES:
            raise FollowupMatrixError(f"unexpected follow-up feature '{feature}'")
        if feature in seen:
            raise FollowupMatrixError(f"duplicate follow-up feature '{feature}'")
        seen.append(feature)
        _string(entry.get("content_hypothesis"), f"{feature}.content_hypothesis", minimum=20)
        _string_list(entry.get("exact_feature_queries"), f"{feature}.exact_feature_queries")
        _string(entry.get("api_requirements"), f"{feature}.api_requirements")
        _string_list(entry.get("shader_targets"), f"{feature}.shader_targets")
        _string(entry.get("toolchain_requirements"), f"{feature}.toolchain_requirements")
        _string(entry.get("runtime_requirements"), f"{feature}.runtime_requirements")
        _string_list(entry.get("deployment_constraints"), f"{feature}.deployment_constraints")
        _string_list(entry.get("licensing_constraints"), f"{feature}.licensing_constraints")
        dependencies = entry.get("dependencies")
        if not isinstance(dependencies, list):
            raise FollowupMatrixError(f"{feature}.dependencies must be an array")
        for dependency in dependencies:
            dep = _mapping(dependency, f"{feature}.dependency")
            _string(dep.get("name"), f"{feature}.dependency.name")
            if not isinstance(dep.get("distributed_bytes"), int) or isinstance(
                dep.get("distributed_bytes"), bool
            ):
                raise FollowupMatrixError(
                    f"{feature}.dependency.distributed_bytes must be an integer"
                )
            if dep.get("optional") is not True:
                raise FollowupMatrixError(
                    f"{feature}.dependency must remain optional"
                )
        _byte_cost(entry, feature)
        disposition = entry.get("follow_up_disposition")
        if disposition not in ALLOWED_DISPOSITIONS:
            raise FollowupMatrixError(
                f"{feature}.follow_up_disposition is not a known disposition"
            )
        if entry.get("prototypes_in_this_task") is not False:
            raise FollowupMatrixError(
                f"{feature} must not prototype inside NW-11"
            )

    missing = [feature for feature in REQUIRED_FOLLOWUP_FEATURES if feature not in seen]
    if missing:
        raise FollowupMatrixError(
            "follow-up matrix is missing required features: " + ", ".join(missing)
        )

    vendor_costs = document.get("vendor_optional_costs")
    if not isinstance(vendor_costs, list):
        raise FollowupMatrixError("vendor_optional_costs must be an array")
    cost_names = []
    for raw in vendor_costs:
        entry = _mapping(raw, "vendor optional cost")
        name = _string(entry.get("item"), "vendor_optional_costs.item")
        cost_names.append(name)
        if entry.get("optional") is not True:
            raise FollowupMatrixError(f"{name} must remain optional")
        if entry.get("counted") is not True:
            raise FollowupMatrixError(f"{name} must be fully counted")
        _byte_cost(entry, name)
    if sorted(cost_names) != sorted(VENDOR_OPTIONAL_COST_ITEMS):
        raise FollowupMatrixError(
            "vendor_optional_costs must cover DLSS, RTX Kit, model weights, and SDK components"
        )

    return dict(document)


def matrix_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_matrix(document)
    return {
        "format": validated["format"],
        "feature_count": len(validated["features"]),
        "features": [entry["feature"] for entry in validated["features"]],
        "prototypes_allowed_in_this_task": False,
        "vendor_optional_costs": [entry["item"] for entry in validated["vendor_optional_costs"]],
    }


__all__ = [
    "MATRIX_FORMAT",
    "MATRIX_VERSION",
    "FollowupMatrixError",
    "load_matrix",
    "validate_matrix",
    "matrix_summary",
]
