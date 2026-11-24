#!/usr/bin/env python3
"""Repository-owned NW-11 verifier used before and after physical validation.

The same file is copied into every validation bundle.  It uses only the Python
standard library and sibling NW-11 modules, so a clean validation machine does
not need a repository checkout, a build, Visual Studio changes, CMake
configuration, or Python package installation.

Exit codes:
    0   verified / accepted (or a deliberately requested informational result)
    2   malformed manifest, profile, experiment, or evidence
    3   capability absence: explicitly unsupported (never a pass)
    4   feature initialization or execution failure (never substituted)
    5   stale evidence bound to a different commit or artifact hash
    6   bundle identity mismatch
    10  evidence is complete but the experiment decision is rejected
    11  required device/environment coverage is still pending
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import nw11_capability as capability  # noqa: E402
import nw11_contract as contract  # noqa: E402
import nw11_evidence as evidence  # noqa: E402
import nw11_experiment as experiment  # noqa: E402
import nw11_bundle as bundle  # noqa: E402
import nw11_followup as followup  # noqa: E402

EXIT_OK = 0
EXIT_MALFORMED = 2
EXIT_UNSUPPORTED = 3
EXIT_EXECUTION_FAILED = 4
EXIT_STALE = 5
EXIT_IDENTITY = 6
EXIT_REJECTED = 10
EXIT_PENDING = 11


def _write_json(path: Path | str | None, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(text, end="")
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")


def _emit_error(code: int, kind: str, message: str) -> int:
    print(
        f"[NW11-VERIFY-ERR][exit_code={code}][kind={kind}] {message}",
        file=sys.stderr,
    )
    return code


def _repo_root() -> Path:
    for candidate in (THIS_DIR.parent, THIS_DIR):
        if (candidate / "benchmarks" / "nw11").is_dir():
            return candidate
    return THIS_DIR.parent


def _load_json(path: Path | str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise evidence.EvidenceError(f"cannot read JSON '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise evidence.EvidenceError(f"'{path}' is not a JSON object")
    return value


def _validate_tracked(root: Path) -> dict[str, Any]:
    profiles = {}
    for profile_id, relative in contract.PROFILE_PATH_BY_ID.items():
        path = root / relative
        profiles[profile_id] = capability.load_profile(path)
    experiments = {}
    for experiment_id, relative in contract.EXPERIMENT_PATH_BY_ID.items():
        experiments[experiment_id] = experiment.load_experiment(path=root / relative)
        experiment.validate_experiment(experiments[experiment_id], repo_root=root)
    matrix = followup.load_matrix(root / contract.FOLLOWUP_MATRIX_PATH)
    shader_contract = bundle.validate_variant_sources(root)
    decisions = {
        experiment_id: evidence.validate_decision_document(
            _load_json(root / relative)
        )
        for experiment_id, relative in contract.DECISION_PATH_BY_ID.items()
    }
    evidence_status = _load_json(root / contract.EVIDENCE_STATUS_PATH)
    if evidence_status.get("format") != "nw11-evidence-status":
        raise evidence.EvidenceError("evidence-status.v1.json has the wrong format")
    payload = {
        "status": "verified",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "profiles": {
            profile_id: document["requirements_digest"]
            for profile_id, document in profiles.items()
        },
        "experiments": sorted(experiments),
        "follow_up_features": [
            entry["feature"] for entry in matrix["features"]
        ],
        "follow_up_prototypes_allowed_in_this_task": False,
        "optional_shader_contract": shader_contract["status"],
        "evidence_status": evidence_status.get("evidence_status"),
        "decisions_aggregate": {
            experiment_id: document.get("aggregate_status")
            for experiment_id, document in decisions.items()
        },
    }
    return payload


def _bundle_manifest_sha256(bundle_root: Path) -> str:
    path = bundle_root / bundle.MANIFEST_NAME
    if not path.is_file():
        raise evidence.EvidenceError("bundle manifest is missing")
    return contract.sha256_bytes(path.read_bytes())


def _load_embedded_json(bundle_root: Path, manifest: Mapping[str, Any], key: str) -> dict[str, Any]:
    record = manifest.get(key)
    if not isinstance(record, Mapping) or "path" not in record:
        raise evidence.EvidenceError(f"bundle manifest has no embedded {key} path")
    return _load_json(bundle_root / str(record["path"]))


def command_self_check(args: argparse.Namespace) -> int:
    root = Path(args.repo_root) if args.repo_root else _repo_root()
    try:
        payload = _validate_tracked(root)
    except (
        capability.CapabilityProfileError,
        experiment.ExperimentError,
        followup.FollowupMatrixError,
        evidence.EvidenceError,
        OSError,
    ) as exc:
        return _emit_error(EXIT_MALFORMED, "self-check", str(exc))
    _write_json(args.json_out, payload)
    return EXIT_OK


def command_bundle(args: argparse.Namespace) -> int:
    try:
        result = bundle.verify_bundle(
            args.bundle_root, expected_commit=args.expected_commit
        )
    except bundle.BundleError as exc:
        return _emit_error(EXIT_IDENTITY, "bundle", str(exc))
    payload = {key: value for key, value in result.items() if key != "manifest"}
    payload["manifest_sha256"] = _bundle_manifest_sha256(Path(args.bundle_root))
    _write_json(args.json_out, payload)
    return EXIT_OK


def command_capability(args: argparse.Namespace) -> int:
    try:
        profile = capability.load_profile(args.profile)
        observation = _load_json(args.observation)
        result = capability.evaluate_observation(profile, observation)
    except (capability.CapabilityProfileError, evidence.EvidenceError) as exc:
        return _emit_error(EXIT_MALFORMED, "capability", str(exc))
    _write_json(args.json_out, result)
    if result["status"] in {"unsupported", "failed"}:
        return EXIT_UNSUPPORTED if result["status"] == "unsupported" else EXIT_EXECUTION_FAILED
    return EXIT_OK


def command_evidence(args: argparse.Namespace) -> int:
    bundle_root = Path(args.bundle_root)
    try:
        verified = bundle.verify_bundle(bundle_root, expected_commit=args.expected_commit)
        manifest = verified["manifest"]
        manifest_sha = _bundle_manifest_sha256(bundle_root)
        experiment_document = _load_embedded_json(
            bundle_root, manifest, "experiment_definition"
        )
        evidence_document = _load_json(args.evidence)
        decisions = evidence.build_decision_document(
            evidence=evidence_document,
            experiment=experiment_document,
            bundle_manifest=manifest,
            bundle_manifest_sha256=manifest_sha,
            expected_commit=args.expected_commit,
        )
        evidence.validate_decision_document(decisions)
    except evidence.StaleEvidenceError as exc:
        return _emit_error(EXIT_STALE, "evidence", str(exc))
    except (
        bundle.BundleError,
        evidence.EvidenceError,
        experiment.ExperimentError,
    ) as exc:
        return _emit_error(EXIT_MALFORMED, "evidence", str(exc))
    _write_json(args.json_out, decisions)
    statuses = {item["status"] for item in decisions["devices"]}
    if "failed" in statuses:
        return EXIT_EXECUTION_FAILED
    if statuses == {"unsupported"}:
        return EXIT_UNSUPPORTED
    if decisions["aggregate_status"] == "rejected":
        return EXIT_REJECTED if args.require_accepted else EXIT_OK
    if args.require_accepted and not decisions["satisfies_completion"]:
        return EXIT_PENDING
    return EXIT_OK


def command_derive(args: argparse.Namespace) -> int:
    try:
        profile = capability.profile_manifest_template(args.profile_id)
    except KeyError as exc:
        return _emit_error(EXIT_MALFORMED, "derive", str(exc))
    _write_json(args.json_out, profile)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    self_check = subparsers.add_parser("self-check")
    self_check.add_argument("--repo-root")
    self_check.add_argument("--json-out")
    self_check.set_defaults(func=command_self_check)

    bundle_parser = subparsers.add_parser("bundle")
    bundle_parser.add_argument("--bundle-root", required=True)
    bundle_parser.add_argument("--expected-commit")
    bundle_parser.add_argument("--json-out")
    bundle_parser.set_defaults(func=command_bundle)

    capability_parser = subparsers.add_parser("capability")
    capability_parser.add_argument("--profile", required=True)
    capability_parser.add_argument("--observation", required=True)
    capability_parser.add_argument("--json-out")
    capability_parser.set_defaults(func=command_capability)

    evidence_parser = subparsers.add_parser("evidence")
    evidence_parser.add_argument("--bundle-root", required=True)
    evidence_parser.add_argument("--evidence", required=True)
    evidence_parser.add_argument("--expected-commit")
    evidence_parser.add_argument("--require-accepted", action="store_true")
    evidence_parser.add_argument("--json-out")
    evidence_parser.set_defaults(func=command_evidence)

    derive_parser = subparsers.add_parser("derive-profile")
    derive_parser.add_argument("--profile-id", required=True)
    derive_parser.add_argument("--json-out")
    derive_parser.set_defaults(func=command_derive)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
