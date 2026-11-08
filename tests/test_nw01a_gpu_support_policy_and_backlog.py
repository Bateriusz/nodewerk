from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKLOG = REPO_ROOT / "docs" / "backlog"
TOOL_PATH = REPO_ROOT / "tools" / "nw01a_backlog.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("nw01a_backlog", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_nw01a_bundle_verifies() -> None:
    tool = _load_tool()
    tool.verify_bundle()


def test_common_profile_digest_is_canonical_and_complete() -> None:
    profile = json.loads(
        (REPO_ROOT / "benchmarks" / "capability-profile.common.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert profile["profile_id"] == "nw.common.d3d12.fl12_0.sm6_0.v1"
    canonical = json.dumps(
        profile["requirements"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    expected = "sha256:v1:" + hashlib.sha256(canonical).hexdigest()
    assert profile["requirements_digest"] == expected

    queried = profile["requirements"]["queried_capabilities"]
    assert queried["feature_level"] == "D3D_FEATURE_LEVEL_12_0"
    assert queried["shader_model"] == "D3D_SHADER_MODEL_6_0"
    assert queried["resource_binding_tier_min"] == 2
    assert queried["resource_heap_tier_min"] == 2
    assert {item["dxgi_format"] for item in queried["required_format_support"]} == {
        "DXGI_FORMAT_R8G8B8A8_UNORM",
        "DXGI_FORMAT_R32G32B32A32_FLOAT",
        "DXGI_FORMAT_R32_UINT",
        "DXGI_FORMAT_D32_FLOAT",
    }

    arch = profile["requirements"]["architectural_constraints"]
    assert arch["descriptor_model"] == "bounded_descriptor_tables"
    assert arch["barrier_model"] == "ordinary_resource_barriers"
    assert arch["queue_model"] == {
        "direct_queue_count": 1,
        "compute_queue_count": 0,
        "copy_queue_count": 0,
    }
    assert arch["baseline_limits"]["max_cbv_srv_uav_descriptors_per_frame"] == 4096


def test_adr_0006_separates_policy_concerns_and_preserves_performance_contract() -> None:
    adr = (
        REPO_ROOT
        / "docs"
        / "architecture"
        / "0006-capability-based-gpu-support-policy.md"
    ).read_text(encoding="utf-8")
    adr_lower = adr.lower()
    for phrase in (
        "Declared support scope",
        "Exact capability requirements",
        "Correctness coverage",
        "Optional artifacts",
        "Performance references",
        "nw.common.d3d12.fl12_0.sm6_0.v1",
        "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a",
        "desktop NVIDIA GeForce RTX 40-series / Ada Lovelace",
        "desktop NVIDIA GeForce RTX 50-series / Blackwell",
        "D3D_FEATURE_LEVEL_12_0",
        "D3D_SHADER_MODEL_6_0",
        "resource binding tier `>= 2`",
        "resource heap tier `>= 2`",
        "bounded descriptor tables",
        "exactly one direct queue",
        "nw07_perf_ref_v1",
        "five-second warm-up",
        "three uncapped 60-second runs",
        "strictly less than 16.67 ms",
        "RTX 4060 Ti",
        "cannot certify v1",
        "13.5",
        "12.00",
        "no new absolute Blackwell performance gate",
    ):
        assert phrase.lower() in adr_lower, f"ADR-0006 missing {phrase!r}"
    assert "never retroactively passes v1" in adr_lower


def test_adr_0005_maps_actual_issue_identities() -> None:
    mapping = (
        REPO_ROOT / "docs" / "architecture" / "0005-task-contracts-nw02-nw12.md"
    ).read_text(encoding="utf-8")
    expected = {
        3: "versioned operator schemas",
        4: "offline shader lowering",
        5: "package-driven D3D12 player",
        6: "specialized players and explicit resource schedules",
        7: "procedural mesh, texture, scene",
        8: "256 KiB architectural proof",
        9: "authoring editor with incremental GPU caching",
        10: "temporal state and replayable simulation",
        11: "compact exports",
        12: "advanced RTX features",
        13: "optional procedural audio",
    }
    for issue, phrase in expected.items():
        assert f"#{issue}" in mapping
        assert phrase.lower() in mapping.lower()
    assert "NW-01 -> NW-01A -> NW-01B -> NW-02..NW-12" in mapping
    assert "Audio Synthesis & Demo Timeline Synchronization" not in mapping
    assert "Final Hardware Qualification & Release Sign-Off" not in mapping


def test_updated_bodies_preserve_originals_markers_and_recomputed_offsets() -> None:
    audit = json.loads((BACKLOG / "nw01a-issue-audit.v1.json").read_text(encoding="utf-8"))
    assert len(audit["tasks"]) == 11
    for task in audit["tasks"]:
        original = (REPO_ROOT / task["original_body_path"]).read_text(encoding="utf-8")
        updated = (REPO_ROOT / task["updated_body_path"]).read_text(encoding="utf-8")
        # The body-only files are retained verbatim and carry the recorded hashes.
        assert task["original_body_hash"] == (
            "sha256:v1:" + hashlib.sha256(original.encode("utf-8")).hexdigest()
        )
        assert task["updated_body_hash"] == (
            "sha256:v1:" + hashlib.sha256(updated.encode("utf-8")).hexdigest()
        )
        assert "<!-- pairallel-refinement:v1 " in original
        assert "<!-- pairallel-refinement:v1 " in updated
        assert "<!-- pairallel-sections:v1 " in updated
        # The refinement marker is preserved byte-for-byte as historical provenance.
        old_marker = original[original.index("<!-- pairallel-refinement:v1 ") :]
        new_marker = updated[updated.index("<!-- pairallel-refinement:v1 ") :]
        assert old_marker.strip() == new_marker.strip()
        # Every required audit section has a disposition.
        slugs = {entry["slug"] for entry in task["sections"]}
        for required in (
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
        ):
            assert required in slugs


def test_downstream_bodies_match_recorded_source_revisions() -> None:
    # These hashes are the durable planning_publication_intents.body_hash values
    # for issues #3 through #13.  They bind the preserved originals to the
    # actual source revisions rather than to a reconstructed summary.
    expected = {
        3: "sha256:v1:5c3e56399192470729cdd9fede7041832698f1ed35c08e943f637e01bf96709e",
        4: "sha256:v1:be156db25c9b4bdd6d8abc2653a3979ad286d7969cc6f005dc537e5b56c0c6da",
        5: "sha256:v1:5c1ae6888a9f7f4ed2f66114c87aca306a71b853312daf29be1c1663e526fe31",
        6: "sha256:v1:ddac60a648ffb052dc2b425892d3eb1188a0cccbf4f9805b13fb64ba91f08c5f",
        7: "sha256:v1:1b5d30e943f5b3b15a3e9e29d6521119f21e479ff1d59aafa75b61533ccfa8fc",
        8: "sha256:v1:4774c208d7461228090e4a81639cd89667b4d719d4321986036518c1746e057f",
        9: "sha256:v1:4950160c4c169b5b1a5633855a056015371367d323e6f64ed484f57e3bb0524e",
        10: "sha256:v1:1cea6bfca133ce5ae45ff8d6b9dcf64bd86f3854aee80ecc64b5ea62f0af9a65",
        11: "sha256:v1:ec79b7b05aadfd867f8854284a25d3e4413add7bcc6391863307e4429cc9a577",
        12: "sha256:v1:789be1b8c685ef70647ee1acb7a590fde68e2a4e61b9b89fac2cf4327de73475",
        13: "sha256:v1:731b912c3196b8d3a082381a7e68c188fae4a6fd1a7e3a99a2eb0cccdab62348",
    }
    audit = json.loads((BACKLOG / "nw01a-issue-audit.v1.json").read_text(encoding="utf-8"))
    seen = {task["issue_number"]: task["original_body_hash"] for task in audit["tasks"]}
    assert seen == expected
    assert audit["source_identity"]["planning_base"] == (
        "743a4c1aec9e016af8c76cf08f9dd1696017270c"
    )
    assert audit["source_identity"]["frozen_source_requirements"] == (
        "sha256:v1:4b7a4e828d9ce4835d3f002bdd2960d7af00d99aefdc418317bc8592f753116d"
    )
    assert audit["downstream_source"]["planning_base"] == (
        "270d298f1b037548e0ab4808b2c123c12ddcc4e6"
    )
    assert audit["downstream_source"]["frozen_source_requirements"] == (
        "sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f"
    )


def test_nw07_reference_and_nw11_generation_contracts() -> None:
    nw07 = (
        BACKLOG / "nw02-nw12-issue-bodies" / "updated" / "issue-08.md"
    ).read_text(encoding="utf-8")
    assert "nw07_perf_ref_v1" in nw07
    assert "1920x1080" in nw07
    assert "strictly less than 16.67 ms" in nw07
    assert "five-second warm-up" in nw07
    assert "three uncapped 60-second runs" in nw07
    assert "raw per-run samples" in nw07
    assert "RTX 4060 Ti" in nw07
    assert "cannot certify" in nw07
    assert "Missing reference hardware leaves `nw07_perf_ref_v1` certification pending" in nw07
    assert "262,144" in nw07
    assert "512 MiB" in nw07
    assert "first-present within 2 seconds" in nw07
    assert "representative compliant desktop Ada" in nw07
    assert "representative compliant desktop Blackwell" in nw07
    assert "Maintain a separate matched-environment Blackwell" in nw07

    nw11 = (
        BACKLOG / "nw02-nw12-issue-bodies" / "updated" / "issue-12.md"
    ).read_text(encoding="utf-8")
    assert "representative desktop Ada and Blackwell" in nw11
    assert "matched baseline/variant pairs" in nw11
    assert "queried capabilities satisfy the profile" in nw11
    assert "separately exported and tested common-profile artifact" in nw11
    assert "Unsupported feature coverage" in nw11
    assert "pending" in nw11
    assert "10 percent lower GPU time" in nw11
    assert "Run paired baseline/variant captures on RTX 4060" not in nw11


def test_nw12_stays_hardware_neutral_and_receives_prerequisite_note() -> None:
    nw12 = (
        BACKLOG / "nw02-nw12-issue-bodies" / "updated" / "issue-13.md"
    ).read_text(encoding="utf-8")
    assert "hardware-neutral" in nw12
    assert "no GPU generation" in nw12
    assert "Global prerequisite: NW-01A and NW-01B" in nw12
    assert "Final Hardware Qualification" not in nw12


def test_performance_and_historical_evidence_are_not_rewritten() -> None:
    rtx4060 = json.loads(
        (REPO_ROOT / "benchmarks" / "hardware" / "rtx-4060-baseline.json").read_text(
            encoding="utf-8"
        )
    )
    assert rtx4060["hardware_tier"] == "rtx_4060_desktop"
    assert rtx4060["environment_metadata"]["gpu_model"] == "NVIDIA GeForce RTX 4060 Ti"
    assert rtx4060["environment_metadata"]["gpu_device_id"] == "0x2803"

    evidence = (REPO_ROOT / "docs" / "evidence-capture.md").read_text(encoding="utf-8")
    assert "rtx_4060_desktop" in evidence
    assert "not RTX 4060 evidence" in evidence
    assert "nw07_perf_ref_v1" in evidence
    assert "legacy_unclassified" in evidence
    assert "unavailable_telemetry" in evidence


def test_profile_verifier_checks_full_contract_and_recorded_digest() -> None:
    tool = _load_tool()
    profile = json.loads(
        (REPO_ROOT / "benchmarks" / "capability-profile.common.v1.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = json.loads(
        (REPO_ROOT / "benchmarks" / "capability-manifest.v1.json").read_text(
            encoding="utf-8"
        )
    )
    toolchain = json.loads(
        (REPO_ROOT / "benchmarks" / "toolchain-manifest.v1.json").read_text(
            encoding="utf-8"
        )
    )
    tool.verify_profile_against_sources(profile, manifest, toolchain)

    # A missing /CETCOMPAT mitigation must be detected.
    broken_profile = copy.deepcopy(profile)
    broken_profile["requirements"]["architectural_constraints"][
        "security_mitigations_required"
    ].remove("/CETCOMPAT")
    with pytest.raises(tool.VerificationError):
        tool.verify_profile_against_sources(broken_profile, manifest, toolchain)

    # Format names alone are insufficient; usages must match the manifest too.
    broken_profile = copy.deepcopy(profile)
    broken_profile["requirements"]["queried_capabilities"][
        "required_format_support"
    ][0]["usages"].remove("swap_chain")
    with pytest.raises(tool.VerificationError):
        tool.verify_profile_against_sources(broken_profile, manifest, toolchain)

    # The pinned DXC release profile must still compile the common shader target.
    broken_toolchain = copy.deepcopy(toolchain)
    broken_toolchain["build_profiles"]["release"]["dxc_flags"].remove("cs_6_0")
    with pytest.raises(tool.VerificationError):
        tool.verify_profile_against_sources(profile, manifest, broken_toolchain)

    audit = json.loads((BACKLOG / "nw01a-issue-audit.v1.json").read_text(encoding="utf-8"))
    broken_audit = copy.deepcopy(audit)
    broken_audit["verified_nw01_contracts"]["source_sha256"] = (
        "sha256:v1:0000000000000000000000000000000000000000000000000000000000000000"
    )
    with pytest.raises(tool.VerificationError):
        tool.verify_recorded_nw01_contracts(broken_audit, profile, manifest)


def test_audit_text_and_changed_reasons_match_the_bodies() -> None:
    tool = _load_tool()
    audit = json.loads((BACKLOG / "nw01a-issue-audit.v1.json").read_text(encoding="utf-8"))
    for task in audit["tasks"]:
        original = (REPO_ROOT / task["original_body_path"]).read_text(encoding="utf-8")
        updated = (REPO_ROOT / task["updated_body_path"]).read_text(encoding="utf-8")
        tool.verify_audit_section_texts(task, original, updated)
        for entry in task["sections"]:
            if entry["status"] == "changed":
                assert "See exact replacement text" not in entry["reason"]
                assert len(entry["reason"].strip()) >= 80

    # A stale replacement text must be detected rather than silently passing.
    task = copy.deepcopy(audit["tasks"][0])
    changed = next(
        entry for entry in task["sections"] if entry["status"] == "changed"
    )
    changed["replacement_text"] += " stale"
    with pytest.raises(tool.VerificationError):
        tool.verify_audit_section_texts(
            task,
            (REPO_ROOT / task["original_body_path"]).read_text(encoding="utf-8"),
            (REPO_ROOT / task["updated_body_path"]).read_text(encoding="utf-8"),
        )


def test_document_wrappers_omit_body_relative_section_metadata() -> None:
    tool = _load_tool()
    audit = json.loads((BACKLOG / "nw01a-issue-audit.v1.json").read_text(encoding="utf-8"))
    for task in audit["tasks"]:
        for body_path, document_path in (
            (task["original_body_path"], task["original_document_path"]),
            (task["updated_body_path"], task["updated_document_path"]),
        ):
            body = (REPO_ROOT / body_path).read_text(encoding="utf-8")
            document = (REPO_ROOT / document_path).read_text(encoding="utf-8")
            assert document.startswith("# " + task["title"] + "\n\n")
            assert "<!-- pairallel-sections:v1 " not in document
            assert "<!-- pairallel-refinement:v1 " not in document
            # The wrapper still carries the same human-readable section content.
            assert tool.extract_sections(document) == tool.extract_sections(body)


class _FakeGitHub:
    def __init__(self, bodies: dict[int, str], titles: dict[int, str], crlf: bool = False):
        self.bodies = dict(bodies)
        self.titles = dict(titles)
        self.crlf = crlf
        self.edits: list[int] = []

    def __call__(self, args: list[str]) -> str:
        if args[:2] == ["issue", "view"]:
            number = int(args[2])
            return json.dumps(
                {"title": self.titles[number], "body": self.bodies[number]}
            )
        if args[:2] == ["issue", "edit"]:
            number = int(args[2])
            body_file = Path(args[args.index("--body-file") + 1])
            body = body_file.read_text(encoding="utf-8")
            if self.crlf:
                body = body.replace("\n", "\r\n")
            self.bodies[number] = body
            self.edits.append(number)
            return ""
        raise AssertionError(f"unexpected gh invocation: {args!r}")


def _publication_fixture(tool):
    audit = json.loads((BACKLOG / "nw01a-issue-audit.v1.json").read_text(encoding="utf-8"))
    record = json.loads(
        (BACKLOG / "nw01a-publication-record.v1.json").read_text(encoding="utf-8")
    )
    bodies = {}
    titles = {}
    for task in audit["tasks"]:
        number = task["issue_number"]
        bodies[number] = (REPO_ROOT / task["original_body_path"]).read_text(
            encoding="utf-8"
        )
        titles[number] = task["title"]
    return audit, record, bodies, titles


def test_publish_dry_run_and_apply_verify_live_offsets(tmp_path: Path) -> None:
    tool = _load_tool()
    audit, record, bodies, titles = _publication_fixture(tool)
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    fake = _FakeGitHub(bodies, titles)
    assert tool.publish(False, fake, record_path=record_path) == 0
    assert fake.edits == []
    # Dry-run must not mutate the record.
    assert json.loads(record_path.read_text(encoding="utf-8")) == record

    fake = _FakeGitHub(bodies, titles)
    assert tool.publish(True, fake, record_path=record_path) == 0
    assert fake.edits == [task["issue_number"] for task in audit["tasks"]]
    published = json.loads(record_path.read_text(encoding="utf-8"))
    assert published["github_publication_status"] == "published"
    for issue in published["issues"]:
        assert issue["completion_status"] == "published_and_verified"
        assert issue["published_section_offsets_verified"] is True
        assert issue["published_refinement_marker_preserved"] is True


def test_publish_accepts_crlf_and_aborts_on_concurrent_edit(tmp_path: Path) -> None:
    tool = _load_tool()
    _audit, record, bodies, titles = _publication_fixture(tool)
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    # GitHub may return CRLF even though the body was published with LF.  The
    # normalized hash and the normalized body-relative offsets must both verify.
    fake = _FakeGitHub(bodies, titles, crlf=True)
    assert tool.publish(True, fake, record_path=record_path) == 0
    published = json.loads(record_path.read_text(encoding="utf-8"))
    assert all(
        issue["published_newline_style"] == "crlf"
        for issue in published["issues"]
    )

    # A concurrent edit must abort before any subsequent issue is touched.
    record_path.write_text(json.dumps(record), encoding="utf-8")
    fake = _FakeGitHub(bodies, titles)
    fake.bodies[3] = "concurrent edit"
    assert tool.publish(True, fake, record_path=record_path) == 3
    assert fake.edits == []
    blocked = json.loads(record_path.read_text(encoding="utf-8"))
    assert blocked["github_publication_status"] == "blocked_concurrent_edit"
    assert all(
        issue["completion_status"] == "prepared_verified_publication_pending"
        for issue in blocked["issues"]
    )


def test_live_offset_verification_is_independent_of_hash_match() -> None:
    tool = _load_tool()
    audit = json.loads((BACKLOG / "nw01a-issue-audit.v1.json").read_text(encoding="utf-8"))
    task = audit["tasks"][0]
    body = (REPO_ROOT / task["updated_body_path"]).read_text(encoding="utf-8")
    marker, _ = tool.extract_marker(tool.normalize_body(body), tool.REFINEMENT_MARKER_RE)
    tool.verify_published_body(body, body, task["section_offsets"], marker)
    wrong_offsets = copy.deepcopy(task["section_offsets"])
    wrong_offsets[0]["start"] += 1
    with pytest.raises(tool.VerificationError):
        tool.verify_published_body(body, body, wrong_offsets, marker)


def test_incomplete_publication_cannot_claim_completion() -> None:
    tool = _load_tool()
    record = json.loads(
        (BACKLOG / "nw01a-publication-record.v1.json").read_text(encoding="utf-8")
    )
    # The checked-in record is intentionally pending in the offline sandbox.
    tool.verify_publication_claims(record)
    broken = copy.deepcopy(record)
    broken["github_publication_status"] = "published"
    with pytest.raises(tool.VerificationError):
        tool.verify_publication_claims(broken)
