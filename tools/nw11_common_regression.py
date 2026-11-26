#!/usr/bin/env python3
"""Build the canonical NW-07 common export before/after NW-11 and compare bytes.

Only actual release-grade exports are accepted. Both revisions are built in
the same clean worktree path, under the same already-pinned Windows toolchain.
No physical evidence or historical manifest is edited by this gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import nw11_contract as contract
import build_attestation

ROOT = Path(__file__).resolve().parents[1]
STEM = "nw_common_d3d12_fl12_0_sm6_0_v1"
FILES = (f"{STEM}.exe", f"{STEM}.nwp", f"{STEM}.manifest.json")
IDENTITY_FIELDS = ("profile_id", "requirements_digest", "artifact_key",
                   "executable_sha256", "executable_bytes", "package_sha256",
                   "package_bytes", "manifest_sha256", "manifest_bytes",
                   "dependency_closure", "capability", "init_opcodes",
                   "frame_opcodes")


def run(args: list[str], cwd: Path) -> None:
    print("+", subprocess.list2cmdline(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def git_sha(revision: str) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"],
        cwd=ROOT, text=True).strip()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(report: dict, artifact: Path) -> dict:
    if (report.get("success") is not True
            or report.get("release_grade") is not True
            or report.get("test_payload") is not False
            or not build_attestation.has_recorded_inputs(report.get("build_inputs", {}))
            or report.get("pe_dependency_closure", {}).get("matches_manifest") is not True):
        raise ValueError("not a verified release-grade NW-07 export")
    result = {key: report["artifact"][key] for key in IDENTITY_FIELDS}
    result["pe_imports"] = sorted(report["pe_dependency_closure"]["imports"])
    result["files"] = {}
    for name, hash_field, size_field in zip(
        FILES,
        ("executable_sha256", "package_sha256", "manifest_sha256"),
        ("executable_bytes", "package_bytes", "manifest_bytes"),
    ):
        path = artifact / name
        digest, size = sha(path), path.stat().st_size
        if digest != result[hash_field] or size != result[size_field]:
            raise ValueError(f"export report does not bind actual bytes of {name}")
        result["files"][name] = {"sha256": digest, "bytes": size}
    return result


def differences(before: dict, after: dict) -> list[str]:
    return sorted(key for key in set(before) | set(after)
                  if before.get(key) != after.get(key))


def build_one(revision: str, worktree: Path, destination: Path, dxc: str) -> dict:
    destination.mkdir(parents=True)
    run(["git", "worktree", "add", "--detach", str(worktree), revision], ROOT)
    try:
        observed = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                           cwd=worktree, text=True).strip()
        if observed != revision:
            raise ValueError("worktree revision mismatch")
        run(["cmake", "--preset", "nw07-release",
             "-DNW10_COMPACT_PACKAGE=OFF", "-DNW_ENFORCE_PINNED_TOOLCHAIN=ON"], worktree)
        run(["cmake", "--build", "--preset", "nw07-release",
             "--target", "nw_procedural_image_player", "--parallel", "2"], worktree)
        template = worktree / "build/nw07-release/staging/procedural_image_player/nw_procedural_image_player.exe"
        artifact = worktree / "build/nw11-common-proof/export"
        report_path = worktree / "build/nw11-common-proof/export-report.json"
        run([sys.executable, "tools/nw07_export.py", "--output", str(artifact),
             "--player-template", str(template), "--dxc", dxc,
             "--report", str(report_path)], worktree)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        result = identity(report, artifact)
        shutil.copy2(report_path, destination / "export-report.json")
        for name in FILES:
            shutil.copy2(artifact / name, destination / name)
        return result
    finally:
        run(["git", "worktree", "remove", "--force", str(worktree)], ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--dxc", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("native pinned Windows export is required")
    before = git_sha(contract.IMPLEMENTATION_BASE)
    after = git_sha(args.candidate)
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    result = {"format": "nw11-common-regression.v1", "base_commit": before,
              "candidate_commit": after, "status": "failed"}
    try:
        with tempfile.TemporaryDirectory(prefix="nw11-common-", dir=os.environ.get("RUNNER_TEMP")) as temp:
            # Re-use exactly the same checkout path, but never a build directory.
            worktree = Path(temp) / "source"
            result["base"] = build_one(before, worktree, args.out / "base", args.dxc)
            result["candidate"] = build_one(after, worktree, args.out / "candidate", args.dxc)
        result["differences"] = differences(result["base"], result["candidate"])
        result["status"] = "verified" if not result["differences"] else "failed"
    except (OSError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        result["error"] = str(exc)
    (args.out / "common-regression.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
