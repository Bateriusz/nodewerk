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

FORBIDDEN_DLL_TOKENS = (
    "vcruntime",
    "msvcp",
    "msvcr",
    "concrt",
    "dxcompiler",
    "d3dcompiler",
    "nvngx",
    "dlss",
    "cuda",
)


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
    if import_rva == 0 or import_size == 0:
        return []
    descriptor_offset = _rva_to_offset(data, sections, import_rva)
    result: list[str] = []
    for index in range(import_size // 20):
        offset = descriptor_offset + index * 20
        name_rva = _u32(data, offset + 12)
        if name_rva == 0:
            break
        name_offset = _rva_to_offset(data, sections, name_rva)
        end = data.find(b"\0", name_offset)
        if end < 0:
            raise PeDependencyError(f"{path} has an unterminated import name")
        result.append(data[name_offset:end].decode("ascii", errors="replace"))
    return sorted(set(result))


def forbidden(dlls: list[str]) -> list[str]:
    return [
        dll
        for dll in dlls
        if any(token in dll.casefold() for token in FORBIDDEN_DLL_TOKENS)
    ]


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
