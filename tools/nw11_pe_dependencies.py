#!/usr/bin/env python3
"""Record Windows PE import DLLs for NW-11 artifacts and reject redistributables.

Validation bundles must run on a clean system with only Windows-inbox
components.  The NW-11 experiment executables therefore use the static MSVC
CRT; this tool fails closed if a measured executable imports VCRUNTIME,
MSVCP, the C++ redistributable, or another non-inbox component.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import nw11_contract as contract  # noqa: E402

FORMAT = "nw11-pe-dependencies"
VERSION = 1

# Explicit Win32 components used by the renderer and bundled validation tools.
# The API-set names below are the inbox UCRT contracts used by the NW-07 release.
WINDOWS_INBOX = frozenset({
    "advapi32.dll", "bcrypt.dll", "cfgmgr32.dll", "combase.dll", "comdlg32.dll",
    "crypt32.dll", "d3d12.dll", "dwmapi.dll", "dxgi.dll", "gdi32.dll", "imm32.dll",
    "kernel32.dll", "kernelbase.dll", "msvcrt.dll", "ntdll.dll", "ole32.dll",
    "oleaut32.dll", "powrprof.dll", "psapi.dll", "rpcrt4.dll", "secur32.dll",
    "setupapi.dll", "shell32.dll", "shlwapi.dll", "tdh.dll", "ucrtbase.dll",
    "user32.dll", "userenv.dll", "version.dll", "winmm.dll", "ws2_32.dll",
    *(f"api-ms-win-crt-{part}-l1-1-0.dll" for part in (
        "conio", "convert", "environment", "filesystem", "heap", "locale",
        "math", "multibyte", "private", "process", "runtime", "stdio", "string",
        "time", "utility")),
})


class PeDependencyError(ValueError):
    """Raised when a PE image cannot be parsed or imports a non-inbox DLL."""


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _rva_to_offset(data: bytes, sections: list[tuple[int, int, int]], rva: int) -> int:
    for virtual_address, size, raw_pointer in sections:
        if virtual_address <= rva < virtual_address + max(size, 1):
            return raw_pointer + (rva - virtual_address)
    raise PeDependencyError(f"RVA 0x{rva:x} is outside every PE section")


def imports(path: Path | str) -> list[str]:
    data = Path(path).read_bytes()
    if data[:2] != b"MZ":
        raise PeDependencyError(f"{path} is not a PE image")
    pe_offset = _u32(data, 0x3C)
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise PeDependencyError(f"{path} has no PE signature")
    coff = pe_offset + 4
    section_count = _u16(data, coff + 2)
    optional_size = _u16(data, coff + 16)
    optional = coff + 20
    magic = _u16(data, optional)
    if magic == 0x20B:
        directory_offset = optional + 112
    elif magic == 0x10B:
        directory_offset = optional + 96
    else:
        raise PeDependencyError(f"{path} has an unknown optional header magic")
    import_rva = _u32(data, directory_offset + 8)
    import_size = _u32(data, directory_offset + 12)
    sections: list[tuple[int, int, int]] = []
    section_table = optional + optional_size
    for index in range(section_count):
        offset = section_table + index * 40
        virtual_size = _u32(data, offset + 8)
        virtual_address = _u32(data, offset + 12)
        raw_size = _u32(data, offset + 16)
        raw_pointer = _u32(data, offset + 20)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_pointer))
    result: set[str] = set()
    # Include delay-loaded DLLs. The validator requires the standard
    # RVA-based delay descriptors emitted by the pinned linker.
    directory_count = _u32(data, directory_offset - 4)
    delayed = (_u32(data, directory_offset + 13 * 8), _u32(data, directory_offset + 13 * 8 + 4)) if directory_count > 13 else (0, 0)
    for rva, size, stride, name_field in (
        (import_rva, import_size, 20, 12), (*delayed, 32, 4)
    ):
        if not rva or not size:
            continue
        descriptor_offset = _rva_to_offset(data, sections, rva)
        for index in range(size // stride):
            offset = descriptor_offset + index * stride
            name_rva = _u32(data, offset + name_field)
            if name_rva == 0:
                break
            if stride == 32 and _u32(data, offset) != 1:
                raise PeDependencyError(f"{path} has a non-RVA delay import")
            name_offset = _rva_to_offset(data, sections, name_rva)
            end = data.find(b"\0", name_offset)
            if end < 0:
                raise PeDependencyError(f"{path} has an unterminated import name")
            result.add(data[name_offset:end].decode("ascii"))
    return sorted(result)


def forbidden(dlls: list[str]) -> list[str]:
    return [dll for dll in dlls if dll.casefold() not in WINDOWS_INBOX]


def deployment_closure(root: Path, executables: list[str], payloads: list[str]) -> tuple[dict, list[dict]]:
    """Resolve direct and transitive imports per actual executable."""
    shipped = {Path(path).name.casefold(): path for path in payloads}
    if len(shipped) != len(payloads) or shipped.keys() & WINDOWS_INBOX:
        raise PeDependencyError("Duplicate payload or bundled copy of an inbox DLL")
    by_executable = {}
    used_by: dict[str, list[str]] = {}
    common = {"bin/common_baseline.exe", "bin/fallback_common.exe", "bin/canonical_release.exe"}
    for executable in sorted(executables):
        pending = imports(root / executable)
        visited: set[str] = set()
        while pending:
            name = pending.pop().casefold()
            if name in visited:
                continue
            visited.add(name)
            if name in shipped:
                if Path(shipped[name]).parent != Path(executable).parent:
                    raise PeDependencyError(f"{executable} cannot resolve payload {name} beside it")
                pending.extend(imports(root / shipped[name]))
            elif name not in WINDOWS_INBOX:
                raise PeDependencyError(f"{executable} imports unclassified DLL {name}")
            used_by.setdefault(name, []).append(executable)
        by_executable[executable] = sorted(visited)
    if shipped.keys() - used_by.keys():
        raise PeDependencyError("Bundle contains an unused deployment DLL")
    ledger = []
    for name, usages in sorted(used_by.items()):
        in_common = bool(common.intersection(usages))
        if name in shipped:
            if in_common or set(usages) != {"bin/optional_variant.exe"}:
                raise PeDependencyError(f"Optional payload {name} has an unexpected consumer")
            path = root / shipped[name]
            payload = {"path": shipped[name], "sha256": contract.sha256_file(path)}
            size = path.stat().st_size
        else:
            payload, size = {}, 0
        ledger.append({
            "name": name, "kind": "bundled_runtime" if size else "windows_inbox",
            "optional": not in_common, "counted": True, "in_common_closure": in_common,
            "distributed_bytes": size, "used_by": usages, **payload,
        })
    return by_executable, ledger


def collect(executables: list[Path | str]) -> dict[str, Any]:
    records = []
    for executable in executables:
        path = Path(executable)
        dlls = imports(path)
        bad = forbidden(dlls)
        if bad:
            raise PeDependencyError(
                f"{path.name} imports non-inbox runtime DLLs: {', '.join(bad)}"
            )
        records.append({"path": path.name, "dlls": dlls})
    return {
        "format": FORMAT,
        "version": VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "executables": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", action="append", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        document = collect(args.exe)
    except (OSError, PeDependencyError, struct.error, IndexError) as exc:
        print(f"[NW11-PE-ERR] {exc}", file=sys.stderr)
        return 2
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
