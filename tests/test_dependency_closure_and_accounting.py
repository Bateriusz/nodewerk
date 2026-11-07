from __future__ import annotations

import json
import shutil
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from dependency_check import (  # noqa: E402
    DependencyClosureError,
    parse_elf64_needed_libraries,
    parse_pe64_sections_and_imports,
    verify_all_recorded_ledgers,
    verify_binary_file,
    verify_closure,
)
from size_ledger import (  # noqa: E402
    SizeLedgerError,
    _measured_import_entries,
    compute_distribution_accounting,
    generate_size_comparison_report,
    record_ledger_from_pe_binary,
)


def build_synthetic_msf_pdb(
    guid: bytes = bytes(range(1, 17)),
    age: int = 1,
) -> bytes:
    """Construct a minimal valid Microsoft MSF 7.00 PDB file with Stream 1 (PDB Info Stream)."""
    assert len(guid) == 16
    block_size = 512
    num_blocks = 5
    pdb = bytearray(block_size * num_blocks)
    msf_sig = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"
    pdb[0:32] = msf_sig
    # SuperBlock: block_size=512, free_block_map=1, num_blocks=5, num_dir_bytes=16, unknown=0, block_map_addr=2
    struct.pack_into("<6I", pdb, 32, block_size, 1, num_blocks, 16, 0, 2)
    # Block 2 (offset 1024): Block map for Stream Directory -> points to Block 3
    struct.pack_into("<I", pdb, 2 * block_size, 3)
    # Block 3 (offset 1536): Stream Directory -> num_streams=2, sizes=[0, 28], stream1_blocks=[4]
    struct.pack_into("<IIII", pdb, 3 * block_size, 2, 0, 28, 4)
    # Block 4 (offset 2048): Stream 1 (PDB Info Stream) -> version=20000404, signature=0, age=age, guid=guid
    struct.pack_into("<III", pdb, 4 * block_size, 20000404, 0, age)
    pdb[4 * block_size + 12 : 4 * block_size + 28] = guid
    return bytes(pdb)


def build_synthetic_pe64(
    imported_dlls: list[str],
    *,
    time_date_stamp: int = 0,
    extra_text_bytes: int = 0,
    pdb_path: str | None = None,
    pdb_guid: bytes = bytes(range(1, 17)),
    pdb_age: int = 1,
    delay_imported_dlls: list[str] | None = None,
) -> bytes:
    """Construct a minimal valid PE32+ (x64) binary image with the specified import table."""
    pe_offset = 0x80
    num_sections = 2
    opt_header_size = 240
    section_table_offset = pe_offset + 24 + opt_header_size

    text_raw_size = 0x200 + extra_text_bytes
    rdata_rva = 0x2000
    rdata_raw_ptr = 0x200 + text_raw_size
    rdata_raw_size = 0x400

    desc_table_size = (len(imported_dlls) + 1) * 20
    names_offset_in_rdata = desc_table_size
    rdata_buf = bytearray(rdata_raw_size)

    current_str_off = names_offset_in_rdata
    for idx, dll in enumerate(imported_dlls):
        name_rva = rdata_rva + current_str_off
        encoded = dll.encode("ascii") + b"\x00"
        rdata_buf[current_str_off : current_str_off + len(encoded)] = encoded
        current_str_off += len(encoded)
        struct.pack_into("<IIIII", rdata_buf, idx * 20, 0, 0, 0, name_rva, 0x2300 + idx * 8)

    debug_dir_offset = current_str_off
    debug_dir_size = 0
    if pdb_path is not None:
        # IMAGE_DEBUG_DIRECTORY (28 bytes) followed by a CodeView RSDS record.
        rsds_offset = debug_dir_offset + 28
        rsds = (
            b"RSDS"
            + pdb_guid
            + struct.pack("<I", pdb_age)
            + pdb_path.encode("utf-8")
            + b"\x00"
        )
        struct.pack_into(
            "<IIHHIIII",
            rdata_buf,
            debug_dir_offset,
            0,
            0,
            0,
            0,
            2,  # IMAGE_DEBUG_TYPE_CODEVIEW
            len(rsds),
            rdata_rva + rsds_offset,
            rdata_raw_ptr + rsds_offset,
        )
        rdata_buf[rsds_offset : rsds_offset + len(rsds)] = rsds
        current_str_off = rsds_offset + len(rsds)
        debug_dir_size = 28

    delay_dir_offset = current_str_off
    delay_dir_size = 0
    delay_dlls = delay_imported_dlls or []
    if delay_dlls:
        delay_dir_size = (len(delay_dlls) + 1) * 32
        delay_name_cursor = delay_dir_offset + delay_dir_size
        for idx, dll in enumerate(delay_dlls):
            encoded = dll.encode("ascii") + b"\x00"
            name_rva = rdata_rva + delay_name_cursor
            rdata_buf[delay_name_cursor : delay_name_cursor + len(encoded)] = encoded
            delay_name_cursor += len(encoded)
            struct.pack_into(
                "<8I",
                rdata_buf,
                delay_dir_offset + idx * 32,
                1,  # dlattrRva
                name_rva,
                0,
                0,
                0,
                0,
                0,
                0,
            )
        current_str_off = delay_name_cursor

    image = bytearray(rdata_raw_ptr + rdata_raw_size)
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, pe_offset)
    image[pe_offset : pe_offset + 4] = b"PE\x00\x00"

    struct.pack_into(
        "<HHIIIHH",
        image,
        pe_offset + 4,
        0x8664,
        num_sections,
        time_date_stamp,
        0,
        0,
        opt_header_size,
        0x0022,
    )

    opt_offset = pe_offset + 24
    struct.pack_into("<H", image, opt_offset, 0x20B)
    struct.pack_into("<H", image, opt_offset + 70, 0x0020 | 0x0040 | 0x0100 | 0x4000)
    struct.pack_into("<I", image, opt_offset + 108, 16)
    struct.pack_into("<II", image, opt_offset + 120, rdata_rva, desc_table_size)
    if debug_dir_size:
        struct.pack_into(
            "<II",
            image,
            opt_offset + 112 + 8 * 6,
            rdata_rva + debug_dir_offset,
            debug_dir_size,
        )
    if delay_dir_size:
        struct.pack_into(
            "<II",
            image,
            opt_offset + 112 + 8 * 13,
            rdata_rva + delay_dir_offset,
            delay_dir_size,
        )

    # Section 0: .text
    struct.pack_into(
        "<8sIIIIIIHHI",
        image,
        section_table_offset,
        b".text\x00\x00\x00",
        0x180 + extra_text_bytes,
        0x1000,
        text_raw_size,
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


def build_synthetic_elf64(needed_libs: list[str]) -> bytes:
    """Construct a minimal valid 64-bit little-endian ELF binary with .shstrtab, .dynstr, and .dynamic (DT_NEEDED)."""
    shstrtab = b"\x00.shstrtab\x00.dynstr\x00.dynamic\x00"
    dynstr = bytearray(b"\x00")
    lib_offsets = []
    for lib in needed_libs:
        lib_offsets.append(len(dynstr))
        dynstr.extend(lib.encode("ascii") + b"\x00")

    dynamic_entries = bytearray()
    for off in lib_offsets:
        dynamic_entries.extend(struct.pack("<qQ", 1, off))  # DT_NEEDED = 1
    dynamic_entries.extend(struct.pack("<qQ", 0, 0))  # DT_NULL = 0

    elf_header_size = 64
    shstr_off = elf_header_size
    dynstr_off = shstr_off + len(shstrtab)
    dyn_off = dynstr_off + len(dynstr)
    shoff = (dyn_off + len(dynamic_entries) + 7) & ~7

    shentsize = 64
    shnum = 4
    sh_table = bytearray(shentsize * shnum)

    # Section 1: .shstrtab (SHT_STRTAB = 3)
    struct.pack_into("<IIQQQQIIQQ", sh_table, 1 * 64, 1, 3, 0, 0, shstr_off, len(shstrtab), 0, 0, 1, 0)
    # Section 2: .dynstr (SHT_STRTAB = 3)
    struct.pack_into("<IIQQQQIIQQ", sh_table, 2 * 64, 11, 3, 0, 0, dynstr_off, len(dynstr), 0, 0, 1, 0)
    # Section 3: .dynamic (SHT_DYNAMIC = 6, sh_link = 2 -> .dynstr)
    struct.pack_into(
        "<IIQQQQIIQQ", sh_table, 3 * 64, 19, 6, 0, 0, dyn_off, len(dynamic_entries), 2, 0, 8, 16
    )

    e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + (b"\x00" * 8)
    elf_hdr = e_ident + struct.pack(
        "<HHIQQQIHHHHHH",
        2,  # ET_EXEC
        0x3E,  # EM_X86_64
        1,
        0x400000,
        0,
        shoff,
        0,
        elf_header_size,
        0,
        0,
        shentsize,
        shnum,
        1,  # e_shstrndx = 1
    )

    buf = bytearray(shoff + len(sh_table))
    buf[: len(elf_hdr)] = elf_hdr
    buf[shstr_off : shstr_off + len(shstrtab)] = shstrtab
    buf[dynstr_off : dynstr_off + len(dynstr)] = dynstr
    buf[dyn_off : dyn_off + len(dynamic_entries)] = dynamic_entries
    buf[shoff : shoff + len(sh_table)] = sh_table
    return bytes(buf)


def test_recorded_ledgers_pass_dependency_and_toolchain_comparison(tmp_path: Path) -> None:
    dep_reports = verify_all_recorded_ledgers(REPO_ROOT)
    assert len(dep_reports) == 6
    assert all(r["closure_verified"] for r in dep_reports)

    comparison = generate_size_comparison_report(REPO_ROOT)
    assert comparison["selected_toolchain"] == "clang-cl-lld"
    assert comparison["claims_64kib_achievable"] is False
    assert comparison["defer_minimum_size_claim_until_task"] == "NW-10"
    by_fixture = {c["fixture_id"]: c for c in comparison["fixture_comparisons"]}
    for fx in ("empty_player", "clear_present", "compute_shader"):
        assert by_fixture[fx]["verification_status"] == "unverified"
        assert by_fixture[fx]["clang_cl_lld_bytes"] is None
        assert by_fixture[fx]["msvc_ltcg_bytes"] is None
        assert by_fixture[fx]["identical_import_closure"] is True

    # Also test recording measured PE binaries into a copy of the ledgers and running comparison
    from schema_validator import validate_file_against_schema

    inventory = json.loads((REPO_ROOT / "benchmarks" / "dependency-inventory.v1.json").read_text(encoding="utf-8"))
    ledger_copy = tmp_path / "empty-player-clang-cl-release.json"
    shutil.copyfile(REPO_ROOT / "benchmarks" / "ledgers" / "empty-player-clang-cl-release.json", ledger_copy)
    pe_path = tmp_path / "nw_empty_player.exe"
    pe_path.write_bytes(build_synthetic_pe64(["kernel32.dll"]))
    updated = record_ledger_from_pe_binary(pe_path, ledger_copy, inventory, repo_root=REPO_ROOT)
    assert updated["verification_status"] == "verified"
    assert updated["total_distributed_bytes"] == len(pe_path.read_bytes())
    assert all(
        imp["classification"] in {"windows_inbox", "ucrt_inbox", "dev_only_diagnostic"}
        for imp in updated["imports"]
    )
    validate_file_against_schema(ledger_copy, REPO_ROOT / "schemas" / "size-ledger.schema.json")

    measured_runtime = {
        "startup_time_ms": 4.5,
        "cpu_time_ms": 9.25,
        "gpu_time_ms": 1.5,
        "committed_memory_bytes": 1048576,
        "gpu_committed_memory_bytes": 2097152,
    }
    updated = record_ledger_from_pe_binary(
        pe_path,
        ledger_copy,
        inventory,
        runtime_metrics=measured_runtime,
        repo_root=REPO_ROOT,
    )
    assert updated["runtime_metrics"] == measured_runtime
    unavailable = {m["metric"] for m in updated["unavailable_measurements"]}
    for metric_key in measured_runtime:
        assert metric_key not in unavailable
    validate_file_against_schema(ledger_copy, REPO_ROOT / "schemas" / "size-ledger.schema.json")

    # An unexpected import in record_ledger_from_pe_binary must fail closure verification
    # and never produce a falsely verified or schema-invalid ledger.
    bad_ledger_copy = tmp_path / "bad-empty-player.json"
    shutil.copyfile(REPO_ROOT / "benchmarks" / "ledgers" / "empty-player-clang-cl-release.json", bad_ledger_copy)
    bad_pe_path = tmp_path / "bad_empty_player.exe"
    bad_pe_path.write_bytes(build_synthetic_pe64(["kernel32.dll", "ucrtbase.dll", "dxcompiler.dll"]))
    with pytest.raises(DependencyClosureError):
        record_ledger_from_pe_binary(bad_pe_path, bad_ledger_copy, inventory, repo_root=REPO_ROOT)
    assert json.loads(bad_ledger_copy.read_text(encoding="utf-8"))["verification_status"] == "unverified"

    # Missing repo_root or missing fixtures/<fixture_id>/fixture.json must fail closed
    # rather than falling back to the ledger's own existing imports list.
    with pytest.raises(SizeLedgerError, match="repo_root is required"):
        record_ledger_from_pe_binary(pe_path, bad_ledger_copy, inventory, repo_root=None)
    with pytest.raises(SizeLedgerError, match="Missing required fixture import contract"):
        record_ledger_from_pe_binary(pe_path, bad_ledger_copy, inventory, repo_root=tmp_path)


def test_size_ledger_rebuilds_sections_and_imports_from_inspected_pe(tmp_path: Path) -> None:
    """Measured ledgers must not inherit template sections or stale imported symbols."""
    inventory = json.loads(
        (REPO_ROOT / "benchmarks" / "dependency-inventory.v1.json").read_text(
            encoding="utf-8"
        )
    )
    ledger_copy = tmp_path / "empty-player-clang-cl-release.json"
    shutil.copyfile(
        REPO_ROOT / "benchmarks" / "ledgers" / "empty-player-clang-cl-release.json",
        ledger_copy,
    )
    template = json.loads(ledger_copy.read_text(encoding="utf-8"))
    template_symbols = template["imports"][0]["symbols"]

    pe_path = tmp_path / "nw_empty_player.exe"
    pe_path.write_bytes(build_synthetic_pe64(["kernel32.dll"]))
    updated = record_ledger_from_pe_binary(
        pe_path, ledger_copy, inventory, repo_root=REPO_ROOT
    )

    # The template lists .text/.rdata/.data/.pdata/.reloc; the inspected PE has only two.
    assert [section["name"] for section in updated["executable_sections"]] == [
        ".text",
        ".rdata",
    ]
    # The synthetic import table has no named thunks, so no template symbol leaks through.
    assert updated["imports"][0]["symbols"] == []
    assert updated["imports"][0]["symbols"] != template_symbols
    assert "imports[*].symbols" not in {
        item["metric"] for item in updated["unavailable_measurements"]
    }
    for section in updated["executable_sections"]:
        assert section["virtual_size_bytes"] is not None
        assert section["raw_size_bytes"] is not None
        assert section["characteristics"]

    measured = _measured_import_entries(
        {
            "imports": ["kernel32.dll"],
            "imports_by_dll": {"kernel32.dll": ["CreateEventA", "WaitForSingleObject"]},
            "delay_imports": ["user32.dll"],
            "delay_imports_by_dll": {"user32.dll": ["RegisterClassA"]},
        },
        inventory,
    )
    assert measured[0]["symbols"] == ["CreateEventA", "WaitForSingleObject"]
    assert measured[1]["dll_name"] == "user32.dll"
    assert measured[1]["delay_load"] is True
    assert measured[1]["symbols"] == ["RegisterClassA"]


def test_synthetic_pe64_and_elf64_import_parsers_and_closure_gates(tmp_path: Path) -> None:
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

    # Test real ELF64 host binary parsing and closure verification
    valid_elf = build_synthetic_elf64(["libc.so.6", "libm.so.6"])
    elf_info = parse_elf64_needed_libraries(valid_elf)
    assert elf_info["needed_libraries"] == ["libc.so.6", "libm.so.6"]

    elf_bin = tmp_path / "nw_empty_player"
    elf_bin.write_bytes(valid_elf)
    elf_res = verify_binary_file(
        elf_bin,
        profile="release",
        fixture_id="empty_player",
        inventory=inventory,
        repo_root=REPO_ROOT,
    )
    assert elf_res["closure_verified"] is True
    assert elf_res["binary_format"] == "ELF64"

    # ELF64 binary linking libstdc++.so.6 or libdxcompiler.so must fail player dependency closure
    for bad_lib in ("libstdc++.so.6", "libdxcompiler.so"):
        bad_elf_bin = tmp_path / "bad_player"
        bad_elf_bin.write_bytes(build_synthetic_elf64(["libc.so.6", bad_lib]))
        with pytest.raises(DependencyClosureError, match=bad_lib[:8]):
            verify_binary_file(
                bad_elf_bin,
                profile="release",
                fixture_id="empty_player",
                inventory=inventory,
                repo_root=REPO_ROOT,
            )

    # Arbitrary non-PE, non-ELF file must fail rather than falling back to fixture.json self-validation
    bogus_bin = tmp_path / "not_an_executable.bin"
    bogus_bin.write_bytes(b"NOT_A_VALID_BINARY")
    with pytest.raises(DependencyClosureError, match="neither a valid Windows PE32\\+"):
        verify_binary_file(
            bogus_bin,
            profile="release",
            fixture_id="empty_player",
            inventory=inventory,
            repo_root=REPO_ROOT,
        )


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


def test_sibling_deployment_executables_are_rejected_not_silently_skipped(tmp_path: Path) -> None:
    inventory = json.loads(
        (REPO_ROOT / "benchmarks" / "dependency-inventory.v1.json").read_text(encoding="utf-8")
    )
    empty = tmp_path / "nw_empty_player.exe"
    empty.write_bytes(build_synthetic_pe64(["kernel32.dll"]))
    clear = tmp_path / "nw_clear_present_fixture.exe"
    clear.write_bytes(
        build_synthetic_pe64(
            ["kernel32.dll", "user32.dll", "d3d12.dll", "dxgi.dll", "ucrtbase.dll"]
        )
    )

    # Production builds isolate each fixture in its own staging directory. A second
    # executable in the same deployment tree is therefore an uncounted required sidecar.
    with pytest.raises(DependencyClosureError, match="nw_clear_present_fixture.exe"):
        verify_binary_file(
            empty,
            profile="release",
            fixture_id="empty_player",
            inventory=inventory,
            repo_root=REPO_ROOT,
        )

    # Nested payloads are also detected and rejected.
    clear.unlink()
    nested = tmp_path / "payloads" / "demo.nwpkg"
    nested.parent.mkdir()
    nested.write_bytes(b"payload")
    with pytest.raises(DependencyClosureError, match="demo.nwpkg"):
        verify_binary_file(
            empty,
            profile="release",
            fixture_id="empty_player",
            inventory=inventory,
            repo_root=REPO_ROOT,
        )


def test_delay_load_imports_are_parsed_and_rejected(tmp_path: Path) -> None:
    inventory = json.loads(
        (REPO_ROOT / "benchmarks" / "dependency-inventory.v1.json").read_text(encoding="utf-8")
    )
    binary = build_synthetic_pe64(
        ["kernel32.dll", "ucrtbase.dll"],
        delay_imported_dlls=["dxcompiler.dll"],
    )
    parsed = parse_pe64_sections_and_imports(binary)
    assert parsed["delay_imports"] == ["dxcompiler.dll"]

    exe = tmp_path / "nw_empty_player.exe"
    exe.write_bytes(binary)
    with pytest.raises(DependencyClosureError, match="dxcompiler.dll"):
        verify_binary_file(
            exe,
            profile="release",
            fixture_id="empty_player",
            inventory=inventory,
            repo_root=REPO_ROOT,
        )


def test_ucrt_api_set_imports_canonicalize_to_inbox_ucrtbase(tmp_path: Path) -> None:
    inventory = json.loads(
        (REPO_ROOT / "benchmarks" / "dependency-inventory.v1.json").read_text(encoding="utf-8")
    )
    exe = tmp_path / "nw_empty_player.exe"
    exe.write_bytes(
        build_synthetic_pe64(
            [
                "kernel32.dll",
                "api-ms-win-crt-runtime-l1-1-0.dll",
                "api-ms-win-crt-heap-l1-1-0.dll",
            ]
        )
    )
    result = verify_closure(
        profile="release",
        fixture_id="empty_player",
        imported_dlls=[
            "kernel32.dll",
            "api-ms-win-crt-runtime-l1-1-0.dll",
            "api-ms-win-crt-heap-l1-1-0.dll",
        ],
        sidecar_files=[],
        inventory=inventory,
        expected_fixture_release_imports=["kernel32.dll", "ucrtbase.dll"],
    )
    assert result["closure_verified"] is True
    assert result["canonical_imports"] == ["kernel32.dll", "ucrtbase.dll"]
