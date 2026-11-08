#!/usr/bin/env python3
"""Zero-dependency JSON Schema Draft 2020-12 validator for Nodewerk NW-01 artifacts.

Validates capability manifests, toolchain manifests, dependency inventories,
size ledgers, hardware benchmark captures, reproducibility reports, clean-system
validation reports, and preserved experiment records against schemas/*.schema.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from evidence_checks import desktop_cpu_model_matches


class SchemaValidationError(ValueError):
    """Raised when a JSON document fails schema or semantic validation."""


def _matches_type(instance: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return instance is None
    if expected_type == "boolean":
        return isinstance(instance, bool)
    if expected_type == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected_type == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected_type == "string":
        return isinstance(instance, str)
    if expected_type == "array":
        return isinstance(instance, list)
    if expected_type == "object":
        return isinstance(instance, dict)
    raise SchemaValidationError(f"Unsupported JSON Schema type '{expected_type}'")


def _resolve_ref(ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaValidationError(f"Only local fragment $ref is supported, got '{ref}'")
    parts = ref[2:].split("/")
    current: Any = root_schema
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise SchemaValidationError(f"Unresolvable $ref '{ref}' at segment '{part}'")
        current = current[part]
    if not isinstance(current, dict):
        raise SchemaValidationError(f"$ref '{ref}' did not resolve to a schema object")
    return current


def validate_instance(
    instance: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any] | None = None,
    path: str = "$",
) -> None:
    """Recursively validate a JSON value against a JSON Schema Draft 2020-12 definition."""
    if root_schema is None:
        root_schema = schema

    if "$ref" in schema:
        target = _resolve_ref(schema["$ref"], root_schema)
        validate_instance(instance, target, root_schema, path)
        return

    if "type" in schema:
        expected = schema["type"]
        allowed_types = [expected] if isinstance(expected, str) else list(expected)
        if not any(_matches_type(instance, t) for t in allowed_types):
            raise SchemaValidationError(
                f"{path}: expected type {allowed_types}, got {type(instance).__name__} ({instance!r})"
            )

    if "const" in schema:
        if instance != schema["const"] or type(instance) is not type(schema["const"]):
            raise SchemaValidationError(
                f"{path}: expected const {schema['const']!r}, got {instance!r}"
            )

    if "enum" in schema:
        if not any(instance == item and type(instance) is type(item) for item in schema["enum"]):
            raise SchemaValidationError(
                f"{path}: value {instance!r} not in enum {schema['enum']!r}"
            )

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaValidationError(
                f"{path}: string length {len(instance)} < minLength {schema['minLength']}"
            )
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise SchemaValidationError(
                f"{path}: string {instance!r} does not match pattern {schema['pattern']!r}"
            )

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaValidationError(
                f"{path}: value {instance} < minimum {schema['minimum']}"
            )
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaValidationError(
                f"{path}: value {instance} > maximum {schema['maximum']}"
            )
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            raise SchemaValidationError(
                f"{path}: value {instance} <= exclusiveMinimum {schema['exclusiveMinimum']}"
            )

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaValidationError(
                f"{path}: array length {len(instance)} < minItems {schema['minItems']}"
            )
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaValidationError(
                f"{path}: array length {len(instance)} > maxItems {schema['maxItems']}"
            )
        if "items" in schema:
            item_schema = schema["items"]
            for idx, elem in enumerate(instance):
                validate_instance(elem, item_schema, root_schema, f"{path}[{idx}]")
        if "contains" in schema:
            contains_schema = schema["contains"]
            matched = False
            for idx, elem in enumerate(instance):
                try:
                    validate_instance(elem, contains_schema, root_schema, f"{path}[{idx}]")
                    matched = True
                    break
                except SchemaValidationError:
                    continue
            if not matched:
                raise SchemaValidationError(
                    f"{path}: array does not contain any element matching {contains_schema!r}"
                )

    if isinstance(instance, dict):
        if "required" in schema:
            for req_key in schema["required"]:
                if req_key not in instance:
                    raise SchemaValidationError(f"{path}: missing required property '{req_key}'")
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, val in instance.items():
            if key in props:
                validate_instance(val, props[key], root_schema, f"{path}.{key}")
            elif additional is False:
                raise SchemaValidationError(f"{path}: unexpected additional property '{key}'")
            elif isinstance(additional, dict):
                validate_instance(val, additional, root_schema, f"{path}.{key}")


def validate_semantic_invariants(instance: dict[str, Any], schema_name: str, doc_path: Path) -> None:
    """Enforce cross-field semantic invariants required by NW-01."""
    if schema_name == "benchmark-result.schema.json":
        status = instance.get("verification_status")
        passed = instance.get("benchmark_passed")
        if status == "unverified" and passed is not False:
            raise SchemaValidationError(
                f"{doc_path}: unverified hardware coverage must have benchmark_passed=false"
            )
        if status == "verified":
            env = instance.get("environment_metadata", {})
            required_env = (
                "gpu_model",
                "gpu_architecture",
                "gpu_vendor_id",
                "gpu_device_id",
                "vram_mb",
                "cpu_model",
                "os_build",
                "nvidia_driver_version",
                "wddm_driver_version",
            )
            for key in required_env:
                if env.get(key) in (None, "", "unverified"):
                    raise SchemaValidationError(
                        f"{doc_path}: verified benchmark requires measured environment_metadata.{key}"
                    )
            if not desktop_cpu_model_matches(str(env.get("cpu_model") or "")):
                raise SchemaValidationError(
                    f"{doc_path}: verified benchmark environment_metadata.cpu_model must identify "
                    "a desktop-class processor; mobile/low-power CPUs cannot certify a desktop tier"
                )
            toolchain = env.get("toolchain", {})
            if toolchain.get("toolchain_id") in (None, "", "unverified"):
                raise SchemaValidationError(
                    f"{doc_path}: verified benchmark requires artifact-bound toolchain provenance"
                )
            power = env.get("power_settings", {})
            if any(value in (None, "", "unverified") for value in power.values()):
                raise SchemaValidationError(
                    f"{doc_path}: verified benchmark requires measured power_settings"
                )
            resolution = env.get("resolution", {})
            if resolution.get("refresh_hz") is None:
                raise SchemaValidationError(
                    f"{doc_path}: verified benchmark requires measured refresh_hz"
                )
            cache = env.get("cache_conditions", {})
            if cache.get("driver_shader_cache_state") == "unverified" or cache.get(
                "os_file_cache_state"
            ) == "unverified":
                raise SchemaValidationError(
                    f"{doc_path}: verified benchmark requires measured cache conditions"
                )
            provenance = instance.get("artifact_provenance", {})
            if (
                provenance.get("toolchain_id") is None
                or provenance.get("source") != "recorded_ledger_artifact_hash"
            ):
                raise SchemaValidationError(
                    f"{doc_path}: verified benchmark requires artifact hash provenance"
                )
        unavailable_metrics = {m["metric"] for m in instance.get("unavailable_measurements", [])}
        for fx in instance.get("fixture_results", []):
            if fx.get("execution_status") == "unverified":
                if fx.get("exit_code") is not None:
                    raise SchemaValidationError(
                        f"{doc_path}: unverified fixture '{fx['fixture_id']}' must have exit_code=null"
                    )
            for metric_key in (
                "total_distributed_bytes",
                "startup_time_ms",
                "cpu_time_ms",
                "gpu_time_ms",
                "committed_memory_bytes",
            ):
                val = fx.get(metric_key)
                if val is None:
                    qualified = f"fixture_results.{metric_key}"
                    per_fixture = f"fixture_results[{fx['fixture_id']}].{metric_key}"
                    if (
                        qualified not in unavailable_metrics
                        and metric_key not in unavailable_metrics
                        and per_fixture not in unavailable_metrics
                    ):
                        raise SchemaValidationError(
                            f"{doc_path}: null metric '{qualified}' must be documented in unavailable_measurements"
                        )
                elif val == 0:
                    raise SchemaValidationError(
                        f"{doc_path}: metric '{metric_key}' must not be recorded as 0 when unavailable"
                    )

    elif schema_name == "size-ledger.schema.json":
        status = instance.get("verification_status")
        components = instance.get("distributed_components", [])
        total_bytes = instance.get("total_distributed_bytes")
        unavailable_metrics = {m["metric"] for m in instance.get("unavailable_measurements", [])}

        if status == "unverified":
            if total_bytes is None:
                if "total_distributed_bytes" not in unavailable_metrics:
                    raise SchemaValidationError(
                        f"{doc_path}: unverified size ledger must list 'total_distributed_bytes' in unavailable_measurements"
                    )
                for comp in components:
                    if comp.get("size_bytes") is not None or comp.get("sha256") is not None:
                        raise SchemaValidationError(
                            f"{doc_path}: unmeasured size ledger component '{comp.get('name')}' must have size_bytes=null and sha256=null"
                        )
            else:
                reason = instance.get("unverified_reason")
                if not isinstance(reason, str) or len(reason.strip()) < 10:
                    raise SchemaValidationError(
                        f"{doc_path}: unverified size ledger retaining measured bytes must include a descriptive unverified_reason"
                    )
                for comp in components:
                    if comp.get("size_bytes") is None or comp.get("sha256") is None:
                        raise SchemaValidationError(
                            f"{doc_path}: measured size ledger component '{comp.get('name')}' must have non-null size_bytes and sha256"
                        )
                computed_sum = sum(
                    c["size_bytes"] for c in components if c.get("counted_in_total_distributed_bytes")
                )
                if computed_sum != total_bytes:
                    raise SchemaValidationError(
                        f"{doc_path}: total_distributed_bytes ({total_bytes}) "
                        f"!= sum of counted distributed_components ({computed_sum})"
                    )
        else:
            if total_bytes is None:
                raise SchemaValidationError(
                    f"{doc_path}: verified size ledger must have integer total_distributed_bytes"
                )
            for comp in components:
                if comp.get("size_bytes") is None or comp.get("sha256") is None:
                    raise SchemaValidationError(
                        f"{doc_path}: verified size ledger component '{comp.get('name')}' must have non-null size_bytes and sha256"
                    )
            computed_sum = sum(c["size_bytes"] for c in components if c.get("counted_in_total_distributed_bytes"))
            if computed_sum != total_bytes:
                raise SchemaValidationError(
                    f"{doc_path}: total_distributed_bytes ({total_bytes}) "
                    f"!= sum of counted distributed_components ({computed_sum})"
                )

        for metric_key, val in instance.get("runtime_metrics", {}).items():
            if val is None and metric_key not in unavailable_metrics:
                raise SchemaValidationError(
                    f"{doc_path}: null runtime_metric '{metric_key}' must be listed in unavailable_measurements"
                )
            if val == 0:
                raise SchemaValidationError(
                    f"{doc_path}: runtime_metric '{metric_key}' must not be recorded as 0"
                )

    elif schema_name == "clean-system-report.schema.json":
        status = instance.get("verification_status")
        inv = instance.get("deployed_dependency_inventory", {})
        sdk_checks = instance.get("sdk_absence_checks", {})
        shader_ev = instance.get("offline_shader_execution_evidence", {})

        if status == "unverified":
            if instance.get("clean_system_passed") is not False:
                raise SchemaValidationError(
                    f"{doc_path}: unverified clean-system report must have clean_system_passed=false"
                )
            # A failed-but-executed run must preserve measured evidence. Only an
            # unexecuted template report is required to keep every measurement null.
            executed = (
                inv.get("total_distributed_bytes") is not None
                or any(
                    run.get("exit_code") is not None
                    for run in instance.get("fixture_runs", [])
                )
                or any(v is not None for v in sdk_checks.values())
            )
            if not executed:
                if any(v is not None for v in sdk_checks.values()):
                    raise SchemaValidationError(
                        f"{doc_path}: unexecuted unverified clean-system report must have null sdk_absence_checks"
                    )
                if inv.get("total_distributed_bytes") is not None:
                    raise SchemaValidationError(
                        f"{doc_path}: unexecuted unverified clean-system report must have total_distributed_bytes=null"
                    )
                if inv.get("inbox_dlls_resolved"):
                    raise SchemaValidationError(
                        f"{doc_path}: unexecuted unverified clean-system report must not advertise "
                        "an inbox DLL closure"
                    )
                system_env = instance.get("system_environment", {})
                for key in ("os_build", "gpu_model", "nvidia_driver_version"):
                    if system_env.get(key) is not None:
                        raise SchemaValidationError(
                            f"{doc_path}: unexecuted unverified clean-system report must have "
                            f"system_environment.{key}=null rather than an unmeasured target value"
                        )
                if shader_ev.get("compiled_offline_with_dxc") is True:
                    raise SchemaValidationError(
                        f"{doc_path}: unexecuted unverified clean-system report must not claim compiled_offline_with_dxc=true"
                    )
        else:
            if instance.get("clean_system_passed") is not True:
                raise SchemaValidationError(
                    f"{doc_path}: verified clean-system report must have clean_system_passed=true"
                )
            if not all(v is True for v in sdk_checks.values()):
                raise SchemaValidationError(
                    f"{doc_path}: verified clean-system report requires every SDK-absence check to be true"
                )
            system_env = instance.get("system_environment", {})
            for key in ("os_build", "gpu_model", "nvidia_driver_version"):
                if system_env.get(key) is None:
                    raise SchemaValidationError(
                        f"{doc_path}: verified clean-system report requires measured "
                        f"system_environment.{key}"
                    )
            if re.search(
                r"^10\.0\.(22[0-9]{3}|26[0-9]{3})\.[0-9]+$",
                str(system_env.get("os_build")),
            ) is None:
                raise SchemaValidationError(
                    f"{doc_path}: verified clean-system report requires a measured Windows 11 "
                    "build in system_environment.os_build"
                )
            if re.search(
                r"^[0-9]{3}\.[0-9]{2}$",
                str(system_env.get("nvidia_driver_version")),
            ) is None:
                raise SchemaValidationError(
                    f"{doc_path}: verified clean-system report requires an NNN.NN NVIDIA driver "
                    "version in system_environment.nvidia_driver_version"
                )
            if shader_ev.get("compiled_offline_with_dxc") is not True:
                raise SchemaValidationError(
                    f"{doc_path}: verified clean-system report requires compiled_offline_with_dxc=true"
                )
            if shader_ev.get("output_verified") is not True:
                raise SchemaValidationError(
                    f"{doc_path}: verified clean-system report requires output_verified=true"
                )
            if shader_ev.get("observed_output_sha256") != shader_ev.get("expected_output_sha256"):
                raise SchemaValidationError(
                    f"{doc_path}: verified clean-system report must have observed_output_sha256 == "
                    "expected_output_sha256 (observed readback bytes, never a copied placeholder)"
                )
            for runtime_key in (
                "runtime_dxcompiler_loaded",
                "runtime_d3dcompiler_loaded",
                "agility_d3d12core_redist_loaded",
            ):
                if shader_ev.get(runtime_key) is not False:
                    raise SchemaValidationError(
                        f"{doc_path}: verified clean-system report requires {runtime_key}=false"
                    )
            for run in instance.get("fixture_runs", []):
                if run.get("execution_status") != "passed" or run.get("exit_code") != 0:
                    raise SchemaValidationError(
                        f"{doc_path}: verified clean-system report requires every fixture run to pass with exit_code=0"
                    )
            if any(f.get("size_bytes") is None for f in inv.get("deployed_files", [])):
                raise SchemaValidationError(
                    f"{doc_path}: verified clean-system report must have non-null deployed_files size_bytes"
                )
            if inv.get("unexpected_sidecars_found"):
                raise SchemaValidationError(
                    f"{doc_path}: verified clean-system report must have no unexpected sidecars or payloads"
                )
            deployed_sum = sum(f["size_bytes"] for f in inv.get("deployed_files", []))
            if deployed_sum != inv.get("total_distributed_bytes"):
                raise SchemaValidationError(
                    f"{doc_path}: deployed_dependency_inventory.total_distributed_bytes ({inv.get('total_distributed_bytes')}) "
                    f"!= sum of deployed_files ({deployed_sum})"
                )

    elif schema_name == "reproducibility-report.schema.json":
        status = instance.get("verification_status")
        artifacts = instance.get("artifacts", [])
        if status == "unverified":
            if instance.get("all_artifacts_match") is not False:
                raise SchemaValidationError(
                    f"{doc_path}: unverified reproducibility report must have all_artifacts_match=false"
                )
            for a in artifacts:
                if (
                    a.get("size_bytes_a") is not None
                    or a.get("size_bytes_b") is not None
                    or a.get("sha256_a") is not None
                    or a.get("sha256_b") is not None
                    or a.get("hash_match") is not None
                ):
                    raise SchemaValidationError(
                        f"{doc_path}: unverified reproducibility report must not contain self-referential sizes or hashes for '{a.get('artifact_name')}'"
                    )
        else:
            for a in artifacts:
                if (
                    a.get("size_bytes_a") is None
                    or a.get("size_bytes_b") is None
                    or a.get("sha256_a") is None
                    or a.get("sha256_b") is None
                    or a.get("hash_match") is None
                ):
                    raise SchemaValidationError(
                        f"{doc_path}: {status} reproducibility report must have non-null sizes and hashes for '{a.get('artifact_name')}'"
                    )
            all_match = all(
                a["size_bytes_a"] == a["size_bytes_b"]
                and a["sha256_a"] == a["sha256_b"]
                and a["hash_match"] is True
                for a in artifacts
            )
            if all_match != instance.get("all_artifacts_match"):
                raise SchemaValidationError(
                    f"{doc_path}: all_artifacts_match does not agree with per-artifact hash comparisons"
                )

    elif schema_name == "experiment-record.schema.json":
        status = instance.get("verification_status")
        meas = instance.get("measurements", {})
        diag = instance.get("diagnostics", {})
        if status == "unverified":
            if (
                meas.get("total_distributed_bytes") is not None
                or meas.get("delta_vs_release_baseline_bytes") is not None
                or diag.get("exit_code") is not None
            ):
                raise SchemaValidationError(
                    f"{doc_path}: unverified experiment record must have null measurements and null exit_code"
                )

    elif schema_name == "toolchain-manifest.schema.json":
        selected_id = instance.get("selected_toolchain", {}).get("toolchain_id")
        evaluated = instance.get("evaluated_toolchains", [])
        for et in evaluated:
            tid = et.get("toolchain_id")
            et_status = et.get("verification_status")
            expected_role = "selected" if tid == selected_id else "retained_alternative"
            if et.get("status") != expected_role:
                raise SchemaValidationError(
                    f"{doc_path}: evaluated_toolchain '{tid}' status must be '{expected_role}' when selected_toolchain is '{selected_id}'"
                )
            byte_fields = (
                et.get("empty_player_bytes"),
                et.get("clear_present_bytes"),
                et.get("compute_fixture_bytes"),
            )
            if et_status == "unverified":
                if et.get("deterministic_repro_verified") is True:
                    raise SchemaValidationError(
                        f"{doc_path}: unverified evaluated_toolchain '{tid}' must not have deterministic_repro_verified=true"
                    )
                if et.get("pdb_debugging_verified") is True:
                    raise SchemaValidationError(
                        f"{doc_path}: unverified evaluated_toolchain '{tid}' must not have pdb_debugging_verified=true"
                    )
                if et.get("fixture_execution_verified") is True:
                    raise SchemaValidationError(
                        f"{doc_path}: unverified evaluated_toolchain '{tid}' must not have fixture_execution_verified=true"
                    )
            elif et_status == "verified":
                if any(v is None for v in byte_fields):
                    raise SchemaValidationError(
                        f"{doc_path}: verified evaluated_toolchain '{tid}' must have non-null byte measurements"
                    )
                if et.get("deterministic_repro_verified") is not True:
                    raise SchemaValidationError(
                        f"{doc_path}: verified evaluated_toolchain '{tid}' must have deterministic_repro_verified=true"
                    )
                if et.get("pdb_debugging_verified") is not True:
                    raise SchemaValidationError(
                        f"{doc_path}: verified evaluated_toolchain '{tid}' must have pdb_debugging_verified=true"
                    )
                if et.get("fixture_execution_verified") is not True:
                    raise SchemaValidationError(
                        f"{doc_path}: verified evaluated_toolchain '{tid}' must have fixture_execution_verified=true"
                    )

        if evaluated and all(et.get("verification_status") == "verified" for et in evaluated):
            totals = {
                et["toolchain_id"]: (
                    int(et["empty_player_bytes"])
                    + int(et["clear_present_bytes"])
                    + int(et["compute_fixture_bytes"])
                )
                for et in evaluated
            }
            min_total = min(totals.values())
            if totals.get(selected_id, min_total + 1) > min_total:
                smaller_ids = [tid for tid, tot in totals.items() if tot == min_total]
                raise SchemaValidationError(
                    f"{doc_path}: selected_toolchain '{selected_id}' ({totals.get(selected_id)} B total) "
                    f"is larger than verified alternative {smaller_ids} ({min_total} B total)"
                )


ARTIFACT_SCHEMA_MAPPINGS: list[tuple[str, str]] = [
    ("benchmarks/capability-manifest.v1.json", "schemas/capability-manifest.schema.json"),
    ("benchmarks/toolchain-manifest.v1.json", "schemas/toolchain-manifest.schema.json"),
    ("benchmarks/dependency-inventory.v1.json", "schemas/dependency-inventory.schema.json"),
    ("benchmarks/ledgers/empty-player-clang-cl-release.json", "schemas/size-ledger.schema.json"),
    ("benchmarks/ledgers/empty-player-msvc-ltcg-release.json", "schemas/size-ledger.schema.json"),
    ("benchmarks/ledgers/clear-present-clang-cl-release.json", "schemas/size-ledger.schema.json"),
    ("benchmarks/ledgers/clear-present-msvc-ltcg-release.json", "schemas/size-ledger.schema.json"),
    ("benchmarks/ledgers/compute-fixture-clang-cl-release.json", "schemas/size-ledger.schema.json"),
    ("benchmarks/ledgers/compute-fixture-msvc-ltcg-release.json", "schemas/size-ledger.schema.json"),
    ("benchmarks/hardware/rtx-4060-baseline.json", "schemas/benchmark-result.schema.json"),
    ("benchmarks/hardware/rtx-50-series-baseline.json", "schemas/benchmark-result.schema.json"),
    ("benchmarks/reproducibility/release-build-reproducibility.json", "schemas/reproducibility-report.schema.json"),
    ("benchmarks/reproducibility/msvc-ltcg-release-reproducibility.json", "schemas/reproducibility-report.schema.json"),
    ("benchmarks/clean-system/clean-system-validation.json", "schemas/clean-system-report.schema.json"),
    ("benchmarks/experiments/msvc-ltcg-comparison.json", "schemas/experiment-record.schema.json"),
    ("benchmarks/experiments/compact-profile-experiment.json", "schemas/experiment-record.schema.json"),
    ("benchmarks/experiments/crt-elimination-experiment.json", "schemas/experiment-record.schema.json"),
]


def validate_file_against_schema(doc_path: Path, schema_path: Path) -> None:
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validate_instance(doc, schema, schema, path=str(doc_path.name))
    validate_semantic_invariants(doc, schema_path.name, doc_path)


def validate_all_repository_artifacts(repo_root: Path) -> dict[str, Any]:
    results = []
    for rel_doc, rel_schema in ARTIFACT_SCHEMA_MAPPINGS:
        doc_path = repo_root / rel_doc
        schema_path = repo_root / rel_schema
        if not doc_path.exists():
            raise SchemaValidationError(f"Missing required artifact: {rel_doc}")
        if not schema_path.exists():
            raise SchemaValidationError(f"Missing required schema: {rel_schema}")
        validate_file_against_schema(doc_path, schema_path)
        results.append({"artifact": rel_doc, "schema": rel_schema, "valid": True})
    return {"validated_count": len(results), "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate NW-01 JSON artifacts against schemas.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--document", type=Path, help="Specific JSON document to validate")
    parser.add_argument("--schema", type=Path, help="Specific JSON schema to validate against")
    args = parser.parse_args(argv)

    try:
        if args.document and args.schema:
            validate_file_against_schema(args.document, args.schema)
            print(json.dumps({"artifact": str(args.document), "schema": str(args.schema), "valid": True}))
        else:
            summary = validate_all_repository_artifacts(args.repo_root)
            print(json.dumps(summary, indent=2))
        return 0
    except Exception as exc:
        print(f"[NW-SCHEMA-ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
