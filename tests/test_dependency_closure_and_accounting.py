from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from dependency_check import (  # noqa: E402
    DependencyClosureError,
    parse_pe64_sections_and_imports,
    verify_all_recorded_ledgers,
    verify_closure,
)
from size_ledger import (  # noqa: E402
    SizeLedgerError,
    compute_distribution_accounting,
    generate_size_comparison_report,
)


def build_synthetic_pe64(imported_dlls: list[str]) -> bytes:
    """Construct a minimal valid PE32+ (x64) binary image with the specified import table."""
    pe_offset = 0x80
    num_sections = 2
    opt_header_size = 240
    section_table_offset = pe_offset + 24 + opt_header_size

    # Section 0: .text at RVA 0x1000, raw offset 0x200, size 0x200
    # Section 1: .rdata (contains import directory) at RVA 0x2000, raw offset 0x400, size 0x400
    rdata_rva = 0x2000
    rdata_raw_ptr = 0x400
    rdata_raw_size = 0x400

    # Build import descriptors inside .rdata
    # Each descriptor is 20 bytes, plus 1 null terminator descriptor
    desc_table_size = (len(imported_dlls) + 1) * 20
    names_offset_in_rdata = desc_table_size
    rdata_buf = bytearray(rdata_raw_size)

    current_str_off = names_offset_in_rdata
    for idx, dll in enumerate(imported_dlls):
        name_rva = rdata_rva + current_str_off
        encoded = dll.encode("ascii") + b"\x00"
        rdata_buf[current_str_off : current_str_off + len(encoded)] = encoded
        current_str_off += len(encoded)
        # Write IMAGE_IMPORT_DESCRIPTOR (OriginalFirstThunk=0, TimeDateStamp=0, ForwarderChain=0, Name=name_rva, FirstThunk=0x2300)
        struct.pack_into("<IIIII", rdata_buf, idx * 20, 0, 0, 0, name_rva, 0x2300 + idx * 8)

    image = bytearray(rdata_raw_ptr + rdata_raw_size)
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, pe_offset)
    image[pe_offset : pe_offset + 4] = b"PE\x00\x00"

    # COFF File Header: Machine=0x8664 (AMD64), NumberOfSections=2, TimeDateStamp=0, SizeOfOptionalHeader=240, Characteristics=0x0022
    struct.pack_into("<HHIIIHH", image, pe_offset + 4, 0x8664, num_sections, 0, 0, 0, opt_header_size, 0x0022)

    opt_offset = pe_offset + 24
    # PE32+ Magic = 0x20B
    struct.pack_into("<H", image, opt_offset, 0x20B)
    # DllCharacteristics at opt_offset + 70: HIGH_ENTROPY_VA | DYNAMIC_BASE | NX_COMPAT | GUARD_CF
    struct.pack_into("<H", image, opt_offset + 70, 0x0020 | 0x0040 | 0x0100 | 0x4000)
    # NumberOfRvaAndSizes = 16 at opt_offset + 108
    struct.pack_into("<I", image, opt_offset + 108, 16)
    # DataDirectory[1] (Import Table) at opt_offset + 112 + 8
    struct.pack_into("<II", image, opt_offset + 120, rdata_rva, desc_table_size)

    # Section 0: .text
    struct.pack_into(
        "<8sIIIIIIHHI",
        image,
        section_table_offset,
        b".text\x00\x00\x00",
        0x180,
        0x1000,
        0x200,
        0x200,
        0,
        0,
        0,
        0,
        0x60000020,
    )
    # Section 1: .rdata
    struct.pack_into(
        "<8sIIIIIIHHI",
        image,
        section_table_offset + 40,
        b".rdata\x00\x00",
        current_str_off,
        rdata_rva,
        rdata_raw_size,
        rdata_raw_ptr,
        0,
        0,
        0,
        0,
        0x40000040,
    )

    image[rdata_raw_ptr : rdata_raw_ptr + rdata_raw_size] = rdata_buf
    return bytes(image)


def test_recorded_ledgers_pass_dependency_and_toolchain_comparison() -> None:
    dep_reports = verify_all_recorded_ledgers(REPO_ROOT)
    assert len(dep_reports) == 6
    assert all(r["closure_verified"] for r in dep_reports)

    comparison = generate_size_comparison_report(REPO_ROOT)
    assert comparison["selected_toolchain"] == "clang-cl-lld"
    assert comparison["claims_64kib_achievable"] is False
    assert comparison["defer_minimum_size_claim_until_task"] == "NW-10"
    by_fixture = {c["fixture_id"]: c for c in comparison["fixture_comparisons"]}
    assert by_fixture["empty_player"]["clang_cl_lld_bytes"] == 10752
    assert by_fixture["empty_player"]["msvc_ltcg_bytes"] == 12288
    assert by_fixture["clear_present"]["clang_cl_lld_bytes"] == 16896
    assert by_fixture["clear_present"]["msvc_ltcg_bytes"] == 19456
    assert by_fixture["compute_shader"]["clang_cl_lld_bytes"] == 14848
    assert by_fixture["compute_shader"]["msvc_ltcg_bytes"] == 16896


def test_synthetic_pe64_import_parser_and_closure_gates() -> None:
    inventory = json.loads((REPO_ROOT / "benchmarks" / "dependency-inventory.v1.json").read_text(encoding="utf-8"))

    valid_pe = build_synthetic_pe64(["KERNEL32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"])
    parsed = parse_pe64_sections_and_imports(valid_pe)
    assert parsed["time_date_stamp"] == 0
    assert [s["name"] for s in parsed["sections"]] == [".text", ".rdata"]
    assert [i.lower() for i in parsed["imports"]] == ["kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]

    res = verify_closure(
        profile="release",
        fixture_id="compute_shader",
        imported_dlls=parsed["imports"],
        sidecar_files=[],
        inventory=inventory,
        expected_fixture_release_imports=["kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"],
    )
    assert res["closure_verified"] is True

    # Forbidden runtime DXC / Agility SDK / non-inbox CRT imports must fail in release
    for forbidden_dll in ("dxcompiler.dll", "D3D12Core.dll", "vcruntime140.dll", "d3d12sdklayers.dll", "nvapi64.dll"):
        bad_pe = build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll", forbidden_dll])
        bad_parsed = parse_pe64_sections_and_imports(bad_pe)
        with pytest.raises(DependencyClosureError):
            verify_closure(
                profile="release",
                fixture_id="empty_player",
                imported_dlls=bad_parsed["imports"],
                sidecar_files=[],
                inventory=inventory,
            )

    # Unexpected sidecars in release must fail, whereas dev allows PDBs and d3d12sdklayers.dll
    with pytest.raises(DependencyClosureError, match="sidecar"):
        verify_closure(
            profile="release",
            fixture_id="empty_player",
            imported_dlls=["kernel32.dll", "ucrtbase.dll"],
            sidecar_files=["nw_empty_player.pdb"],
            inventory=inventory,
        )

    dev_res = verify_closure(
        profile="dev",
        fixture_id="clear_present",
        imported_dlls=["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll", "ucrtbased.dll"],
        sidecar_files=["nw_clear_present_fixture.pdb", "d3d12sdklayers.dll"],
        inventory=inventory,
    )
    assert dev_res["closure_verified"] is True


def test_distribution_byte_accounting_excludes_only_inbox_and_driver_dlls() -> None:
    inventory = json.loads((REPO_ROOT / "benchmarks" / "dependency-inventory.v1.json").read_text(encoding="utf-8"))
    candidates = [
        {"name": "nw_compute_shader_fixture.exe", "category": "executable", "size_bytes": 14848, "sha256": "a" * 64},
        {"name": "kernel32.dll", "category": "dll", "size_bytes": 780000},
        {"name": "d3d12.dll", "category": "dll", "size_bytes": 150000},
        {"name": "dxgi.dll", "category": "dll", "size_bytes": 900000},
        {"name": "ucrtbase.dll", "category": "dll", "size_bytes": 1100000},
        {"name": "nvwgf2umx.dll", "category": "dll", "size_bytes": 85000000},
        {"name": "D3D12Core.dll", "category": "dll", "size_bytes": 4200000, "sha256": "b" * 64},
        {"name": "demo_scene.nwpkg", "category": "data_payload", "size_bytes": 8192, "sha256": "c" * 64},
    ]
    acct = compute_distribution_accounting(candidates, inventory)
    counted_names = [c["name"] for c in acct["distributed_components"]]
    excluded_names = [c["name"] for c in acct["excluded_system_components"]]

    assert counted_names == ["nw_compute_shader_fixture.exe", "D3D12Core.dll", "demo_scene.nwpkg"]
    assert set(excluded_names) == {"kernel32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll", "nvwgf2umx.dll"}
    assert acct["total_distributed_bytes"] == 14848 + 4200000 + 8192

    with pytest.raises(SizeLedgerError):
        compute_distribution_accounting(
            [{"name": "uncounted_sidecar.bin", "category": "sidecar", "size_bytes": 0}],
            inventory,
        )
