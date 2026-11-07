#!/usr/bin/env python3
"""Automated Dependency Closure & Sidecar Verifier for Nodewerk NW-01.

Enforces separate development ('dev') and release ('release', 'compact_experiment')
dependency closures defined in benchmarks/dependency-inventory.v1.json.
Fails immediately if:
- An unexpected or forbidden DLL/shared-library import is present in a binary or ledger;
- A forbidden or uncounted sidecar DLL/file is present in the deployment directory;
- Compiler selection (clang-cl vs MSVC) silently changes the expected release import closure;
- A binary passed for inspection is neither a valid PE32+ nor a valid ELF64 executable.
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


ALLOWED_HOST_ELF_NEEDED_LIBS = {
    "libc.so.6",
    "libm.so.6",
    "libgcc_s.so.1",
    "libpthread.so.0",
    "libdl.so.2",
    "ld-linux-x86-64.so.2",
}

# Release deployment closures may contain only the player/fixture executables themselves.
# Any additional file with these suffixes is a required sidecar (DLL, shader, payload, model,
# or executable) and must be counted or rejected rather than silently ignored.
SIDECAR_SUFFIXES = {
    ".dll",
    ".exe",
    ".so",
    ".pdb",
    ".dxil",
    ".cso",
    ".bin",
    ".dat",
    ".pak",
    ".pack",
    ".nwp",
    ".model",
    ".mesh",
    ".dds",
    ".ktx2",
    ".png",
    ".qoa",
    ".ogg",
}


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
    if opt_offset + opt_header_size > len(pe_bytes):
        raise ValueError("Truncated PE optional header")
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
        if sec_off + 40 > len(pe_bytes):
            raise ValueError("Truncated PE section table")
        raw_name, virtual_size, virtual_address, raw_size, raw_ptr, _, _, _, _, sec_chars = struct.unpack_from(
            "<8sIIIIIIHHI", pe_bytes, sec_off
        )
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", errors="replace")
        sections.append(
            {
                "name": name,
                "virtual_size_bytes": virtual_size,
                "raw_size_bytes": raw_size,
                "virtual_address": virtual_address,
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


def parse_elf64_needed_libraries(elf_bytes: bytes) -> dict[str, Any]:
    """Parse section headers and DT_NEEDED shared libraries from a 64-bit little-endian ELF binary."""
    if len(elf_bytes) < 64 or elf_bytes[:4] != b"\x7fELF":
        raise ValueError("Not a valid ELF executable")
    ei_class = elf_bytes[4]
    ei_data = elf_bytes[5]
    if ei_class != 2 or ei_data != 1:
        raise ValueError(f"Expected 64-bit little-endian ELF (got class={ei_class}, data={ei_data})")

    e_shoff = struct.unpack_from("<Q", elf_bytes, 40)[0]
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", elf_bytes, 58)
    if e_shoff == 0 or e_shnum == 0 or e_shoff + e_shnum * e_shentsize > len(elf_bytes):
        raise ValueError("ELF binary is missing a valid section header table")

    sections: list[dict[str, Any]] = []
    for idx in range(e_shnum):
        sh_pos = e_shoff + idx * e_shentsize
        sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size, sh_link, sh_info, sh_addralign, sh_entsize = (
            struct.unpack_from("<IIQQQQIIQQ", elf_bytes, sh_pos)
        )
        if sh_type != 8 and (sh_offset + sh_size > len(elf_bytes)):  # SHT_NOBITS == 8
            raise ValueError(f"ELF section {idx} exceeds file bounds")
        sections.append(
            {
                "index": idx,
                "sh_name": sh_name,
                "sh_type": sh_type,
                "sh_offset": sh_offset,
                "sh_size": sh_size,
                "sh_link": sh_link,
                "sh_entsize": sh_entsize,
            }
        )

    def read_strtab(strtab_sec: dict[str, Any], str_offset: int) -> str:
        base = strtab_sec["sh_offset"]
        limit = base + strtab_sec["sh_size"]
        pos = base + str_offset
        if pos >= limit:
            return ""
        end = elf_bytes.find(b"\x00", pos, limit)
        if end == -1:
            end = limit
        return elf_bytes[pos:end].decode("ascii", errors="replace")

    shstr_sec = sections[e_shstrndx] if e_shstrndx < len(sections) else None
    section_names: list[str] = []
    for sec in sections:
        sec_name = read_strtab(shstr_sec, sec["sh_name"]) if shstr_sec else ""
        sec["name"] = sec_name
        if sec_name:
            section_names.append(sec_name)

    needed_libs: list[str] = []
    # SHT_DYNAMIC == 6
    for sec in sections:
        if sec["sh_type"] == 6:
            strtab_idx = sec["sh_link"]
            if strtab_idx >= len(sections):
                raise ValueError("ELF .dynamic section has invalid sh_link string table index")
            dynstr_sec = sections[strtab_idx]
            ent_size = sec["sh_entsize"] or 16
            pos = sec["sh_offset"]
            end = pos + sec["sh_size"]
            while pos + 16 <= end:
                d_tag, d_val = struct.unpack_from("<qQ", elf_bytes, pos)
                if d_tag == 0:  # DT_NULL
                    break
                if d_tag == 1:  # DT_NEEDED
                    lib_name = read_strtab(dynstr_sec, d_val)
                    if lib_name:
                        needed_libs.append(lib_name)
                pos += ent_size

    return {
        "format": "ELF64",
        "sections": section_names,
        "needed_libraries": needed_libs,
    }


def verify_elf64_host_closure(
    *,
    profile: str,
    fixture_id: str,
    needed_libs: list[str],
    sidecar_files: list[str],
) -> dict[str, Any]:
    """Verify that a host ELF64 player binary does not link C++ stdlib/compiler libraries or carry sidecars."""
    violations: list[str] = []
    for lib in needed_libs:
        lower_lib = lib.lower()
        if "stdc++" in lower_lib or "libc++" in lower_lib:
            violations.append(
                f"Forbidden C++ standard library dependency '{lib}' in host ELF player binary {fixture_id} ({profile}); "
                "player builds must disable exceptions/RTTI and avoid heavy C++ runtime dependencies"
            )
        elif "dxcompiler" in lower_lib or "d3dcompiler" in lower_lib:
            violations.append(
                f"Forbidden runtime shader compiler dependency '{lib}' in {fixture_id} ({profile})"
            )
        elif lower_lib not in ALLOWED_HOST_ELF_NEEDED_LIBS:
            violations.append(
                f"Unexpected shared library dependency '{lib}' in host ELF binary {fixture_id} ({profile}); "
                f"allowed={sorted(ALLOWED_HOST_ELF_NEEDED_LIBS)}"
            )

    for sidecar in sidecar_files:
        lower_sidecar = sidecar.lower()
        if profile in ("release", "compact_experiment"):
            violations.append(
                f"Unexpected sidecar file '{sidecar}' detected in {fixture_id} ({profile}) deployment closure"
            )
        elif not lower_sidecar.endswith((".pdb", ".debug")):
            violations.append(
                f"Unexpected sidecar file '{sidecar}' detected in {fixture_id} ({profile}) closure"
            )

    if violations:
        raise DependencyClosureError("; ".join(violations))

    return {
        "fixture_id": fixture_id,
        "profile": profile,
        "binary_format": "ELF64",
        "needed_libraries": needed_libs,
        "sidecar_files": sidecar_files,
        "closure_verified": True,
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


def verify_binary_file(
    binary_path: Path,
    *,
    profile: str,
    fixture_id: str,
    inventory: dict[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    """Inspect a built PE32+ or ELF64 binary file on disk and verify its dependency and sidecar closure."""
    if not binary_path.exists():
        raise DependencyClosureError(f"Binary not found: {binary_path}")
    raw = binary_path.read_bytes()
    sidecars = [
        p.name
        for p in binary_path.parent.iterdir()
        if p.is_file()
        and p.name != binary_path.name
        and p.suffix.lower() in SIDECAR_SUFFIXES
    ]

    if raw[:2] == b"MZ":
        try:
            pe_info = parse_pe64_sections_and_imports(raw)
        except ValueError as exc:
            raise DependencyClosureError(f"Invalid PE32+ executable '{binary_path.name}': {exc}") from exc
        imported_dlls = pe_info["imports"]
        fx_json = repo_root / "fixtures" / fixture_id / "fixture.json"
        expected_imports = None
        if fx_json.exists() and profile in ("release", "compact_experiment"):
            expected_imports = json.loads(fx_json.read_text(encoding="utf-8")).get("expected_release_imports")
        return verify_closure(
            profile=profile,
            fixture_id=fixture_id,
            imported_dlls=imported_dlls,
            sidecar_files=sidecars,
            inventory=inventory,
            expected_fixture_release_imports=expected_imports,
        )

    if raw[:4] == b"\x7fELF":
        try:
            elf_info = parse_elf64_needed_libraries(raw)
        except ValueError as exc:
            raise DependencyClosureError(f"Invalid ELF64 executable '{binary_path.name}': {exc}") from exc
        return verify_elf64_host_closure(
            profile=profile,
            fixture_id=fixture_id,
            needed_libs=elf_info["needed_libraries"],
            sidecar_files=sidecars,
        )

    raise DependencyClosureError(
        f"Binary '{binary_path.name}' is neither a valid Windows PE32+ executable nor a valid ELF64 executable"
    )


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
            res = verify_binary_file(
                args.binary,
                profile=args.profile,
                fixture_id=args.fixture,
                inventory=inventory,
                repo_root=args.repo_root,
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
