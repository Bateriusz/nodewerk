#!/usr/bin/env python3
"""Record and verify the exact Windows toolchain used for NW-11 artifacts.

The bundle manifest must not merely repeat the profile's frozen requirement
values.  Hosted CI runs this tool against the actual `dxc.exe`, `cl.exe`,
CMake, Ninja, and Windows SDK so the bundle binds the measured compiler
identity and fails closed on drift.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import nw11_contract as contract  # noqa: E402

FORMAT = "nw11-toolchain-identity"
VERSION = 1


class ToolchainIdentityError(ValueError):
    """Raised when the measured toolchain cannot be recorded or verified."""


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ToolchainIdentityError(
            f"cannot execute '{' '.join(command)}': {exc}"
        ) from exc
    return result.stdout.strip()


def _first_version(text: str) -> str:
    match = re.search(r"\d+\.\d+(?:\.\d+){0,2}", text)
    if match is None:
        raise ToolchainIdentityError(f"no version number in tool output: {text!r}")
    return match.group(0)


def _allowed_dxc_identities() -> set[str]:
    candidates = (
        THIS_DIR / "nw01" / "tool-version-identities.json",
        THIS_DIR / "tool-version-identities.json",
    )
    for path in candidates:
        if path.is_file():
            mapping = json.loads(path.read_text(encoding="utf-8"))
            return set(mapping["dxc"][contract.TOOLCHAIN_DXC_VERSION]["reported_versions"])
    raise ToolchainIdentityError("NW-01 exact DXC identity mapping is missing")


def _dxc_identity(banner: str) -> str:
    tokens = set(re.findall(r"(?<![\d.])\d+\.\d+\.\d+\.\d+(?![\d.])", banner))
    matched = tokens & _allowed_dxc_identities()
    if len(matched) != 1:
        raise ToolchainIdentityError(f"unrecognized or ambiguous pinned DXC banner: {banner!r}")
    return matched.pop()


def _run_allow_failure(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        raise ToolchainIdentityError(
            f"cannot execute '{' '.join(command)}': {exc}"
        ) from exc
    return result.stdout.strip()


def collect(
    *,
    dxc: str,
    cl: str | None = None,
    cmake: str | None = None,
    ninja: str | None = None,
    windows_sdk_version: str | None = None,
) -> dict[str, Any]:
    dxc_output = _run([dxc, "--version"])
    # DXC prints a short DLL version ("1.8") before the actual pinned
    # binary identity. Reuse the NW-01 repository-owned identity mapping.
    dxc_version = _dxc_identity(dxc_output)
    if dxc_version not in _allowed_dxc_identities():
        raise ToolchainIdentityError(
            f"dxc {dxc_version} does not match pinned "
            f"{contract.TOOLCHAIN_DXC_VERSION}"
        )
    document: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "dxc_version": dxc_version,
        "dxc_banner": dxc_output.splitlines()[0] if dxc_output else "",
        "required_dxc_version": contract.TOOLCHAIN_DXC_VERSION,
        "measured_dxc_version": dxc_version,
    }
    if cl:
        cl_output = _run_allow_failure([cl])
        document["msvc_version"] = _first_version(cl_output)
    if cmake:
        document["cmake_version"] = _first_version(_run([cmake, "--version"]))
    if ninja:
        document["ninja_version"] = _first_version(_run([ninja, "--version"]))
    if windows_sdk_version:
        document["windows_sdk_version"] = windows_sdk_version
    return document


def verify(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("format") != FORMAT or document.get("version") != VERSION:
        raise ToolchainIdentityError("toolchain identity has the wrong format/version")
    if document.get("task") != contract.TASK:
        raise ToolchainIdentityError("toolchain identity has the wrong task")
    if document.get("implementation_base") != contract.IMPLEMENTATION_BASE:
        raise ToolchainIdentityError("toolchain identity is bound to a different base")
    dxc = str(document.get("dxc_version", ""))
    if dxc not in _allowed_dxc_identities():
        raise ToolchainIdentityError(
            f"recorded dxc version '{dxc}' is not the pinned "
            f"{contract.TOOLCHAIN_DXC_VERSION}"
        )
    return dict(document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dxc", required=True)
    parser.add_argument("--cl")
    parser.add_argument("--cmake")
    parser.add_argument("--ninja")
    parser.add_argument("--windows-sdk-version")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        document = collect(
            dxc=args.dxc,
            cl=args.cl,
            cmake=args.cmake,
            ninja=args.ninja,
            windows_sdk_version=args.windows_sdk_version,
        )
        verify(document)
    except ToolchainIdentityError as exc:
        print(f"[NW11-TOOLCHAIN-ERR] {exc}", file=sys.stderr)
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
