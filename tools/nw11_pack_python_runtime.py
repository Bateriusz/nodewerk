#!/usr/bin/env python3
"""Create a deterministic self-contained Windows Python runtime archive for NW-11."""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

FIXED_TIME = (1980, 1, 1, 0, 0, 0)
ROOT_PATTERNS = ("python*.exe", "python*.dll", "vcruntime*.dll", "*.zip", "*._pth")
EXCLUDED_PARTS = {"__pycache__", "site-packages", "test", "tests"}


class RuntimePackError(ValueError):
    pass


def _files(root: Path) -> list[Path]:
    if not (root / "python.exe").is_file():
        raise RuntimePackError("python.exe is missing from the selected runtime root")
    for required in ("Lib/json/__init__.py", "Lib/encodings/__init__.py"):
        if not (root / required).is_file():
            raise RuntimePackError(f"required standard-library file is missing: {required}")
    selected: set[Path] = set()
    for pattern in ROOT_PATTERNS:
        selected.update(path for path in root.glob(pattern) if path.is_file())
    for dirname in ("Lib", "DLLs"):
        base = root / dirname
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() == ".pyc":
                continue
            relative = path.relative_to(root)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            selected.add(path)
    return sorted(selected, key=lambda path: path.relative_to(root).as_posix().casefold())


def pack(root: Path, output: Path) -> dict[str, object]:
    files = _files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return {
        "format": "nw11-python-runtime",
        "version": 1,
        "python_version": ".".join(str(value) for value in sys.version_info[:3]),
        "file_count": len(files),
        "archive_bytes": output.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    try:
        result = pack(args.python_root.resolve(), args.output.resolve())
    except (OSError, RuntimePackError) as exc:
        print(f"[NW11-PYTHON-RUNTIME-ERR] {exc}", file=sys.stderr)
        return 2
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
