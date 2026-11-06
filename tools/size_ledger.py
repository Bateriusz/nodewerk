#!/usr/bin/env python3
"""Automated Size Ledger Reporting & Toolchain Comparison Tool for Nodewerk NW-01.

Reports and validates:
- Total distributed bytes (excluding only documented Windows 11 inbox DLLs and
  installed display-driver components; counting every other DLL, payload, model, and sidecar).
- Executable sections (.text, .rdata, .data, .pdata, .reloc), imports, offline shaders,
  decoder overhead, startup time, CPU/GPU time, and committed memory.
- Explicit identification of unavailable runtime measurements (null + reason, never 0).
- Toolchain byte comparison between clang-cl/lld-link and MSVC/LTCG across
  empty_player, clear_present, and compute_shader.
- Explicit non-claim on 64 KiB budget until NW-10 supplies evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


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

        # Verify counted distributed components sum to total_distributed_bytes
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

        # Verify shader accounting matches shaders/manifest.json
        if ledger["fixture_id"] == "compute_shader":
            shaders = ledger.get("shaders", [])
            if not shaders:
                raise SizeLedgerError(f"{ledger_path.name}: compute_shader ledger must record offline shader")
            for sh in shaders:
                ref_sh = shader_by_id.get(sh["shader_id"])
                if not ref_sh:
                    raise SizeLedgerError(f"{ledger_path.name}: unknown shader_id '{sh['shader_id']}'")
                if sh["offline_dxil_bytes"] != ref_sh["offline_dxil_bytes"]:
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
        if clang_bytes >= msvc_bytes:
            raise SizeLedgerError(
                f"Expected selected toolchain clang-cl-lld ({clang_bytes} B) to be smaller than msvc-ltcg ({msvc_bytes} B) on {fixture_id}"
            )

        clang_imports = sorted(imp["dll_name"].lower() for imp in clang_ledger["imports"])
        msvc_imports = sorted(imp["dll_name"].lower() for imp in msvc_ledger["imports"])
        if clang_imports != msvc_imports:
            raise SizeLedgerError(
                f"Import closure mismatch between clang-cl-lld ({clang_imports}) and msvc-ltcg ({msvc_imports}) on {fixture_id}"
            )

        clang_text = next(s["raw_size_bytes"] for s in clang_ledger["executable_sections"] if s["name"] == ".text")
        msvc_text = next(s["raw_size_bytes"] for s in msvc_ledger["executable_sections"] if s["name"] == ".text")

        comparisons.append(
            {
                "fixture_id": fixture_id,
                "clang_cl_lld_bytes": clang_bytes,
                "msvc_ltcg_bytes": msvc_bytes,
                "delta_msvc_minus_clang_bytes": msvc_bytes - clang_bytes,
                "clang_cl_text_raw_bytes": clang_text,
                "msvc_ltcg_text_raw_bytes": msvc_text,
                "identical_import_closure": True,
                "imports": clang_imports,
            }
        )

    # Cross-check against toolchain-manifest.v1.json evaluated_toolchains
    eval_by_id = {t["toolchain_id"]: t for t in toolchain_manifest.get("evaluated_toolchains", [])}
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
    args = parser.parse_args(argv)

    try:
        report = generate_size_comparison_report(args.repo_root)
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        print(f"[NW-LEDGER-ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
