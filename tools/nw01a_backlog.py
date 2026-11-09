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
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKLOG = REPO_ROOT / "docs" / "backlog"
AUDIT_PATH = BACKLOG / "nw01a-issue-audit.v1.json"
RECORD_PATH = BACKLOG / "nw01a-publication-record.v1.json"
PROFILE_PATH = REPO_ROOT / "benchmarks" / "capability-profile.common.v1.json"
CAPABILITY_MANIFEST_PATH = REPO_ROOT / "benchmarks" / "capability-manifest.v1.json"
TOOLCHAIN_MANIFEST_PATH = REPO_ROOT / "benchmarks" / "toolchain-manifest.v1.json"
ADR0007_PATH = REPO_ROOT / "docs" / "architecture" / "0007-nw02-nw12-section-audit.md"
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


def sha256_file_v1(path: Path) -> str:
    # Git may check text files out with CRLF on Windows.  Line-ending spelling is
    # not semantic content, so hash the newline-normalized bytes to keep the
    # recorded LF-based source digest stable across checkouts.
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return "sha256:v1:" + hashlib.sha256(data).hexdigest()


def normalize_body(text: str) -> str:
    """Normalize GitHub newline spelling and trailing newlines.

    Section offsets are defined over this normalized form.  GitHub may return
    CRLF even when the published body was LF; normalizing before recomputing
    offsets prevents a matching normalized hash from hiding stale offsets.
    """

    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def normalized_body_hash(text: str) -> str:
    """Hash a GitHub body after newline normalization and trailing-newline trim."""

    return sha256_v1(normalize_body(text))


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


def extract_sections(body: str) -> dict[str, str]:
    """Return section bodies keyed by slug from a rendered issue body/document.

    Machine metadata markers and any title line are ignored.  Offsets are not
    returned here; the manifest verifier owns the exact offset contract.
    """

    normalized = normalize_body(body)
    section_cut = normalized.find("<!-- pairallel-sections:v1")
    if section_cut != -1:
        normalized = normalized[:section_cut].rstrip()
    refinement_cut = normalized.find("<!-- pairallel-refinement:v1")
    if refinement_cut != -1:
        normalized = normalized[:refinement_cut].rstrip()
    headings = list(SECTION_RE.finditer(normalized))
    sections: dict[str, str] = {}
    for index, match in enumerate(headings):
        heading = match.group(1).strip()
        slug = HEADING_TO_SLUG.get(heading)
        if slug is None:
            continue
        start = match.end()
        if normalized[start : start + 2] == "\n\n":
            start += 2
        end = headings[index + 1].start() if index + 1 < len(headings) else len(normalized)
        if index + 1 < len(headings):
            end -= 2
        sections[slug] = normalized[start:end].strip()
    return sections


def render_section_manifest(offsets: list[dict]) -> str:
    return "<!-- pairallel-sections:v1 " + json.dumps(
        {"version": "v1", "sections": offsets},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ) + " -->"


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


def _format_usage_map(items: list[dict]) -> dict[str, set[str]]:
    return {
        item["dxgi_format"]: {usage for usage in item.get("usages", [])}
        for item in items
    }


def verify_profile_against_sources(profile: dict, manifest: dict, toolchain: dict) -> None:
    """Cross-check every profile requirement category against NW-01 sources."""

    req = profile["requirements"]
    expected_categories = {
        "architectural_constraints",
        "pinned_build_tools",
        "queried_capabilities",
        "runtime_requirements",
        "shader_targets",
    }
    if set(req) != expected_categories:
        raise VerificationError(
            "common profile has unknown or missing requirement categories: "
            f"{sorted(set(req) ^ expected_categories)}"
        )

    queried = req["queried_capabilities"]
    graphics = manifest["graphics_api"]
    required = manifest["required_features"]

    # Queried capabilities.
    if queried.get("graphics_api") != graphics.get("api"):
        raise VerificationError("profile graphics API disagrees with NW-01 manifest")
    if queried.get("feature_level") != "D3D_FEATURE_LEVEL_" + graphics["feature_level"]:
        raise VerificationError("profile feature level disagrees with NW-01 manifest")
    if queried.get("feature_level_hex") != "0xc000":
        raise VerificationError("profile feature level hex is not 0xc000")
    expected_shader_model = "D3D_SHADER_MODEL_" + graphics["shader_model"].replace(
        ".", "_"
    )
    if queried.get("shader_model") != expected_shader_model:
        raise VerificationError("profile shader model disagrees with NW-01 manifest")
    if queried.get("shader_model_hex") != "0x60":
        raise VerificationError("profile shader model hex is not 0x60")
    if required.get("resource_binding_tier") != queried.get("resource_binding_tier_min"):
        raise VerificationError("profile binding tier disagrees with NW-01 manifest")
    if required.get("resource_heap_tier") != queried.get("resource_heap_tier_min"):
        raise VerificationError("profile heap tier disagrees with NW-01 manifest")
    profile_formats = _format_usage_map(queried.get("required_format_support", []))
    manifest_formats = _format_usage_map(manifest.get("required_formats", []))
    if profile_formats != manifest_formats:
        raise VerificationError(
            "profile format support or usages disagree with NW-01 manifest: "
            f"{profile_formats!r} != {manifest_formats!r}"
        )
    for optional_feature in (
        "TypedUAVLoadAdditionalFormats",
        "WaveOps",
        "Int64ShaderOps",
    ):
        if required.get(optional_feature) is not False:
            raise VerificationError(
                f"NW-01 manifest treats optional feature {optional_feature} as common"
            )
    optional_queries = queried.get("optional_features_queried_only_when_emitted", [])
    for optional_feature in (
        "TypedUAVLoadAdditionalFormats",
        "WaveOps",
        "Int64ShaderOps",
        "Native16BitShaderOpsSupported",
    ):
        if not any(
            optional_feature in query for query in optional_queries
        ):
            raise VerificationError(
                f"profile is missing emitted-only query for {optional_feature}"
            )

    # Architectural constraints.
    arch = req["architectural_constraints"]
    if graphics.get("descriptor_model") != arch.get("descriptor_model"):
        raise VerificationError("profile descriptor model disagrees with NW-01 manifest")
    if graphics.get("barrier_model") != arch.get("barrier_model"):
        raise VerificationError("profile barrier model disagrees with NW-01 manifest")
    if graphics.get("queue_model") != arch.get("queue_model"):
        raise VerificationError("profile queue model disagrees with NW-01 manifest")
    if arch.get("queue_model", {}).get("direct_queue_count") != 1:
        raise VerificationError("common profile must require exactly one direct queue")
    if arch.get("queue_model", {}).get("compute_queue_count") != 0:
        raise VerificationError("common profile must not require a compute queue")
    if arch.get("queue_model", {}).get("copy_queue_count") != 0:
        raise VerificationError("common profile must not require a copy queue")
    if manifest["baseline_limits"] != arch.get("baseline_limits"):
        raise VerificationError("profile limits disagree with NW-01 manifest")
    if manifest["excluded_baseline_features"] != arch.get("excluded_features"):
        raise VerificationError("profile exclusions disagree with NW-01 manifest")
    if arch.get("enhanced_barriers") is not False:
        raise VerificationError("common profile must exclude enhanced barriers")
    if set(arch.get("barrier_types", [])) != {"TRANSITION", "UAV", "ALIASING"}:
        raise VerificationError("common profile barrier types changed")

    # Shader targets and pinned build tools.
    shader_targets = req["shader_targets"]
    if set(shader_targets.get("profiles", [])) != {"cs_6_0", "vs_6_0", "ps_6_0"}:
        raise VerificationError("common profile shader target set changed")
    if shader_targets.get("compilation") != "offline_only":
        raise VerificationError("common profile must require offline shader compilation")
    if shader_targets.get("runtime_shader_compilation") is not False:
        raise VerificationError("common profile must forbid runtime shader compilation")
    if shader_targets.get("sm_6_6_direct_heap_indexing") is not False:
        raise VerificationError("common profile must exclude SM 6.6 direct heap indexing")
    if shader_targets.get("required_dxil_container_validation") is not True:
        raise VerificationError("common profile must retain DXIL container validation")

    pinned = toolchain["pinned_dependencies"]
    if pinned.get("dxc_shader_model_target") != graphics.get("shader_model"):
        raise VerificationError("toolchain DXC shader model disagrees with NW-01 manifest")
    pinned_tools = req["pinned_build_tools"]
    if pinned_tools.get("dxc_version") != pinned.get("dxc_version"):
        raise VerificationError("profile DXC version disagrees with toolchain manifest")
    release_build = toolchain["build_profiles"]["release"]
    if "cs_6_0" not in release_build.get("dxc_flags", []):
        raise VerificationError("release toolchain does not compile the cs_6_0 target")
    for flag in pinned_tools.get("dxc_flags", []):
        if flag not in release_build["dxc_flags"]:
            raise VerificationError(
                f"profile DXC flag {flag!r} is missing from release toolchain"
            )
    toolchain_ids = {
        entry["toolchain_id"] for entry in toolchain.get("evaluated_toolchains", [])
    }
    if not {"clang-cl-lld", "msvc-ltcg"}.issubset(toolchain_ids):
        raise VerificationError("toolchain manifest is missing a required compiler profile")
    if pinned_tools.get("compiler_and_linker_profiles") != [
        "clang-cl/lld-link",
        "MSVC/LTCG",
    ]:
        raise VerificationError("profile compiler/linker profile list changed")
    if pinned_tools.get("reproducible_build_required") is not True:
        raise VerificationError("common profile must require reproducible builds")
    repro = toolchain.get("reproducibility_policy", {})
    for key in (
        "require_brepro_flag",
        "require_path_map",
        "require_deterministic_timestamp",
        "require_two_clean_dir_hash_match",
    ):
        if repro.get(key) is not True:
            raise VerificationError(f"toolchain reproducibility policy weakens {key}")

    # Runtime requirements.
    runtime = req["runtime_requirements"]
    target_platform = manifest["target_platform"]
    if runtime.get("os") != target_platform.get("os"):
        raise VerificationError("profile OS disagrees with NW-01 manifest")
    if runtime.get("architecture") != target_platform.get("architecture"):
        raise VerificationError("profile architecture disagrees with NW-01 manifest")
    if (
        runtime.get("minimum_tested_windows_build")
        != target_platform.get("minimum_tested_windows_build")
        or runtime.get("minimum_tested_windows_build")
        != toolchain.get("minimum_tested_windows_build")
    ):
        raise VerificationError("profile minimum tested Windows build disagrees with sources")
    if target_platform.get("minimum_tested_windows_build_status") != "verified":
        raise VerificationError("manifest minimum tested Windows build is not verified")
    if toolchain.get("minimum_tested_windows_build_status") != "verified":
        raise VerificationError("toolchain minimum tested Windows build is not verified")
    if (
        runtime.get("intended_lower_runtime_build")
        != toolchain.get("intended_minimum_windows_build")
    ):
        raise VerificationError("profile intended lower Windows build disagrees with sources")
    if "inbox" not in runtime.get("d3d12_runtime", ""):
        raise VerificationError("profile must require the inbox D3D12 runtime")
    if (
        runtime.get("mandatory_agility_sdk_redistribution") is not False
        or pinned.get("agility_sdk_redistributed_in_release") is not False
    ):
        raise VerificationError("profile must not require Agility SDK redistribution")
    if (
        runtime.get("runtime_dxc_or_slang_dependency") is not False
        or pinned.get("dxc_redistributed_in_release") is not False
    ):
        raise VerificationError("profile must not require runtime DXC/Slang")
    if runtime.get("proprietary_vendor_sdks") is not False:
        raise VerificationError("profile must not require proprietary vendor SDKs")
    if runtime.get("executable_packers") is not False:
        raise VerificationError("profile must not require executable packers")

    # Security mitigations, including /CETCOMPAT.
    mitigations = arch.get("security_mitigations_required", [])
    expected_mitigations = {
        "/GS",
        "/guard:cf",
        "/DYNAMICBASE",
        "/HIGHENTROPYVA",
        "/NXCOMPAT",
        "/CETCOMPAT",
    }
    if set(mitigations) != expected_mitigations:
        raise VerificationError("common profile security mitigation list changed")
    profile_flags = set(release_build.get("compiler_flags", [])) | set(
        release_build.get("linker_flags", [])
    )
    for mitigation in mitigations:
        if mitigation not in profile_flags:
            raise VerificationError(
                f"release toolchain is missing required mitigation {mitigation!r}"
            )
    if release_build.get("security_mitigations_retained") is not True:
        raise VerificationError("release toolchain no longer retains security mitigations")


def verify_recorded_nw01_contracts(audit: dict, profile: dict, manifest: dict) -> None:
    """Verify the audit's recorded source revision and contract snapshot."""

    recorded = audit.get("verified_nw01_contracts", {})
    if recorded.get("source_path") != "benchmarks/capability-manifest.v1.json":
        raise VerificationError("audit records an unexpected NW-01 source path")
    if recorded.get("source_sha256") != sha256_file_v1(CAPABILITY_MANIFEST_PATH):
        raise VerificationError(
            "audit's recorded NW-01 source digest is stale or does not match the manifest"
        )
    graphics = manifest["graphics_api"]
    required = manifest["required_features"]
    expected = {
        "feature_level": graphics["feature_level"],
        "shader_model": graphics["shader_model"],
        "resource_binding_tier": required["resource_binding_tier"],
        "resource_heap_tier": required["resource_heap_tier"],
        "descriptor_model": graphics["descriptor_model"],
        "barrier_model": graphics["barrier_model"],
        "queue_model": graphics["queue_model"],
        "baseline_limits": manifest["baseline_limits"],
    }
    for key, value in expected.items():
        if recorded.get(key) != value:
            raise VerificationError(
                f"audit recorded NW-01 contract {key!r} disagrees with the manifest"
            )
    common = audit.get("common_profile", {})
    if common.get("profile_id") != profile.get("profile_id"):
        raise VerificationError("audit common-profile ID disagrees with the profile")
    if common.get("requirements_digest") != profile.get("requirements_digest"):
        raise VerificationError("audit common-profile digest disagrees with the profile")


def verify_profile() -> dict:
    manifest = json.loads(CAPABILITY_MANIFEST_PATH.read_text(encoding="utf-8"))
    toolchain = json.loads(TOOLCHAIN_MANIFEST_PATH.read_text(encoding="utf-8"))
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
    verify_profile_against_sources(profile, manifest, toolchain)
    return profile


def verify_audit_section_texts(task: dict, original_body: str, updated_body: str) -> None:
    """Ensure the audit's exact original/replacement text matches the bodies."""

    original_sections = extract_sections(original_body)
    updated_sections = extract_sections(updated_body)
    for entry in task["sections"]:
        slug = entry["slug"]
        if slug not in original_sections or slug not in updated_sections:
            raise VerificationError(f"{task['task']}: section {slug!r} is missing from a body")
        original_text = entry.get("original_text")
        if original_text is None or original_text.strip() != original_sections[slug]:
            raise VerificationError(
                f"{task['task']}: audit original_text for {slug!r} does not match the original body"
            )
        if entry.get("status") == "changed":
            replacement = entry.get("replacement_text")
            if not replacement or replacement.strip() != updated_sections[slug]:
                raise VerificationError(
                    f"{task['task']}: audit replacement_text for {slug!r} "
                    "does not match the updated body"
                )
            reason = entry.get("reason", "")
            if "See exact replacement text" in reason or len(reason.strip()) < 80:
                raise VerificationError(
                    f"{task['task']}: changed section {slug!r} lacks a specific reason"
                )
        else:
            if entry.get("replacement_text") is not None:
                raise VerificationError(
                    f"{task['task']}: unchanged section {slug!r} carries replacement text"
                )
            if updated_sections[slug] != original_sections[slug]:
                raise VerificationError(
                    f"{task['task']}: unchanged section {slug!r} changed in the updated body"
                )


def verify_adr_audit(audit: dict) -> None:
    if not ADR0007_PATH.exists():
        raise VerificationError("ADR-0007 section audit is missing")
    adr = ADR0007_PATH.read_text(encoding="utf-8")
    for task in audit["tasks"]:
        if f"## {task['task']} — issue #{task['issue_number']}" not in adr:
            raise VerificationError(f"ADR-0007 is missing {task['task']}")
        for entry in task["sections"]:
            heading = f"### {entry['heading']} — {entry['status']}"
            if heading not in adr:
                raise VerificationError(
                    f"ADR-0007 is missing disposition {heading!r} for {task['task']}"
                )
            if entry["reason"] not in adr:
                raise VerificationError(
                    f"ADR-0007 is missing the recorded reason for "
                    f"{task['task']} {entry['slug']}"
                )
            if entry["status"] == "changed":
                if entry["replacement_text"] not in adr:
                    raise VerificationError(
                        f"ADR-0007 is missing exact replacement text for "
                        f"{task['task']} {entry['slug']}"
                    )


def verify_publication_claims(record: dict) -> None:
    """Keep "complete" claims coupled to verified live publication."""

    issues = record.get("issues", [])
    published = [
        issue
        for issue in issues
        if issue.get("completion_status") == "published_and_verified"
    ]
    if len(published) == len(issues) and issues:
        if record.get("github_publication_status") != "published":
            raise VerificationError(
                "publication record has all issues verified but is not marked published"
            )
        if record.get("task_status") != "complete":
            raise VerificationError(
                "publication record has all issues verified but task_status is not complete"
            )
        return
    if record.get("github_publication_status") == "published":
        raise VerificationError(
            "publication record claims published while an issue is not published_and_verified"
        )
    publication_status = record.get("github_publication_status", "")
    if not publication_status.startswith("blocked_") and publication_status != "partial":
        raise VerificationError(
            "unpublished publication record must carry a blocked_* or partial status"
        )
    if record.get("task_status") != "blocked":
        raise VerificationError(
            "NW-01A must be marked blocked while live publication is incomplete"
        )
    if not record.get("task_status_reason"):
        raise VerificationError("blocked NW-01A record is missing task_status_reason")
    readme = (BACKLOG / "README.md").read_text(encoding="utf-8").lower()
    if (
        "nw-01a is blocked" not in readme
        or "not published" not in readme
        or "contingent on live publication" not in readme
    ):
        raise VerificationError(
            "backlog README must state that NW-01A is blocked and not published"
        )
    handoff = (REPO_ROOT / "docs" / "nw01b-handoff.md").read_text(encoding="utf-8")
    if "NW-01A is blocked" not in handoff or "NW-01B must not start" not in handoff:
        raise VerificationError(
            "handoff must mark NW-01A blocked and not unblock NW-01B before publication"
        )


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
        "/CETCOMPAT",
        "/GS",
        "security mitigations",
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
    manifest = json.loads(CAPABILITY_MANIFEST_PATH.read_text(encoding="utf-8"))
    verify_recorded_nw01_contracts(audit, profile, manifest)
    verify_publication_claims(record)
    verify_policy_docs(profile)
    verify_adr_audit(audit)

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
        verify_audit_section_texts(task, original_body, updated_body)
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


def verify_published_body(
    live_body: str,
    expected_body: str,
    expected_offsets: list[dict],
    expected_refinement_marker: str,
) -> dict:
    """Verify a live GitHub body by hash *and* recomputed section offsets.

    Offsets are recomputed after newline normalization so a hash match cannot
    hide stale offsets when GitHub returns CRLF.  The exact published body may
    use either newline spelling; the recorded offsets remain body-relative over
    the normalized form.
    """

    live_normalized = normalize_body(live_body)
    live_hash = normalized_body_hash(live_body)
    if live_hash != normalized_body_hash(expected_body):
        raise VerificationError("live body hash does not match the intended body")
    live_marker, _ = extract_marker(live_normalized, REFINEMENT_MARKER_RE)
    if live_marker != expected_refinement_marker:
        raise VerificationError("live body refinement provenance marker changed")
    live_offsets = verify_section_manifest(live_normalized)
    if live_offsets != expected_offsets:
        raise VerificationError(
            "live body section offsets differ from the recorded machine metadata"
        )
    return {
        "published_body_hash": live_hash,
        "published_section_offsets": live_offsets,
        "published_newline_style": "crlf" if "\r\n" in live_body else "lf",
        "published_section_offsets_verified": True,
        "published_refinement_marker_preserved": True,
    }


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        # Windows can briefly deny replacement while a scanner or another
        # reader still has the old file open.  Retry a few times without
        # weakening the atomic replace contract.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        temporary.unlink(missing_ok=True)


def publish(
    apply: bool,
    gh_runner=None,
    *,
    audit_path: Path = AUDIT_PATH,
    record_path: Path = RECORD_PATH,
) -> int:
    runner = gh_runner or run_gh
    verify_bundle()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record_by_task = {item["task"]: item for item in record["issues"]}

    def persist_record() -> None:
        if apply:
            _atomic_write_json(record_path, record)

    def fail(status: str, message: str, code: int) -> int:
        if apply:
            record["task_status"] = "blocked"
            record["task_status_reason"] = (
                f"Live publication is blocked at {status}; NW-01A is not complete "
                "and NW-01B must not start."
            )
            record["github_publication_status"] = status
            record["github_publication_error"] = message
            persist_record()
        print(message, file=sys.stderr)
        return code

    for task in audit["tasks"]:
        number = task["issue_number"]
        title = task["title"]
        updated_path = REPO_ROOT / task["updated_body_path"]
        _, updated_body = read_issue_file(updated_path)
        _, original_body = read_issue_file(REPO_ROOT / task["original_body_path"])
        expected_updated_hash = normalized_body_hash(updated_body)
        expected_original_hash = normalized_body_hash(original_body)
        expected_refinement_marker, _ = extract_marker(
            normalize_body(updated_body), REFINEMENT_MARKER_RE
        )
        try:
            raw = runner(
                ["issue", "view", str(number), "--repo", GH_REPO, "--json", "title,body"]
            )
            current = json.loads(raw)
        except (VerificationError, json.JSONDecodeError) as exc:
            return fail(
                "blocked_live_read",
                f"{task['task']} #{number}: live read failed: {exc}",
                2,
            )
        current_title = current.get("title", "")
        current_body = current.get("body", "") or ""
        current_hash = normalized_body_hash(current_body)
        if current_title != title:
            return fail(
                "blocked_concurrent_edit",
                f"{task['task']} #{number}: concurrent title edit detected; aborting",
                3,
            )
        if current_hash == expected_updated_hash:
            try:
                verification = verify_published_body(
                    current_body,
                    updated_body,
                    task["section_offsets"],
                    expected_refinement_marker,
                )
            except VerificationError as exc:
                return fail(
                    "blocked_readback_verification",
                    f"{task['task']} #{number}: live body matches hash but fails "
                    f"offset/provenance readback: {exc}",
                    4,
                )
            print(f"{task['task']} #{number}: already updated and verified")
            rec = record_by_task[task["task"]]
            rec.update(verification)
            rec["completion_status"] = "published_and_verified"
            rec["github_publication_status"] = "published"
            persist_record()
            continue
        if current_hash != expected_original_hash:
            return fail(
                "blocked_concurrent_edit",
                f"{task['task']} #{number}: live body differs from recorded original; "
                "concurrent edit requires manual reconciliation",
                3,
            )
        if not apply:
            print(f"{task['task']} #{number}: would publish {updated_path}")
            continue
        # The repository body-only file is the exact issue body; GitHub must not
        # receive the human-readable "# title" document wrapper.  Close the
        # temporary file before invoking gh: Windows does not permit a second
        # process to open a NamedTemporaryFile while the Python handle is open.
        fd, temporary_name = tempfile.mkstemp(suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(updated_body)
            runner(
                [
                    "issue",
                    "edit",
                    str(number),
                    "--repo",
                    GH_REPO,
                    "--body-file",
                    temporary_name,
                ]
            )
        finally:
            for attempt in range(5):
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
        readback = json.loads(
            runner(
                ["issue", "view", str(number), "--repo", GH_REPO, "--json", "title,body"]
            )
        )
        if readback.get("title", "") != title:
            return fail(
                "blocked_readback_verification",
                f"{task['task']} #{number}: readback title changed; record left incomplete",
                4,
            )
        published_body = readback.get("body") or ""
        try:
            verification = verify_published_body(
                published_body,
                updated_body,
                task["section_offsets"],
                expected_refinement_marker,
            )
        except VerificationError as exc:
            return fail(
                "blocked_readback_verification",
                f"{task['task']} #{number}: readback verification failed: {exc}; "
                "record left incomplete",
                4,
            )
        rec = record_by_task[task["task"]]
        rec.update(verification)
        rec["completion_status"] = "published_and_verified"
        rec["github_publication_status"] = "published"
        persist_record()
        print(f"{task['task']} #{number}: published and verified")
    if apply:
        statuses = {
            rec.get("completion_status") for rec in record["issues"]
        }
        if statuses == {"published_and_verified"}:
            record["task_status"] = "complete"
            record.pop("task_status_reason", None)
            record["github_publication_status"] = "published"
            record.pop("github_publication_blocker", None)
            record.pop("github_publication_error", None)
            record.pop("publication_attempt", None)
        else:
            record["task_status"] = "blocked"
            record["github_publication_status"] = "partial"
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
