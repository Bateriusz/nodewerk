#!/usr/bin/env python3
"""Recover measured NW-11 bundles without launching a renderer; export a public projection.

Run this newer repository-owned tool against the original trusted bundle and
the untouched raw directories. Never replace old evidence or relabel its
tested commit. All raw JSON, logs and pixels remain private.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gpu_experiment_measurements as measurements
import nw11_bundle as bundle
import nw11_contract as contract
import nw11_evidence as evidence
import nw11_verify as verifier


def write_new(path: Path, data: dict) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != data:
            raise evidence.EvidenceError(f"Refusing to replace different existing derived record: {path.name}")
        return
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)


def image_projection(image: dict) -> dict:
    # The comparator emits only relative tick identities, scalar metrics and hashes.
    return {
        **{key: image[key] for key in (
            "fixed_ticks", "max_abs_channel_error", "rmse",
            "baseline_manifest_sha256", "variant_manifest_sha256",
        ) if key in image},
        "per_tick": [
            {key: row[key] for key in (
                "tick", "max_abs_channel_error", "rmse", "baseline_sha256", "variant_sha256",
            )}
            for row in image.get("per_tick", [])
        ],
        "loop_boundary": {
            role: {key: metrics[key] for key in ("max_abs_channel_error", "rmse")}
            for role, metrics in image.get("loop_boundary", {}).items()
            if role in {"baseline", "variant"}
        },
    }


def public_projection(raw: dict, manifest: dict, protocol_version: int) -> dict:
    evidence.validate_decision_document(raw, published=True,
                                        expected_experiment_id=manifest["experiment_id"])
    for decision_key, manifest_key in (("commit", "commit"), ("bundle_id", "bundle_id"),
                                       ("variant_profile_id", "profile_id"),
                                       ("fallback_artifact_id", "fallback_artifact_id")):
        if raw.get(decision_key) != manifest.get(manifest_key):
            raise evidence.EvidenceError("public projection decision differs from the measured bundle")
    if raw["aggregate_status"] != "accepted" or not raw["satisfies_completion"]:
        raise evidence.EvidenceError("Public functional acceptance needs both verified devices")
    result = {key: raw[key] for key in (
        "format", "version", "task", "implementation_base", "commit", "bundle_id",
        "bundle_manifest_sha256", "evidence_file", "experiment_id", "variant_profile_id",
        "fallback_artifact_id", "aggregate_status", "fallback_tested", "satisfies_completion",
        "bundle_adoption_budget_status", "per_device_decision_isolation", "pending_is_not_pass",
        "unsupported_is_not_failure_or_pass",
    ) if key in raw}
    result["evidence_file"] = None
    cross = raw["cross_device_equivalence"]
    result["cross_device_equivalence"] = {
        "status": cross["status"], "tolerance": cross["tolerance"],
        "rows": {tick: {key: row[key] for key in ("maximum_error", "maximum_relative_error")}
                 for tick, row in cross["rows"].items()},
    }
    result["devices"] = []
    hashes = {row["role"]: row["sha256"] for row in manifest["artifacts"]
              if row["role"] in measurements.ARTIFACT_ROLES.values()}
    hashes["canonical_release"] = next(row["sha256"] for row in manifest["artifacts"]
                                       if row["path"] == "bin/canonical_release.exe")
    for device in raw["devices"]:
        item = {key: device[key] for key in (
            "device_tier", "device_key", "environment_key", "status", "satisfies_completion",
            "fallback_tested", "functional_status", "image_equivalence_status",
            "performance_status", "default_adoption_status", "evidence_sha256",
        )}
        item.update(
            evidence_file="evidence-recovered-v4.json",
            reason="Verified functional execution with separately reported image and performance observations",
            publication_kind="sanitized_projection_of_locally_reverified_raw_evidence",
            raw_aggregate_decision_id=raw["decision_id"],
            measured_protocol_version=protocol_version,
            artifact_sha256=hashes,
            reason_codes=sorted({row["code"] for row in device.get("reasons", [])}),
        )
        item["primary"] = {key: device["primary"][key] for key in (
            "metric", "baseline", "variant", "relative_reduction_percent",
            "relative_reduction_percent_per_run", "required_reduction_percent", "evaluation",
        )}
        item["metrics"] = {}
        for role in measurements.ROLE_ORDER:
            arm = device["metrics"][role]
            item["metrics"][role] = {
                **{key: arm[key] for key in ("distribution_bytes", "shader_count",
                                            "peak_engine_committed_gpu_bytes")},
                "image": image_projection(arm["image"]),
                "process_runs": [
                    {**{key: run[key] for key in ("startup_ms", "presented_frames", "displayed_presents",
                                                "dropped_presents", "peak_engine_committed_gpu_bytes")
                         if key in run},
                     "presentation_telemetry_status": run.get("presentation_telemetry_status", "available"),
                     **{key: run[key] for key in ("gpu_time", "cpu_submit", "frame_time")}}
                    for run in arm["process_runs"]
                ],
            }
        canonical = device["canonical_release_correctness"]
        item["canonical_release_correctness"] = image_projection(canonical)
        item["canonical_release_correctness"]["numeric_loop_boundary"] = {
            key: canonical["numeric_loop_boundary"][key]
            for key in ("status", "start_sha256", "boundary_sha256")
        }
        result["devices"].append(item)
    result["decision_id"] = evidence.decision_id(result)
    return evidence.validate_decision_document(result, published=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--trust-record", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--evidence-dir", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if len(args.evidence_dir) != 2 or len({p.resolve() for p in args.evidence_dir}) != 2:
            raise evidence.EvidenceError("Supply exactly two distinct physical evidence directories")
        root = args.bundle_root.resolve()
        manifest_path = root / bundle.MANIFEST_NAME
        trust = measurements.load_json(args.trust_record)
        manifest_sha = contract.sha256_file(manifest_path)
        if trust.get("status") != "verified" or trust.get("manifest_sha256") != manifest_sha:
            raise evidence.EvidenceError("Bundle does not match its independent CI trust record")
        verified = bundle.verify_bundle(root, expected_commit=args.expected_commit)
        manifest = verified["manifest"]
        if (trust.get("commit") != manifest["commit"]
                or trust.get("bundle_id") != manifest["bundle_id"]):
            raise evidence.EvidenceError("CI trust record identifies a different bundle")
        settings = measurements.load_json(root / manifest["experiment_definition"]["path"])
        if settings["version"] not in {3, 4}:
            raise evidence.EvidenceError("Recovery supports functional protocols v3 and v4 only")
        files = []
        for directory in args.evidence_dir:
            directory = directory.resolve()
            if root == directory or root in directory.parents:
                raise evidence.EvidenceError("Evidence must remain outside the immutable bundle")
            path = directory / "evidence-recovered-v4.json"
            write_new(path, measurements.collect(root, manifest, directory, path.name))
            files.append(path)
        with tempfile.TemporaryDirectory(prefix="nw11-recovery-") as temporary:
            private_path = Path(temporary) / "decisions.json"
            command = ["evidence", "--bundle-root", str(root), "--expected-commit", args.expected_commit,
                       "--require-complete", "--require-accepted", "--json-out", str(private_path)]
            for path in files:
                command += ["--evidence", str(path)]
            if verifier.main(command) != verifier.EXIT_OK:
                raise evidence.EvidenceError("Raw aggregate did not satisfy complete functional verification")
            private = measurements.load_json(private_path)
        write_new(files[0].parent / "decisions-recovered-v4.json", private)
        out = args.out.resolve()
        if root == out or root in out.parents:
            raise evidence.EvidenceError("Output must remain outside the immutable bundle")
        write_new(out, public_projection(private, manifest, settings["version"]))
        print("NW-11: complete local raw verification; sanitized projection written:", out)
    except (ValueError, OSError, TypeError, KeyError, StopIteration) as exc:
        print(f"[NW11-RECOVERY-ERR] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
