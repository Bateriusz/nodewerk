#!/usr/bin/env python3
"""Verify and optionally publish the NW-01A backlog correction bundle.

NW-01A is an architecture/documentation/backlog task.  This tool is deliberately
standalone: it validates preserved original issue bodies, exact updated bodies,
recomputed pairallel section offsets, the canonical common-profile digest, and
the NW-07/NW-11 policy invariants.  ``publish --apply`` is guarded by a live
body-hash compare so concurrent GitHub edits are never overwritten silently.

This is a backlog metadata/readback tool, not the NW-01B evidence schema,
capture-tool, or GPU evidence verifier.  Those implementations remain deferred
to NW-01B as described in ``docs/nw01b-handoff.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKLOG = REPO_ROOT / "docs" / "backlog"
AUDIT_PATH = BACKLOG / "nw01a-issue-audit.v1.json"
RECORD_PATH = BACKLOG / "nw01a-publication-record.v1.json"
PROFILE_PATH = REPO_ROOT / "benchmarks" / "capability-profile.common.v1.json"
GH_REPO = "Bateriusz/nodewerk"

SECTION_MANIFEST_RE = re.compile(r"<!-- pairallel-sections:v1 (\{.*?\}) -->", re.S)
REFINEMENT_MARKER_RE = re.compile(r"<!-- pairallel-refinement:v1 (\{.*?\}) -->", re.S)
SECTION_RE = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*$")

EXPECTED_TITLES = {
    3: "NW-02 — Implement versioned operator schemas, typed DAGs, and deterministic evaluation",
    4: "NW-03 — Build offline shader lowering and a bounded release-package format",
    5: "NW-04 — Implement the minimal package-driven D3D12 player",
    6: "NW-05 — Compile whole graphs into specialized players and explicit resource schedules",
    7: "NW-06 — Add procedural mesh, texture, scene, and integer-timeline operators",
    8: "NW-07 — Deliver and benchmark the 256 KiB architectural proof",
    9: "NW-08 — Add a minimal authoring editor with incremental GPU caching",
    10: "NW-09 — Introduce explicit temporal state and replayable simulation",
    11: "NW-10 — Engineer compact exports and determine true 64 KiB feasibility",
    12: "NW-11 — Evaluate advanced RTX features through bounded, costed experiments",
    13: "NW-12 — Add optional procedural audio through the authoritative clock boundary",
}

EXPECTED_SCOPE_KEYS = {
    3: "versioned operator schemas",
    4: "offline shader lowering",
    5: "package-driven d3d12 player",
    6: "specialized players and explicit resource schedules",
    7: "procedural mesh, texture, scene",
    8: "256 kib architectural proof",
    9: "authoring editor with incremental gpu caching",
    10: "temporal state and replayable simulation",
    11: "compact exports",
    12: "advanced rtx features",
    13: "optional procedural audio",
}

HEADING_TO_SLUG = {
    "Goal": "goal",
    "Source context": "source-context",
    "Final technical design": "final-technical-design",
    "Scope": "scope",
    "Affected components": "affected-components",
    "Required behavior": "required-behavior",
    "State, configuration, and API changes": "state-configuration-api-changes",
    "Recovery semantics": "recovery-semantics",
    "Compatibility and safety constraints": "compatibility-and-safety-constraints",
    "Test plan": "test-plan",
    "Regressions": "regressions",
    "Acceptance criteria": "acceptance-criteria",
    "Decomposition dependencies and boundaries": "decomposition-dependencies-and-boundaries",
}

REQUIRED_SLUGS = [
    "final-technical-design",
    "scope",
    "required-behavior",
    "state-configuration-api-changes",
    "recovery-semantics",
    "compatibility-and-safety-constraints",
    "test-plan",
    "regressions",
    "acceptance-criteria",
    "decomposition-dependencies-and-boundaries",
]

class VerificationError(RuntimeError):
    pass


def sha256_v1(text: str) -> str:
    return "sha256:v1:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalized_body_hash(text: str) -> str:
    """Hash a GitHub body after newline normalization and trailing-newline trim."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return sha256_v1(normalized)


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def read_issue_file(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("# "):
        # Body-only file used for exact hash comparison and publication.
        return "", text
    if text.startswith("## "):
        return "", text
    first, rest = text.split("\n", 1)
    title = first[2:].strip()
    body = rest[1:] if rest.startswith("\n") else rest
    return title, body


def extract_marker(body: str, pattern: re.Pattern[str]) -> tuple[str, dict]:
    matches = list(pattern.finditer(body))
    if len(matches) != 1:
        raise VerificationError(
            f"expected exactly one {pattern.pattern!r} marker, found {len(matches)}"
        )
    raw = matches[0].group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"marker JSON is malformed: {exc}") from exc
    return matches[0].group(0), data


def verify_section_manifest(body: str) -> list[dict]:
    manifest_matches = list(SECTION_MANIFEST_RE.finditer(body))
    if not manifest_matches:
        raise VerificationError("missing pairallel-sections:v1 manifest")
    manifest_match = manifest_matches[-1]
    if body[manifest_match.end() :].strip().startswith("<!-- pairallel-refinement"):
        pass
    elif body[manifest_match.end() :].strip():
        raise VerificationError("section manifest is not the final rendered metadata")
    if len(manifest_matches) != 1:
        raise VerificationError("duplicate pairallel-sections:v1 manifests")
    try:
        manifest = json.loads(manifest_match.group(1))
    except json.JSONDecodeError as exc:
        raise VerificationError(f"section manifest JSON is malformed: {exc}") from exc
    if manifest.get("version") != "v1" or not isinstance(manifest.get("sections"), list):
        raise VerificationError("section manifest has an unsupported shape")

    body_core = body[: manifest_match.start()].rstrip()
    headings = list(SECTION_RE.finditer(body_core))
    if not headings:
        raise VerificationError("issue body has no markdown sections")
    calculated = []
    for index, match in enumerate(headings):
        heading = match.group(1).strip()
        slug = HEADING_TO_SLUG.get(heading)
        if slug is None:
            raise VerificationError(f"unknown public section heading {heading!r}")
        start = match.end()
        if body_core[start : start + 2] == "\n\n":
            start += 2
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body_core)
        if index + 1 < len(headings):
            end -= 2  # rendered "\n\n" separator
        calculated.append({"slug": slug, "start": start, "end": end})
    if calculated != manifest["sections"]:
        raise VerificationError(
            "pairallel-sections:v1 offsets do not match the current body content"
        )
    for entry in calculated:
        if not isinstance(entry["start"], int) or not isinstance(entry["end"], int):
            raise VerificationError("section offsets must be integers")
        if entry["end"] < entry["start"]:
            raise VerificationError("section offset end precedes start")
        if not body_core[entry["start"] : entry["end"]].strip():
            raise VerificationError(f"section {entry['slug']} has empty content")
    return calculated


def verify_profile() -> dict:
    manifest_path = REPO_ROOT / "benchmarks" / "capability-manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if profile.get("profile_id") != "nw.common.d3d12.fl12_0.sm6_0.v1":
        raise VerificationError("unexpected common profile ID")
    digest = "sha256:v1:" + hashlib.sha256(
        canonical_json(profile["requirements"]).encode("utf-8")
    ).hexdigest()
    if digest != profile.get("requirements_digest"):
        raise VerificationError(
            f"common profile digest mismatch: computed {digest}, "
            f"recorded {profile.get('requirements_digest')}"
        )
    req = profile["requirements"]
    queried = req["queried_capabilities"]
    if queried.get("feature_level") != "D3D_FEATURE_LEVEL_12_0":
        raise VerificationError("common profile does not require feature level 12_0")
    if queried.get("shader_model") != "D3D_SHADER_MODEL_6_0":
        raise VerificationError("common profile does not require shader model 6.0")
    if queried.get("resource_binding_tier_min") != 2:
        raise VerificationError("common profile does not require resource binding tier 2")
    if queried.get("resource_heap_tier_min") != 2:
        raise VerificationError("common profile does not require resource heap tier 2")
    arch = req["architectural_constraints"]
    if arch.get("descriptor_model") != "bounded_descriptor_tables":
        raise VerificationError("common profile descriptor model changed")
    if arch.get("barrier_model") != "ordinary_resource_barriers":
        raise VerificationError("common profile barrier model changed")
    if arch.get("queue_model", {}).get("direct_queue_count") != 1:
        raise VerificationError("common profile must require exactly one direct queue")

    # Cross-check the profile against the frozen NW-01 capability manifest.
    graphics = manifest["graphics_api"]
    required = manifest["required_features"]
    if graphics.get("feature_level") != "12_0":
        raise VerificationError("NW-01 manifest feature level changed")
    if graphics.get("shader_model") != "6.0":
        raise VerificationError("NW-01 manifest shader model changed")
    if required.get("resource_binding_tier") != queried.get("resource_binding_tier_min"):
        raise VerificationError("profile binding tier disagrees with NW-01 manifest")
    if required.get("resource_heap_tier") != queried.get("resource_heap_tier_min"):
        raise VerificationError("profile heap tier disagrees with NW-01 manifest")
    if graphics.get("descriptor_model") != arch.get("descriptor_model"):
        raise VerificationError("profile descriptor model disagrees with NW-01 manifest")
    if graphics.get("barrier_model") != arch.get("barrier_model"):
        raise VerificationError("profile barrier model disagrees with NW-01 manifest")
    if graphics.get("queue_model") != arch.get("queue_model"):
        raise VerificationError("profile queue model disagrees with NW-01 manifest")
    manifest_formats = {item["dxgi_format"] for item in manifest["required_formats"]}
    profile_formats = {
        item["dxgi_format"] for item in queried["required_format_support"]
    }
    if manifest_formats != profile_formats:
        raise VerificationError("profile formats disagree with NW-01 manifest")
    if manifest["baseline_limits"] != arch["baseline_limits"]:
        raise VerificationError("profile limits disagree with NW-01 manifest")
    if manifest["excluded_baseline_features"] != arch["excluded_features"]:
        raise VerificationError("profile exclusions disagree with NW-01 manifest")
    return profile


def verify_policy_docs(profile: dict) -> None:
    adr = (REPO_ROOT / "docs/architecture/0006-capability-based-gpu-support-policy.md").read_text(
        encoding="utf-8"
    )
    adr_lower = adr.lower()
    for needle in (
        profile["profile_id"],
        profile["requirements_digest"],
        "D3D_FEATURE_LEVEL_12_0",
        "D3D_SHADER_MODEL_6_0",
        "resource binding tier `>= 2`",
        "resource heap tier `>= 2`",
        "bounded descriptor tables",
        "ordinary `TRANSITION`/`UAV`/`ALIASING` barriers",
        "exactly one direct queue",
        "nw07_perf_ref_v1",
        "16.67",
        "five-second warm-up",
        "three uncapped 60-second runs",
        "RTX 4060 Ti",
        "cannot certify",
        "pending",
        "13.5",
        "12.00",
    ):
        if needle.lower() not in adr_lower:
            raise VerificationError(f"ADR-0006 is missing required text: {needle!r}")
    mapping = (REPO_ROOT / "docs/architecture/0005-task-contracts-nw02-nw12.md").read_text(
        encoding="utf-8"
    )
    for number, title in EXPECTED_TITLES.items():
        task = f"NW-{number - 1:02d}"
        if task not in mapping or f"#{number}" not in mapping:
            raise VerificationError(f"ADR-0005 is missing {task} / issue #{number}")
        if EXPECTED_SCOPE_KEYS[number] not in mapping.lower():
            raise VerificationError(f"ADR-0005 is missing actual scope text for {task}")
    for stale in (
        "Audio Synthesis & Demo Timeline Synchronization",
        "Authoring Application (`nodewerk-studio`) | Strictly separated",
        "Final Hardware Qualification & Release Sign-Off",
    ):
        if stale in mapping:
            raise VerificationError(f"ADR-0005 retains stale mapping text: {stale!r}")


def verify_task_policy(record: dict, body: str) -> None:
    task = record["task"]
    required_slugs = {entry["slug"] for entry in record["sections"]}
    missing = set(REQUIRED_SLUGS) - required_slugs
    if missing:
        raise VerificationError(f"{task}: audit is missing sections {sorted(missing)}")
    if "Global prerequisite: NW-01A and NW-01B" not in body:
        raise VerificationError(f"{task}: missing global prerequisite note")

    if task == "NW-07":
        for needle in (
            "nw07_perf_ref_v1",
            "1920x1080",
            "strictly less than 16.67 ms",
            "five-second warm-up",
            "three uncapped 60-second runs",
            "raw per-run samples",
            "percentile",
            "RTX 4060 Ti",
            "cannot certify",
            "pending",
            "262,144",
            "512 MiB",
            "first-present within 2 seconds",
            "representative compliant desktop Ada",
            "representative compliant desktop Blackwell",
        ):
            if needle not in body:
                raise VerificationError(f"NW-07 body is missing required text: {needle!r}")
        if "The historical NW-01 RTX 4060 Ti capture is not a qualifying RTX 4060 NW-07 result" not in body:
            raise VerificationError("NW-07 body does not reject the historical RTX 4060 Ti capture")
        if "Maintain a separate matched-environment Blackwell development/performance series" not in body:
            raise VerificationError("NW-07 body is missing the separate Blackwell series")
    elif task == "NW-11":
        for needle in (
            "representative desktop Ada and Blackwell",
            "matched baseline/variant pairs",
            "queried capabilities satisfy the profile",
            "separately exported and tested common-profile artifact",
            "Unsupported feature coverage",
            "pending",
            "10 percent lower GPU time",
            "evaluated separately for each capability-qualified matched device and environment",
        ):
            if needle not in body:
                raise VerificationError(f"NW-11 body is missing required text: {needle!r}")
        if "Run paired baseline/variant captures on RTX 4060 and representative RTX 50 hardware" in body:
            raise VerificationError("NW-11 body retains the exact-RTX-4060 paired-test prerequisite")
    elif task == "NW-12":
        for needle in ("hardware-neutral", "no GPU generation", "Global prerequisite"):
            if needle not in body:
                raise VerificationError(f"NW-12 body is missing hardware-neutral text: {needle!r}")


def verify_bundle() -> None:
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    record = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    if len(audit["tasks"]) != 11 or len(record["issues"]) != 11:
        raise VerificationError("bundle must contain all eleven NW-02..NW-12 tasks")
    profile = verify_profile()
    verify_policy_docs(profile)

    record_by_task = {item["task"]: item for item in record["issues"]}
    for task in audit["tasks"]:
        number = task["issue_number"]
        if EXPECTED_TITLES.get(number) != task["title"]:
            raise VerificationError(f"issue #{number}: title does not match verified identity")
        source_revision = task.get("source_revision") or {}
        if (
            source_revision.get("issue_number") != number
            or source_revision.get("original_body_hash") != task["original_body_hash"]
            or source_revision.get("refinement_hash") != task["source_refinement_hash"]
        ):
            raise VerificationError(f"{task['task']}: source revision identity is inconsistent")
        original_path = REPO_ROOT / task["original_body_path"]
        updated_path = REPO_ROOT / task["updated_body_path"]
        original_title, original_body = read_issue_file(original_path)
        updated_title, updated_body = read_issue_file(updated_path)
        if task.get("original_document_path") and task.get("updated_document_path"):
            original_doc_title, _ = read_issue_file(
                REPO_ROOT / task["original_document_path"]
            )
            updated_doc_title, _ = read_issue_file(
                REPO_ROOT / task["updated_document_path"]
            )
            if original_doc_title != task["title"] or updated_doc_title != task["title"]:
                raise VerificationError(f"{task['task']}: title differs from audit record")
        if sha256_v1(original_body) != task["original_body_hash"]:
            raise VerificationError(f"{task['task']}: original body hash mismatch")
        if sha256_v1(updated_body) != task["updated_body_hash"]:
            raise VerificationError(f"{task['task']}: updated body hash mismatch")
        original_marker, _ = extract_marker(original_body, REFINEMENT_MARKER_RE)
        updated_marker, _ = extract_marker(updated_body, REFINEMENT_MARKER_RE)
        if original_marker != updated_marker:
            raise VerificationError(f"{task['task']}: refinement provenance marker changed")
        offsets = verify_section_manifest(updated_body)
        if offsets != task["section_offsets"]:
            raise VerificationError(f"{task['task']}: recomputed offsets differ from audit")
        rec = record_by_task.get(task["task"])
        if rec is None:
            raise VerificationError(f"{task['task']}: missing publication record")
        if rec["original_body_hash"] != task["original_body_hash"]:
            raise VerificationError(f"{task['task']}: publication original hash mismatch")
        if rec["updated_body_hash"] != task["updated_body_hash"]:
            raise VerificationError(f"{task['task']}: publication updated hash mismatch")
        verify_task_policy(task, updated_body)


def run_gh(args: list[str]) -> str:
    result = subprocess.run(
        ["gh", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(
            "gh command failed: " + (result.stderr.strip() or result.stdout.strip())
        )
    return result.stdout


def publish(apply: bool) -> int:
    verify_bundle()
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    record = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    record_by_task = {item["task"]: item for item in record["issues"]}

    def persist_record() -> None:
        if apply:
            RECORD_PATH.write_text(
                json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    for task in audit["tasks"]:
        number = task["issue_number"]
        title = task["title"]
        updated_path = REPO_ROOT / task["updated_body_path"]
        _, updated_body = read_issue_file(updated_path)
        _, original_body = read_issue_file(REPO_ROOT / task["original_body_path"])
        expected_updated_hash = normalized_body_hash(updated_body)
        expected_original_hash = normalized_body_hash(original_body)
        try:
            raw = run_gh(
                ["issue", "view", str(number), "--repo", GH_REPO, "--json", "title,body"]
            )
            current = json.loads(raw)
        except (VerificationError, json.JSONDecodeError) as exc:
            print(f"{task['task']} #{number}: live read failed: {exc}", file=sys.stderr)
            return 2
        current_title = current.get("title", "")
        current_body = current.get("body", "") or ""
        current_hash = normalized_body_hash(current_body)
        if current_title != title:
            print(
                f"{task['task']} #{number}: concurrent title edit detected; aborting",
                file=sys.stderr,
            )
            return 3
        if current_hash == expected_updated_hash:
            print(f"{task['task']} #{number}: already updated")
            rec = record_by_task[task["task"]]
            rec["completion_status"] = "published_and_verified"
            rec["github_publication_status"] = "published"
            rec["published_body_hash"] = current_hash
            persist_record()
            continue
        if current_hash != expected_original_hash:
            print(
                f"{task['task']} #{number}: live body differs from recorded original; "
                "concurrent edit requires manual reconciliation",
                file=sys.stderr,
            )
            return 3
        if not apply:
            print(f"{task['task']} #{number}: would publish {updated_path}")
            continue
        # The repository file carries a human-readable "# title" line; GitHub
        # issue bodies must contain only the body.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md") as handle:
            handle.write(updated_body)
            handle.flush()
            run_gh(
                [
                    "issue",
                    "edit",
                    str(number),
                    "--repo",
                    GH_REPO,
                    "--body-file",
                    handle.name,
                ]
            )
        readback = json.loads(
            run_gh(
                ["issue", "view", str(number), "--repo", GH_REPO, "--json", "title,body"]
            )
        )
        # The file starts with "# title\n\n"; GitHub issue body must contain only
        # the body.  Re-drive the published form to make the hash comparison exact.
        published_body = (readback.get("body") or "")
        expected_body = updated_body
        if normalized_body_hash(published_body) != normalized_body_hash(expected_body):
            print(
                f"{task['task']} #{number}: readback verification failed; "
                "record left incomplete",
                file=sys.stderr,
            )
            return 4
        rec = record_by_task[task["task"]]
        rec["completion_status"] = "published_and_verified"
        rec["github_publication_status"] = "published"
        rec["published_body_hash"] = normalized_body_hash(published_body)
        persist_record()
        print(f"{task['task']} #{number}: published and verified")
    if apply:
        record["github_publication_status"] = "published"
        persist_record()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("verify", help="verify the local backlog bundle (default)")
    pub = sub.add_parser("publish", help="publish the exact bodies with concurrent-edit guard")
    pub.add_argument("--apply", action="store_true", help="perform gh issue edits")
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            return publish(args.apply)
        verify_bundle()
        print("NW-01A backlog bundle verified")
        return 0
    except VerificationError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
