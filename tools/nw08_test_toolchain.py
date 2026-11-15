#!/usr/bin/env python3
"""Create a deterministic NW-08 contract-test toolchain.

This is *not* production provisioning and never claims hardware evidence.  It
exists so Windows CI can exercise the real GUI/CLI export path with the
established NW-04 DXIL fixture container and a content-free player template.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import nw04_fixture  # noqa: E402


def create_toolchain(directory: Path, *, template_bytes: int = 256) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    container = nw04_fixture.make_dxil_container()
    dxc = directory / "mock_dxc.py"
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
        "fo.parent.mkdir(parents=True, exist_ok=True)\n"
        f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        dxc.chmod(0o755)
    template = directory / "template.exe"
    template.write_bytes(b"MZ" + b"\x00" * template_bytes)
    return {
        "directory": str(directory),
        "dxc": str(dxc),
        "player_template": str(template),
        "dxc_version": "1.8.2407.12",
        "template_bytes": str(template.stat().st_size),
        "hardware_evidence": "false",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    result = create_toolchain(Path(args.directory))
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
