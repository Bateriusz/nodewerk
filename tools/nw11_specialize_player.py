#!/usr/bin/env python3
"""Generate the NW-11-only translation unit from the canonical PackagePlayer.

The overlay is a bounded set of context-checked changes, not a forked renderer.
The normal NW-07 and NW-10 builds continue to consume the original source.
A changed upstream player requires an explicit overlay rebase.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def specialize(source: Path, overlay: Path) -> str:
    text = source.read_text(encoding="utf-8")
    record = json.loads(overlay.read_text(encoding="utf-8"))
    if (record.get("format") != "nw11-package-player-overlay.v1"
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != record.get("source_sha256")):
        raise ValueError("canonical PackagePlayer changed; rebase the NW-11 specialization explicitly")
    lines = text.split("\n")
    output: list[str] = []
    cursor = 0
    for hunk in record["hunks"]:
        start, old, new = hunk["start"], hunk["old"], hunk["new"]
        if start < cursor or lines[start:start + len(old)] != old:
            raise ValueError("NW-11 production specialization context mismatch")
        output.extend(lines[cursor:start])
        output.extend(new)
        cursor = start + len(old)
    output.extend(lines[cursor:])
    return "\n".join(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(specialize(args.source, args.overlay), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
