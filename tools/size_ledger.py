#!/usr/bin/env python3
"""Automated Size Ledger Reporting & Toolchain Comparison Tool for Nodewerk NW-01.

Reports and validates:
- Total distributed bytes (excluding only documented Windows 11 inbox DLLs and
  installed display-driver components; counting every other DLL, payload, model, and sidecar).
- Executable sections (.text, .rdata, .data, .pdata, .reloc), imports, offline shaders,
  decoder overhead, startup time, CPU/GPU time, and committed memory.
- Explicit identification of unavailable measurements (null + reason in unavailable_measurements,
  never 0 and never fabricated numbers).
- Toolchain byte comparison between clang-cl/lld-link and MSVC/LTCG across
  empty_player, clear_present, and compute_shader.
- Explicit non-claim on 64 KiB budget until NW-10 supplies evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from dependency_check import (
    canonical_inbox_dll,
    collect_deployment_files,
    parse_pe64_sections_and_imports,
    verify_closure,
)
from schema_validator import validate_instance, validate_semantic_invariants


class SizeLedgerError(ValueError):
    """Raised when a size ledger violates accounting or toolchain comparison contracts."""


def resolve_fixture_executable(directory: Path, fixture_id: str, executable_name: str) -> Path | None:
    """Locate a fixture executable, preferring its isolated staging directory."""
    candidates = (
        directory / "staging" / fixture_id / executable_name,
        directory / "deploy" / executable_name,
        directory / "deploy" / fixture_id / executable_name,
        directory / executable_name,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def compute_distribution_accounting(
    candidate_files: list[dict[str, Any]],
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """Classify candidate files into counted distributed components vs excluded system components.

    Excludes ONLY documented Windows 11 inbox OS DLLs and installed display-driver
    components. Counts every executable, redistributable DLL, shader payload,
    data payload, model, and sidecar.
    """
    inbox_dlls = {item["dll_name"].lower() for item in inventory.get("inbox_windows_allowlist", [])}
    driver_dlls = {
        item["dll_name"].lower() for item in inventory.get("installed_display_driver_allowlist", [])
    }

    distributed_components: list[dict[str, Any]] = []
    excluded_system_components: list[dict[str, Any]] = []
    total_distributed_bytes = 0

    for item in candidate_files:
        name = item["name"]
        lower_name = name.lower()
        size_bytes = int(item.get("size_bytes", 0))
        category = item.get("category", "sidecar")

        if canonical_inbox_dll(lower_name) in inbox_dlls:
            excluded_system_components.append(
                {
                    "name": name,
                    "exclusion_basis": "windows_11_inbox_os_component",
                    "counted_in_total_distributed_bytes": False,
                }
            )
        elif lower_name in driver_dlls:
            excluded_system_components.append(
                {
                    "name": name,
                    "exclusion_basis": "installed_display_driver_component",
                    "counted_in_total_distributed_bytes": False,
                }
            )
        else:
            if size_bytes <= 0:
                raise SizeLedgerError(f"Counted distributed component '{name}' must have size_bytes > 0")
            distributed_components.append(
                {
                    "name": name,
                    "category": category,
                    "size_bytes": size_bytes,
                    "sha256": item.get("sha256", "0" * 64),
                    "counted_in_total_distributed_bytes": True,
                }
            )
            total_distributed_bytes += size_bytes

    return {
        "total_distributed_bytes": total_distributed_bytes,
        "distributed_components": distributed_components,
        "excluded_system_components": excluded_system_components,
    }


def _classify_imported_dll(dll_name: str, inventory: dict[str, Any]) -> str:
    lower_dll = dll_name.lower()
    if canonical_inbox_dll(lower_dll) == "ucrtbase.dll":
        return "ucrt_inbox"
    inbox_dlls = {item["dll_name"].lower() for item in inventory.get("inbox_windows_allowlist", [])}
    if lower_dll in inbox_dlls:
        return "windows_inbox"
    dev_closure = inventory.get("closures", {}).get("dev", {})
    dev_dlls = {
        *(d.lower() for d in dev_closure.get("allowed_optional_dev_dlls", [])),
        "ucrtbased.dll",
        "vcruntime140d.dll",
    }
    if lower_dll in dev_dlls:
        return "dev_only_diagnostic"
    raise SizeLedgerError(f"Imported DLL '{dll_name}' has no valid schema classification in dependency inventory")


def record_ledger_from_pe_binary(
    binary_path: Path,
    ledger_path: Path,
    inventory: dict[str, Any],
    *,
    offline_dxil_bytes: int | None = None,
    runtime_metrics: dict[str, Any] | None = None,
    repo_root: Path | None = None,
    verified: bool = True,
    unverified_reason: str | None = None,
) -> dict[str, Any]:
    """Update a size ledger JSON file from a real measured PE32+ executable after verifying dependency closure and schema."""
    raw = binary_path.read_bytes()
    pe_info = parse_pe64_sections_and_imports(raw)
    sha256_hex = hashlib.sha256(raw).hexdigest()
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

    if repo_root is None:
        raise SizeLedgerError("repo_root is required to resolve fixture import contracts and schemas")

    profile = ledger.get("build_profile", "release")
    fixture_id = ledger["fixture_id"]
    existing_imports = {
        imp["dll_name"].lower(): imp for imp in ledger.get("imports", [])
    }

    expected_imports: list[str] | None = None
    if profile in ("release", "compact_experiment"):
        fx_json = repo_root / "fixtures" / fixture_id / "fixture.json"
        if not fx_json.is_file():
            raise SizeLedgerError(f"Missing required fixture import contract at {fx_json}")
        fx_data = json.loads(fx_json.read_text(encoding="utf-8"))
        expected_imports = fx_data.get("expected_release_imports")
        if not isinstance(expected_imports, list) or not expected_imports:
            raise SizeLedgerError(
                f"Fixture contract {fx_json} is missing non-empty 'expected_release_imports'"
            )

    sidecars = collect_deployment_files(
        binary_path.parent,
        exclude_paths={binary_path, ledger_path},
    )

    verify_closure(
        profile=profile,
        fixture_id=fixture_id,
        imported_dlls=pe_info["imports"],
        delay_imported_dlls=pe_info.get("delay_imports", []),
        sidecar_files=sidecars,
        inventory=inventory,
        expected_fixture_release_imports=expected_imports,
    )

    candidates = [
        {
            "name": binary_path.name,
            "category": "executable",
            "size_bytes": len(raw),
            "sha256": sha256_hex,
        }
    ]
    for exc in ledger.get("excluded_system_components", []):
        candidates.append({"name": exc["name"], "category": "dll", "size_bytes": 0})

    acct = compute_distribution_accounting(candidates, inventory)
    # A verified compute ledger must include a measured offline DXIL payload; a PE-only
    # measurement is retained as unverified rather than claiming a complete shader ledger.
    effective_verified = verified and (
        offline_dxil_bytes is not None or not ledger.get("shaders")
    )
    if effective_verified:
        ledger["verification_status"] = "verified"
        ledger.pop("unverified_reason", None)
    else:
        ledger["verification_status"] = "unverified"
        ledger["unverified_reason"] = (
            unverified_reason
            or (
                "Measured PE32+ release binary retained as an unverified experiment record because "
                "the offline DXIL payload size was not measured."
                if ledger.get("shaders") and offline_dxil_bytes is None
                else "Measured PE32+ release binary retained as an unverified experiment record because "
                "toolchain verification evidence (CodeView/PDB debugging and two-clean-directory "
                "reproducibility across all fixtures) was incomplete."
            )
        )
    ledger["total_distributed_bytes"] = acct["total_distributed_bytes"]
    ledger["distributed_components"] = acct["distributed_components"]

    updated_imports: list[dict[str, Any]] = []
    for dll_name in pe_info["imports"]:
        lower_dll = dll_name.lower()
        if lower_dll in existing_imports:
            updated_imports.append(existing_imports[lower_dll])
        else:
            updated_imports.append(
                {
                    "dll_name": lower_dll,
                    "classification": _classify_imported_dll(lower_dll, inventory),
                    "symbols": ["<imported>"],
                }
            )
    for dll_name in pe_info.get("delay_imports", []):
        lower_dll = dll_name.lower()
        entry = existing_imports.get(lower_dll)
        if entry is None:
            entry = {
                "dll_name": lower_dll,
                "classification": _classify_imported_dll(lower_dll, inventory),
                "symbols": ["<delay-loaded>"],
                "delay_load": True,
            }
        else:
            entry = dict(entry)
            entry["delay_load"] = True
        updated_imports.append(entry)
    ledger["imports"] = updated_imports

    sec_by_name = {s["name"]: s for s in pe_info["sections"]}
    for sec_entry in ledger.get("executable_sections", []):
        measured_sec = sec_by_name.get(sec_entry["name"])
        if measured_sec:
            sec_entry["virtual_size_bytes"] = measured_sec["virtual_size_bytes"]
            sec_entry["raw_size_bytes"] = measured_sec["raw_size_bytes"]

    if offline_dxil_bytes is not None:
        for sh in ledger.get("shaders", []):
            sh["offline_dxil_bytes"] = offline_dxil_bytes
            sh["embedded_in_executable_bytes"] = offline_dxil_bytes
    elif ledger.get("shaders"):
        # The PE binary was measured but the offline DXIL payload was not. Preserve the
        # executable measurement as an unverified experiment record instead of discarding
        # it, and never leave stale DXIL bytes from a previous run on the ledger.
        for sh in ledger.get("shaders", []):
            sh["offline_dxil_bytes"] = None
            sh["embedded_in_executable_bytes"] = None

    if runtime_metrics is not None:
        ledger["runtime_metrics"] = {
            key: runtime_metrics.get(key)
            for key in (
                "startup_time_ms",
                "cpu_time_ms",
                "gpu_time_ms",
                "committed_memory_bytes",
                "gpu_committed_memory_bytes",
            )
        }

    removed_metrics = {"total_distributed_bytes", "executable_sections"}
    if offline_dxil_bytes is not None:
        removed_metrics.add("shaders[0].offline_dxil_bytes")
    if runtime_metrics is not None:
        missing_runtime_metrics: list[str] = []
        for metric_key in (
            "startup_time_ms",
            "cpu_time_ms",
            "gpu_time_ms",
            "committed_memory_bytes",
            "gpu_committed_memory_bytes",
        ):
            if runtime_metrics.get(metric_key) is not None:
                removed_metrics.add(f"runtime_metrics.{metric_key}")
                removed_metrics.add(metric_key)
            else:
                missing_runtime_metrics.append(metric_key)
    else:
        missing_runtime_metrics = []
    ledger["unavailable_measurements"] = [
        m
        for m in ledger.get("unavailable_measurements", [])
        if m["metric"] not in removed_metrics
    ]
    if offline_dxil_bytes is None and ledger.get("shaders"):
        existing_metrics = {m["metric"] for m in ledger["unavailable_measurements"]}
        if "shaders[0].offline_dxil_bytes" not in existing_metrics:
            ledger["unavailable_measurements"].insert(
                0,
                {
                    "metric": "shaders[0].offline_dxil_bytes",
                    "reason": (
                        "The compute fixture PE binary was measured, but the offline DXIL "
                        "payload size was unavailable because DXC compilation was not verified."
                    ),
                },
            )

    if runtime_metrics is not None and missing_runtime_metrics:
        existing_metric_names = {m["metric"] for m in ledger["unavailable_measurements"]}
        for metric_key in missing_runtime_metrics:
            if metric_key not in existing_metric_names:
                ledger["unavailable_measurements"].append(
                    {
                        "metric": metric_key,
                        "reason": (
                            "The fixture did not emit a measured "
                            f"{metric_key}; recorded as null rather than zero."
                        ),
                    }
                )

    schema_root = repo_root or Path(__file__).resolve().parent.parent
    schema_path = schema_root / "schemas" / "size-ledger.schema.json"
    if schema_path.exists():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validate_instance(ledger, schema, schema, path=ledger_path.name)
        validate_semantic_invariants(ledger, schema_path.name, ledger_path)

    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return ledger


def _reset_ledger_to_unverified(
    ledger_path: Path,
    *,
    unverified_reason: str | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Reset a size ledger to unmeasured/unverified state when its binary was not produced."""
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["verification_status"] = "unverified"
    ledger["unverified_reason"] = (
        unverified_reason
        or ledger.get("unverified_reason")
        or "Release fixture binary has not been verified with both two-clean-directory "
        "reproducibility and CodeView/PDB debugging evidence; byte size is recorded as null."
    )
    ledger["total_distributed_bytes"] = None
    for comp in ledger.get("distributed_components", []):
        comp["size_bytes"] = None
        comp["sha256"] = None
    for sec in ledger.get("executable_sections", []):
        sec["virtual_size_bytes"] = None
        sec["raw_size_bytes"] = None
    for sh in ledger.get("shaders", []):
        sh["offline_dxil_bytes"] = None
        sh["embedded_in_executable_bytes"] = None

    existing_metrics = {m["metric"] for m in ledger.get("unavailable_measurements", [])}
    prefix_unavailable: list[dict[str, str]] = []
    if "total_distributed_bytes" not in existing_metrics:
        prefix_unavailable.append(
            {
                "metric": "total_distributed_bytes",
                "reason": ledger["unverified_reason"],
            }
        )
    if "executable_sections" not in existing_metrics:
        prefix_unavailable.append(
            {
                "metric": "executable_sections",
                "reason": "PE32+ section virtual/raw sizes are unavailable because the fixture binary is unverified.",
            }
        )
    if ledger.get("shaders") and "shaders[0].offline_dxil_bytes" not in existing_metrics:
        prefix_unavailable.append(
            {
                "metric": "shaders[0].offline_dxil_bytes",
                "reason": "Offline DXIL payload size is unavailable because the compute fixture is unverified.",
            }
        )
    if prefix_unavailable:
        ledger["unavailable_measurements"] = prefix_unavailable + list(
            ledger.get("unavailable_measurements", [])
        )

    schema_root = repo_root or Path(__file__).resolve().parent.parent
    schema_path = schema_root / "schemas" / "size-ledger.schema.json"
    if schema_path.exists():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validate_instance(ledger, schema, schema, path=ledger_path.name)
        validate_semantic_invariants(ledger, schema_path.name, ledger_path)

    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return ledger


_BYTE_FIELDS = (
    ("empty_player_bytes", "empty_player"),
    ("clear_present_bytes", "clear_present"),
    ("compute_fixture_bytes", "compute_shader"),
)


def _complete_byte_total(entry: dict[str, Any]) -> int | None:
    """Return the aggregate measured bytes only when all three fixture counts are present."""
    values = [entry.get(field) for field, _ in _BYTE_FIELDS]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def _winner_from_eligible(
    toolchain_manifest: dict[str, Any],
    eligible: list[tuple[str, int]],
) -> str | None:
    """Pick the smallest aggregate eligible toolchain, keeping an eligible current selection on ties."""
    if not eligible:
        return None
    min_total = min(total for _, total in eligible)
    winners = {toolchain_id for toolchain_id, total in eligible if total == min_total}
    current_id = toolchain_manifest.get("selected_toolchain", {}).get("toolchain_id")
    if current_id in winners:
        return current_id
    for entry in toolchain_manifest.get("evaluated_toolchains", []):
        toolchain_id = entry.get("toolchain_id")
        if toolchain_id in winners:
            return toolchain_id
    return sorted(winners)[0]


def _select_measured_toolchain(toolchain_manifest: dict[str, Any]) -> str | None:
    """Select the smaller fully measured, verified toolchain from the manifest itself.

    No compiler name is preferred. Only toolchains with verified status and all three
    recorded fixture byte counts are eligible; if exactly one toolchain qualifies it is
    selected instead of leaving an unverified default in place.
    """
    eligible: list[tuple[str, int]] = []
    for entry in toolchain_manifest.get("evaluated_toolchains", []):
        if (
            entry.get("verification_status") != "verified"
            or entry.get("fixture_execution_verified") is not True
        ):
            continue
        total = _complete_byte_total(entry)
        if total is None:
            continue
        eligible.append((entry["toolchain_id"], total))
    return _winner_from_eligible(toolchain_manifest, eligible)


def _select_toolchain_from_ledgers(
    toolchain_manifest: dict[str, Any],
    ledgers_by_key: dict[tuple[str, str], dict[str, Any]],
    fixture_ids: list[str],
) -> str | None:
    """Select the smaller verified toolchain directly from the recorded size ledgers."""
    eligible: list[tuple[str, int]] = []
    for entry in toolchain_manifest.get("evaluated_toolchains", []):
        toolchain_id = entry.get("toolchain_id")
        if not toolchain_id:
            continue
        if entry.get("fixture_execution_verified") is not True:
            continue
        rows = [ledgers_by_key.get((fixture_id, toolchain_id)) for fixture_id in fixture_ids]
        if any(
            row is None
            or row.get("verification_status") != "verified"
            or row.get("total_distributed_bytes") is None
            for row in rows
        ):
            continue
        eligible.append(
            (toolchain_id, sum(int(row["total_distributed_bytes"]) for row in rows))
        )
    return _winner_from_eligible(toolchain_manifest, eligible)


def _sync_evaluated_toolchains_from_ledgers(
    toolchain_manifest: dict[str, Any],
    ledgers_by_key: dict[tuple[str, str], dict[str, Any]],
) -> bool:
    """Reconcile manifest byte fields and verification state with the size ledgers.

    Returns True when the manifest changed. A verified manifest entry is downgraded if any
    fixture ledger for that toolchain is unverified, so stale verified entries can never
    disagree with the measured ledgers after a failed re-verification run.
    """
    changed = False
    for entry in toolchain_manifest.get("evaluated_toolchains", []):
        toolchain_id = entry.get("toolchain_id")
        if not toolchain_id:
            continue
        statuses: list[str] = []
        for field, fixture_id in _BYTE_FIELDS:
            ledger = ledgers_by_key.get((fixture_id, toolchain_id))
            if ledger is None:
                continue
            measured = ledger.get("total_distributed_bytes")
            if entry.get(field) != measured:
                entry[field] = measured
                changed = True
            statuses.append(str(ledger.get("verification_status", "unverified")))

        if not statuses:
            continue
        if len(statuses) != len(_BYTE_FIELDS) or any(
            status != "verified" for status in statuses
        ):
            if (
                entry.get("verification_status") != "unverified"
                or entry.get("pdb_debugging_verified") is not False
                or entry.get("deterministic_repro_verified") is not False
                or entry.get("fixture_execution_verified") is not False
            ):
                entry["verification_status"] = "unverified"
                entry["pdb_debugging_verified"] = False
                entry["deterministic_repro_verified"] = False
                entry["fixture_execution_verified"] = False
                changed = True
        elif (
            entry.get("pdb_debugging_verified") is True
            and entry.get("deterministic_repro_verified") is True
            and entry.get("fixture_execution_verified") is True
        ):
            if entry.get("verification_status") != "verified":
                entry["verification_status"] = "verified"
                changed = True
        else:
            # Verified ledgers without both evidence flags cannot be promoted to verified.
            if (
                entry.get("verification_status") != "unverified"
                or entry.get("pdb_debugging_verified") is not False
                or entry.get("deterministic_repro_verified") is not False
                or entry.get("fixture_execution_verified") is not False
            ):
                entry["verification_status"] = "unverified"
                entry["pdb_debugging_verified"] = False
                entry["deterministic_repro_verified"] = False
                entry["fixture_execution_verified"] = False
                changed = True
    return changed


_TOOLCHAIN_PIN_METADATA: dict[str, dict[str, str]] = {
    "clang-cl-lld": {
        "compiler_name": "LLVM clang-cl",
        "compiler_executable": "clang-cl.exe",
        "linker_name": "LLVM lld-link",
        "linker_executable": "lld-link.exe",
        "target_triple": "x86_64-pc-windows-msvc",
    },
    "msvc-ltcg": {
        "compiler_name": "Microsoft C/C++ Optimizing Compiler (MSVC)",
        "compiler_executable": "cl.exe",
        "linker_name": "Microsoft Incremental Linker",
        "linker_executable": "link.exe",
        "target_triple": "x86_64-pc-windows-msvc",
    },
}


def _apply_selected_toolchain_pin(toolchain_manifest: dict[str, Any], winner_id: str) -> None:
    """Update selected_toolchain and evaluated_toolchains[*].status to match winner_id."""
    entries = {t["toolchain_id"]: t for t in toolchain_manifest.get("evaluated_toolchains", [])}
    winner_entry = entries.get(winner_id)
    meta = _TOOLCHAIN_PIN_METADATA.get(winner_id)
    if winner_entry is None or meta is None:
        return
    pin = toolchain_manifest.setdefault("selected_toolchain", {})
    pin["toolchain_id"] = winner_id
    pin["compiler_name"] = meta["compiler_name"]
    pin["compiler_executable"] = meta["compiler_executable"]
    pin["compiler_version"] = winner_entry["compiler_version"]
    pin["linker_name"] = meta["linker_name"]
    pin["linker_executable"] = meta["linker_executable"]
    pin["linker_version"] = winner_entry["linker_version"]
    pin["target_triple"] = meta["target_triple"]
    for t in toolchain_manifest.get("evaluated_toolchains", []):
        t["status"] = "selected" if t["toolchain_id"] == winner_id else "retained_alternative"


def _measured_byte_summary(entry: dict[str, Any]) -> str | None:
    parts = [
        f"{label}={entry[field]} B"
        for field, label in _BYTE_FIELDS
        if entry.get(field) is not None
    ]
    return "; ".join(parts) if parts else None


def _evidence_state(entry: dict[str, Any] | None) -> str:
    """Summarize measured/verification flags so evidence text never drifts from recorded state."""
    if entry is None:
        return "not evaluated"
    return (
        f"verification_status={entry.get('verification_status', 'unverified')}, "
        f"pdb_debugging_verified={entry.get('pdb_debugging_verified') is True}, "
        f"deterministic_repro_verified={entry.get('deterministic_repro_verified') is True}, "
        f"fixture_execution_verified={entry.get('fixture_execution_verified') is True}"
    )


def _set_toolchain_unverified(
    eval_entry: dict[str, Any],
    repo_root: Path | None = None,
    *,
    preserve_measured_bytes: bool = False,
    unverified_reason: str | None = None,
) -> None:
    """Mark an evaluated toolchain unverified, clearing its evidence flags and syncing its ledgers.

    When preserve_measured_bytes is True, any byte counts measured during an incomplete or failed
    verification run are retained on eval_entry and in the toolchain's size ledgers (marked
    verification_status='unverified' with unverified_reason). When preserve_measured_bytes is False,
    both eval_entry's byte fields and its size ledgers are cleared to null/unverified.
    """
    if not preserve_measured_bytes:
        for field, _ in _BYTE_FIELDS:
            eval_entry[field] = None
    eval_entry["deterministic_repro_verified"] = False
    eval_entry["pdb_debugging_verified"] = False
    eval_entry["fixture_execution_verified"] = False
    eval_entry["verification_status"] = "unverified"

    if repo_root is not None:
        tid = eval_entry.get("toolchain_id")
        suffix = {"clang-cl-lld": "clang-cl", "msvc-ltcg": "msvc-ltcg"}.get(tid or "")
        if suffix:
            ledger_dir = repo_root / "benchmarks" / "ledgers"
            reason = (
                unverified_reason
                or "Toolchain verification is incomplete (CodeView/PDB debugging and two-clean-directory "
                "reproducibility must both be verified across all three fixtures)."
            )
            for ledger_name, (field, _) in zip(
                (
                    f"empty-player-{suffix}-release.json",
                    f"clear-present-{suffix}-release.json",
                    f"compute-fixture-{suffix}-release.json",
                ),
                _BYTE_FIELDS,
            ):
                ledger_path = ledger_dir / ledger_name
                if not ledger_path.exists():
                    continue
                if not preserve_measured_bytes or eval_entry.get(field) is None:
                    _reset_ledger_to_unverified(
                        ledger_path,
                        unverified_reason=reason,
                        repo_root=repo_root,
                    )
                else:
                    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                    # Preserve measured bytes, but never leave a ledger marked verified
                    # after its toolchain entry has been downgraded to unverified.
                    ledger["verification_status"] = "unverified"
                    ledger["unverified_reason"] = reason
                    schema_path = repo_root / "schemas" / "size-ledger.schema.json"
                    if schema_path.exists():
                        schema = json.loads(schema_path.read_text(encoding="utf-8"))
                        validate_instance(ledger, schema, schema, path=ledger_path.name)
                        validate_semantic_invariants(ledger, schema_path.name, ledger_path)
                    ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")


def _derive_toolchain_evidence_text(toolchain_manifest: dict[str, Any]) -> None:
    """Refresh the selected toolchain and human-readable evidence from recorded state.

    Selection is measurement-driven: among toolchains whose verification is complete and whose
    three fixture byte counts are recorded, the smaller aggregate distributed footprint is
    selected (ties retain the eligible current selection). If only one toolchain is verified it
    is selected instead of leaving an unverified default in place. Rationale text is always
    derived from the recorded byte counts and evidence flags rather than from a fixed compiler
    preference.
    """
    entries = {t["toolchain_id"]: t for t in toolchain_manifest.get("evaluated_toolchains", [])}
    if not entries:
        return

    winner_id = _select_measured_toolchain(toolchain_manifest)
    if winner_id is not None:
        _apply_selected_toolchain_pin(toolchain_manifest, winner_id)
    else:
        current_selected_id = toolchain_manifest.get("selected_toolchain", {}).get("toolchain_id")
        if current_selected_id not in entries:
            _apply_selected_toolchain_pin(toolchain_manifest, next(iter(entries)))
        else:
            for t in toolchain_manifest.get("evaluated_toolchains", []):
                t["status"] = "selected" if t["toolchain_id"] == current_selected_id else "retained_alternative"

    selected_id = toolchain_manifest.get("selected_toolchain", {}).get("toolchain_id")
    selected = entries.get(selected_id)
    if selected is None:
        return
    other_id = next((tid for tid in entries if tid != selected_id), None)
    other = entries.get(other_id) if other_id else None

    selected_summary = _measured_byte_summary(selected)
    other_summary = _measured_byte_summary(other) if other else None
    selected_bytes_complete = selected_summary is not None and all(
        selected.get(field) is not None for field, _ in _BYTE_FIELDS
    )
    other_bytes_complete = bool(other) and all(other.get(field) is not None for field, _ in _BYTE_FIELDS)
    selected_verified = selected.get("verification_status") == "verified"
    other_verified = bool(other) and other.get("verification_status") == "verified"
    selected_pdb = selected.get("pdb_debugging_verified") is True
    other_pdb = bool(other) and other.get("pdb_debugging_verified") is True
    selected_repro = selected.get("deterministic_repro_verified") is True
    other_repro = bool(other) and other.get("deterministic_repro_verified") is True

    pin = toolchain_manifest.get("selected_toolchain", {})
    selected_name = f"{pin.get('compiler_name', selected_id)} {pin.get('compiler_version', '')}".strip()
    selected_linker = f"{pin.get('linker_name', '')} {pin.get('linker_version', '')}".strip()
    other_label = other_id or "comparison toolchain"

    if selected_bytes_complete and other_bytes_complete and other is not None:
        if selected_verified and other_verified:
            details = "; ".join(
                f"{label} {selected.get(field)} B vs {other.get(field)} B"
                for field, label in _BYTE_FIELDS
            )
            selected_total = sum(int(selected[field]) for field, _ in _BYTE_FIELDS)
            other_total = sum(int(other[field]) for field, _ in _BYTE_FIELDS)
            if all(int(selected[field]) < int(other[field]) for field, _ in _BYTE_FIELDS):
                ordering_clause = f"{other_label} is larger on every fixture ({selected_total} B vs {other_total} B aggregate)"
            elif selected_total < other_total:
                per_fixture_winners = ", ".join(
                    f"{label}: {selected_id if int(selected[f]) < int(other[f]) else (other_label if int(other[f]) < int(selected[f]) else 'tie')}"
                    for f, label in _BYTE_FIELDS
                )
                ordering_clause = (
                    f"{selected_id} has the smaller aggregate distributed footprint "
                    f"({selected_total} B vs {other_total} B across the three fixtures; per-fixture smaller toolchain: {per_fixture_winners})"
                )
            else:
                ordering_clause = (
                    f"{selected_id} and {other_label} tied on aggregate distributed bytes "
                    f"({selected_total} B across the three fixtures)"
                )
            pin["selection_rationale"] = (
                f"Selected {selected_name} + {selected_linker} as the primary toolchain configuration "
                f"after identical-fixture comparison against {other_label}. Measured distributed bytes: "
                f"{details}. {ordering_clause}, and both toolchains have verified "
                "CodeView/PDB debugging evidence plus bit-for-bit two-clean-directory reproducibility."
            )
        else:
            # Byte counts can be complete for both toolchains while one (or both) still lacks
            # PDB or reproducibility evidence. Derive the rationale from the actual flags so a
            # stale "bytes remain null" sentence cannot survive a partial comparison.
            pin["selection_rationale"] = (
                f"Selected {selected_name} + {selected_linker} as the primary toolchain configuration "
                f"paired against {other_label}. Measured distributed bytes for {selected_id} "
                f"({selected.get('verification_status', 'unverified')}): {selected_summary}; "
                f"for {other_label} ({other.get('verification_status', 'unverified') if other else 'unverified'}): "
                f"{other_summary}. Both toolchains have complete byte measurements, but the comparison "
                "is not verified: "
                f"{selected_id} [{_evidence_state(selected)}], "
                f"{other_label} [{_evidence_state(other)}]. No completed size winner is claimed."
            )
    elif selected_summary and other_summary:
        other_status = other.get("verification_status", "unverified") if other else "unverified"
        pin["selection_rationale"] = (
            f"Selected {selected_name} + {selected_linker} as the primary toolchain configuration "
            f"paired against {other_label}. Measured distributed bytes for {selected_id} "
            f"({selected.get('verification_status', 'unverified')}): {selected_summary}; "
            f"for {other_label} ({other_status}): {other_summary}. Full toolchain comparison "
            "verification remains incomplete until both toolchains have verified CodeView/PDB "
            "debugging and two-clean-directory reproducibility evidence."
        )
    elif selected_summary:
        pin["selection_rationale"] = (
            f"Selected {selected_name} + {selected_linker} as the primary toolchain configuration. "
            f"Measured distributed bytes for the selected toolchain "
            f"({selected.get('verification_status', 'unverified')}): {selected_summary}. "
            f"The {other_label} comparison fixtures remain unverified, so no completed size comparison "
            "is claimed in this manifest."
        )
    elif other_summary:
        other_status = other.get("verification_status", "unverified") if other else "unverified"
        pin["selection_rationale"] = (
            f"Selected {selected_name} + {selected_linker} as the primary toolchain configuration "
            f"paired against {other_label}. Measured distributed bytes for {other_label} "
            f"({other_status}): {other_summary}. Selected toolchain {selected_id} byte counts "
            "remain null (unverified), so no completed size comparison is claimed in this manifest."
        )
    else:
        pin["selection_rationale"] = (
            f"Selected {selected_name} + {selected_linker} with ThinLTO (-flto=thin, /O1 /Os /Gw /Gy /GF "
            "/OPT:REF /OPT:ICF=4) as the primary toolchain configuration paired against "
            f"{other_label} across identical empty_player, clear_present, and compute_shader fixtures. "
            "Both toolchains share identical Windows 11 inbox DLL import closures, CodeView PDB symbol "
            "support, and /Brepro deterministic linking; exact PE byte counts remain null (unverified) "
            "until compiled on a Windows 11 x64 toolchain host."
        )

    if selected_pdb and other_pdb:
        pin["debugging_evidence"] = (
            f"Verified CodeView/PDB debug symbol evidence for both {selected_id} and {other_label}: "
            "dev-profile /DEBUG:FULL builds produced PDB files with the Microsoft MSF signature and "
            "PE debug directories containing RSDS CodeView records for every fixture executable."
        )
    elif selected_pdb:
        pin["debugging_evidence"] = (
            f"Verified CodeView/PDB debug symbol evidence for {selected_id}: a dev-profile "
            "/DEBUG:FULL build produced MSF PDB files and RSDS CodeView records. "
            f"Live PDB verification for {other_label} remains pending."
        )
    elif other_pdb:
        pin["debugging_evidence"] = (
            f"Verified CodeView/PDB debug symbol evidence for {other_label}: a dev-profile "
            "/DEBUG:FULL build produced MSF PDB files and RSDS CodeView records. "
            f"Live PDB verification for {selected_id} remains pending."
        )
    else:
        pin["debugging_evidence"] = (
            "Configured with CodeView /DEBUG:FULL (dev) and /PDBALTPATH:%_PDB% (release) symbol "
            "emission for WinDbg, Visual Studio Debugger, and PIX for Windows GPU captures; "
            "live Windows debugger verification is pending Windows 11 x64 host execution."
        )

    if selected_repro and other_repro:
        pin["reproducibility_evidence"] = (
            f"Verified bit-for-bit identical release artifacts across two independent clean build "
            f"directories for both {selected_id} and {other_label} using /Brepro, "
            "-ffile-prefix-map=${CMAKE_SOURCE_DIR}=., and /PDBALTPATH:%_PDB%."
        )
    elif selected_repro:
        pin["reproducibility_evidence"] = (
            f"Verified bit-for-bit identical release artifacts across two independent clean build "
            f"directories for {selected_id}. Two-clean-directory evidence for {other_label} remains "
            "pending and is not claimed."
        )
    elif other_repro:
        pin["reproducibility_evidence"] = (
            f"Verified bit-for-bit identical release artifacts across two independent clean build "
            f"directories for {other_label}. Two-clean-directory evidence for {selected_id} remains "
            "pending and is not claimed."
        )
    else:
        pin["reproducibility_evidence"] = (
            "Configured for bit-for-bit identical PE executables across independent clean build "
            "directories using /Brepro, -ffile-prefix-map=${CMAKE_SOURCE_DIR}=., and /PDBALTPATH:%_PDB%, "
            "enforced by cmake/ToolchainManifest.cmake and tools/verify_nw01.py."
        )


def _refresh_toolchain_unavailable_measurements(toolchain_manifest: dict[str, Any]) -> None:
    unavailable = [
        m
        for m in toolchain_manifest.get("unavailable_measurements", [])
        if not m["metric"].startswith("evaluated_toolchains")
    ]
    for field, _ in _BYTE_FIELDS:
        missing = [
            t["toolchain_id"]
            for t in toolchain_manifest.get("evaluated_toolchains", [])
            if t.get(field) is None
        ]
        if missing:
            unavailable.append(
                {
                    "metric": f"evaluated_toolchains[{','.join(missing)}].{field}",
                    "reason": (
                        "This toolchain fixture has not been built and measured on the pinned "
                        "Windows 11 x64 host; recorded as null rather than interpolated."
                    ),
                }
            )
    toolchain_manifest["unavailable_measurements"] = unavailable


def record_release_ledgers(
    repo_root: Path,
    binary_dir: Path,
    toolchain_id: str = "clang-cl-lld",
    *,
    deterministic_repro_verified: bool | None = None,
    pdb_debugging_verified: bool | None = None,
    fixture_execution_verified: bool | None = None,
    runtime_metrics_by_fixture: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record measured release artifacts into the toolchain-specific size ledgers.

    Updates the specified toolchain's ledgers and byte fields. Unmeasured comparison
    toolchains remain explicitly unverified rather than being copied or interpolated.

    A toolchain is only marked verified when all three fixtures were measured AND the
    two-clean-directory reproducibility result, the CodeView/PDB debugging result, and
    successful execution of the identical release fixtures are explicitly True. When
    verification is incomplete, measured PE binaries are still preserved in the size ledgers
    and toolchain manifest with verification_status='unverified' and false evidence flags
    rather than discarding the measurements or leaving stale verified ledgers.
    """
    inventory_path = repo_root / "benchmarks" / "dependency-inventory.v1.json"
    toolchain_path = repo_root / "benchmarks" / "toolchain-manifest.v1.json"
    shader_manifest_path = repo_root / "shaders" / "manifest.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    toolchain_manifest = json.loads(toolchain_path.read_text(encoding="utf-8"))
    shader_manifest = json.loads(shader_manifest_path.read_text(encoding="utf-8"))
    shader_bytes = (shader_manifest.get("shaders") or [{}])[0].get("offline_dxil_bytes")

    suffix = {
        "clang-cl-lld": "clang-cl",
        "msvc-ltcg": "msvc-ltcg",
    }.get(toolchain_id)
    if suffix is None:
        raise SizeLedgerError(f"Unknown toolchain_id '{toolchain_id}'")

    specs = [
        ("empty_player", "nw_empty_player.exe", f"empty-player-{suffix}-release.json",
         "empty_player_bytes", None),
        ("clear_present", "nw_clear_present_fixture.exe", f"clear-present-{suffix}-release.json",
         "clear_present_bytes", None),
        ("compute_shader", "nw_compute_shader_fixture.exe", f"compute-fixture-{suffix}-release.json",
         "compute_fixture_bytes", shader_bytes),
    ]

    recorded: dict[str, Any] = {}
    eval_entry = next(
        (t for t in toolchain_manifest.get("evaluated_toolchains", []) if t["toolchain_id"] == toolchain_id),
        None,
    )
    if eval_entry is None:
        raise SizeLedgerError(f"Toolchain manifest has no evaluated entry for '{toolchain_id}'")

    verification_complete = (
        deterministic_repro_verified is True
        and pdb_debugging_verified is True
        and fixture_execution_verified is True
    )
    ready_by_fixture: dict[str, tuple[Path, Path, str, str, int | None]] = {}
    missing_fixtures: list[str] = []
    for fixture_id, exe_name, ledger_name, manifest_field, dxil_bytes in specs:
        exe_path = resolve_fixture_executable(binary_dir, fixture_id, exe_name)
        ledger_path = repo_root / "benchmarks" / "ledgers" / ledger_name
        if exe_path is not None and ledger_path.exists():
            ready_by_fixture[fixture_id] = (
                exe_path,
                ledger_path,
                ledger_name,
                manifest_field,
                dxil_bytes,
            )
        else:
            missing_fixtures.append(exe_name)

    all_three_binaries_ready = len(ready_by_fixture) == len(specs)
    offline_dxil_ready = shader_bytes is not None
    all_measurements_complete = all_three_binaries_ready and offline_dxil_ready
    toolchain_verified = verification_complete and all_measurements_complete
    unverified_reason = None
    if not toolchain_verified:
        if not verification_complete:
            unverified_reason = (
                "Refusing to mark ledgers verified: two-clean-directory reproducibility and "
                "CodeView/PDB debugging evidence and successful execution of the identical "
                "release fixtures must all be explicitly verified; preserving "
                "measured binary sizes in an unverified record."
            )
        elif not all_three_binaries_ready:
            unverified_reason = (
                "Refusing to mark ledgers verified: missing release fixture executables "
                f"({', '.join(missing_fixtures)}); preserving every available measured binary "
                "in an unverified experiment record rather than reporting a completed toolchain."
            )
        else:
            unverified_reason = (
                "Refusing to mark ledgers verified: the offline DXIL payload size was not "
                "measured (DXC compilation unverified); the measured compute fixture PE bytes "
                "and every other measured binary are preserved in an unverified record."
            )

    for fixture_id, _exe_name, ledger_name, manifest_field, _dxil_bytes in specs:
        ledger_path = repo_root / "benchmarks" / "ledgers" / ledger_name
        if fixture_id in ready_by_fixture:
            exe_path, ledger_path, ledger_name, manifest_field, dxil_bytes = ready_by_fixture[fixture_id]
            ledger = record_ledger_from_pe_binary(
                exe_path,
                ledger_path,
                inventory,
                offline_dxil_bytes=dxil_bytes,
                runtime_metrics=(runtime_metrics_by_fixture or {}).get(fixture_id),
                repo_root=repo_root,
                verified=toolchain_verified,
                unverified_reason=unverified_reason,
            )
            eval_entry[manifest_field] = ledger["total_distributed_bytes"]
            recorded[fixture_id] = {
                "ledger": ledger_name,
                "verification_status": ledger["verification_status"],
                "total_distributed_bytes": ledger["total_distributed_bytes"],
                "sha256": ledger["distributed_components"][0]["sha256"],
            }
        else:
            eval_entry[manifest_field] = None
            if ledger_path.exists():
                _reset_ledger_to_unverified(
                    ledger_path,
                    unverified_reason=unverified_reason,
                    repo_root=repo_root,
                )

    if toolchain_verified:
        eval_entry["deterministic_repro_verified"] = True
        eval_entry["pdb_debugging_verified"] = True
        eval_entry["fixture_execution_verified"] = True
        eval_entry["verification_status"] = "verified"
    else:
        eval_entry["fixture_execution_verified"] = bool(fixture_execution_verified is True)
        _set_toolchain_unverified(
            eval_entry,
            repo_root,
            preserve_measured_bytes=True,
            unverified_reason=unverified_reason,
        )

    _refresh_toolchain_unavailable_measurements(toolchain_manifest)
    _derive_toolchain_evidence_text(toolchain_manifest)
    toolchain_schema_path = (repo_root or Path(__file__).resolve().parent.parent) / "schemas" / "toolchain-manifest.schema.json"
    if toolchain_schema_path.exists():
        toolchain_schema = json.loads(toolchain_schema_path.read_text(encoding="utf-8"))
        validate_instance(toolchain_manifest, toolchain_schema, toolchain_schema, path=toolchain_path.name)
        validate_semantic_invariants(toolchain_manifest, toolchain_schema_path.name, toolchain_path)
    toolchain_path.write_text(json.dumps(toolchain_manifest, indent=2) + "\n", encoding="utf-8")

    other_toolchains_unverified = any(
        t.get("verification_status") != "verified"
        for t in toolchain_manifest.get("evaluated_toolchains", [])
        if t["toolchain_id"] != toolchain_id
    )
    result: dict[str, Any] = {
        "toolchain_id": toolchain_id,
        "selected_toolchain": toolchain_manifest["selected_toolchain"]["toolchain_id"],
        "verification_status": eval_entry["verification_status"],
        "recorded_fixtures": recorded,
        "deterministic_repro_verified": eval_entry["deterministic_repro_verified"],
        "pdb_debugging_verified": eval_entry["pdb_debugging_verified"],
        "fixture_execution_verified": eval_entry.get("fixture_execution_verified", False),
        "comparison_toolchain_bytes_remain_unverified": other_toolchains_unverified,
    }
    if unverified_reason is not None:
        result["reason"] = unverified_reason
    return result


def generate_size_comparison_report(repo_root: Path) -> dict[str, Any]:
    """Validate all recorded ledgers and compare clang-cl/lld-link vs MSVC/LTCG."""
    inventory_path = repo_root / "benchmarks" / "dependency-inventory.v1.json"
    toolchain_path = repo_root / "benchmarks" / "toolchain-manifest.v1.json"
    shader_manifest_path = repo_root / "shaders" / "manifest.json"

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    original_manifest_text = toolchain_path.read_text(encoding="utf-8")
    toolchain_manifest = json.loads(original_manifest_text)
    shader_manifest = json.loads(shader_manifest_path.read_text(encoding="utf-8"))
    shader_by_id = {s["shader_id"]: s for s in shader_manifest.get("shaders", [])}

    inbox_dlls = {item["dll_name"].lower() for item in inventory.get("inbox_windows_allowlist", [])}
    driver_dlls = {
        item["dll_name"].lower() for item in inventory.get("installed_display_driver_allowlist", [])
    }

    ledger_dir = repo_root / "benchmarks" / "ledgers"
    ledgers_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for ledger_path in sorted(ledger_dir.glob("*.json")):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        key = (ledger["fixture_id"], ledger["toolchain_id"])
        ledgers_by_key[key] = ledger
        status = ledger.get("verification_status")

        # Verify budget claim policy
        policy = ledger.get("budget_claim_policy", {})
        if policy.get("claims_64kib_achievable") is not False:
            raise SizeLedgerError(f"{ledger_path.name}: must not claim 64 KiB is achievable before NW-10")
        if policy.get("defer_minimum_size_claim_until_task") != "NW-10":
            raise SizeLedgerError(f"{ledger_path.name}: must defer minimum size claim until NW-10")

        # Verify excluded components are strictly in allowlists
        for exc in ledger.get("excluded_system_components", []):
            exc_name = exc["name"].lower()
            basis = exc["exclusion_basis"]
            if basis == "windows_11_inbox_os_component" and exc_name not in inbox_dlls:
                raise SizeLedgerError(f"{ledger_path.name}: '{exc_name}' is not a documented Windows inbox DLL")
            if basis == "installed_display_driver_component" and exc_name not in driver_dlls:
                raise SizeLedgerError(f"{ledger_path.name}: '{exc_name}' is not a documented display driver DLL")

        unavailable_metrics = {m["metric"] for m in ledger.get("unavailable_measurements", [])}

        if status == "verified":
            if ledger.get("total_distributed_bytes") is None:
                raise SizeLedgerError(f"{ledger_path.name}: verified ledger must have non-null total_distributed_bytes")
            counted_sum = sum(
                c["size_bytes"]
                for c in ledger.get("distributed_components", [])
                if c.get("counted_in_total_distributed_bytes")
            )
            if counted_sum != ledger["total_distributed_bytes"]:
                raise SizeLedgerError(
                    f"{ledger_path.name}: total_distributed_bytes ({ledger['total_distributed_bytes']}) "
                    f"!= sum of distributed_components ({counted_sum})"
                )
        elif status == "unverified":
            if ledger.get("total_distributed_bytes") is None:
                if "total_distributed_bytes" not in unavailable_metrics:
                    raise SizeLedgerError(
                        f"{ledger_path.name}: unverified ledger must list 'total_distributed_bytes' in unavailable_measurements"
                    )
            else:
                if not ledger.get("unverified_reason"):
                    raise SizeLedgerError(
                        f"{ledger_path.name}: unverified ledger with preserved byte measurements must record unverified_reason"
                    )
                counted_sum = sum(
                    c["size_bytes"]
                    for c in ledger.get("distributed_components", [])
                    if c.get("counted_in_total_distributed_bytes")
                )
                if counted_sum != ledger["total_distributed_bytes"]:
                    raise SizeLedgerError(
                        f"{ledger_path.name}: total_distributed_bytes ({ledger['total_distributed_bytes']}) "
                        f"!= sum of distributed_components ({counted_sum})"
                    )
        else:
            raise SizeLedgerError(f"{ledger_path.name}: unexpected verification_status '{status}'")

        # Verify shader accounting matches shaders/manifest.json
        if ledger["fixture_id"] == "compute_shader":
            shaders = ledger.get("shaders", [])
            if not shaders:
                raise SizeLedgerError(f"{ledger_path.name}: compute_shader ledger must record offline shader")
            for sh in shaders:
                ref_sh = shader_by_id.get(sh["shader_id"])
                if not ref_sh:
                    raise SizeLedgerError(f"{ledger_path.name}: unknown shader_id '{sh['shader_id']}'")
                if status == "verified" and ref_sh["offline_dxil_bytes"] is None:
                    raise SizeLedgerError(
                        f"{ledger_path.name}: verified compute ledger requires measured "
                        "shaders/manifest.json offline_dxil_bytes"
                    )
                if (
                    sh["offline_dxil_bytes"] is not None
                    and ref_sh["offline_dxil_bytes"] is not None
                    and sh["offline_dxil_bytes"] != ref_sh["offline_dxil_bytes"]
                ):
                    raise SizeLedgerError(
                        f"{ledger_path.name}: shader byte mismatch ({sh['offline_dxil_bytes']} != {ref_sh['offline_dxil_bytes']})"
                    )
                if sh["runtime_compilation_used"] or sh["agility_sdk_required"]:
                    raise SizeLedgerError(f"{ledger_path.name}: offline shader must not use runtime DXC or Agility SDK")

    eval_by_id = {
        t["toolchain_id"]: t for t in toolchain_manifest.get("evaluated_toolchains", [])
    }
    fixtures = ["empty_player", "clear_present", "compute_shader"]
    comparisons: list[dict[str, Any]] = []
    for fixture_id in fixtures:
        clang_ledger = ledgers_by_key.get((fixture_id, "clang-cl-lld"))
        msvc_ledger = ledgers_by_key.get((fixture_id, "msvc-ltcg"))
        if not clang_ledger or not msvc_ledger:
            raise SizeLedgerError(f"Missing clang-cl-lld or msvc-ltcg ledger for fixture '{fixture_id}'")

        clang_bytes = clang_ledger["total_distributed_bytes"]
        msvc_bytes = msvc_ledger["total_distributed_bytes"]
        both_measured = (clang_bytes is not None) and (msvc_bytes is not None)
        smaller_on_fixture: str | None = None
        if both_measured:
            if clang_bytes < msvc_bytes:
                smaller_on_fixture = "clang-cl-lld"
            elif msvc_bytes < clang_bytes:
                smaller_on_fixture = "msvc-ltcg"
            else:
                smaller_on_fixture = "tie"

        clang_imports = sorted(
            {canonical_inbox_dll(imp["dll_name"]) for imp in clang_ledger["imports"]}
        )
        msvc_imports = sorted(
            {canonical_inbox_dll(imp["dll_name"]) for imp in msvc_ledger["imports"]}
        )
        if clang_imports != msvc_imports:
            raise SizeLedgerError(
                f"Import closure mismatch between clang-cl-lld ({clang_imports}) and msvc-ltcg ({msvc_imports}) on {fixture_id}"
            )

        clang_text = next(
            (s["raw_size_bytes"] for s in clang_ledger["executable_sections"] if s["name"] == ".text"),
            None,
        )
        msvc_text = next(
            (s["raw_size_bytes"] for s in msvc_ledger["executable_sections"] if s["name"] == ".text"),
            None,
        )

        comparisons.append(
            {
                "fixture_id": fixture_id,
                "verification_status": (
                    "verified"
                    if clang_ledger["verification_status"] == "verified"
                    and msvc_ledger["verification_status"] == "verified"
                    and eval_by_id["clang-cl-lld"].get("fixture_execution_verified") is True
                    and eval_by_id["msvc-ltcg"].get("fixture_execution_verified") is True
                    else "unverified"
                ),
                "clang_cl_lld_bytes": clang_bytes,
                "msvc_ltcg_bytes": msvc_bytes,
                "delta_msvc_minus_clang_bytes": (msvc_bytes - clang_bytes) if both_measured else None,
                "smaller_toolchain": smaller_on_fixture,
                "clang_cl_text_raw_bytes": clang_text,
                "msvc_ltcg_text_raw_bytes": msvc_text,
                "identical_import_closure": True,
                "imports": clang_imports,
            }
        )

    # Reconcile the manifest with the actually recorded ledger measurements before
    # cross-checking. A stale manifest must never force a fixed compiler choice or make a
    # valid MSVC/LTCG-win comparison fail merely because the manifest still named clang-cl.
    manifest_changed = _sync_evaluated_toolchains_from_ledgers(
        toolchain_manifest, ledgers_by_key
    )
    measured_selection = _select_toolchain_from_ledgers(
        toolchain_manifest, ledgers_by_key, fixtures
    )
    selection_reconciled = False
    if measured_selection is not None:
        previous_selection = toolchain_manifest.get("selected_toolchain", {}).get(
            "toolchain_id"
        )
        _apply_selected_toolchain_pin(toolchain_manifest, measured_selection)
        selection_reconciled = previous_selection != measured_selection
    if measured_selection is not None or manifest_changed:
        _derive_toolchain_evidence_text(toolchain_manifest)
        updated_manifest_text = json.dumps(toolchain_manifest, indent=2) + "\n"
        if updated_manifest_text != original_manifest_text:
            toolchain_schema = repo_root / "schemas" / "toolchain-manifest.schema.json"
            if toolchain_schema.exists():
                schema = json.loads(toolchain_schema.read_text(encoding="utf-8"))
                validate_instance(
                    toolchain_manifest, schema, schema, path=toolchain_path.name
                )
                validate_semantic_invariants(
                    toolchain_manifest, toolchain_schema.name, toolchain_path
                )
            toolchain_path.write_text(updated_manifest_text, encoding="utf-8")

    # Cross-check against toolchain-manifest.v1.json evaluated_toolchains
    toolchain_ids = [t["toolchain_id"] for t in toolchain_manifest.get("evaluated_toolchains", [])]
    for tid in toolchain_ids:
        all_tid_ledgers_verified = all(
            ledgers_by_key[(fx, tid)]["verification_status"] == "verified"
            for fx in fixtures
        )
        expected_tid_status = (
            "verified"
            if all_tid_ledgers_verified
            and eval_by_id[tid].get("fixture_execution_verified") is True
            else "unverified"
        )
        if eval_by_id[tid].get("verification_status") != expected_tid_status:
            raise SizeLedgerError(
                f"Toolchain manifest verification_status mismatch for {tid}: "
                f"expected '{expected_tid_status}', got '{eval_by_id[tid].get('verification_status')}'"
            )
    comparison_byte_key = {
        "clang-cl-lld": "clang_cl_lld_bytes",
        "msvc-ltcg": "msvc_ltcg_bytes",
    }
    for comp in comparisons:
        fx = comp["fixture_id"]
        field = "compute_fixture_bytes" if fx == "compute_shader" else f"{fx}_bytes"
        for tid, comparison_key in comparison_byte_key.items():
            if eval_by_id[tid][field] != comp[comparison_key]:
                raise SizeLedgerError(f"Toolchain manifest mismatch for {tid}.{field}")

    selected_id = toolchain_manifest["selected_toolchain"]["toolchain_id"]
    smaller_verified_toolchain: str | None = None
    if all(c["verification_status"] == "verified" for c in comparisons):
        eligible = [
            (
                tid,
                sum(
                    int(eval_by_id[tid]["compute_fixture_bytes" if fx == "compute_shader" else f"{fx}_bytes"])
                    for fx in fixtures
                ),
            )
            for tid in toolchain_ids
        ]
        smaller_verified_toolchain = _winner_from_eligible(toolchain_manifest, eligible)
        if selected_id != smaller_verified_toolchain:
            raise SizeLedgerError(
                f"Toolchain manifest selected_toolchain is '{selected_id}', but measured "
                f"comparison shows '{smaller_verified_toolchain}' is the smaller verified "
                "configuration after measurement reconciliation"
            )
        rationale = toolchain_manifest["selected_toolchain"].get("selection_rationale", "")
        other_id = next((tid for tid in toolchain_ids if tid != selected_id), None)
        if other_id and f"{other_id} is larger on every fixture" in rationale:
            selected_key = comparison_byte_key[selected_id]
            other_key = comparison_byte_key[other_id]
            if not all(int(c[selected_key]) < int(c[other_key]) for c in comparisons):
                raise SizeLedgerError(
                    f"Toolchain manifest selection_rationale claims '{other_id} is larger on every fixture', "
                    "which contradicts the recorded fixture byte measurements"
                )

    return {
        "selected_toolchain": selected_id,
        "smaller_verified_toolchain": smaller_verified_toolchain,
        "selection_reconciled": selection_reconciled,
        "selection_basis": (
            "smallest verified aggregate distributed bytes across empty_player, "
            "clear_present, and compute_shader"
        ),
        "claims_64kib_achievable": False,
        "defer_minimum_size_claim_until_task": "NW-10",
        "fixture_comparisons": comparisons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and verify NW-01 size ledgers and toolchain comparison.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument(
        "--record-release-build",
        type=Path,
        default=None,
        help="Directory of built release executables to record into measured size ledgers",
    )
    parser.add_argument(
        "--toolchain-id",
        choices=["clang-cl-lld", "msvc-ltcg"],
        default="clang-cl-lld",
    )
    parser.add_argument(
        "--deterministic-repro-verified",
        action="store_true",
        default=None,
        help="Assert that two independent clean-directory builds produced identical hashes",
    )
    parser.add_argument(
        "--pdb-debugging-verified",
        action="store_true",
        default=None,
        help="Assert that dev /DEBUG:FULL CodeView/PDB evidence was verified for this toolchain",
    )
    parser.add_argument(
        "--fixture-execution-verified",
        action="store_true",
        default=None,
        help="Assert that the identical release fixtures executed successfully for this toolchain",
    )
    args = parser.parse_args(argv)

    try:
        if args.record_release_build is not None:
            recorded = record_release_ledgers(
                args.repo_root,
                args.record_release_build,
                args.toolchain_id,
                deterministic_repro_verified=args.deterministic_repro_verified,
                pdb_debugging_verified=args.pdb_debugging_verified,
                fixture_execution_verified=args.fixture_execution_verified,
            )
            report = {"recorded": recorded, "comparison": generate_size_comparison_report(args.repo_root)}
        else:
            report = generate_size_comparison_report(args.repo_root)
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        print(f"[NW-LEDGER-ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
