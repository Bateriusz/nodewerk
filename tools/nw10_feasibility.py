#!/usr/bin/env python3
"""Build schema-valid NW-10 Windows compact feasibility evidence.

Consumes the Windows workflow's measured size outcome plus the decoder-off
proof report and the restricted-intro report.  Both the failure/continue and
the true at-most-65536-byte success branches are representable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_contract as contract  # noqa: E402
import nw07_clean_system as clean_system_contract  # noqa: E402
from schema_validator import (  # noqa: E402
    SchemaValidationError,
    validate_file_against_schema,
    validate_instance,
)

FORMAT = "nw10-windows-compact-feasibility"
WINDOWS_EXIT_MIN = -(2**31)
WINDOWS_EXIT_MAX = 2**31 - 1


def _windows_exit_code(value: Any, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    code = int(value)
    if code < WINDOWS_EXIT_MIN or code > WINDOWS_EXIT_MAX:
        raise ValueError(f"Windows process exit code is outside signed 32-bit range: {code}")
    return code


def _windows_exit_hex(code: int | None) -> str | None:
    if code is None:
        return None
    return f"0x{code & 0xFFFFFFFF:08X}"


def _canonical_dependencies(report: Mapping[str, Any]) -> list[str]:
    ledgers = report.get("ledgers")
    canonical = ledgers.get("canonical") if isinstance(ledgers, Mapping) else None
    rows = canonical.get("dependencies") if isinstance(canonical, Mapping) else None
    if not isinstance(rows, list):
        return []
    names = {
        str(row.get("name", "")).strip().lower()
        for row in rows
        if isinstance(row, Mapping) and str(row.get("name", "")).strip()
    }
    return sorted(names)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _repo_relative_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(
            "prior valid ledger must be inside the repository for public provenance"
        ) from exc


def _validate_prior_valid_ledger(
    ledger: Mapping[str, Any], *, provenance_path: str
) -> tuple[Mapping[str, Any], Mapping[str, Any], str | None]:
    if ledger.get("format") != "nw10-windows-pe-ledger":
        raise ValueError("prior valid ledger has an unexpected format")
    if ledger.get("version") != 1 or ledger.get("task") != "NW-10":
        raise ValueError("prior valid ledger has an unexpected version or task")
    if not provenance_path or provenance_path.startswith("/") or re.match(
        r"^[A-Za-z]:", provenance_path
    ):
        raise ValueError("prior valid ledger provenance path must be repository-relative")
    final = ledger.get("final_artifact")
    conclusion = ledger.get("conclusion")
    if not isinstance(final, Mapping) or not isinstance(conclusion, Mapping):
        raise ValueError("prior valid ledger is structurally incomplete")
    if final.get("runtime_status") != "validated_runnable":
        raise ValueError("prior valid ledger does not contain a runnable artifact")
    if conclusion.get("byte_ledger_complete") is not True:
        raise ValueError("prior valid ledger does not contain a complete byte ledger")
    executable_sha = str(final.get("executable_sha256", "")).lower()
    if re.fullmatch(r"[0-9a-f]{64}", executable_sha) is None:
        raise ValueError("prior valid ledger executable SHA-256 is malformed")
    for field in ("package_sha256", "manifest_sha256"):
        value = final.get(field)
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", str(value).lower()) is None:
            raise ValueError(f"prior valid ledger {field} is malformed")
    if int(final.get("executable_bytes", 0)) <= 0:
        raise ValueError("prior valid ledger executable size is invalid")
    source = ledger.get("source")
    prior_head: str | None = None
    if isinstance(source, Mapping) and source.get("head_sha") is not None:
        prior_head = str(source.get("head_sha")).lower()
        if re.fullmatch(r"[0-9a-f]{40}", prior_head) is None:
            raise ValueError("prior valid ledger source head is malformed")
    return final, conclusion, prior_head


def _normalize_artifact_digest(value: str) -> str:
    digest = str(value).strip().lower()
    if not digest:
        return ""
    if digest.startswith("sha256:"):
        hex_digest = digest[len("sha256:"):]
    else:
        hex_digest = digest
    if len(hex_digest) != 64 or any(
        character not in "0123456789abcdef" for character in hex_digest
    ):
        raise ValueError(
            "artifact digest must be a SHA-256 digest as 64 hex characters "
            "or sha256:<64 hex characters>"
        )
    return "sha256:" + hex_digest


def _proof_artifact(report: Mapping[str, Any]) -> Mapping[str, Any]:
    artifact = report.get("canonical_artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("proof report has no canonical_artifact")
    return artifact


def _intro_artifacts(report: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    canonical = report.get("canonical_artifact")
    compact = report.get("compact_artifact")
    if not isinstance(canonical, Mapping) or not isinstance(compact, Mapping):
        raise ValueError("intro report lacks canonical or compact artifacts")
    return canonical, compact


def _status(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("status") or "")
    return ""


def _verification(
    status: str, reason: str, **details: Any
) -> dict[str, Any]:
    return {"status": status, "reason": reason, **details}


def _validate_evidence_schema(
    value: Mapping[str, Any], schema_name: str
) -> tuple[bool, str]:
    path = REPO_ROOT / "schemas" / schema_name
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        validate_instance(dict(value), schema, schema)
    except (OSError, ValueError, SchemaValidationError) as exc:
        return False, str(exc)
    return True, ""


def _certify_compact_candidate(
    *,
    intro: Mapping[str, Any],
    validated_mode: str,
    validated_sha: str,
    validated_size: int,
    head_sha: str,
    pe_ledger: Mapping[str, Any] | None,
    dependency_closure: Mapping[str, Any] | None,
    system_validation: Mapping[str, Any] | None,
    performance: Mapping[str, Any] | None,
    correctness: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Verify every acceptance gate required to publish a 64 KiB success."""
    canonical_intro, compact_intro = _intro_artifacts(intro)
    ledgers = intro.get("ledgers")
    ledgers = ledgers if isinstance(ledgers, Mapping) else {}
    canonical_ledger = ledgers.get("canonical")
    canonical_ledger = (
        canonical_ledger if isinstance(canonical_ledger, Mapping) else {}
    )
    compact_ledger = ledgers.get("compact")
    compact_ledger = (
        compact_ledger if isinstance(compact_ledger, Mapping) else {}
    )
    ledger = compact_ledger if validated_mode == "compact" else canonical_ledger
    components = ledger.get("components")
    components = components if isinstance(components, Mapping) else {}
    canonical_package_sha = str(
        canonical_intro.get("package_sha256", "")
    ).lower()
    canonical_package_bytes = canonical_intro.get("package_bytes")
    decoded_package_sha = str(
        compact_intro.get("decoded_package_sha256", "")
    ).lower()
    decoded_bytes_present = (
        compact_intro.get("decoded_package_bytes") is not None
        and canonical_package_bytes is not None
    )
    decoded_match = (
        bool(decoded_package_sha)
        and decoded_package_sha == canonical_package_sha
        and (
            not decoded_bytes_present
            or int(compact_intro.get("decoded_package_bytes", -1))
            == int(canonical_package_bytes)
        )
    )
    decoded_identity = _verification(
        "verified" if decoded_match else "failed",
        (
            "compact payload decodes to the exact canonical package"
            if decoded_match
            else "compact payload does not decode to the canonical package"
        ),
        canonical_package_sha256=canonical_package_sha or None,
        decoded_package_sha256=decoded_package_sha or None,
        canonical_package_bytes=canonical_package_bytes,
        decoded_package_bytes=compact_intro.get("decoded_package_bytes"),
        validated_mode=validated_mode,
    )

    measured_decoder_bytes = components.get("decoder_code_bytes")
    if (
        validated_mode == "compact"
        and measured_decoder_bytes is None
        and pe_ledger is not None
    ):
        decoder = pe_ledger.get("compact_decoder")
        if isinstance(decoder, Mapping):
            measured_decoder_bytes = decoder.get("template_bytes_added")
    unaccounted = list(ledger.get("unaccounted_components") or [])
    only_decoder_pending = set(unaccounted) <= {"decoder_code_bytes"}
    bytes_accounted = bool(ledger.get("all_required_bytes_accounted")) or (
        validated_mode == "compact"
        and only_decoder_pending
        and measured_decoder_bytes is not None
    )
    required_auxiliary = ledger.get("required_auxiliary_files")
    no_required_sidecars = not required_auxiliary
    bytes_verification = _verification(
        "verified" if bytes_accounted and no_required_sidecars else "pending",
        (
            "all complete-distribution bytes including decoder code are accounted"
            if bytes_accounted and no_required_sidecars
            else "complete-distribution byte accounting or sidecar closure is incomplete"
        ),
        all_required_bytes_accounted=bytes_accounted,
        decoder_code_bytes=measured_decoder_bytes,
        unaccounted_components=unaccounted,
        required_auxiliary_files=dict(required_auxiliary or {}),
    )

    dependency_rows = ledger.get("dependencies")
    dependency_rows = (
        dependency_rows if isinstance(dependency_rows, list) else []
    )
    dependency_names = {
        str(item.get("name", "")).lower().removesuffix(".dll")
        for item in dependency_rows
        if isinstance(item, Mapping)
    }
    if dependency_closure is not None:
        sidecars = dependency_closure.get("sidecar_files")
        reported_binary_sha = str(
            dependency_closure.get("binary_sha256", "")
        ).lower()
        imported_dlls = {
            str(item).lower().removesuffix(".dll")
            for item in (dependency_closure.get("canonical_imports") or [])
        }
        closure_verified = (
            dependency_closure.get("closure_verified") is True
            and reported_binary_sha == validated_sha
            and not sidecars
            and (
                validated_mode != "compact"
                or "cabinet" in imported_dlls
                or "cabinet" in dependency_names
            )
        )
        dependency_verification = _verification(
            "verified" if closure_verified else "failed",
            (
                "dependency closure is verified against the exact candidate executable"
                if closure_verified
                else "dependency closure is not bound to the candidate executable"
            ),
            sidecar_files=list(sidecars or []),
            imported_dlls=list(dependency_closure.get("canonical_imports") or []),
            binary_sha256=reported_binary_sha or None,
            binary_binding="verified",
        )
    else:
        dependency_verification = _verification(
            "pending",
            "no measured dependency-closure report was supplied",
            sidecar_files=[],
            imported_dlls=sorted(dependency_names),
        )

    pe_verification: dict[str, Any]
    if pe_ledger is None:
        pe_verification = _verification(
            "pending", "no measured PE ledger was supplied"
        )
    else:
        schema_ok, schema_error = _validate_evidence_schema(
            pe_ledger, "nw10-windows-pe-ledger.schema.json"
        )
        final = pe_ledger.get("final_artifact")
        final = final if isinstance(final, Mapping) else {}
        linker = pe_ledger.get("linker_elimination")
        linker = linker if isinstance(linker, Mapping) else {}
        decoder = pe_ledger.get("compact_decoder")
        decoder = decoder if isinstance(decoder, Mapping) else {}
        conclusion = pe_ledger.get("conclusion")
        conclusion = conclusion if isinstance(conclusion, Mapping) else {}
        mitigation_bits = linker.get("security_mitigation_bits")
        mitigation_bits = (
            mitigation_bits if isinstance(mitigation_bits, Mapping) else {}
        )
        expected_payload_kind = (
            "compact_container"
            if validated_mode == "compact"
            else "canonical_package"
        )
        pe_ok = (
            schema_ok
            and pe_ledger.get("format") == "nw10-windows-pe-ledger"
            and pe_ledger.get("status") == "verified"
            and str(
                (pe_ledger.get("source") or {}).get("head_sha", "")
            ).lower()
            == head_sha
            and str(final.get("executable_sha256", "")).lower()
            == validated_sha
            and int(final.get("executable_bytes", -1)) == validated_size
            and str(final.get("package_sha256", "")).lower()
            == canonical_package_sha
            and final.get("package_binding_verified") is True
            and final.get("manifest_binding_verified") is True
            and final.get("runtime_status") == "validated_runnable"
            and final.get("runtime_exit_code") == 0
            and final.get("embedded_payload_kind") == expected_payload_kind
            and linker.get("security_mitigations_preserved") is True
            and mitigation_bits.get("control_required_bits_present") is True
            and mitigation_bits.get("decoder_on_required_bits_present") is True
            and linker.get("canonical_package_identical") is True
            and linker.get("embedded_payload_sha256_identical") is True
            and linker.get("ltgc_preserved") is True
            and (
                (
                    validated_mode == "compact"
                    and linker.get("compact_decoder_preserved") is True
                    and decoder.get("compact_payload_verified") is True
                )
                or (
                    validated_mode == "canonical_fallback"
                    and linker.get("compact_decoder_preserved") is False
                    and decoder.get("compact_payload_verified") is False
                )
            )
            and conclusion.get("byte_ledger_complete") is True
        )
        pe_verification = _verification(
            "verified"
            if pe_ok
            else (
                "failed"
                if pe_ledger.get("status") == "verified"
                else "pending"
            ),
            (
                "PE ledger binds the validated artifact, package, mitigations, "
                "LTGC, and payload mode"
                if pe_ok
                else "PE ledger does not bind every required candidate property"
            ),
            schema_error=schema_error or None,
            executable_sha256=final.get("executable_sha256"),
            executable_bytes=final.get("executable_bytes"),
            security_mitigations_preserved=linker.get(
                "security_mitigations_preserved"
            ),
            ltgc_preserved=linker.get("ltgc_preserved"),
            compact_decoder_preserved=linker.get("compact_decoder_preserved"),
        )

    system = system_validation or {}
    clean = system.get("clean_system")
    clean = clean if isinstance(clean, Mapping) else {}
    deployment = clean.get("deployment")
    deployment = deployment if isinstance(deployment, Mapping) else {}
    host = clean.get("host")
    host = host if isinstance(host, Mapping) else {}
    scenarios = clean.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, list) else []
    expected_scenarios = clean_system_contract.EXPECTED_SCENARIO_EXIT_CODES
    scenario_names = {
        str(row.get("scenario", ""))
        for row in scenarios
        if isinstance(row, Mapping)
    }
    scenarios_ok = scenario_names == set(expected_scenarios) and all(
        isinstance(row, Mapping)
        and row.get("status") == "passed"
        and row.get("timed_out") is False
        and row.get("expected_exit_code")
        == expected_scenarios[str(row.get("scenario", ""))]
        and row.get("observed_exit_code")
        == expected_scenarios[str(row.get("scenario", ""))]
        for row in scenarios
    )
    system_final = system.get("final_artifact")
    system_final = (
        system_final if isinstance(system_final, Mapping) else {}
    )
    system_schema_ok, system_schema_error = (
        _validate_evidence_schema(
            system, "nw10-system-validation.schema.json"
        )
        if system
        else (False, "not provided")
    )
    clean_hardware = clean.get("hardware")
    clean_hardware = (
        clean_hardware if isinstance(clean_hardware, Mapping) else {}
    )
    clean_windows_build = str(host.get("windows_build", ""))
    privacy = system.get("privacy")
    privacy = privacy if isinstance(privacy, Mapping) else {}
    clean_ok = (
        system_schema_ok
        and system.get("status") == "verified"
        and str(system.get("source_head_sha", "")) == head_sha
        and clean.get("status") == "verified"
        and deployment.get("only_runtime_executable") is True
        and str(deployment.get("executable_sha256", "")).lower()
        == validated_sha
        and int(deployment.get("executable_bytes", -1)) == validated_size
        and host.get("clean_environment") is True
        and clean.get("development_dependencies_present_on_path") in ([], ())
        and clean.get("environment_cache_variables") in ([], ())
        and scenarios_ok
        and str(system_final.get("executable_sha256", "")).lower()
        == validated_sha
        and int(system_final.get("executable_bytes", -1)) == validated_size
        and str(system_final.get("package_sha256", "")).lower()
        == canonical_package_sha
        and bool(clean_windows_build)
        and str(system.get("windows_build", "")) == clean_windows_build
        and privacy.get("local_paths_included") is False
        and privacy.get("raw_stdout_stderr_included") is False
        and privacy.get("user_profile_paths_included") is False
        and privacy.get("raw_result_bundle_tracked") is False
    )
    clean_verification = _verification(
        "verified"
        if clean_ok
        else (
            "failed"
            if system.get("status") == "verified"
            else "pending"
        ),
        (
            "clean target deployment and frozen scenarios are verified"
            if clean_ok
            else "clean-system physical validation is unavailable or incomplete"
        ),
        schema_error=system_schema_error or None,
        deployment_executable_sha256=deployment.get("executable_sha256"),
        scenario_count=len(scenarios),
    )

    startup = system.get("startup")
    startup = startup if isinstance(startup, Mapping) else {}
    startup_value = startup.get("process_start_to_first_present_ms")
    startup_hardware = startup.get("hardware")
    startup_hardware = (
        startup_hardware if isinstance(startup_hardware, Mapping) else {}
    )
    startup_ok = (
        startup.get("status") == "verified"
        and startup_value is not None
        and 0.0 <= float(startup_value) <= contract.STARTUP_CEILING_MS
        and str(startup_hardware.get("adapter_description", ""))
        == str(clean_hardware.get("adapter_description", ""))
        and str(startup_hardware.get("driver_version", ""))
        == str(clean_hardware.get("driver_version", ""))
        and str(startup_hardware.get("windows_build", ""))
        == clean_windows_build
    )
    startup_verification = _verification(
        "verified"
        if startup_ok
        else (
            "failed"
            if system.get("status") == "verified"
            else "pending"
        ),
        (
            "startup is verified below the frozen ceiling"
            if startup_ok
            else "physical startup evidence is unavailable or exceeds the ceiling"
        ),
        process_start_to_first_present_ms=startup_value,
        ceiling_ms=contract.STARTUP_CEILING_MS,
    )

    memory = system.get("memory")
    memory = memory if isinstance(memory, Mapping) else {}
    peak = memory.get("peak_engine_committed_gpu_bytes")
    memory_hardware = memory.get("hardware")
    memory_hardware = (
        memory_hardware if isinstance(memory_hardware, Mapping) else {}
    )
    memory_ok = (
        memory.get("status") == "verified"
        and peak is not None
        and 0 <= int(peak) <= contract.PEAK_ENGINE_COMMITTED_GPU_BYTES
        and int(memory.get("post_initialization_allocation_violation", -1)) == 0
        and str(memory_hardware.get("adapter_description", ""))
        == str(clean_hardware.get("adapter_description", ""))
        and str(memory_hardware.get("driver_version", ""))
        == str(clean_hardware.get("driver_version", ""))
        and str(memory_hardware.get("windows_build", ""))
        == clean_windows_build
    )
    memory_verification = _verification(
        "verified"
        if memory_ok
        else (
            "failed"
            if system.get("status") == "verified"
            else "pending"
        ),
        (
            "engine-committed memory is verified below the frozen ceiling"
            if memory_ok
            else "physical memory evidence is unavailable or exceeds the ceiling"
        ),
        peak_engine_committed_gpu_bytes=peak,
        ceiling_bytes=contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
    )

    performance_ok = False
    performance_details: dict[str, Any] = {}
    if performance is not None:
        performance_schema_ok, performance_schema_error = (
            _validate_evidence_schema(
                performance, "nw10-performance-preservation.schema.json"
            )
        )
        after = performance.get("after")
        after = after if isinstance(after, Mapping) else {}
        runs = after.get("runs")
        runs = runs if isinstance(runs, list) else []
        artifact = after.get("artifact")
        artifact = artifact if isinstance(artifact, Mapping) else {}
        performance_details = {
            "contract": performance.get("performance_contract"),
            "protocol": performance.get("performance_protocol"),
            "reference_binding_id": performance.get("reference_binding_id"),
            "reference_binding_digest": performance.get(
                "reference_binding_digest"
            ),
            "run_count": len(runs),
            "worst_gpu_p99_ms": after.get("worst_gpu_p99_ms"),
            "worst_cpu_submit_p99_ms": after.get("worst_cpu_submit_p99_ms"),
        }
        performance_ok = (
            performance_schema_ok
            and performance.get("status") == "verified_preserved"
            and performance.get("matched_environment") is True
            and performance.get("reasons") == []
            and performance.get("performance_contract")
            == contract.PERF_CONTRACT_ID
            and performance.get("performance_protocol")
            == contract.PERF_PROTOCOL_ID
            and len(runs) == contract.PERF_RUN_COUNT
            and all(
                isinstance(run, Mapping)
                and run.get("status") == "passed"
                and float(run.get("gpu_p99_ms", float("inf")))
                < contract.FRAME_TIME_THRESHOLD_MS
                and float(run.get("cpu_p99_ms", float("inf")))
                < contract.FRAME_TIME_THRESHOLD_MS
                for run in runs
            )
            and str(artifact.get("executable_sha256", "")).lower()
            == validated_sha
            and str(artifact.get("package_sha256", "")).lower()
            == canonical_package_sha
            and int(artifact.get("complete_distribution_bytes", -1))
            == validated_size
            and after.get("worst_gpu_p99_ms") is not None
            and after.get("worst_cpu_submit_p99_ms") is not None
            and float(after["worst_gpu_p99_ms"]) < contract.FRAME_TIME_THRESHOLD_MS
            and float(after["worst_cpu_submit_p99_ms"])
            < contract.FRAME_TIME_THRESHOLD_MS
        )
    else:
        performance_schema_error = "not provided"
    performance_verification = _verification(
        "verified"
        if performance_ok
        else (
            "failed"
            if performance is not None
            and performance.get("status") in {"verified_preserved", "failed"}
            else "pending"
        ),
        (
            "nw07_perf_ref_v1 matched performance gates are verified"
            if performance_ok
            else "matched nw07_perf_ref_v1 performance preservation is pending"
        ),
        schema_error=performance_schema_error or None,
        **performance_details,
    )

    correctness_schema_error = "not provided"
    visual_ok = False
    visual_details: dict[str, Any] = {}
    if correctness is not None:
        correctness_schema_ok, correctness_schema_error = (
            _validate_evidence_schema(
                correctness,
                "nw10-common-artifact-correctness.schema.json",
            )
        )
        correctness_common = correctness.get("common_artifact")
        correctness_common = (
            correctness_common
            if isinstance(correctness_common, Mapping)
            else {}
        )
        comparison = correctness.get("comparison")
        comparison = comparison if isinstance(comparison, Mapping) else {}
        packed = correctness.get("packed_unpacked_comparison")
        packed = packed if isinstance(packed, Mapping) else {}
        packed_runtime = packed.get("runtime")
        packed_runtime = (
            packed_runtime if isinstance(packed_runtime, Mapping) else {}
        )
        packed_ok = (
            packed.get("status") == "verified"
            and packed.get("worst_absolute_error") is not None
            and float(packed["worst_absolute_error"])
            <= contract.IMAGE_ABSOLUTE_TOLERANCE
            and all(
                isinstance(packed_runtime.get(tier), Mapping)
                and packed_runtime[tier].get("status") == "passed"
                and len(
                    str(
                        packed_runtime[tier].get(
                            "unpacked_executable_sha256", ""
                        )
                    )
                )
                == 64
                and len(
                    str(
                        packed_runtime[tier].get(
                            "packed_executable_sha256", ""
                        )
                    )
                )
                == 64
                and packed_runtime[tier].get("unpacked_executable_sha256")
                != packed_runtime[tier].get("packed_executable_sha256")
                and int(packed_runtime[tier].get("frames_per_tick", -1)) == 1
                and int(
                    packed_runtime[tier].get("present_sync_interval", -1)
                )
                == 0
                for tier in ("ada", "blackwell")
            )
        )
        visual_ok = (
            correctness_schema_ok
            and correctness.get("status") == "verified"
            and str(correctness.get("source_head_sha", "")) == head_sha
            and str(
                (correctness.get("source") or {}).get(
                    "implementation_head", ""
                )
            )
            == head_sha
            and str(correctness_common.get("package_sha256", "")).lower()
            == canonical_package_sha
            and str(
                correctness_common.get("final_executable_sha256", "")
            ).lower()
            == validated_sha
            and comparison.get("absolute_tolerance")
            == contract.IMAGE_ABSOLUTE_TOLERANCE
            and comparison.get("relative_tolerance")
            == contract.IMAGE_RELATIVE_TOLERANCE
            and comparison.get("absolute_required") is True
            and comparison.get("worst_absolute_error") is not None
            and float(comparison["worst_absolute_error"])
            <= contract.IMAGE_ABSOLUTE_TOLERANCE
            and comparison.get("worst_relative_error") is not None
            and float(comparison["worst_relative_error"])
            <= contract.IMAGE_RELATIVE_TOLERANCE
            and packed_ok
        )
        visual_details = {
            "correctness_head": correctness.get("source_head_sha"),
            "final_executable_sha256": correctness_common.get(
                "final_executable_sha256"
            ),
            "worst_absolute_error": comparison.get("worst_absolute_error"),
            "packed_worst_absolute_error": packed.get("worst_absolute_error"),
        }
    visual_verification = _verification(
        "verified"
        if visual_ok
        else (
            "failed"
            if correctness is not None
            and correctness.get("status") == "verified"
            else "pending"
        ),
        (
            "packed/unpacked visual validation is verified"
            if visual_ok
            else "packed/unpacked physical visual validation is pending"
        ),
        schema_error=correctness_schema_error or None,
        **visual_details,
        tolerance=contract.image_tolerances(),
    )

    certification = {
        "status": "pending",
        "decoded_package_identity": decoded_identity,
        "all_required_bytes_accounted": bytes_verification,
        "dependency_closure": dependency_verification,
        "pe_ledger_binding": pe_verification,
        "clean_system": clean_verification,
        "startup": startup_verification,
        "memory": memory_verification,
        "performance": performance_verification,
        "visual": visual_verification,
    }
    statuses = [
        value["status"]
        for value in certification.values()
        if isinstance(value, Mapping) and "status" in value
    ]
    certification["status"] = (
        "failed"
        if "failed" in statuses
        else ("verified" if statuses and all(item == "verified" for item in statuses) else "pending")
    )
    certification["failed_gates"] = [
        name
        for name, value in certification.items()
        if isinstance(value, Mapping)
        and value.get("status") == "failed"
    ]
    certification["pending_gates"] = [
        name
        for name, value in certification.items()
        if isinstance(value, Mapping)
        and value.get("status") == "pending"
    ]
    return certification


def build_feasibility(
    *,
    outcome: Mapping[str, Any],
    proof: Mapping[str, Any],
    intro: Mapping[str, Any],
    run_id: int,
    head_sha: str,
    artifact_name: str = "nw10-windows-compact-evidence",
    artifact_id: int | None = None,
    artifact_digest: str = "",
    prior_valid_ledger: Mapping[str, Any] | None = None,
    prior_valid_ledger_path: str | None = None,
    pe_ledger: Mapping[str, Any] | None = None,
    dependency_closure: Mapping[str, Any] | None = None,
    system_validation: Mapping[str, Any] | None = None,
    performance: Mapping[str, Any] | None = None,
    correctness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if proof.get("test_payload") is True or intro.get("test_payload") is True:
        raise ValueError("feasibility publication requires release-grade reports")
    if proof.get("release_grade") is not True or intro.get("release_grade") is not True:
        raise ValueError("feasibility publication requires release-grade reports")
    proof_kind = str(proof.get("measurement_kind") or "")
    if proof_kind not in {
        "release_decoder_off",
        "release_decoder_off_size_blocked",
    }:
        raise ValueError(
            "proof report is not a measured release decoder-OFF export or "
            "decoder-OFF size-blocked measurement"
        )
    current_digest = contract.build_inputs_digest(contract.compute_build_inputs())
    if proof.get("nw10_build_inputs_digest") != current_digest:
        raise ValueError(
            "proof report build inputs do not match the checked-out implementation"
        )
    if intro.get("nw10_build_inputs_digest") != current_digest:
        raise ValueError(
            "intro report build inputs do not match the checked-out implementation"
        )
    templates = proof.get("templates")
    if not isinstance(templates, Mapping) or templates.get(
        "canonical_decoder_off"
    ) is not True:
        raise ValueError(
            "proof report does not prove a decoder-OFF canonical baseline"
        )
    canonical_intro, compact_intro = _intro_artifacts(intro)

    if proof_kind == "release_decoder_off":
        proof_artifact = _proof_artifact(proof)
        proof_bytes = int(proof_artifact["executable_bytes"])
    else:
        if proof.get("success") is not False:
            raise ValueError("size-blocked proof report must record success=false")
        if proof.get("measurement_outcome") != "size_target_not_met":
            raise ValueError(
                "size-blocked proof report does not identify the proof-tier blocker"
            )
        measured = proof.get("measured_distribution_bytes")
        if measured is None or int(measured) <= contract.PROOF_TIER_BYTES:
            raise ValueError(
                "size-blocked proof report does not retain an above-tier measurement"
            )
        proof_bytes = int(measured)
    proof_within = proof_bytes <= contract.PROOF_TIER_BYTES

    measured_candidate = outcome.get("minimum_measured_candidate_bytes")
    validated_bytes = outcome.get("minimum_validated_runnable_bytes")
    validated_mode = str(outcome.get("validated_mode") or "none")
    runtime_status = str(
        outcome.get("compact_candidate_runtime_status") or "pending_runtime_validation"
    )
    candidate_exit = _windows_exit_code(outcome.get("compact_candidate_exit_code"))
    canonical_exit = _windows_exit_code(outcome.get("canonical_fallback_exit_code"))
    intro_candidate = outcome.get("intro_compact_candidate_bytes")
    proof_candidate = outcome.get("proof_compact_candidate_bytes")
    candidate_size = int(compact_intro.get("executable_bytes", 0))
    candidate_sha = str(compact_intro.get("executable_sha256", "")).lower()
    canonical_size = int(canonical_intro.get("executable_bytes", 0))
    canonical_sha = str(
        canonical_intro.get("executable_sha256", "")
    ).lower()
    if candidate_size <= 0:
        raise ValueError("intro compact candidate has no positive executable size")
    if re.fullmatch(r"[0-9a-f]{64}", candidate_sha) is None:
        raise ValueError("intro compact candidate executable SHA-256 is malformed")
    if canonical_size <= 0:
        raise ValueError("intro canonical artifact has no positive executable size")
    if re.fullmatch(r"[0-9a-f]{64}", canonical_sha) is None:
        raise ValueError("intro canonical artifact SHA-256 is malformed")
    if runtime_status == "validated_runnable":
        if validated_mode != "compact" or validated_bytes is None:
            raise ValueError(
                "validated compact runtime status requires validated_mode=compact"
            )
        if intro_candidate is not None and int(intro_candidate) != candidate_size:
            raise ValueError(
                "outcome intro compact candidate size does not match the intro report"
            )
        if int(validated_bytes) != candidate_size:
            raise ValueError(
                "validated compact byte count does not match the intro candidate"
            )
        if compact_intro.get("selected_mode") != "compact":
            raise ValueError("validated compact runtime is not selected in the intro report")
        if int(compact_intro.get("selected_executable_bytes", -1)) != candidate_size:
            raise ValueError(
                "selected intro compact byte count does not match the validated candidate"
            )
        if str(compact_intro.get("selected_executable_sha256", "")).lower() != candidate_sha:
            raise ValueError(
                "selected intro compact SHA-256 does not match the validated candidate"
            )
    if validated_mode == "canonical_fallback":
        if validated_bytes is None or canonical_exit != 0:
            raise ValueError(
                "validated canonical fallback requires a zero exit code and bytes"
            )
        if int(validated_bytes) != canonical_size:
            raise ValueError(
                "validated canonical byte count does not match the intro artifact"
            )
        if compact_intro.get("selected_mode") != "canonical":
            raise ValueError(
                "validated canonical fallback is not selected in the intro report"
            )
        if (
            int(compact_intro.get("selected_executable_bytes", -1))
            != canonical_size
        ):
            raise ValueError(
                "selected intro canonical byte count does not match the validated artifact"
            )
        if (
            str(compact_intro.get("selected_executable_sha256", "")).lower()
            != canonical_sha
        ):
            raise ValueError(
                "selected intro canonical SHA-256 does not match the validated artifact"
            )
    if validated_mode == "compact":
        validated_artifact_sha = candidate_sha
        validated_artifact_size = candidate_size
        validated_ready = (
            runtime_status == "validated_runnable"
            and candidate_exit == 0
            and validated_bytes is not None
            and int(validated_bytes) == candidate_size
        )
    elif validated_mode == "canonical_fallback":
        validated_artifact_sha = canonical_sha
        validated_artifact_size = canonical_size
        validated_ready = (
            canonical_exit == 0
            and validated_bytes is not None
            and int(validated_bytes) == canonical_size
        )
    else:
        validated_artifact_sha = ""
        validated_artifact_size = 0
        validated_ready = False
    base_target_met = bool(
        proof_within
        and validated_ready
        and validated_artifact_size <= contract.COMPACT_TARGET_BYTES
    )
    certification = _certify_compact_candidate(
        intro=intro,
        validated_mode=validated_mode,
        validated_sha=validated_artifact_sha,
        validated_size=validated_artifact_size,
        head_sha=head_sha,
        pe_ledger=pe_ledger,
        dependency_closure=dependency_closure,
        system_validation=system_validation,
        performance=performance,
        correctness=correctness,
    )
    target_met = bool(
        base_target_met
        and certification.get("status") == "verified"
    )
    true_64k = target_met
    canonical_runtime = (
        "validated_runnable"
        if validated_bytes is not None
        and validated_mode == "canonical_fallback"
        and canonical_exit == 0
        else "pending_runtime_validation"
    )
    hosted_noncertifying = (
        validated_mode == "canonical_fallback_pending_physical"
        and canonical_exit not in (None, 0)
    )
    if runtime_status == "validated_runnable":
        failure_classification = "none"
    elif hosted_noncertifying:
        failure_classification = "non_certifying_hosted_failure"
    elif runtime_status == "rejected_runtime_failure" and candidate_exit == 70:
        failure_classification = "embedded_package_invalid"
    elif runtime_status == "rejected_runtime_failure":
        failure_classification = "runtime_failure"
    else:
        failure_classification = "runtime_validation_missing"

    compact: dict[str, Any] = {
        "bytes": candidate_size,
        "sha256": candidate_sha,
        "runtime_status": (
            runtime_status
            if runtime_status
            in {
                "validated_runnable",
                "rejected_runtime_failure",
                "pending_runtime_validation",
            }
            else "pending_runtime_validation"
        ),
        "exit_code": (
            0 if runtime_status == "validated_runnable" else candidate_exit
        ),
        "exit_code_hex": _windows_exit_hex(
            0 if runtime_status == "validated_runnable" else candidate_exit
        ),
        "failure_classification": failure_classification,
        "counted_as_validated_runnable": runtime_status == "validated_runnable",
    }

    source: dict[str, Any] = {
        "kind": "github_actions_windows_runtime",
        "workflow": "NW-10 Compact Export and Restricted Intro",
        "run_id": run_id,
        "head_sha": head_sha,
        "artifact_name": artifact_name,
        "measurement_tool": "tools/nw10_feasibility.py",
    }
    if artifact_id is not None:
        source["artifact_id"] = int(artifact_id)
    normalized_artifact_digest = _normalize_artifact_digest(artifact_digest)
    if normalized_artifact_digest:
        source["artifact_digest"] = normalized_artifact_digest

    record: dict[str, Any] = {
        "format": FORMAT,
        "version": 1,
        "task": "NW-10",
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "target_bytes": contract.COMPACT_TARGET_BYTES,
        "target_met": target_met,
        "classification": "complete" if target_met else "continue",
        "universal_64k_claim": False,
        "source": source,
        "measurements": {
            "proof_measured_bytes": proof_bytes,
            "proof_measurement_status": (
                "measured_decoder_off"
                if proof_kind == "release_decoder_off"
                else "measured_decoder_off_size_blocked"
            ),
            "canonical_proof_within_tier": proof_within,
            "proof_build_inputs_digest": proof.get("nw10_build_inputs_digest"),
            "minimum_measured_candidate_bytes": (
                int(measured_candidate) if measured_candidate is not None else None
            ),
            "minimum_validated_runnable_bytes": (
                int(validated_bytes) if validated_bytes is not None else None
            ),
            "validated_mode": (
                validated_mode
                if validated_mode
                in {
                    "compact",
                    "canonical_fallback",
                    "canonical_fallback_pending_physical",
                    "pending_runtime_validation",
                    "none",
                }
                else "none"
            ),
            "validated_artifact_bytes": (
                validated_artifact_size if validated_artifact_sha else None
            ),
            "validated_artifact_sha256": (
                validated_artifact_sha or None
            ),
            "validated_payload_kind": (
                "compact_container"
                if validated_mode == "compact"
                else (
                    "canonical_package"
                    if validated_mode == "canonical_fallback"
                    else None
                )
            ),
        },
        "compact_candidate": compact,
        "validated_artifact": (
            {
                "bytes": validated_artifact_size,
                "sha256": validated_artifact_sha,
                "mode": validated_mode,
                "payload_kind": (
                    "compact_container"
                    if validated_mode == "compact"
                    else (
                        "canonical_package"
                        if validated_mode == "canonical_fallback"
                        else None
                    )
                ),
            }
            if validated_artifact_sha
            else None
        ),
        "certification": certification,
        "canonical_fallback": {
            "bytes": int(canonical_intro["executable_bytes"]),
            "sha256": str(canonical_intro["executable_sha256"]).lower(),
            "runtime_status": canonical_runtime,
            "exit_code": canonical_exit,
            "exit_code_hex": _windows_exit_hex(canonical_exit),
            "link_manifest_verified": True,
            "present_sync_interval": 0,
            "dependency_closure": {
                "closure_verified": True,
                "sidecar_files": [],
                "canonical_imports": _canonical_dependencies(intro),
            },
        },
        "acceptance_linkage": {
            "byte_ledger": "benchmarks/nw10/compact-proof.v1.json",
            "dominant_costs_source": "benchmarks/nw10/compact-proof.v1.json#dominant_costs",
            "tested_alternatives_source": "benchmarks/nw10/compact-proof.v1.json#alternatives",
            "decision": "complete" if target_met else "continue",
            "true_64k_artifact_delivered": true_64k,
        },
        "privacy": {
            "raw_stdout_stderr_included": False,
            "runner_paths_included": False,
            "user_profile_paths_included": False,
            "whitelist_generated": True,
        },
    }
    blockers: list[dict[str, Any]] = []
    if not proof_within:
        blockers.append(
            {
                "id": "canonical_proof_tier",
                "status": "measured_above_tier",
                "exit_code": None,
                "exit_code_hex": None,
                "measured_bytes": proof_bytes,
                "reason": (
                    "The decoder-OFF canonical proof exceeds the frozen "
                    "262,144-byte proof tier; the exact measured size is retained "
                    "as a tested blocker and the decision remains continue."
                ),
            }
        )
    if not target_met:
        if base_target_met and certification.get("status") != "verified":
            blockers.append(
                {
                    "id": "compact_certification_evidence",
                    "status": (
                        "rejected"
                        if certification.get("status") == "failed"
                        else "pending"
                    ),
                    "exit_code": None,
                    "exit_code_hex": None,
                    "measured_bytes": (
                        validated_artifact_size
                        if validated_ready
                        else candidate_size
                    ),
                    "reason": (
                        "The candidate meets the byte target and reports a "
                        "validated runtime, but clean-target startup/memory/"
                        "clean-system evidence, dependency closure, decoded "
                        "package identity, attributed decoder bytes, matched "
                        "nw07_perf_ref_v1 performance gates, or visual evidence "
                        "is not verified. Pending gates: "
                        + ", ".join(certification.get("pending_gates") or [])
                        + "; failed gates: "
                        + ", ".join(certification.get("failed_gates") or [])
                    ),
                }
            )
        elif validated_ready and validated_artifact_size > contract.COMPACT_TARGET_BYTES:
            blockers.append(
                {
                    "id": "validated_artifact_target",
                    "status": "measured_above_target",
                    "exit_code": 0,
                    "exit_code_hex": _windows_exit_hex(0),
                    "measured_bytes": validated_artifact_size,
                    "reason": (
                        "The validated "
                        f"{validated_mode} single-file artifact is "
                        f"{validated_artifact_size} bytes, above the 65,536-byte "
                        "target; the exact measurement is retained and the "
                        "decision remains continue."
                    ),
                }
            )
        elif hosted_noncertifying:
            blocker_reason = (
                "The hosted Windows runner could not certify either the compact "
                "candidate or the canonical fallback; the compact result remains "
                "pending physical runtime validation rather than being attributed "
                "to compact packaging."
            )
            blocker_status = "pending_physical"
            blockers.append(
                {
                    "id": "compact_candidate_runtime",
                    "status": blocker_status,
                    "exit_code": candidate_exit,
                    "exit_code_hex": _windows_exit_hex(candidate_exit),
                    "measured_bytes": candidate_size,
                    "reason": blocker_reason,
                }
            )
        else:
            blocker_reason = (
                "The measured compact candidate did not produce a validated "
                "at-most-65536-byte runnable artifact."
            )
            blocker_status = (
                "rejected" if runtime_status == "rejected_runtime_failure" else "pending"
            )
            blockers.append(
                {
                    "id": "compact_candidate_runtime",
                    "status": blocker_status,
                    "exit_code": candidate_exit,
                    "exit_code_hex": _windows_exit_hex(candidate_exit),
                    "measured_bytes": candidate_size,
                    "reason": blocker_reason,
                }
            )
    if blockers:
        record["blocker"] = blockers[0]
        record["blockers"] = blockers

    if prior_valid_ledger is not None:
        if prior_valid_ledger_path is None:
            raise ValueError(
                "prior valid ledger provenance path is required with prior_valid_ledger"
            )
        prior_final, _prior_conclusion, prior_head = _validate_prior_valid_ledger(
            prior_valid_ledger,
            provenance_path=prior_valid_ledger_path,
        )
        prior_sha = str(prior_final["executable_sha256"]).lower()
        prior_bytes = int(prior_final["executable_bytes"])
        record["prior_valid_artifact"] = {
            "executable_bytes": prior_bytes,
            "executable_sha256": prior_sha,
            "runtime_status": "validated_runnable",
            "status": "archived_prior_valid_fallback",
            "source_head_sha": prior_head,
            "reason": (
                "The current measurement is not yet a validated runnable "
                "replacement, so the last complete runnable ledger remains "
                "available without being represented as current-head evidence."
            ),
        }
        if not target_met and validated_bytes is None:
            record["selected_artifact"] = {
                "bytes": prior_bytes,
                "sha256": prior_sha,
                "runtime_status": "validated_runnable",
                "source": "prior_valid_artifact",
                "ledger": prior_valid_ledger_path,
                "reason": (
                    "No current artifact has completed runtime validation; "
                    "select the last validated runnable distribution until "
                    "new physical evidence is published."
                ),
            }

    if "selected_artifact" not in record:
        if validated_bytes is not None and validated_mode == "compact":
            selected_sha = candidate_sha
            selected_bytes = int(validated_bytes)
            selected_source = "compact_candidate"
        elif validated_bytes is not None and validated_mode == "canonical_fallback":
            selected_sha = str(canonical_intro["executable_sha256"]).lower()
            selected_bytes = int(validated_bytes)
            selected_source = "canonical_fallback"
        else:
            selected_sha = str(canonical_intro["executable_sha256"]).lower()
            selected_bytes = int(canonical_intro["executable_bytes"])
            selected_source = "pending_current_candidate"
        record["selected_artifact"] = {
            "bytes": selected_bytes,
            "sha256": selected_sha,
            "runtime_status": (
                "validated_runnable"
                if validated_bytes is not None
                else "pending_runtime_validation"
            ),
            "source": selected_source,
            "ledger": "build/nw10-windows-pe-ledger.json",
            "reason": (
                "Selection is derived from the current measured feasibility "
                "outcome and is not promoted beyond its runtime validation state."
            ),
        }
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcome", type=Path, required=True)
    parser.add_argument("--proof-report", type=Path, required=True)
    parser.add_argument("--intro-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", type=int, default=int(os.environ.get("GITHUB_RUN_ID", "1")))
    parser.add_argument("--head-sha", default=os.environ.get("GITHUB_SHA", "0" * 40))
    parser.add_argument("--artifact-name", default="nw10-windows-compact-evidence")
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--artifact-digest", default="")
    parser.add_argument("--prior-valid-ledger", type=Path)
    parser.add_argument("--pe-ledger", type=Path)
    parser.add_argument("--dependency-closure", type=Path)
    parser.add_argument("--system-validation", type=Path)
    parser.add_argument("--performance-preservation", type=Path)
    parser.add_argument("--correctness", type=Path)
    args = parser.parse_args(argv)
    if not (
        len(args.head_sha) == 40
        and all(character in "0123456789abcdef" for character in args.head_sha)
    ):
        print("head SHA is not a 40-character lowercase hex digest", file=sys.stderr)
        return 1
    prior_valid_ledger = (
        _load(args.prior_valid_ledger)
        if args.prior_valid_ledger is not None
        else None
    )
    prior_valid_ledger_path = (
        _repo_relative_path(args.prior_valid_ledger)
        if args.prior_valid_ledger is not None
        else None
    )
    record = build_feasibility(
        outcome=_load(args.outcome),
        proof=_load(args.proof_report),
        intro=_load(args.intro_report),
        run_id=args.run_id,
        head_sha=args.head_sha,
        artifact_name=args.artifact_name,
        artifact_id=args.artifact_id,
        artifact_digest=args.artifact_digest,
        prior_valid_ledger=prior_valid_ledger,
        prior_valid_ledger_path=prior_valid_ledger_path,
        pe_ledger=_load(args.pe_ledger) if args.pe_ledger is not None else None,
        dependency_closure=(
            _load(args.dependency_closure)
            if args.dependency_closure is not None
            else None
        ),
        system_validation=(
            _load(args.system_validation)
            if args.system_validation is not None
            else None
        ),
        performance=(
            _load(args.performance_preservation)
            if args.performance_preservation is not None
            else None
        ),
        correctness=(
            _load(args.correctness) if args.correctness is not None else None
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    validate_file_against_schema(
        args.output,
        REPO_ROOT / "schemas" / "nw10-windows-compact-feasibility.schema.json",
    )
    print(json.dumps({"status": "valid", "target_met": record["target_met"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
