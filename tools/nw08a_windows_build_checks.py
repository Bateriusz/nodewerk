#!/usr/bin/env python3
"""Validate and record the Windows canonical NW-07 BuildChecks evidence.

The Windows validation workflow runs ``verify_nw07.py --mode BuildChecks`` and
retains both clean-directory export reports.  This helper makes the acceptance
criteria explicit and machine-checkable:

* the BuildChecks summary is a successful ``BuildChecks`` result;
* the native release export matched the frozen build-input digest;
* both clean export directories produced identical package, manifest,
  executable, and link-manifest hashes;
* the frozen executable stays within the unchanged 262,144-byte hard limit.

It writes a small deterministic record that the workflow uploads beside the raw
BuildChecks JSON.
"""

from __future__ import annotations

import argparse
import build_attestation
import json
import sys
from pathlib import Path
from typing import Any, Mapping

FORMAT = "nw08a-windows-build-checks-record"
VERSION = 1
COMPLETE_DISTRIBUTION_CEILING_BYTES = 262144
IDENTITY_FIELDS = (
    "package_sha256",
    "manifest_sha256",
    "executable_sha256",
    "link_manifest_sha256",
)


class BuildChecksRecordError(ValueError):
    """Raised when Windows BuildChecks evidence is incomplete or inconsistent."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildChecksRecordError(message)


def _load_json(path: Path, description: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildChecksRecordError(f"{description} is not readable JSON: {exc}") from exc
    _require(isinstance(value, Mapping), f"{description} must be a JSON object")
    return value


def _native_evidence(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    _require(
        summary.get("format") == "nw07-verification-summary",
        "BuildChecks summary has the wrong format",
    )
    _require(summary.get("mode") == "BuildChecks", "summary is not BuildChecks")
    _require(summary.get("success") is True, "BuildChecks summary is not successful")
    checks = summary.get("checks")
    _require(isinstance(checks, list), "BuildChecks summary has no checks list")
    native = [
        check
        for check in checks
        if isinstance(check, Mapping)
        and check.get("name") == "nw07_native_release_export"
    ]
    _require(
        len(native) == 1,
        "BuildChecks summary must contain exactly one native release export result",
    )
    check = native[0]
    _require(
        check.get("status") == "passed",
        "native release export did not pass on the Windows host",
    )
    evidence = check.get("evidence")
    _require(
        isinstance(evidence, Mapping),
        "native release export has no evidence object",
    )
    return evidence


def _artifact(report: Mapping[str, Any], description: str) -> Mapping[str, Any]:
    _require(report.get("success") is True, f"{description} is not successful")
    _require(
        report.get("release_grade") is True and report.get("test_payload") is False,
        f"{description} is not a release-grade non-test export",
    )
    build_inputs = report.get("build_inputs")
    _require(
        isinstance(build_inputs, Mapping)
        and build_attestation.has_recorded_inputs(build_inputs),
        f"{description} did not record its build inputs",
    )
    artifact = report.get("artifact")
    _require(
        isinstance(artifact, Mapping), f"{description} has no artifact identity"
    )
    return artifact


def build_record(
    *,
    summary_path: Path,
    verification_root: Path,
) -> dict[str, Any]:
    """Validate the BuildChecks summary and clean-directory reports."""

    summary = _load_json(summary_path, "BuildChecks summary")
    evidence = _native_evidence(summary)

    _require(build_attestation.has_recorded_inputs(evidence),
             "native export evidence has no source-input provenance")
    for field in ("reproducible", "within_ceiling"):
        _require(
            evidence.get(field) is True,
            f"native export evidence does not report {field}=true",
        )
    _require(
        evidence.get("release_grade") is True and evidence.get("test_payload") is False,
        "native export evidence is not a release-grade non-test export",
    )
    complete = evidence.get("complete_distribution_bytes")
    _require(
        isinstance(complete, int) and complete > 0,
        "native export evidence has no complete distribution byte count",
    )
    _require(
        complete <= COMPLETE_DISTRIBUTION_CEILING_BYTES,
        f"frozen executable is {complete} bytes, above the "
        f"{COMPLETE_DISTRIBUTION_CEILING_BYTES}-byte hard limit",
    )

    repro = verification_root / "repro"
    first_report = _load_json(repro / "clean-a-report.json", "clean-a export report")
    second_report = _load_json(
        repro / "clean-b-report.json", "clean-b export report"
    )
    first = _artifact(first_report, "clean-a export report")
    second = _artifact(second_report, "clean-b export report")

    identities: dict[str, dict[str, Any]] = {}
    for field in IDENTITY_FIELDS:
        first_value = first.get(field)
        second_value = second.get(field)
        _require(
            isinstance(first_value, str) and bool(first_value),
            f"clean-a export has no {field}",
        )
        _require(
            first_value == second_value,
            f"clean-directory exports disagree on {field}: "
            f"{first_value!r} != {second_value!r}",
        )
        if field in evidence:
            _require(
                evidence.get(field) == first_value,
                f"BuildChecks evidence {field} does not match the clean exports",
            )
        identities[field] = {"first": first_value, "second": second_value, "match": True}

    first_size = first.get("size_accounting")
    second_size = second.get("size_accounting")
    _require(
        isinstance(first_size, Mapping) and isinstance(second_size, Mapping),
        "clean export reports have no size accounting",
    )
    first_complete = first_size.get("complete_distribution_bytes")
    second_complete = second_size.get("complete_distribution_bytes")
    _require(
        first_complete == second_complete == complete,
        "clean-directory distribution sizes do not agree with BuildChecks evidence",
    )

    first_inputs = first_report["build_inputs"]
    second_inputs = second_report["build_inputs"]
    digest = first_inputs.get("digest")
    _require(
        isinstance(digest, str)
        and digest
        and digest == second_inputs.get("digest")
        and digest == evidence.get("build_inputs_digest"),
        "clean-directory build-input digests do not agree with BuildChecks evidence",
    )

    return {
        "format": FORMAT,
        "version": VERSION,
        "success": True,
        "build_checks_summary": str(summary_path),
        "verification_root": str(verification_root),
        "native_release_export_status": "passed",
        "clean_build_directories": [
            str(repro / "clean-a"),
            str(repro / "clean-b"),
        ],
        "build_inputs_digest": digest,
        "matches_frozen_digest": evidence.get("matches_frozen_digest") is True,
        "source_inputs_recorded": build_attestation.has_recorded_inputs(evidence),
        "reproducible": True,
        "release_grade": True,
        "test_payload": False,
        "identity": identities,
        "complete_distribution_bytes": complete,
        "hard_ceiling_bytes": COMPLETE_DISTRIBUTION_CEILING_BYTES,
        "within_hard_ceiling": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--verification-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        record = build_record(
            summary_path=Path(args.summary),
            verification_root=Path(args.verification_root),
        )
    except BuildChecksRecordError as exc:
        print(f"NW-08A Windows BuildChecks record failed: {exc}", file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
