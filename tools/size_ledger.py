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
    SIDECAR_SUFFIXES,
    parse_pe64_sections_and_imports,
    verify_closure,
)
from schema_validator import validate_instance, validate_semantic_invariants


class SizeLedgerError(ValueError):
    """Raised when a size ledger violates accounting or toolchain comparison contracts."""


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

        if lower_name in inbox_dlls:
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


KNOWN_FIXTURE_EXECUTABLES = {
    "nw_empty_player.exe",
    "nw_clear_present_fixture.exe",
    "nw_compute_shader_fixture.exe",
}


def _classify_imported_dll(dll_name: str, inventory: dict[str, Any]) -> str:
    lower_dll = dll_name.lower()
    if lower_dll == "ucrtbase.dll":
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
    repo_root: Path | None = None,
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

    sidecars = [
        p.name
        for p in binary_path.parent.iterdir()
        if p.is_file()
        and p.name != binary_path.name
        and p.name not in KNOWN_FIXTURE_EXECUTABLES
        and p.suffix.lower() in SIDECAR_SUFFIXES
    ]

    verify_closure(
        profile=profile,
        fixture_id=fixture_id,
        imported_dlls=pe_info["imports"],
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
    ledger["verification_status"] = "verified"
    ledger.pop("unverified_reason", None)
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

    ledger["unavailable_measurements"] = [
        m
        for m in ledger.get("unavailable_measurements", [])
        if m["metric"] not in ("total_distributed_bytes", "executable_sections", "shaders[0].offline_dxil_bytes")
    ]

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
        f"deterministic_repro_verified={entry.get('deterministic_repro_verified') is True}"
    )


def _set_toolchain_unverified(eval_entry: dict[str, Any]) -> None:
    """Atomically clear measurements and both evidence flags for an unverified toolchain entry.

    verification_status=unverified is only valid when no byte measurement is claimed and
    neither the CodeView/PDB nor the two-clean-directory reproducibility evidence is asserted.
    """
    for field, _ in _BYTE_FIELDS:
        eval_entry[field] = None
    eval_entry["deterministic_repro_verified"] = False
    eval_entry["pdb_debugging_verified"] = False
    eval_entry["verification_status"] = "unverified"


def _derive_toolchain_evidence_text(toolchain_manifest: dict[str, Any]) -> None:
    """Refresh the human-readable selection/debug/reproducibility evidence from recorded state.

    Keeps the manifest from claiming that byte counts remain unverified (or that live PDB
    verification is pending) after tools/verify_nw01.py has recorded measured evidence.
    """
    entries = {t["toolchain_id"]: t for t in toolchain_manifest.get("evaluated_toolchains", [])}
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

    if selected_bytes_complete and other_bytes_complete:
        if selected_verified and other_verified:
            details = "; ".join(
                f"{label} {selected.get(field)} B vs {other.get(field)} B"
                for field, label in _BYTE_FIELDS
            )
            pin["selection_rationale"] = (
                f"Selected {selected_name} + {selected_linker} as the primary toolchain configuration "
                f"after identical-fixture comparison against {other_label}. Measured distributed bytes: "
                f"{details}. {other_label} is larger on every fixture, and both toolchains have verified "
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
) -> dict[str, Any]:
    """Record measured release artifacts into the toolchain-specific size ledgers.

    Updates only the selected toolchain's ledgers and byte fields. Unmeasured comparison
    toolchains remain explicitly unverified rather than being copied or interpolated.

    A toolchain is only marked verified when its fixtures were measured AND both the two-clean-
    directory reproducibility result and the CodeView/PDB debugging result are explicitly True.
    Missing evidence therefore cannot silently produce a verified toolchain or ledger.
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
        deterministic_repro_verified is True and pdb_debugging_verified is True
    )
    ready_specs = [
        (fixture_id, exe_path, ledger_path, ledger_name, manifest_field, dxil_bytes)
        for fixture_id, exe_name, ledger_name, manifest_field, dxil_bytes in specs
        for exe_path, ledger_path in [
            (binary_dir / exe_name, repo_root / "benchmarks" / "ledgers" / ledger_name)
        ]
        if exe_path.exists()
        and ledger_path.exists()
        and (fixture_id != "compute_shader" or dxil_bytes is not None)
    ]
    all_three_ready = len(ready_specs) == len(specs)

    if not verification_complete or not all_three_ready:
        _set_toolchain_unverified(eval_entry)
        _refresh_toolchain_unavailable_measurements(toolchain_manifest)
        _derive_toolchain_evidence_text(toolchain_manifest)
        toolchain_schema_path = repo_root / "schemas" / "toolchain-manifest.schema.json"
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
        return {
            "toolchain_id": toolchain_id,
            "verification_status": "unverified",
            "recorded_fixtures": {},
            "reason": (
                "Refusing to record verified ledgers: two-clean-directory reproducibility and "
                "CodeView/PDB debugging evidence must both be explicitly verified."
                if not verification_complete
                else "Refusing to record verified ledgers: not all three fixture executables "
                "and offline DXIL measurements were available."
            ),
            "deterministic_repro_verified": False,
            "pdb_debugging_verified": False,
            "comparison_toolchain_bytes_remain_unverified": other_toolchains_unverified,
        }

    measured_bytes_by_field: dict[str, int] = {}
    for fixture_id, exe_path, ledger_path, ledger_name, manifest_field, dxil_bytes in ready_specs:
        ledger = record_ledger_from_pe_binary(
            exe_path,
            ledger_path,
            inventory,
            offline_dxil_bytes=dxil_bytes,
            repo_root=repo_root,
        )
        measured_bytes_by_field[manifest_field] = ledger["total_distributed_bytes"]
        recorded[fixture_id] = {
            "ledger": ledger_name,
            "total_distributed_bytes": ledger["total_distributed_bytes"],
            "sha256": ledger["distributed_components"][0]["sha256"],
        }

    for field, total_bytes in measured_bytes_by_field.items():
        eval_entry[field] = total_bytes
    eval_entry["deterministic_repro_verified"] = True
    eval_entry["pdb_debugging_verified"] = True
    eval_entry["verification_status"] = "verified"

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
    return {
        "toolchain_id": toolchain_id,
        "verification_status": eval_entry["verification_status"],
        "recorded_fixtures": recorded,
        "comparison_toolchain_bytes_remain_unverified": other_toolchains_unverified,
    }


def generate_size_comparison_report(repo_root: Path) -> dict[str, Any]:
    """Validate all recorded ledgers and compare clang-cl/lld-link vs MSVC/LTCG."""
    inventory_path = repo_root / "benchmarks" / "dependency-inventory.v1.json"
    toolchain_path = repo_root / "benchmarks" / "toolchain-manifest.v1.json"
    shader_manifest_path = repo_root / "shaders" / "manifest.json"

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    toolchain_manifest = json.loads(toolchain_path.read_text(encoding="utf-8"))
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
            if ledger.get("total_distributed_bytes") is not None:
                raise SizeLedgerError(
                    f"{ledger_path.name}: unverified ledger must not claim a non-null total_distributed_bytes"
                )
            if "total_distributed_bytes" not in unavailable_metrics:
                raise SizeLedgerError(
                    f"{ledger_path.name}: unverified ledger must list 'total_distributed_bytes' in unavailable_measurements"
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
        if both_measured and clang_bytes >= msvc_bytes:
            raise SizeLedgerError(
                f"Expected selected toolchain clang-cl-lld ({clang_bytes} B) to be smaller than msvc-ltcg ({msvc_bytes} B) on {fixture_id}"
            )

        clang_imports = sorted(imp["dll_name"].lower() for imp in clang_ledger["imports"])
        msvc_imports = sorted(imp["dll_name"].lower() for imp in msvc_ledger["imports"])
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
                    else "unverified"
                ),
                "clang_cl_lld_bytes": clang_bytes,
                "msvc_ltcg_bytes": msvc_bytes,
                "delta_msvc_minus_clang_bytes": (msvc_bytes - clang_bytes) if both_measured else None,
                "clang_cl_text_raw_bytes": clang_text,
                "msvc_ltcg_text_raw_bytes": msvc_text,
                "identical_import_closure": True,
                "imports": clang_imports,
            }
        )

    # Cross-check against toolchain-manifest.v1.json evaluated_toolchains
    eval_by_id = {t["toolchain_id"]: t for t in toolchain_manifest.get("evaluated_toolchains", [])}
    for tid in ("clang-cl-lld", "msvc-ltcg"):
        all_tid_ledgers_verified = all(
            ledgers_by_key[(fx, tid)]["verification_status"] == "verified"
            for fx in fixtures
        )
        expected_tid_status = "verified" if all_tid_ledgers_verified else "unverified"
        if eval_by_id[tid].get("verification_status") != expected_tid_status:
            raise SizeLedgerError(
                f"Toolchain manifest verification_status mismatch for {tid}: "
                f"expected '{expected_tid_status}', got '{eval_by_id[tid].get('verification_status')}'"
            )
    for comp in comparisons:
        fx = comp["fixture_id"]
        field = "compute_fixture_bytes" if fx == "compute_shader" else f"{fx}_bytes"
        if eval_by_id["clang-cl-lld"][field] != comp["clang_cl_lld_bytes"]:
            raise SizeLedgerError(f"Toolchain manifest mismatch for clang-cl-lld.{field}")
        if eval_by_id["msvc-ltcg"][field] != comp["msvc_ltcg_bytes"]:
            raise SizeLedgerError(f"Toolchain manifest mismatch for msvc-ltcg.{field}")

    return {
        "selected_toolchain": toolchain_manifest["selected_toolchain"]["toolchain_id"],
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
    args = parser.parse_args(argv)

    try:
        if args.record_release_build is not None:
            recorded = record_release_ledgers(
                args.repo_root,
                args.record_release_build,
                args.toolchain_id,
                deterministic_repro_verified=args.deterministic_repro_verified,
                pdb_debugging_verified=args.pdb_debugging_verified,
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
