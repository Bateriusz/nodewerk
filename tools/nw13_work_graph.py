#!/usr/bin/env python3
"""Record a matched Work Graphs run or the hosted conventional WARP smoke."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import nw13_evidence as evidence
from nw13_production import measured_revision


CRITERION = "median GPU dispatch time below 95% of conventional, exact output, backing allocation at most 32 MiB"


def measure(executable: Path, *, revision: str, warp: bool) -> dict:
    shader_dir = executable.parent
    baseline = shader_dir / "particle_routing_cs.dxil"
    optional = shader_dir / "particle_routing_lib.dxil"
    mode = "--warp" if warp else "--matched"
    result = subprocess.run(
        [str(executable), mode, str(baseline), str(optional)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode not in ({0} if warp else {0, 3}):
        raise evidence.EvidenceError(f"particle routing driver failed ({result.returncode}): {result.stderr}")
    observation = json.loads(result.stdout)
    if result.returncode == 3 and observation["status"] != "unavailable":
        raise evidence.EvidenceError("missing explicit unsupported Work Graphs capability observation")
    if result.returncode == 3:
        fallback = subprocess.run(
            [str(executable), "--baseline", str(baseline), str(optional)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if fallback.returncode != 0:
            raise evidence.EvidenceError(f"conventional Work Graphs fallback failed ({fallback.returncode})")
        fallback_observation = json.loads(fallback.stdout)
        if fallback_observation["status"] != "baseline_executed":
            raise evidence.EvidenceError("conventional Work Graphs fallback did not report execution")
        observation["fallback"] = fallback_observation
    decision = "pending_unavailable"
    if observation["status"] == "measured":
        if observation["samples_per_arm"] != 9 or observation["backing_bytes"] > 32 * 1024 * 1024:
            raise evidence.EvidenceError("Work Graphs observation violates the predeclared experimental bounds")
        decision = evidence.work_graph_experiment(
            conventional_metric=observation["conventional_gpu_median_us"],
            work_graph_metric=observation["work_graph_gpu_median_us"],
            criterion=CRITERION,
        )["decision"]
    bundle = {
        "format": "nw13.work-graph-run", "version": 1,
        "revision": revision, "criterion": CRITERION, "decision": decision,
        "functional_execution": observation["status"],
        "observation": observation,
        "inputs": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (executable, baseline, optional)},
    }
    return {**bundle, "bundle_sha256": evidence.sha256_text(evidence.canonical_bytes(bundle).decode("ascii"))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--warp", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    revision = measured_revision(Path.cwd(), require_clean=True)
    result = measure(args.executable.resolve(), revision=revision, warp=args.warp)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
