from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_contract as nw07_contract  # noqa: E402
import nw07_evidence as nw07_evidence  # noqa: E402
import nw10_perf_preservation as preservation  # noqa: E402
from schema_validator import validate_instance  # noqa: E402
import nw10_perf_candidate as candidate  # noqa: E402
import nw04_fixture  # noqa: E402


PERF_REF = REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"


def _identity(*, exe: str, manifest: str, artifact: str, package: str = "1" * 64) -> dict:
    return {
        "artifact_key": "sha256:v1:" + artifact * 64,
        "package_sha256": package,
        "manifest_sha256": manifest * 64,
        "executable_sha256": exe * 64,
        "executable_bytes": 260000 if exe == "3" else 264000,
        "link_manifest_sha256": manifest * 64,
    }


def _contract() -> dict:
    return json.loads(PERF_REF.read_text(encoding="utf-8"))


def _bound(identity: dict) -> dict:
    return nw07_evidence.fully_bound_reference(
        _contract(),
        artifact_identity=identity,
        bound_at_utc="2026-11-30T00:00:00+00:00",
    )


def _samples(value: float, count: int = 800) -> dict:
    values = [value + (index % 5) * 0.0001 for index in range(count)]
    return {
        "samples": values,
        "sample_count": count,
        "reported_p99_ms": nw07_evidence.nearest_rank_percentile(values),
        "gate": False,
    }


def _capture(bound: dict, identity: dict, capture_id: str, base_ms: float) -> dict:
    warmup_start = datetime(2026, 12, 1, tzinfo=timezone.utc)
    runs = []
    for index in range(1, 4):
        started = warmup_start + timedelta(seconds=5, minutes=index)
        timing = {
            "run_started_utc": started.isoformat(),
            "run_ended_utc": (started + timedelta(seconds=60)).isoformat(),
            "uncapped": True,
            "fresh_process": True,
            "application_pipeline_cache": False,
            "player_pid": 1000 + index,
            "artifact_sha256": identity["executable_sha256"],
            "collector_protocol": nw07_contract.PERF_COLLECTION_PROTOCOL,
            "collector_exit_code": 0,
            "player_exited_early": False,
        }
        runs.append(
            {
                "run_index": index,
                "duration_seconds": 60,
                "uncapped": True,
                "fresh_process": True,
                "application_pipeline_cache": False,
                "player_pid": 1000 + index,
                "collector_protocol": nw07_contract.PERF_COLLECTION_PROTOCOL,
                "artifact_sha256": identity["executable_sha256"],
                "collector_exit_code": 0,
                "player_exited_early": False,
                "timing": timing,
                "sample_density_hz": 13.3,
                "gpu_frame_time_ms": _samples(base_ms),
                "cpu_submit_ms": _samples(base_ms - 0.1),
                "displayed_frame_intervals_ms": _samples(13.3),
            }
        )
    capture = {
        "capture_id": capture_id,
        "task": nw07_contract.TASK,
        "implementation_base": nw07_contract.IMPLEMENTATION_BASE,
        "coverage_target": nw07_contract.PERF_CONTRACT_ID,
        "evidence_role": nw07_contract.PERFORMANCE_CERTIFICATION_ROLE,
        "present_sync_interval": 0,
        "presentation_probe": {
            "exit_code": 0,
            "present_sync_interval_line": "NW05_PRESENT_SYNC_INTERVAL=0",
            "stdout": "NW05_PRESENT_SYNC_INTERVAL=0\n",
        },
        "contract_id": nw07_contract.PERF_CONTRACT_ID,
        "execution_environment": {"kind": "physical", "documentation": ""},
        "reference": copy.deepcopy(bound["reference"]),
        "reference_binding_digest": bound["binding_digest"],
        "reference_binding_id": bound["binding_id"],
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "executable_bytes": identity["executable_bytes"],
        "profile_id": nw07_contract.PROFILE_ID,
        "requirements_digest": nw07_contract.REQUIREMENTS_DIGEST,
        "tools": copy.deepcopy(bound["tools"]),
        "protocol": {
            "protocol_id": nw07_contract.PERF_RUN_PROTOCOL,
            "warmup_seconds": nw07_contract.WARMUP_SECONDS,
            "runs": nw07_contract.PERF_RUN_COUNT,
            "run_seconds": nw07_contract.PERF_RUN_SECONDS,
            "uncapped": True,
            "fresh_process_per_run": True,
            "application_pipeline_cache": False,
        },
        "warmup": {
            "warmup_started_utc": warmup_start.isoformat(),
            "warmup_ended_utc": (warmup_start + timedelta(seconds=5)).isoformat(),
            "duration_seconds": 5,
            "player_pid": 999,
            "artifact_sha256": identity["executable_sha256"],
            "completed": True,
        },
        "runs": runs,
    }
    capture["validation"] = nw07_evidence.validate_perf_capture(
        capture, bound, artifact_identity=identity
    )
    capture["status"] = capture["validation"]["status"]
    return capture


def _fixture() -> tuple[dict, dict, dict, dict]:
    before_identity = _identity(exe="3", manifest="2", artifact="a")
    after_identity = _identity(exe="4", manifest="5", artifact="b")
    bound = _bound(before_identity)
    before = _capture(bound, before_identity, "before", 10.0)
    after = _capture(bound, after_identity, "after", 10.5)
    frozen = preservation.nw07_export.load_canonical_config()
    graph = preservation.nw07_export.verify_canonical_graph(
        frozen, repo_root=REPO_ROOT
    )
    pinned = preservation.nw07_export.verify_pinned_inputs(
        frozen, repo_root=REPO_ROOT
    )
    candidate = {
        "comparison_role": "after_common_artifact",
        "performance_contract": nw07_contract.PERF_CONTRACT_ID,
        "performance_protocol": nw07_contract.PERF_RUN_PROTOCOL,
        "profile_id": nw07_contract.PROFILE_ID,
        "requirements_digest": nw07_contract.REQUIREMENTS_DIGEST,
        "common_graph": {
            "path": str(frozen["graph"]),
            "sha256": graph["graph_sha256"],
            "ir_sha256": graph["ir_sha256"],
        },
        "build_inputs": {
            "nw07_frozen_digest": pinned["frozen_digest"],
            "observed_digest": pinned["digest"],
            "matches_frozen_digest": pinned["matches_frozen_digest"],
            "nw10_digest": preservation.nw10_contract.build_inputs_digest(
                preservation.nw10_contract.compute_build_inputs()
            ),
        },
        "artifact": {
            "artifact_key": after_identity["artifact_key"],
            "package_sha256": after_identity["package_sha256"],
            "manifest_sha256": after_identity["manifest_sha256"],
            "executable_sha256": after_identity["executable_sha256"],
            "complete_distribution_bytes": after_identity["executable_bytes"],
        },
        "dependency_closure": {
            "imports": ["d3d12", "kernel32"],
            "matches_manifest": True,
        },
        "runtime_probe": {
            "status": "passed",
            "exit_code": 0,
            "uncapped_present_verified": True,
        },
        "release_grade": True,
        "test_payload": False,
    }
    return bound, before, after, candidate


def test_matched_after_artifact_preserves_frozen_performance_contract() -> None:
    bound, before, after, candidate = _fixture()
    assert before["validation"]["status"] == "verified"
    assert after["validation"]["status"] == "unverified"
    assert after["validation"]["reasons"] == [
        "capture does not reference the exact final artifact"
    ]
    result = preservation.validate_preservation(before, after, bound, candidate)
    assert result["status"] == "verified_preserved"
    assert result["matched_environment"] is True
    assert result["comparison"]["same_canonical_package_sha256"] is True
    assert result["comparison"]["faster_substitute_used"] is False
    assert result["privacy"]["raw_samples_included"] is False
    assert '"samples"' not in json.dumps(result)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("comparison_role",), "wrong"),
        (("performance_protocol",), "wrong"),
        (("profile_id",), "wrong"),
        (("requirements_digest",), "wrong"),
        (("common_graph", "sha256"), "9" * 64),
        (("build_inputs", "observed_digest"), "wrong"),
        (("build_inputs", "matches_frozen_digest"), False),
        (("dependency_closure", "matches_manifest"), False),
        (("runtime_probe", "status"), "failed"),
        (("release_grade",), False),
        (("test_payload",), True),
    ],
)
def test_candidate_provenance_mismatch_cannot_claim_preservation(
    path, value
) -> None:
    bound, before, after, candidate = _fixture()
    target = candidate
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = preservation.validate_preservation(before, after, bound, candidate)
    assert result["status"] == "failed"


def test_changed_common_package_cannot_claim_preservation() -> None:
    bound, before, after, candidate = _fixture()
    after["package_sha256"] = "9" * 64
    candidate["artifact"]["package_sha256"] = "9" * 64
    result = preservation.validate_preservation(before, after, bound, candidate)
    assert result["status"] == "failed"
    assert any("canonical package hashes differ" in item for item in result["reasons"])


def test_public_evidence_rejects_local_user_profile_paths() -> None:
    with pytest.raises(ValueError, match="user-profile path"):
        preservation._scan_public_payload(
            {"bad": r"C:\Users\alice\Desktop\capture.csv"}, []
        )


def test_public_evidence_rejects_explicit_privacy_tokens() -> None:
    with pytest.raises(ValueError, match="forbidden privacy token"):
        preservation._scan_public_payload({"bad": "private-login"}, ["PRIVATE-LOGIN"])


def test_after_candidate_retains_over_proof_ceiling_without_relaxing_it(
    tmp_path: Path,
) -> None:
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    template = toolchain / "template.exe"
    template.write_bytes(b"MZ" + b"\\0" * 262144)
    dxc = toolchain / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\\n"
        "from pathlib import Path\\n"
        "if '--version' in sys.argv:\\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\\n"
        "    sys.exit(0)\\n"
        "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\\n"
        "fo.parent.mkdir(parents=True, exist_ok=True)\\n"
        f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\\n",
        encoding="utf-8",
    )
    report = candidate.build_candidate(
        tmp_path / "after",
        player_template=template,
        dxc_executable=str(dxc),
        report_path=tmp_path / "after-report.json",
        allow_test_payloads=True,
    )
    assert report["comparison_role"] == "after_common_artifact"
    assert report["size_classification"]["within_nw07_proof_ceiling"] is False
    assert report["size_classification"]["nw07_proof_ceiling_is_not_relaxed"] is True
    assert report["size_classification"]["retained_for_performance_comparison_only"] is True
    assert report["size_classification"]["within_standard_tier"] is True
    assert (tmp_path / "after" / "nw_common_d3d12_fl12_0_sm6_0_v1.exe").is_file()


def test_publish_rejects_private_path_and_promotes_only_performance_status(
    tmp_path: Path,
) -> None:
    bound, before, after, candidate = _fixture()
    public = preservation.validate_preservation(before, after, bound, candidate)
    source = tmp_path / "public.json"
    source.write_text(json.dumps(public), encoding="utf-8")
    destination = tmp_path / "repo" / "benchmarks" / "nw10" / "performance.json"
    status_path = tmp_path / "repo" / "benchmarks" / "nw10" / "evidence-status.v1.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {
                "format": "nw10-evidence-status",
                "version": 1,
                "task": "NW-10",
                "evidence_status": "pending_physical_windows_capture",
                "contract_evidence": "verified_by_pytest",
                "physical_evidence": "pending",
                "reference_capture": "pending_reference_capture",
                "ada_correctness": "pending_unverified",
                "blackwell_correctness": "pending_unverified",
                "clean_system": "pending_unverified",
                "startup": "pending_unverified",
                "memory": "pending_unverified",
                "frame_time": "pending_unverified",
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    old_root = preservation.REPO_ROOT
    preservation.REPO_ROOT = tmp_path / "repo"
    try:
        status = preservation.publish_public_evidence(
            source,
            destination,
            status_path,
            implementation_head="a" * 40,
        )
    finally:
        preservation.REPO_ROOT = old_root
    assert status["evidence_status"] == "performance_verified_correctness_pending"
    assert status["physical_evidence"] == "performance_verified"
    assert status["reference_capture"] == "verified_reference_capture"
    assert status["frame_time"] == "verified_preserved"
    assert status["ada_correctness"] == "pending_unverified"
    assert status["blackwell_correctness"] == "pending_unverified"
    assert status["startup"] == "pending_unverified"
    assert status["memory"] == "pending_unverified"
    assert destination.is_file()

    poisoned = copy.deepcopy(public)
    poisoned["reference_environment"]["host"]["motherboard"] = r"C:\Users\alice\secret"
    source.write_text(json.dumps(poisoned), encoding="utf-8")
    with pytest.raises(ValueError, match="user-profile path"):
        preservation.publish_public_evidence(source, destination, status_path)


def test_preservation_schema_accepts_float_relative_alert_threshold() -> None:
    schema = json.loads(
        (REPO_ROOT / "schemas" / "nw10-performance-preservation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    bound, before, after, candidate = _fixture()
    public = preservation.validate_preservation(before, after, bound, candidate)
    assert public["comparison"]["relative_alert_threshold_percent"] == 10.0
    validate_instance(public, schema, schema)


def test_publish_preserves_existing_compact_progress(tmp_path: Path) -> None:
    import nw10_evidence

    bound, before, after, candidate = _fixture()
    public = preservation.validate_preservation(before, after, bound, candidate)
    source = tmp_path / "public.json"
    source.write_text(json.dumps(public), encoding="utf-8")
    repo = tmp_path / "repo"
    destination = repo / "benchmarks" / "nw10" / "performance.json"
    status_path = repo / "benchmarks" / "nw10" / "evidence-status.v1.json"
    status_path.parent.mkdir(parents=True)
    compact_relative = "benchmarks/nw10/windows-compact-feasibility.v1.json"
    compact_path = repo / compact_relative
    compact_path.write_text(
        '{"status":"verified_windows_runtime"}\n', encoding="utf-8"
    )
    compact_sha = preservation._canonical_text_sha256(compact_path)
    status_path.write_text(
        json.dumps(
            {
                "format": "nw10-evidence-status",
                "version": 1,
                "task": "NW-10",
                "evidence_status": "performance_and_compact_verified_correctness_pending",
                "contract_evidence": "verified_by_pytest",
                "physical_evidence": "performance_verified",
                "reference_capture": "verified_reference_capture",
                "ada_correctness": "pending_unverified",
                "blackwell_correctness": "pending_unverified",
                "clean_system": "pending_unverified",
                "startup": "pending_unverified",
                "memory": "pending_unverified",
                "frame_time": "verified_preserved",
                "compact_feasibility": {
                    "status": "verified_windows_runtime",
                    "path": compact_relative,
                    "sha256": compact_sha,
                },
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    old_root = preservation.REPO_ROOT
    preservation.REPO_ROOT = repo
    try:
        status = preservation.publish_public_evidence(
            source,
            destination,
            status_path,
            implementation_head="a" * 40,
        )
    finally:
        preservation.REPO_ROOT = old_root
    assert (
        status["evidence_status"]
        == "performance_and_compact_verified_correctness_pending"
    )
    compact_rows = [
        row
        for row in status["files"]
        if row.get("path") == compact_relative
    ]
    assert compact_rows == [
        {"path": compact_relative, "sha256": compact_sha}
    ]
    assert (
        nw10_evidence._physical_evidence_is_current(
            status, "a" * 40, status_path.parent
        )
        is True
    )


def test_publish_recomputes_advanced_state_from_component_records(tmp_path: Path) -> None:
    import nw10_evidence

    bound, before, after, candidate = _fixture()
    public = preservation.validate_preservation(before, after, bound, candidate)
    source = tmp_path / "public.json"
    source.write_text(json.dumps(public), encoding="utf-8")
    repo = tmp_path / "repo"
    evidence_root = repo / "benchmarks" / "nw10"
    destination = evidence_root / "performance-preservation-rtx4060ti.v1.json"
    status_path = evidence_root / "evidence-status.v1.json"
    evidence_root.mkdir(parents=True)

    component_specs = {
        "correctness_evidence": (
            "common-artifact-correctness.v1.json",
            "verified",
        ),
        "system_validation": ("system-validation.v1.json", "verified"),
        "compact_feasibility": (
            "windows-compact-feasibility.v1.json",
            "verified_windows_runtime",
        ),
        "pe_ledger": ("windows-pe-ledger.v1.json", "verified"),
    }
    entries = {}
    file_rows = []
    for field, (name, component_status) in component_specs.items():
        path = evidence_root / name
        path.write_text(
            json.dumps({"status": component_status}) + "\n",
            encoding="utf-8",
        )
        digest = preservation._canonical_text_sha256(path)
        relative = f"benchmarks/nw10/{name}"
        entries[field] = {
            "status": component_status,
            "path": relative,
            "sha256": digest,
        }
        file_rows.append({"path": relative, "sha256": digest})

    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": "a" * 40,
        "evidence_status": "correctness_and_system_reused_performance_pending",
        "contract_evidence": "verified_by_pytest",
        "physical_evidence": "correctness_and_system_reused",
        "reference_capture": "pending_reference_capture",
        "ada_correctness": "verified",
        "blackwell_correctness": "verified",
        "clean_system": "verified",
        "startup": "verified",
        "memory": "verified",
        "frame_time": "pending_unverified",
        "files": file_rows,
        **entries,
    }
    status_path.write_text(json.dumps(status), encoding="utf-8")

    old_root = preservation.REPO_ROOT
    preservation.REPO_ROOT = repo
    try:
        published = preservation.publish_public_evidence(
            source,
            destination,
            status_path,
            implementation_head="a" * 40,
        )
    finally:
        preservation.REPO_ROOT = old_root

    assert published["evidence_status"] == "verified_complete"
    assert (
        published["physical_evidence"]
        == "performance_correctness_and_system_verified"
    )
    assert published["reference_capture"] == "verified_reference_capture"
    assert published["frame_time"] == "verified_preserved"
    assert (
        nw10_evidence._physical_evidence_is_current(
            published, "a" * 40, evidence_root
        )
        is True
    )


def _component_entries(evidence_root: Path, specs: dict[str, tuple[str, str]]) -> dict:
    entries = {}
    for field, (name, component_status) in specs.items():
        path = evidence_root / name
        path.write_text(
            json.dumps({"status": component_status}) + "\n",
            encoding="utf-8",
        )
        entries[field] = {
            "status": component_status,
            "path": f"benchmarks/nw10/{name}",
            "sha256": preservation._canonical_text_sha256(path),
        }
    return entries


def test_publish_carries_all_preserved_component_file_rows(
    tmp_path: Path,
) -> None:
    import nw10_evidence

    bound, before, after, candidate = _fixture()
    public = preservation.validate_preservation(before, after, bound, candidate)
    source = tmp_path / "public.json"
    source.write_text(json.dumps(public), encoding="utf-8")
    repo = tmp_path / "repo"
    evidence_root = repo / "benchmarks" / "nw10"
    evidence_root.mkdir(parents=True)
    destination = evidence_root / "performance-preservation-rtx4060ti.v1.json"
    status_path = evidence_root / "evidence-status.v1.json"

    # Component records are already verified, but the status ledger has no
    # matching file rows.  Performance-only republication must reconstruct all
    # of them, not just the compact-feasibility row.
    entries = _component_entries(
        evidence_root,
        {
            "correctness_evidence": (
                "common-artifact-correctness.v1.json",
                "verified",
            ),
            "system_validation": ("system-validation.v1.json", "verified"),
            "compact_feasibility": (
                "windows-compact-feasibility.v1.json",
                "verified_windows_runtime",
            ),
            "pe_ledger": ("windows-pe-ledger.v1.json", "verified"),
        },
    )
    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": "a" * 40,
        "evidence_status": (
            "performance_correctness_and_system_verified_packaging_pending"
        ),
        "contract_evidence": "verified_by_pytest",
        "physical_evidence": "performance_correctness_and_system_verified",
        "reference_capture": "verified_reference_capture",
        "ada_correctness": "verified",
        "blackwell_correctness": "verified",
        "clean_system": "verified",
        "startup": "verified",
        "memory": "verified",
        "frame_time": "verified_preserved",
        "files": [],
        **entries,
    }
    status_path.write_text(json.dumps(status), encoding="utf-8")

    old_root = preservation.REPO_ROOT
    preservation.REPO_ROOT = repo
    try:
        published = preservation.publish_public_evidence(
            source,
            destination,
            status_path,
            implementation_head="a" * 40,
        )
    finally:
        preservation.REPO_ROOT = old_root

    ledger_paths = {row["path"] for row in published["files"]}
    for field in (
        "correctness_evidence",
        "system_validation",
        "compact_feasibility",
        "pe_ledger",
    ):
        assert published[field]["path"] in ledger_paths
    assert published["evidence_status"] == "verified_complete"
    assert (
        nw10_evidence._physical_evidence_is_current(
            published, "a" * 40, evidence_root
        )
        is True
    )


def test_publish_refuses_unknown_later_state_without_mutating_outputs(
    tmp_path: Path,
) -> None:
    bound, before, after, candidate = _fixture()
    public = preservation.validate_preservation(before, after, bound, candidate)
    source = tmp_path / "public.json"
    source.write_text(json.dumps(public), encoding="utf-8")
    repo = tmp_path / "repo"
    evidence_root = repo / "benchmarks" / "nw10"
    evidence_root.mkdir(parents=True)
    destination = evidence_root / "performance-preservation.v1.json"
    destination.write_text('{"sentinel":"unchanged"}\n', encoding="utf-8")
    status_path = evidence_root / "evidence-status.v1.json"

    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": "a" * 40,
        "evidence_status": "future_more_advanced_state",
        "contract_evidence": "verified_by_pytest",
        "physical_evidence": "correctness_and_system_reused",
        "reference_capture": "pending_reference_capture",
        "ada_correctness": "verified",
        "blackwell_correctness": "verified",
        "clean_system": "verified",
        "startup": "verified",
        "memory": "verified",
        "frame_time": "pending_unverified",
        "files": [],
    }
    status_path.write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    destination_before = destination.read_bytes()
    status_before = status_path.read_bytes()

    old_root = preservation.REPO_ROOT
    preservation.REPO_ROOT = repo
    try:
        with pytest.raises(
            preservation.evidence_publisher.UnsupportedEvidenceStatusError,
            match="refusing to reinterpret or downgrade",
        ):
            preservation.publish_public_evidence(
                source,
                destination,
                status_path,
                implementation_head="a" * 40,
            )
    finally:
        preservation.REPO_ROOT = old_root

    assert destination.read_bytes() == destination_before
    assert status_path.read_bytes() == status_before

