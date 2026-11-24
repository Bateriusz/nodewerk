#!/usr/bin/env python3
"""Publish SHA-256 identities for compiled NW-11 shader payloads.

The measured executables embed DXIL byte arrays.  The source shader hashes in
the shader manifest are not sufficient to bind the exact compiled payloads
that shipped, so hosted CI records the DXIL/adapter-header identities here and
the validation bundle carries this manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import nw11_contract as contract  # noqa: E402

FORMAT = "nw11-shader-payloads"
VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect(directory: Path | str) -> dict[str, Any]:
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"shader payload directory is missing: {root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".dxil", ".h"}:
            continue
        data = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    if not entries:
        raise ValueError(f"no DXIL or generated shader headers under {root}")
    return {
        "format": FORMAT,
        "version": VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "payloads": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        document = collect(args.directory)
    except (OSError, ValueError) as exc:
        print(f"[NW11-SHADER-PAYLOAD-ERR] {exc}", file=sys.stderr)
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
