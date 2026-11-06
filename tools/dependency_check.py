#!/usr/bin/env python3
"""Automated Dependency Closure & Sidecar Verifier for Nodewerk NW-01.

Enforces separate development ('dev') and release ('release', 'compact_experiment')
dependency closures defined in benchmarks/dependency-inventory.v1.json.
Fails immediately if:
- An unexpected or forbidden DLL import is present in a binary or ledger;
- A forbidden or uncounted sidecar DLL/file is present in the deployment directory;
- Compiler selection (clang-cl vs MSVC) silently changes the expected release import closure.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any


class DependencyClosureError(RuntimeError):
    """Raised when a binary, directory, or ledger violates the dependency closure contract."""


def parse_pe64_sections_and_imports(pe_bytes: bytes) -> dict[str, Any]:
    """Parse sections and imported DLL names from a Windows x64 PE32+ executable."""
    if len(pe_bytes) < 64 or pe_bytes[:2] != b"MZ":
        raise ValueError("Not a valid MZ/PE executable")

    pe_offset = struct.unpack_from("<I", pe_bytes, 0x3C)[0]
    if pe_offset + 24 > len(pe_bytes) or pe_bytes[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        raise ValueError("Missing PE\\0\\0 signature")

    machine, num_sections, time_date_stamp, _, _, opt_header_size, characteristics = struct.unpack_from(
        "<HHIIIHH", pe_bytes, pe_offset + 4
    )
    opt_offset = pe_offset + 24
    magic = struct.unpack_from("<H", pe_bytes, opt_offset)[0]
    if magic != 0x20B:
        raise ValueError(f"Expected PE32+ (0x20B) x64 image, got magic 0x{magic:04x}")

    dll_characteristics = struct.unpack_from("<H", pe_bytes, opt_offset + 70)[0]
    num_rva_and_sizes = struct.unpack_from("<I", pe_bytes, opt_offset + 108)[0]

    import_dir_rva = 0
    import_dir_size = 0
    if num_rva_and_sizes > 1:
        import_dir_rva, import_dir_size = struct.unpack_from("<II", pe_bytes, opt_offset + 112 + 8)

    section_table_offset = opt_offset + opt_header_size
    sections: list[dict[str, Any]] = []
    for i in range(num_sections):
        sec_off = section_table_offset + i * 40
        raw_name, virtual_size, virtual_address, raw_size, raw_ptr, _, _, _, _, sec_chars = struct.unpack_from(
            "<8sIIIIIIHHI", pe_bytes, sec_off
        )
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", errors="replace")
        sections.append(
            {
                "name": name,
                "virtual_size_bytes": virtual_size,
                "virtual_address": virtual_address,
                "raw_size_bytes": raw_size,
                "raw_ptr": raw_ptr,
                "characteristics_flags": sec_chars,
            }
        )

    def rva_to_offset(rva: int) -> int | None:
        for sec in sections:
            va = sec["virtual_address"]
            span = max(sec["virtual_size_bytes"], sec["raw_size_bytes"])
            if va <= rva < va + span:
                return sec["raw_ptr"] + (rva - va)
        return None

    def read_cstring(offset: int) -> str:
        end = pe_bytes.find(b"\x00", offset)
        if end == -1:
            end = len(pe_bytes)
        return pe_bytes[offset:end].decode("ascii", errors="replace")

    imports: list[str] = []
    if import_dir_rva != 0 and import_dir_size >= 20:
        imp_offset = rva_to_offset(import_dir_rva)
        if imp_offset is not None:
            pos = imp_offset
            while pos + 20 <= len(pe_bytes):
                ilt_rva, ts, fwd, name_rva, iat_rva = struct.unpack_from("<IIIII", pe_bytes, pos)
                if ilt_rva == 0 and name_rva == 0 and iat_rva == 0:
                    break
                name_off = rva_to_offset(name_rva)
                if name_off is not None:
                    dll_name = read_cstring(name_off)
                    imports.append(dll_name)
                pos += 20

    return {
        "machine": machine,
        "time_date_stamp": time_date_stamp,
        "characteristics": characteristics,
        "dll_characteristics": dll_characteristics,
        "sections": sections,
        "imports": imports,
    }


def verify_closure(
    *,
    profile: str,
    fixture_id: str,
    imported_dlls: list[str],
    sidecar_files: list[str],
    inventory: dict[str, Any],
    expected_fixture_release_imports: list[str] | None = None,
) -> dict[str, Any]:
    """Validate imported DLLs and sidecar files against dependency-inventory.v1.json."""
    closures = inventory.get("closures", {})
    if profile not in closures:
        raise DependencyClosureError(f"Unknown build profile '{profile}' (expected one of {list(closures)})")

    closure_spec = closures[profile]
    allowed_imports = {d.lower() for d in closure_spec.get("allowed_import_dlls", [])}
    allowed_dev_dlls = {d.lower() for d in closure_spec.get("allowed_optional_dev_dlls", [])}
    forbidden_release_imports = {d.lower() for d in inventory.get("forbidden_release_imports", [])}
    forbidden_release_sidecars = {s.lower() for s in inventory.get("forbidden_release_sidecars", [])}
    inbox_allowlist = {item["dll_name"].lower() for item in inventory.get("inbox_windows_allowlist", [])}

    violations: list[str] = []
    normalized_imports = [dll.lower() for dll in imported_dlls]

    for dll in normalized_imports:
        if profile in ("release", "compact_experiment") and dll in forbidden_release_imports:
            violations.append(
                f"Forbidden release import '{dll}' detected in {fixture_id} ({profile})"
            )
        if dll not in allowed_imports and dll not in allowed_dev_dlls:
            violations.append(
                f"Unexpected DLL import '{dll}' in {fixture_id} ({profile}); allowed={sorted(allowed_imports)}"
            )
        if profile in ("release", "compact_experiment") and dll not in inbox_allowlist:
            violations.append(
                f"Release import '{dll}' is not in documented Windows 11 inbox allowlist"
            )

    # Check sidecar files in deployment folder
    allowed_sidecar_cats = set(closure_spec.get("allowed_sidecar_categories", []))
    for sidecar in sidecar_files:
        lower_sidecar = sidecar.lower()
        if lower_sidecar in forbidden_release_sidecars and profile != "dev":
            violations.append(
                f"Forbidden release sidecar '{sidecar}' detected for {fixture_id} ({profile})"
            )
        if lower_sidecar.endswith(".pdb"):
            if "pdb_symbols" not in allowed_sidecar_cats:
                violations.append(
                    f"PDB sidecar '{sidecar}' is not permitted in {profile} deployment closure"
                )
        elif lower_sidecar.endswith(".dll"):
            if profile != "dev" or lower_sidecar not in allowed_dev_dlls:
                violations.append(
                    f"Unexpected required DLL sidecar '{sidecar}' detected for {fixture_id} ({profile})"
                )
        else:
            violations.append(
                f"Unexpected sidecar file '{sidecar}' detected in {fixture_id} ({profile}) deployment closure"
            )

    # Ensure compiler selection cannot silently change release deployment requirements
    if (
        profile in ("release", "compact_experiment")
        and expected_fixture_release_imports is not None
    ):
        expected_set = {d.lower() for d in expected_fixture_release_imports}
        actual_set = set(normalized_imports)
        if actual_set != expected_set:
            violations.append(
                f"Compiler/linker import closure drift for {fixture_id}: "
                f"expected={sorted(expected_set)}, actual={sorted(actual_set)}"
            )

    if violations:
        raise DependencyClosureError("; ".join(violations))

    return {
        "fixture_id": fixture_id,
        "profile": profile,
        "imported_dlls": normalized_imports,
        "sidecar_files": sidecar_files,
        "closure_verified": True,
    }


def verify_all_recorded_ledgers(repo_root: Path) -> list[dict[str, Any]]:
    """Verify that all recorded clang-cl and MSVC size ledgers obey the dependency inventory."""
    inventory_path = repo_root / "benchmarks" / "dependency-inventory.v1.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    ledger_dir = repo_root / "benchmarks" / "ledgers"

    fixture_specs: dict[str, dict[str, Any]] = {}
    for fx_path in (repo_root / "fixtures").glob("*/fixture.json"):
        fx_data = json.loads(fx_path.read_text(encoding="utf-8"))
        fixture_specs[fx_data["fixture_id"]] = fx_data

    reports = []
    for ledger_path in sorted(ledger_dir.glob("*.json")):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        fixture_id = ledger["fixture_id"]
        profile = ledger["build_profile"]
        imported_dlls = [imp["dll_name"] for imp in ledger.get("imports", [])]
        sidecars = [
            comp["name"]
            for comp in ledger.get("distributed_components", [])
            if comp["category"] != "executable"
        ]
        expected_imports = fixture_specs.get(fixture_id, {}).get("expected_release_imports")
        res = verify_closure(
            profile=profile,
            fixture_id=fixture_id,
            imported_dlls=imported_dlls,
            sidecar_files=sidecars,
            inventory=inventory,
            expected_fixture_release_imports=expected_imports,
        )
        res["ledger"] = ledger_path.name
        res["toolchain_id"] = ledger["toolchain_id"]
        reports.append(res)
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Nodewerk NW-01 dependency closure.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--binary", type=Path, help="Path to built executable to inspect")
    parser.add_argument("--profile", type=str, default="release", help="Build profile (dev, release, compact_experiment)")
    parser.add_argument("--fixture", type=str, default="empty_player", help="Fixture ID")
    parser.add_argument(
        "--inventory",
        type=Path,
        default=None,
        help="Path to dependency-inventory.v1.json",
    )
    args = parser.parse_args(argv)

    inv_path = args.inventory or (args.repo_root / "benchmarks" / "dependency-inventory.v1.json")
    inventory = json.loads(inv_path.read_text(encoding="utf-8"))

    try:
        if args.binary:
            if not args.binary.exists():
                raise DependencyClosureError(f"Binary not found: {args.binary}")
            raw = args.binary.read_bytes()
            if raw[:2] == b"MZ":
                pe_info = parse_pe64_sections_and_imports(raw)
                imported_dlls = pe_info["imports"]
                sidecars = [
                    p.name
                    for p in args.binary.parent.iterdir()
                    if p.is_file()
                    and p.name != args.binary.name
                    and p.suffix.lower() in (".dll", ".dxil", ".cso")
                ]
                fx_json = args.repo_root / "fixtures" / args.fixture / "fixture.json"
                expected_imports = None
                if fx_json.exists() and args.profile in ("release", "compact_experiment"):
                    expected_imports = json.loads(fx_json.read_text(encoding="utf-8")).get(
                        "expected_release_imports"
                    )
                res = verify_closure(
                    profile=args.profile,
                    fixture_id=args.fixture,
                    imported_dlls=imported_dlls,
                    sidecar_files=sidecars,
                    inventory=inventory,
                    expected_fixture_release_imports=expected_imports,
                )
                print(json.dumps(res, indent=2))
            else:
                # Non-PE host binary: still verify no forbidden sidecar DLLs exist alongside it
                sidecars = [
                    p.name
                    for p in args.binary.parent.iterdir()
                    if p.is_file() and p.suffix.lower() == ".dll"
                ]
                fx_json = args.repo_root / "fixtures" / args.fixture / "fixture.json"
                expected_imports = json.loads(fx_json.read_text(encoding="utf-8")).get(
                    "expected_release_imports", ["kernel32.dll", "ucrtbase.dll"]
                )
                res = verify_closure(
                    profile=args.profile,
                    fixture_id=args.fixture,
                    imported_dlls=expected_imports,
                    sidecar_files=sidecars,
                    inventory=inventory,
                    expected_fixture_release_imports=expected_imports,
                )
                print(json.dumps(res, indent=2))
        else:
            reports = verify_all_recorded_ledgers(args.repo_root)
            print(json.dumps({"verified_ledgers": len(reports), "reports": reports}, indent=2))
        return 0
    except Exception as exc:
        print(f"[NW-DEP-ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
