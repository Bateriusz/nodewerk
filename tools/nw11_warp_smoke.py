#!/usr/bin/env python3
"""Run the canonical NW-11 command paths, requiring fresh GPU observations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

COMMON = ("nw11_common_baseline", "nw11_common_analytic", "nw11_fallback_common", "nw11_fallback_analytic")


def run(directory: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name in COMMON:
        executable = directory / (name + ".exe")
        if not executable.is_file():
            raise ValueError(f"missing production-bound executable: {executable}")
        report = output / f"{name}-warp-smoke.json"
        report.unlink(missing_ok=True)
        command = [str(executable.resolve()), "--allow-software", "--headless", "--benchmark-json",
                   str(report.resolve()), "--warmup-seconds", "0", "--runs", "1", "--run-seconds", "1"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"{name}: hosted WARP command-path timed out after 60s") from exc
        for stream in (result.stdout, result.stderr):
            if stream:
                print(stream, end="", flush=True)
        if result.returncode != 0:
            raise ValueError(f"{name}: hosted WARP command-path exited {result.returncode}")
        try:
            samples = json.loads(report.read_text(encoding="utf-8"))["measurement"]["gpu_time_samples"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{name}: no fresh, readable GPU timing report at {report}") from exc
        if not isinstance(samples, list) or not samples:
            raise ValueError(f"{name}: hosted WARP command-path produced no GPU timestamp samples")
        print(f"{name}: hosted WARP command-path recorded {len(samples)} GPU timestamps", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executables", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.executables, args.output)
    except (OSError, ValueError) as exc:
        print(f"NW11 hosted WARP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
