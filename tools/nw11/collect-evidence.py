#!/usr/bin/env python3
"""Collect raw evidence written by the NW-11 physical validation launcher."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import nw11_bundle as bundle
import gpu_experiment_measurements as measurements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = bundle.verify_bundle(args.bundle_root, expected_commit=args.expected_commit)["manifest"]
        report = measurements.collect(args.bundle_root, manifest, args.out.parent, args.out.name)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"[NW11-COLLECT-ERR] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
