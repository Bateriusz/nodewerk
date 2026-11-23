from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw05_export  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw10_compact as compact  # noqa: E402
import nw10_contract as contract  # noqa: E402
import nw10_proof as proof  # noqa: E402
import nw10_pe_ledger as pe_ledger  # noqa: E402
import nw10_perf_preservation as perf_preservation  # noqa: E402
from schema_validator import (  # noqa: E402
    SchemaValidationError,
    validate_file_against_schema,
    validate_instance,
)


def _canonical_text_sha256(path: Path) -> str:
    return hashlib.sha256(
        path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def test_proof_export_uses_the_canonical_nw07_graph_and_export_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    report = proof.export_proof(
        tmp_path / "proof",
        contracts_only=True,
        report_path=tmp_path / "report.json",
        reproducibility=True,
    )
    assert report["canonical_graph"] == "fixtures/nw07/production_loop.nwgraph"
    assert report["canonical_config"] == "benchmarks/nw07/canonical-production.v1.json"
    assert report["nw07_perf_contract"] == "nw07_perf_ref_v1"
    assert report["release_grade"] is False
    assert report["test_payload"] is True
    assert report["reproducibility"]["matching"] is True
    canonical = report["canonical_artifact"]
    assert canonical["package_sha256"]
    assert canonical["executable_sha256"]
    package = (tmp_path / "proof" / "nw_common_d3d12_fl12_0_sm6_0_v1.nwp").read_bytes()
    assert hashlib.sha256(package).hexdigest() == canonical["package_sha256"]
    nwp.decode_package(package, allow_dev=False)


def test_compact_selection_never_increases_total_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    report = proof.export_proof(tmp_path / "proof", contracts_only=True)
    canonical_ledger = report["ledgers"]["canonical"]
    compact_ledger = report["ledgers"]["compact"]
    assert (
        compact_ledger["complete_runnable_distribution_bytes"]
        <= canonical_ledger["complete_runnable_distribution_bytes"]
    )
    if compact_ledger["compression_rejected_for_increasing_distribution"]:
        assert compact_ledger["complete_runnable_distribution_bytes"] == (
            canonical_ledger["complete_runnable_distribution_bytes"]
        )
        assert report["compact_candidate"]["selected_mode"] == "canonical"
    else:
        assert report["compact_candidate"]["selected_mode"] == "compact"
        assert compact_ledger["components"]["encoded_package_bytes"] < (
            compact_ledger["components"]["canonical_package_bytes"]
        )


def test_compact_embedded_executable_round_trips_to_the_canonical_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    report = proof.export_proof(tmp_path / "proof", contracts_only=True)
    stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    canonical = (tmp_path / "proof" / f"{stem}.exe").read_bytes()
    candidate = (tmp_path / "proof" / "compact" / f"{stem}.compact.exe").read_bytes()
    encoded_package = (
        tmp_path / "proof" / "compact" / f"{stem}.compact.nwc"
    ).read_bytes()
    canonical_package = (tmp_path / "proof" / f"{stem}.nwp").read_bytes()
    assert compact.is_compact_container(encoded_package)
    assert compact.decode_compact(encoded_package) == canonical_package
    # The candidate still embeds the compact bytes and the same canonical
    # manifest binding; the player expands at load time.
    embedded, embedded_manifest = nw05_export.extract_embedded_package(candidate)
    assert embedded == encoded_package
    assert embedded_manifest
    assert report["compact_candidate"]["encoded_package_sha256"] == hashlib.sha256(
        encoded_package
    ).hexdigest()
    assert canonical


def test_ledgers_attribute_every_required_component_and_preserve_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    report = proof.export_proof(tmp_path / "proof", contracts_only=True)
    ledger = report["ledgers"]["compact"]
    # Contract-only evidence cannot measure the exact Windows decoder code.
    # The ledger must say so instead of claiming complete accounting.
    assert ledger["all_required_bytes_accounted"] is False
    assert "decoder_code_bytes" in ledger["unaccounted_components"]
    assert report["ledgers"]["canonical"]["all_required_bytes_accounted"] is True
    assert ledger["components"]["decoder_source_bytes"] and (
        ledger["components"]["decoder_source_bytes"] > 0
    )
    assert ledger["components"]["embedded_manifest_binding_bytes"] == 32
    assert ledger["decoded_package_bound_bytes"] == report["canonical_artifact"]["package_bytes"]
    assert ledger["codec_id"] in contract.PLAYER_SUPPORTED_CODEC_IDS
    assert ledger["layout_id"] in contract.KNOWN_LAYOUT_IDS
    assert ledger["budget_tiers"]["stretch_bytes"] == 131_072
    assert ledger["budget_tiers"]["proof_bytes"] == 262_144
    assert ledger["budget_tiers"]["standard_bytes"] == 1_048_576
    assert ledger["within_proof"] is True
    assert ledger["within_standard"] is True
    assert ledger["packaging_tool"]["identity"] == "tools/nw10_compact.py"
    assert len(ledger["packaging_tool"]["version"]) == 64
    assert "cabinet.dll" in {
        item["name"].lower() for item in ledger["dependencies"]
    }
    assert ledger["components"]["compact_player_image_bytes"] is not None


def test_byte_ledger_normalizes_cabinet_dependency_aliases() -> None:
    encoding = compact.CompactEncoding(
        bytes=b"x",
        codec_id=contract.CODEC_MSZIP_ID,
        layout_id=contract.LAYOUT_AOS_ID,
        decoded_size=1,
        payload_size=1,
        container_size=1,
        record_count=1,
        reference_count=0,
        decoded_sha256="0" * 64,
    )
    ledger = compact.build_byte_ledger(
        artifact_kind="restricted_intro",
        canonical_executable_bytes=200,
        canonical_package_bytes=1,
        manifest_bytes=1,
        compact=encoding,
        compact_executable_bytes=180,
        decoder_code_bytes=1,
        shader_bytes=0,
        dependencies=[
            {
                "name": "cabinet",
                "kind": "windows_inbox",
                "distributed_bytes": 0,
            }
        ],
        evidence_status="test",
    )
    normalized = [
        item["name"].lower().removesuffix(".dll")
        for item in ledger["dependencies"]
    ]
    assert normalized.count("cabinet") == 1


def test_measurement_record_includes_every_required_alternative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    report = proof.export_proof(tmp_path / "proof", contracts_only=True)
    measurements = report["measurements"]
    assert "parameter_packing" in measurements
    assert "delta_references" in measurements
    assert "layouts" in measurements
    assert "shaders" in measurements
    assert measurements["shaders"]["requires_new_shader_semantics"] is False
    assert "compression_candidates" in measurements
    codecs = {item["codec"] for item in measurements["compression_candidates"]}
    assert contract.CODEC_MSZIP_ID in codecs
    assert "candidate.lzma" in codecs
    assert measurements["decoder_code_status"] in {
        "pending_physical_windows",
        "host_proxy_measurement",
    }


def test_performance_evidence_remains_pending_without_reference_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    report = proof.export_proof(tmp_path / "proof", contracts_only=True)
    performance = report["performance_evidence"]
    assert performance["contract_id"] == "nw07_perf_ref_v1"
    assert performance["protocol"] == "nw07-perf-run.v1"
    assert performance["runs"] == 3
    assert performance["warmup_seconds"] == 5
    assert performance["run_seconds"] == 60
    assert performance["fresh_process_per_run"] is True
    assert performance["application_pipeline_cache"] is False
    assert performance["classification"] == "pending_physical"
    assert performance["reference_capture"] == "pending_reference_capture"
    assert report["decision"]["physical_target_met"] is False
    assert report["decision"]["universal_64k_claim"] is False
    assert report["decision"]["classification"] == "continue"


def test_static_nw10_evidence_files_validate_and_archive_reproducible_identities() -> None:
    evidence_path = REPO_ROOT / "benchmarks" / "nw10" / "compact-proof.v1.json"
    intro_path = REPO_ROOT / "benchmarks" / "nw10" / "intro-profile.v1.json"
    status_path = REPO_ROOT / "benchmarks" / "nw10" / "evidence-status.v1.json"
    correctness_path = (
        REPO_ROOT / "benchmarks" / "nw10" / "common-artifact-correctness.v1.json"
    )
    system_path = REPO_ROOT / "benchmarks" / "nw10" / "system-validation.v1.json"
    pe_ledger_path = REPO_ROOT / "benchmarks" / "nw10" / "windows-pe-ledger.v1.json"
    reuse_path = REPO_ROOT / "benchmarks" / "nw10" / "reused-physical-evidence.v1.json"
    validate_file_against_schema(
        evidence_path, REPO_ROOT / "schemas" / "nw10-compact-evidence.schema.json"
    )
    validate_file_against_schema(
        intro_path, REPO_ROOT / "schemas" / "nw10-intro-profile.schema.json"
    )
    validate_file_against_schema(
        status_path, REPO_ROOT / "schemas" / "nw10-evidence-status.schema.json"
    )
    validate_file_against_schema(
        correctness_path,
        REPO_ROOT / "schemas" / "nw10-common-artifact-correctness.schema.json",
    )
    validate_file_against_schema(
        system_path, REPO_ROOT / "schemas" / "nw10-system-validation.schema.json"
    )
    validate_file_against_schema(
        pe_ledger_path, REPO_ROOT / "schemas" / "nw10-windows-pe-ledger.schema.json"
    )
    validate_file_against_schema(
        reuse_path, REPO_ROOT / "schemas" / "nw10-reused-physical-evidence.schema.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["evidence_status"] == "informational_contract_only"
    assert evidence["physical_certification"] == "pending_physical_windows_capture"
    assert "reused_physical_evidence" not in evidence
    assert evidence["reference_capture"] == "pending_reference_capture"
    assert evidence["proof"]["reproducibility"]["matching"] is True
    assert evidence["tiers"]["compact_target_bytes"] == 65_536
    assert evidence["tiers"]["stretch_bytes"] == 131_072
    assert evidence["tiers"]["proof_bytes"] == 262_144
    assert evidence["tiers"]["standard_bytes"] == 1_048_576
    assert evidence["decision"]["custom_compressor_started"] is False
    assert evidence["decision"]["universal_64k_claim"] is False
    assert (
        evidence["codec_encoding_identity"]["cross_platform_byte_parity_claimed"]
        is False
    )
    assert (
        evidence["codec_encoding_identity"]["encoding_id"]
        == "nwc.codec.mszip.v1"
    )
    current_inputs = contract.compute_build_inputs()
    assert evidence["nw10_build_inputs"] == current_inputs
    assert evidence["nw10_build_inputs_digest"] == contract.build_inputs_digest(
        current_inputs
    )
    proof_report_path = REPO_ROOT / "benchmarks" / "nw10" / "proof-export-report.v1.json"
    intro_report_path = REPO_ROOT / "benchmarks" / "nw10" / "intro-export-report.v1.json"
    proof_report = json.loads(proof_report_path.read_text(encoding="utf-8"))
    intro_report = json.loads(intro_report_path.read_text(encoding="utf-8"))
    assert proof_report["nw10_build_inputs"] == current_inputs
    assert proof_report["nw10_build_inputs_digest"] == evidence["nw10_build_inputs_digest"]
    assert intro_report["nw10_build_inputs"] == current_inputs
    assert intro_report["nw10_build_inputs_digest"] == evidence["nw10_build_inputs_digest"]
    assert _canonical_text_sha256(proof_report_path) == evidence["proof"]["report_sha256"]
    assert _canonical_text_sha256(intro_report_path) == evidence["intro"]["report_sha256"]
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["contract_evidence"] == "verified_by_pytest"
    assert status["source_head_sha"] is None
    assert status["evidence_status"] == "pending_physical_windows_capture"
    assert status["physical_evidence"] == "pending"
    assert status["reference_capture"] == "pending_reference_capture"
    assert status["frame_time"] == "pending_unverified"
    assert status["ada_correctness"] == "pending_unverified"
    assert status["blackwell_correctness"] == "pending_unverified"
    assert status["clean_system"] == "pending_unverified"
    assert status["startup"] == "pending_unverified"
    assert status["memory"] == "pending_unverified"
    assert not (
        REPO_ROOT
        / "benchmarks"
        / "nw10"
        / "performance-preservation-rtx4060ti.v1.json"
    ).exists()
    correctness = json.loads(correctness_path.read_text(encoding="utf-8"))
    assert correctness["status"] == "pending_unverified"
    packed = correctness["packed_unpacked_comparison"]
    assert packed["status"] == "pending_physical"
    assert packed["direct_separate_packed_gpu_capture_recorded"] is False
    assert packed["packed_package_sha256"] is None
    assert packed["runtime"]["ada"]["status"] == "pending_physical"
    assert packed["runtime"]["blackwell"]["status"] == "pending_physical"
    system = json.loads(system_path.read_text(encoding="utf-8"))
    assert system["status"] == "pending_unverified"
    assert system["final_artifact"] is None
    assert system["startup"] is None
    assert system["memory"] is None
    assert system["clean_system"] is None
    assert system["windows_build"] is None
    reuse = json.loads(reuse_path.read_text(encoding="utf-8"))
    assert reuse["status"] == "archived_informational"
    assert reuse["current_head_transfer"]["packed_unpacked_correctness"]["direct_separate_packed_gpu_capture_recorded"] is False
    assert reuse["physical_measurements"]["system"]["same_physical_host"] is False
    pe_ledger = json.loads(pe_ledger_path.read_text(encoding="utf-8"))
    assert pe_ledger["status"] == "archived_superseded"
    assert pe_ledger["source"]["workflow_run_id"] == 44469176377
    assert pe_ledger["current_attempt"]["source"]["workflow_run_id"] == 44542203189
    assert pe_ledger["conclusion"]["byte_ledger_complete"] is True

    assert (
        "withdrawn" in status["superseded_physical_evidence"]["reason"].casefold()
        or "withdrawn" in json.dumps(status["superseded_physical_evidence"]).casefold()
    )
    for item in status["files"]:
        path = REPO_ROOT / item["path"]
        assert _canonical_text_sha256(path) == item["sha256"]


def test_historical_nw07_digest_is_attributable_not_erased() -> None:
    config = json.loads(
        (REPO_ROOT / contract.CANONICAL_PROOF_CONFIG).read_text(encoding="utf-8")
    )
    provenance = config["build_inputs_provenance"]
    assert contract.HISTORICAL_NW07_BUILD_INPUTS_DIGEST in provenance["superseded_digests"]
    assert config["build_inputs_digest"] != contract.HISTORICAL_NW07_BUILD_INPUTS_DIGEST


def test_grade_evidence_for_an_exact_physical_artifact_is_not_promoted_by_synthetic_data() -> None:
    assert contract.classify_measurement({"status": "contract_only", "test_payload": True}) == (
        "informational_contract_only"
    )
    assert contract.classify_measurement(
        {
            "status": "verified_physical",
            "execution_kind": "virtual_machine",
            "artifact_sha256": "a" * 64,
            "environment_match": True,
            "protocol": "nw07-perf-run.v1",
        }
    ) == "pending_physical"


def test_dependency_and_build_input_accounting_cover_the_compact_decoder() -> None:
    inventory = json.loads(
        (REPO_ROOT / "benchmarks" / "dependency-inventory.v1.json").read_text(
            encoding="utf-8"
        )
    )
    inbox = {item["dll_name"] for item in inventory["inbox_windows_allowlist"]}
    assert "cabinet.dll" in inbox
    for closure in inventory["closures"].values():
        assert "cabinet.dll" in closure["allowed_import_dlls"]
    pinned = set(contract.nw10_pinned_build_inputs())
    assert "src/package/nw_compact.cpp" in pinned
    assert "src/package/nw_compact.h" in pinned
    assert "src/player/embedded_package.cpp" in pinned
    assert "tools/nw10_proof.py" in pinned
    cmake = (REPO_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "src/package/nw_compact.cpp" in cmake
    assert "NW10_COMPACT_PACKAGE" in cmake
    assert "if(NW10_COMPACT_PACKAGE)" in cmake
    assert "cabinet" in cmake
    embedded = (
        REPO_ROOT / "src" / "player" / "embedded_package.cpp"
    ).read_text(encoding="utf-8")
    assert "DecodeCompactPackage" in embedded
    assert "IsCompactPackage" in embedded
    header = (
        REPO_ROOT / "src" / "package" / "nw_compact.h"
    ).read_text(encoding="utf-8")
    assert "kCompactMagic" in header
    assert "kCompactCodecMszip" in header
    assert "kCompactLayoutAos" in header
    assert "kCompactLayoutSoa" in header
    assert "kCompactMaxDecodedBytes" in header
    decoder_source = (
        REPO_ROOT / "src" / "package" / "nw_compact.cpp"
    ).read_text(encoding="utf-8")
    # Keep the compact decoder buildable in hosted clang-cl environments that
    # do not propagate the SDK target-architecture macros through
    # compressapi.h/winnt.h. The declaration-only shim preserves the inbox
    # cabinet.dll ABI without introducing a replacement compressor.
    assert "#include <compressapi.h>" not in decoder_source
    assert "CreateDecompressor" in decoder_source
    assert "kCompressAlgorithmMszip = 2u" in decoder_source


def test_static_windows_compact_feasibility_evidence_is_attributed_and_fail_closed() -> None:
    path = REPO_ROOT / "benchmarks" / "nw10" / "windows-compact-feasibility.v1.json"
    validate_file_against_schema(
        path, REPO_ROOT / "schemas" / "nw10-windows-compact-feasibility.schema.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    pe = json.loads(
        (REPO_ROOT / "benchmarks" / "nw10" / "windows-pe-ledger.v1.json").read_text(
            encoding="utf-8"
        )
    )
    system = json.loads(
        (REPO_ROOT / "benchmarks" / "nw10" / "system-validation.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["target_bytes"] == 65_536
    assert record["target_met"] is False
    assert record["classification"] == "continue"
    assert record["universal_64k_claim"] is False
    assert record["measurement_status"] == "archived_superseded"
    assert record["current_implementation_measurement"] is False
    assert record["measurements"]["proof_measured_bytes"] == 260_294
    assert record["measurements"]["proof_measurement_status"] == "measured_decoder_off"
    assert record["measurements"]["canonical_proof_within_tier"] is True
    assert record["measurements"]["minimum_measured_candidate_bytes"] == 228_181
    assert record["compact_candidate"]["runtime_status"] == "pending_runtime_validation"
    assert record["compact_candidate"]["exit_code"] == -1073741819
    assert record["compact_candidate"]["failure_classification"] == "non_certifying_hosted_failure"
    assert record["compact_candidate"]["counted_as_validated_runnable"] is False
    assert record["measurements"]["minimum_validated_runnable_bytes"] is None
    assert record["measurements"]["validated_mode"] == "canonical_fallback_pending_physical"
    assert record["canonical_fallback"]["runtime_status"] == "pending_runtime_validation"
    assert record["canonical_fallback"]["exit_code"] == -1073741819
    assert record["canonical_fallback"]["dependency_closure"]["closure_verified"] is True
    assert record["canonical_fallback"]["dependency_closure"]["sidecar_files"] == []
    assert "cabinet.dll" not in record["canonical_fallback"]["dependency_closure"]["canonical_imports"]
    assert record["source"]["run_id"] == 44542203189
    assert record["source"]["head_sha"] == "4d216ddf3cb4d5e0cecdc5bf0c49af3ff7d20eee"
    assert record["privacy"]["raw_stdout_stderr_included"] is False
    assert record["privacy"]["runner_paths_included"] is False
    assert record["privacy"]["user_profile_paths_included"] is False
    assert system["status"] == "pending_unverified"
    assert pe["status"] == "archived_superseded"
    assert pe["final_artifact"]["executable_sha256"] == (
        record["prior_valid_artifact"]["executable_sha256"]
    )
    assert pe["final_artifact"]["runtime_status"] == "validated_runnable"
    assert pe["current_attempt"]["measured_artifact"]["executable_sha256"] == (
        record["canonical_fallback"]["sha256"]
    )
    assert pe["current_attempt"]["runtime"]["status"] == (
        "rejected_runtime_failure"
    )
    assert pe["conclusion"]["current_head_artifact_validated"] is False
    assert record["selected_artifact"]["sha256"] == (
        record["prior_valid_artifact"]["executable_sha256"]
    )
    assert record["selected_artifact"]["runtime_status"] == "validated_runnable"
    text = json.dumps(record).casefold()
    for token in ("c:\\users\\", "/home/", "micha", "michal", "matyl"):
        assert token not in text

    status = json.loads(
        (REPO_ROOT / "benchmarks" / "nw10" / "evidence-status.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["evidence_status"] == "pending_physical_windows_capture"
    assert status["reference_capture"] == "pending_reference_capture"


def test_compact_feasibility_schema_supports_a_valid_success_branch() -> None:
    import copy

    schema = json.loads(
        (REPO_ROOT / "schemas" / "nw10-windows-compact-feasibility.schema.json").read_text(
            encoding="utf-8"
        )
    )
    record = json.loads(
        (
            REPO_ROOT / "benchmarks" / "nw10" / "windows-compact-feasibility.v1.json"
        ).read_text(encoding="utf-8")
    )
    success = copy.deepcopy(record)
    success["target_met"] = True
    success["classification"] = "complete"
    success["measurements"] = {
        "proof_measured_bytes": 260_000,
        "proof_measurement_status": "measured_decoder_off",
        "canonical_proof_within_tier": True,
        "minimum_measured_candidate_bytes": 65_000,
        "minimum_validated_runnable_bytes": 65_000,
        "validated_mode": "compact",
    }
    success["compact_candidate"] = {
        "bytes": 65_000,
        "sha256": "a" * 64,
        "runtime_status": "validated_runnable",
        "exit_code": 0,
        "failure_classification": "none",
        "counted_as_validated_runnable": True,
    }
    success["canonical_fallback"] = {
        "bytes": 65_001,
        "sha256": "b" * 64,
        "runtime_status": "validated_runnable",
        "exit_code": 0,
        "link_manifest_verified": True,
        "present_sync_interval": 0,
        "dependency_closure": {
            "closure_verified": True,
            "sidecar_files": [],
            "canonical_imports": ["d3d12.dll", "cabinet.dll"],
        },
    }
    success["acceptance_linkage"]["decision"] = "complete"
    success["acceptance_linkage"]["true_64k_artifact_delivered"] = True
    success["validated_artifact"] = {
        "bytes": 65_000,
        "sha256": "a" * 64,
        "mode": "compact",
        "payload_kind": "compact_container",
    }
    success["certification"] = {
        "status": "verified",
        "decoded_package_identity": {"status": "verified", "reason": "ok"},
        "all_required_bytes_accounted": {"status": "verified", "reason": "ok"},
        "dependency_closure": {"status": "verified", "reason": "ok"},
        "pe_ledger_binding": {"status": "verified", "reason": "ok"},
        "clean_system": {"status": "verified", "reason": "ok"},
        "startup": {"status": "verified", "reason": "ok"},
        "memory": {"status": "verified", "reason": "ok"},
        "performance": {"status": "verified", "reason": "ok"},
        "visual": {"status": "verified", "reason": "ok"},
        "failed_gates": [],
        "pending_gates": [],
    }
    validate_instance(success, schema, schema)
    invalid = copy.deepcopy(success)
    invalid["validated_artifact"]["payload_kind"] = None
    with pytest.raises(SchemaValidationError):
        validate_instance(invalid, schema, schema)


def test_nw10_schemas_structurally_bind_verified_physical_evidence() -> None:
    from schema_validator import validate_instance, SchemaValidationError

    system_schema = json.loads(
        (REPO_ROOT / "schemas" / "nw10-system-validation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    system = json.loads(
        (REPO_ROOT / "benchmarks" / "nw10/system-validation.v1.json").read_text(
            encoding="utf-8"
        )
    )
    system["status"] = "verified"
    with pytest.raises(SchemaValidationError):
        validate_instance(system, system_schema, system_schema)

    correctness_schema = json.loads(
        (REPO_ROOT / "schemas" / "nw10-common-artifact-correctness.schema.json").read_text(
            encoding="utf-8"
        )
    )
    correctness = json.loads(
        (REPO_ROOT / "benchmarks" / "nw10/common-artifact-correctness.v1.json").read_text(
            encoding="utf-8"
        )
    )
    correctness["status"] = "verified"
    with pytest.raises(SchemaValidationError):
        validate_instance(correctness, correctness_schema, correctness_schema)

def test_verified_system_validation_cannot_omit_measurements(tmp_path: Path) -> None:
    record = {
        "format": "nw10-system-validation",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "final_artifact": None,
        "startup": None,
        "memory": None,
        "clean_system": None,
        "windows_build": None,
        "privacy": {
            "local_paths_included": False,
            "raw_stdout_stderr_included": False,
            "user_profile_paths_included": False,
            "raw_result_bundle_tracked": False,
        },
    }
    path = tmp_path / "system.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(
        Exception,
        match=r"(requires a measured final_artifact|final_artifact: expected type)",
    ):
        validate_file_against_schema(
            path,
            REPO_ROOT / "schemas" / "nw10-system-validation.schema.json",
        )


def _verified_system_record() -> dict:
    artifact = {
        "executable_sha256": "1" * 64,
        "executable_bytes": 240_702,
        "package_sha256": "2" * 64,
        "manifest_sha256": "3" * 64,
        "selected_mode": "canonical",
    }
    scenarios = [
        {
            "scenario": name,
            "expected_exit_code": code,
            "observed_exit_code": code,
            "status": "passed",
            "timed_out": False,
        }
        for name, code in (
            ("corrupt_payload", 70),
            ("deployed_startup", 0),
            ("missing_required_feature", 23),
            ("initialization_failure", 42),
            ("resize", 0),
            ("device_removal", 51),
        )
    ]
    hardware = {
        "adapter_description": "NVIDIA GeForce RTX 5070 Ti",
        "driver_version": "32.0.16.1088",
        "windows_build": "10.0.26200",
    }
    return {
        "format": "nw10-system-validation",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "source_head_sha": "4" * 40,
        "final_artifact": artifact,
        "windows_build": "10.0.26200",
        "startup": {
            "status": "verified",
            "execution_kind": "physical",
            "hardware": dict(hardware),
            "cache_state": {
                "kind": "warm",
                "application_pipeline_cache_used": False,
            },
            "process_start_to_first_present_ms": 250.0,
            "ceiling_ms": 2000.0,
        },
        "memory": {
            "status": "verified",
            "execution_kind": "physical",
            "hardware": dict(hardware),
            "package_sha256": artifact["package_sha256"],
            "peak_engine_committed_gpu_bytes": 42_926_080,
            "ceiling_bytes": 536_870_912,
            "post_initialization_allocation_violation": 0,
        },
        "clean_system": {
            "status": "verified",
            "execution_kind": "physical",
            "host": {
                "windows_build": "10.0.26200",
                "architecture": "AMD64",
                "clean_environment": True,
                "virtualization_indicators": [],
                "vm_markers": [],
                "virtualization_probe": {
                    "hypervisor_present": False,
                    "vm_markers": [],
                },
            },
            "hardware": dict(hardware),
            "development_dependencies_present_on_path": [],
            "environment_cache_variables": [],
            "deployment": {
                "only_runtime_executable": True,
                "sidecars": [],
                "forbidden_runtime_dependencies": [],
                "executable_sha256": artifact["executable_sha256"],
                "executable_bytes": artifact["executable_bytes"],
            },
            "scenarios": scenarios,
        },
        "privacy": {
            "local_paths_included": False,
            "raw_stdout_stderr_included": False,
            "user_profile_paths_included": False,
            "raw_result_bundle_tracked": False,
        },
    }


def test_verified_system_validation_binds_every_measurement(tmp_path: Path) -> None:
    schema = REPO_ROOT / "schemas" / "nw10-system-validation.schema.json"
    record = _verified_system_record()
    path = tmp_path / "system.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    validate_file_against_schema(path, schema)

    record["memory"]["package_sha256"] = "9" * 64
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(Exception, match="memory measurement is not bound"):
        validate_file_against_schema(path, schema)

    record = _verified_system_record()
    record["startup"]["hardware"]["driver_version"] = "unavailable"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(Exception, match="driver does not match"):
        validate_file_against_schema(path, schema)


def test_verified_system_validation_freezes_scenarios_and_dependencies(
    tmp_path: Path,
) -> None:
    schema = REPO_ROOT / "schemas" / "nw10-system-validation.schema.json"
    path = tmp_path / "system.json"

    record = _verified_system_record()
    record["clean_system"]["scenarios"][0]["expected_exit_code"] = 0
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(Exception, match="expected exit code changed"):
        validate_file_against_schema(path, schema)

    record = _verified_system_record()
    record["clean_system"]["scenarios"][0]["scenario"] = "renamed_scenario"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(Exception, match="scenario set changed"):
        validate_file_against_schema(path, schema)

    record = _verified_system_record()
    record["clean_system"]["deployment"]["forbidden_runtime_dependencies"] = [
        "developer-only.dll"
    ]
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(Exception, match="forbidden runtime dependencies"):
        validate_file_against_schema(path, schema)

    record = _verified_system_record()
    record["memory"]["post_initialization_allocation_violation"] = 1
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(Exception, match="post-initialization allocation violation"):
        validate_file_against_schema(path, schema)

    record = _verified_system_record()
    record["startup"]["hardware"]["windows_build"] = "10.0.99999"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(Exception, match="Windows build does not match"):
        validate_file_against_schema(path, schema)

    record = _verified_system_record()
    record["clean_system"]["development_dependencies_present_on_path"] = [
        "cmake.exe"
    ]
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(Exception, match="development dependencies"):
        validate_file_against_schema(path, schema)


def test_verified_correctness_and_pe_ledger_cannot_claim_unmeasured_runtime(
    tmp_path: Path,
) -> None:
    correctness = json.loads(
        (
            REPO_ROOT
            / "benchmarks"
            / "nw10"
            / "common-artifact-correctness.v1.json"
        ).read_text(encoding="utf-8")
    )
    correctness["status"] = "verified"
    correctness["ada"]["status"] = "verified"
    correctness["blackwell"]["status"] = "verified"
    correctness["ada"]["hardware"]["device_form_factor"] = "desktop"
    correctness["blackwell"]["hardware"]["device_form_factor"] = "desktop"
    correctness["ada"]["hardware"]["device_form_factor_source"] = (
        "win32_system_enclosure_and_battery"
    )
    correctness["blackwell"]["hardware"]["device_form_factor_source"] = (
        "win32_system_enclosure_and_battery"
    )
    correctness["common_artifact"]["package_sha256"] = "a" * 64
    correctness["comparison"]["fixed_tick_status"] = "passed"
    correctness["comparison"]["ada_loop_boundary_status"] = "passed"
    correctness["comparison"]["blackwell_loop_boundary_status"] = "passed"
    correctness["comparison"]["worst_absolute_error"] = 0.0
    correctness["comparison"]["worst_relative_error"] = 0.0
    correctness["packed_unpacked_comparison"] = {
        "status": "verified",
        "direct_separate_packed_gpu_capture_recorded": False,
        "codec_id": "nwc.codec.mszip.v1",
        "layout_id": "nwc.layout.soa.v1",
        "packed_package_sha256": "a" * 64,
        "unpacked_package_sha256": "a" * 64,
        "worst_absolute_error": 0.0,
        "runtime": {
            "ada": {"status": "passed"},
            "blackwell": {"status": "passed"},
        },
    }
    correctness["source"]["execution_kind"] = "physical_cross_hardware"
    path = tmp_path / "correctness.json"
    path.write_text(json.dumps(correctness), encoding="utf-8")
    with pytest.raises(
        Exception,
        match=r"(direct separate packed GPU capture|direct_separate_packed_gpu_capture_recorded: expected const True)",
    ):
        validate_file_against_schema(
            path,
            REPO_ROOT
            / "schemas"
            / "nw10-common-artifact-correctness.schema.json",
        )

    pe = json.loads(
        (
            REPO_ROOT / "benchmarks" / "nw10" / "windows-pe-ledger.v1.json"
        ).read_text(encoding="utf-8")
    )
    pe["status"] = "verified"
    pe["final_artifact"]["runtime_status"] = "pending_runtime_validation"
    pe["conclusion"]["byte_ledger_complete"] = True
    path.write_text(json.dumps(pe), encoding="utf-8")
    with pytest.raises(Exception, match="validated runnable"):
        validate_file_against_schema(
            path,
            REPO_ROOT / "schemas" / "nw10-windows-pe-ledger.schema.json",
        )


def test_performance_publisher_binds_status_to_implementation_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(perf_preservation, "REPO_ROOT", tmp_path)
    source = tmp_path / "build" / "performance.json"
    destination = (
        tmp_path / "benchmarks" / "nw10" / "performance-preservation.v1.json"
    )
    status_path = tmp_path / "benchmarks" / "nw10" / "evidence-status.v1.json"
    source.parent.mkdir(parents=True)
    status_path.parent.mkdir(parents=True)
    runs = [{"status": "passed"} for _ in range(3)]
    public = {
        "format": perf_preservation.FORMAT,
        "version": perf_preservation.VERSION,
        "task": "NW-10",
        "performance_contract": contract.PERF_CONTRACT_ID,
        "performance_protocol": contract.PERF_PROTOCOL_ID,
        "reference_binding_id": "binding",
        "status": "verified_preserved",
        "reasons": [],
        "matched_environment": True,
        "before": {
            "worst_gpu_p99_ms": 1.0,
            "worst_cpu_submit_p99_ms": 1.0,
            "runs": runs,
        },
        "after": {
            "worst_gpu_p99_ms": 1.1,
            "worst_cpu_submit_p99_ms": 1.1,
            "runs": runs,
        },
        "comparison": {
            "same_canonical_package_sha256": True,
            "faster_substitute_used": False,
            "absolute_gate_operator": "<",
            "absolute_gate_ms": perf_preservation.nw07_contract.FRAME_TIME_THRESHOLD_MS,
        },
        "privacy": {
            "raw_samples_included": False,
            "process_ids_included": False,
            "stdout_stderr_included": False,
            "local_paths_included": False,
            "whitelist_generated": True,
        },
    }
    source.write_text(json.dumps(public), encoding="utf-8")
    status_path.write_text(
        json.dumps(
            {
                "format": "nw10-evidence-status",
                "version": 1,
                "task": "NW-10",
                "evidence_status": "pending_physical_windows_capture",
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    head = "a" * 40
    status = perf_preservation.publish_public_evidence(
        source,
        destination,
        status_path,
        implementation_head=head,
    )
    assert status["source_head_sha"] == head
    entry = status["performance_preservation"]
    assert entry["status"] == "verified_preserved"
    assert entry["path"] == "benchmarks/nw10/performance-preservation.v1.json"
    assert any(
        row["path"] == entry["path"] and row["sha256"] == entry["sha256"]
        for row in status["files"]
    )


def test_pe_ledger_rejects_malformed_prior_artifact_identity() -> None:
    valid = {
        "format": pe_ledger.FORMAT,
        "version": pe_ledger.VERSION,
        "task": "NW-10",
        "final_artifact": {
            "runtime_status": "validated_runnable",
            "executable_sha256": "1" * 64,
            "executable_bytes": 240_702,
            "package_sha256": "2" * 64,
            "manifest_sha256": "3" * 64,
        },
        "conclusion": {"byte_ledger_complete": True},
        "linker_elimination": {},
        "compact_decoder": {},
    }
    pe_ledger._validate_prior_valid_ledger(valid)

    for field, value, message in (
        ("executable_bytes", 0, "executable size"),
        ("package_sha256", "not-a-digest", "package_sha256"),
        ("manifest_sha256", "4" * 63, "manifest_sha256"),
    ):
        malformed = json.loads(json.dumps(valid))
        malformed["final_artifact"][field] = value
        with pytest.raises(ValueError, match=message):
            pe_ledger._validate_prior_valid_ledger(malformed)


def test_contract_evidence_regeneration_rejects_unbound_verified_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nw10_evidence

    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    output = tmp_path / "evidence"
    output.mkdir()
    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": nw10_evidence._git_head(),
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "evidence_status": "performance_correctness_and_system_verified_packaging_pending",
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
    }
    (output / "evidence-status.v1.json").write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    nw10_evidence.build_evidence(output)
    regenerated = json.loads(
        (output / "evidence-status.v1.json").read_text(encoding="utf-8")
    )
    assert regenerated["evidence_status"] == "pending_physical_windows_capture"
    assert regenerated["physical_evidence"] == "pending"
    assert regenerated["reference_capture"] == "pending_reference_capture"
    assert regenerated["ada_correctness"] == "pending_unverified"
    assert regenerated["blackwell_correctness"] == "pending_unverified"
    assert regenerated["clean_system"] == "pending_unverified"
    assert regenerated["startup"] == "pending_unverified"
    assert regenerated["memory"] == "pending_unverified"

def test_physical_status_preservation_requires_matching_file_hashes(
    tmp_path: Path,
) -> None:
    import nw10_evidence

    evidence_root = tmp_path / "benchmarks" / "nw10"
    evidence_root.mkdir(parents=True)
    correctness_path = evidence_root / "common-artifact-correctness.v1.json"
    correctness_path.write_text('{"status":"verified"}\n', encoding="utf-8")
    current_head = "1" * 40
    relative = "benchmarks/nw10/common-artifact-correctness.v1.json"
    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": current_head,
        "evidence_status": "pending_physical_windows_capture",
        "physical_evidence": "pending",
        "reference_capture": "pending_reference_capture",
        "frame_time": "pending_unverified",
        "ada_correctness": "verified",
        "blackwell_correctness": "verified",
        "clean_system": "pending_unverified",
        "startup": "pending_unverified",
        "memory": "pending_unverified",
        "correctness_evidence": {
            "status": "verified",
            "path": relative,
            "sha256": "0" * 64,
        },
        "files": [{"path": relative, "sha256": "0" * 64}],
    }
    assert (
        nw10_evidence._physical_evidence_is_current(
            status, current_head, evidence_root
        )
        is False
    )

    digest = nw10_evidence._sha256(correctness_path)
    status["correctness_evidence"]["sha256"] = digest
    status["files"][0]["sha256"] = digest
    assert (
        nw10_evidence._physical_evidence_is_current(
            status, current_head, evidence_root
        )
        is True
    )

    outside = tmp_path / "outside.json"
    outside.write_text('{"status":"verified"}\n', encoding="utf-8")
    outside_digest = nw10_evidence._sha256(outside)
    for escaped in (
        str(outside),
        "benchmarks/nw10/../../outside.json",
        "benchmarks/other/outside.json",
    ):
        escaped_status = json.loads(json.dumps(status))
        escaped_status["correctness_evidence"]["path"] = escaped
        escaped_status["correctness_evidence"]["sha256"] = outside_digest
        escaped_status["files"][0]["path"] = escaped
        escaped_status["files"][0]["sha256"] = outside_digest
        assert (
            nw10_evidence._physical_evidence_is_current(
                escaped_status, current_head, evidence_root
            )
            is False
        )

    status["blackwell_correctness"] = "pending_unverified"
    assert (
        nw10_evidence._physical_evidence_is_current(
            status, current_head, evidence_root
        )
        is False
    )
    status["blackwell_correctness"] = "verified"
    status["evidence_status"] = "verified_complete"
    assert (
        nw10_evidence._physical_evidence_is_current(
            status, current_head, evidence_root
        )
        is False
    )


def test_physical_status_preservation_accepts_compact_only_progress(
    tmp_path: Path,
) -> None:
    import nw10_evidence

    evidence_root = tmp_path / "benchmarks" / "nw10"
    evidence_root.mkdir(parents=True)
    performance_path = evidence_root / "performance-preservation.v1.json"
    compact_path = evidence_root / "windows-compact-feasibility.v1.json"
    performance_path.write_text('{"status":"verified_preserved"}\n', encoding="utf-8")
    compact_path.write_text('{"status":"verified_windows_runtime"}\n', encoding="utf-8")
    current_head = "2" * 40
    performance_relative = (
        "benchmarks/nw10/performance-preservation.v1.json"
    )
    compact_relative = "benchmarks/nw10/windows-compact-feasibility.v1.json"
    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": current_head,
        "evidence_status": (
            "performance_and_compact_verified_correctness_pending"
        ),
        "physical_evidence": "performance_verified",
        "reference_capture": "verified_reference_capture",
        "frame_time": "verified_preserved",
        "ada_correctness": "pending_unverified",
        "blackwell_correctness": "pending_unverified",
        "clean_system": "pending_unverified",
        "startup": "pending_unverified",
        "memory": "pending_unverified",
        "performance_preservation": {
            "status": "verified_preserved",
            "path": performance_relative,
            "sha256": nw10_evidence._sha256(performance_path),
        },
        "compact_feasibility": {
            "status": "verified_windows_runtime",
            "path": compact_relative,
            "sha256": nw10_evidence._sha256(compact_path),
        },
        "files": [
            {
                "path": performance_relative,
                "sha256": nw10_evidence._sha256(performance_path),
            }
        ],
    }
    # A verified compact component without its ledger row is not current.
    assert (
        nw10_evidence._physical_evidence_is_current(
            status, current_head, evidence_root
        )
        is False
    )
    status["files"].append(
        {
            "path": compact_relative,
            "sha256": nw10_evidence._sha256(compact_path),
        }
    )
    assert (
        nw10_evidence._physical_evidence_is_current(
            status, current_head, evidence_root
        )
        is True
    )


def test_physical_status_preservation_accepts_correctness_system_before_performance(
    tmp_path: Path,
) -> None:
    import nw10_evidence

    evidence_root = tmp_path / "benchmarks" / "nw10"
    evidence_root.mkdir(parents=True)
    current_head = "3" * 40
    components = {}
    rows = []
    for field, name in (
        ("correctness_evidence", "common-artifact-correctness.v1.json"),
        ("system_validation", "system-validation.v1.json"),
    ):
        path = evidence_root / name
        component_status = "verified"
        path.write_text(
            json.dumps({"status": component_status}) + "\n",
            encoding="utf-8",
        )
        digest = nw10_evidence._sha256(path)
        relative = f"benchmarks/nw10/{name}"
        components[field] = {
            "status": component_status,
            "path": relative,
            "sha256": digest,
        }
        rows.append({"path": relative, "sha256": digest})

    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": current_head,
        "evidence_status": "correctness_and_system_reused_performance_pending",
        "physical_evidence": "correctness_and_system_reused",
        "reference_capture": "pending_reference_capture",
        "frame_time": "pending_unverified",
        "ada_correctness": "verified",
        "blackwell_correctness": "verified",
        "clean_system": "verified",
        "startup": "verified",
        "memory": "verified",
        "files": rows,
        **components,
    }
    assert (
        nw10_evidence._physical_evidence_is_current(
            status, current_head, evidence_root
        )
        is True
    )

    # The superseded explicit reuse bridge remains intentionally non-certifying.
    status["reused_physical_evidence"] = {
        "status": "verified_reuse_with_explicit_limitations"
    }
    assert (
        nw10_evidence._physical_evidence_is_current(
            status, current_head, evidence_root
        )
        is False
    )


def test_unknown_future_evidence_state_is_opaque_to_this_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nw10_evidence

    evidence_root = tmp_path / "benchmarks" / "nw10"
    evidence_root.mkdir(parents=True)
    status_path = evidence_root / "evidence-status.v1.json"
    current_head = "a" * 40
    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": current_head,
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
    original = status_path.read_bytes()
    monkeypatch.setattr(nw10_evidence, "_git_head", lambda: current_head)

    assert (
        nw10_evidence._physical_evidence_is_current(
            status, current_head, evidence_root
        )
        is False
    )
    with pytest.raises(
        ValueError,
        match="refusing to regenerate NW-10 contract evidence",
    ):
        nw10_evidence._merge_status(status_path, files=[])
    assert status_path.read_bytes() == original


def test_build_evidence_refuses_unknown_future_state_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nw10_evidence

    evidence_root = tmp_path / "benchmarks" / "nw10"
    evidence_root.mkdir(parents=True)
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
    sentinel_paths = [
        status_path,
        evidence_root / "compact-proof.v1.json",
        evidence_root / "intro-profile.v1.json",
        evidence_root / "proof-export-report.v1.json",
        evidence_root / "intro-export-report.v1.json",
        evidence_root / "common-artifact-correctness.v1.json",
        evidence_root / "system-validation.v1.json",
    ]
    for index, path in enumerate(sentinel_paths[1:], start=1):
        path.write_bytes(f"sentinel-{index}\n".encode("utf-8"))
    originals = {path: path.read_bytes() for path in sentinel_paths}

    def unexpected_export(*args, **kwargs):
        pytest.fail("contract report generation ran before unsupported-state preflight")

    def unexpected_write(*args, **kwargs):
        pytest.fail("physical evidence publisher wrote before unsupported-state preflight")

    monkeypatch.setattr(nw10_evidence.proof, "export_proof", unexpected_export)
    monkeypatch.setattr(nw10_evidence.intro, "export_intro", unexpected_export)
    monkeypatch.setattr(nw10_evidence.publisher, "_write", unexpected_write)

    with pytest.raises(
        ValueError,
        match="refusing to regenerate NW-10 contract evidence",
    ):
        nw10_evidence.build_evidence(evidence_root)

    assert {path: path.read_bytes() for path in sentinel_paths} == originals


def test_build_evidence_refuses_malformed_status_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nw10_evidence

    evidence_root = tmp_path / "benchmarks" / "nw10"
    evidence_root.mkdir(parents=True)
    status_path = evidence_root / "evidence-status.v1.json"
    status_path.write_bytes(b'{"format": "nw10-evidence-status", "version": 1')
    sentinel_paths = [
        status_path,
        evidence_root / "compact-proof.v1.json",
        evidence_root / "intro-profile.v1.json",
        evidence_root / "proof-export-report.v1.json",
        evidence_root / "intro-export-report.v1.json",
        evidence_root / "common-artifact-correctness.v1.json",
        evidence_root / "system-validation.v1.json",
    ]
    for index, path in enumerate(sentinel_paths[1:], start=1):
        path.write_bytes(f"sentinel-{index}\n".encode("utf-8"))
    originals = {path: path.read_bytes() for path in sentinel_paths}

    def unexpected_export(*args, **kwargs):
        pytest.fail("contract report generation ran before malformed-status preflight")

    def unexpected_write(*args, **kwargs):
        pytest.fail("physical evidence publisher wrote before malformed-status preflight")

    monkeypatch.setattr(nw10_evidence.proof, "export_proof", unexpected_export)
    monkeypatch.setattr(nw10_evidence.intro, "export_intro", unexpected_export)
    monkeypatch.setattr(nw10_evidence.publisher, "_write", unexpected_write)

    with pytest.raises(ValueError, match="malformed"):
        nw10_evidence.build_evidence(evidence_root)

    assert {path: path.read_bytes() for path in sentinel_paths} == originals


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("format", "unrelated-summary", "unexpected format"),
        ("version", 2, "unexpected version"),
        ("task", "NW-09", "unexpected task"),
    ],
)
def test_build_evidence_refuses_foreign_status_identity_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    import nw10_evidence

    evidence_root = tmp_path / "benchmarks" / "nw10"
    evidence_root.mkdir(parents=True)
    status_path = evidence_root / "evidence-status.v1.json"
    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "evidence_status": "pending_physical_windows_capture",
        "files": [],
    }
    status[field] = value
    status_path.write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sentinel_paths = [
        status_path,
        evidence_root / "compact-proof.v1.json",
        evidence_root / "intro-profile.v1.json",
        evidence_root / "proof-export-report.v1.json",
        evidence_root / "intro-export-report.v1.json",
        evidence_root / "common-artifact-correctness.v1.json",
        evidence_root / "system-validation.v1.json",
    ]
    for index, path in enumerate(sentinel_paths[1:], start=1):
        path.write_bytes(f"sentinel-{index}\n".encode("utf-8"))
    originals = {path: path.read_bytes() for path in sentinel_paths}

    def unexpected_export(*args, **kwargs):
        pytest.fail("contract report generation ran before identity preflight")

    def unexpected_write(*args, **kwargs):
        pytest.fail("physical evidence publisher wrote before identity preflight")

    monkeypatch.setattr(nw10_evidence.proof, "export_proof", unexpected_export)
    monkeypatch.setattr(nw10_evidence.intro, "export_intro", unexpected_export)
    monkeypatch.setattr(nw10_evidence.publisher, "_write", unexpected_write)

    with pytest.raises(ValueError, match=message):
        nw10_evidence.build_evidence(evidence_root)

    assert {path: path.read_bytes() for path in sentinel_paths} == originals


def test_pe_ledger_artifact_binding_is_reproducible() -> None:
    report = {
        "format": "nw10-windows-pe-ledger",
        "version": 1,
        "current_attempt": {
            "source": {
                "head_sha": "1" * 40,
                "workflow_run_id": 123,
            }
        },
    }
    bound = pe_ledger.bind_artifact_source(
        report,
        artifact_name="nw10-windows-compact-evidence",
        artifact_id=456,
        artifact_digest="a" * 64,
    )
    source = bound["current_attempt"]["source"]
    assert source["artifact_name"] == "nw10-windows-compact-evidence"
    assert source["artifact_id"] == 456
    assert source["artifact_digest"] == "sha256:" + "a" * 64
    assert source["artifact_retained"] is True


def test_reused_physical_evidence_regeneration_does_not_promote_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil
    import nw10_evidence

    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    output = tmp_path / "evidence"
    output.mkdir()
    shutil.copy2(
        REPO_ROOT / "benchmarks" / "nw10" / "reused-physical-evidence.v1.json",
        output / "reused-physical-evidence.v1.json",
    )
    nw10_evidence.build_evidence(output)

    compact_record = json.loads(
        (output / "compact-proof.v1.json").read_text(encoding="utf-8")
    )
    intro_record = json.loads(
        (output / "intro-profile.v1.json").read_text(encoding="utf-8")
    )
    assert compact_record["evidence_status"] == "informational_contract_only"
    assert compact_record["correctness_coverage"]["desktop_ada_representative"]["status"] == "pending_unverified"
    assert intro_record["physical_measurements"] == "pending_physical_windows_capture"
    assert "reused_physical_evidence" not in intro_record


def test_regeneration_withdraws_same_head_reuse_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nw10_evidence

    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    output = tmp_path / "evidence"
    output.mkdir()
    head = nw10_evidence._git_head()
    status = {
        "format": "nw10-evidence-status",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": head,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "evidence_status": "performance_correctness_and_system_verified_packaging_pending",
        "contract_evidence": "verified_by_pytest",
        "physical_evidence": "performance_correctness_and_system_verified",
        "reference_capture": "pending_reference_capture",
        "ada_correctness": "verified",
        "blackwell_correctness": "verified",
        "clean_system": "verified",
        "startup": "verified",
        "memory": "verified",
        "frame_time": "pending_unverified",
        "reused_physical_evidence": "benchmarks/nw10/reused-physical-evidence.v1.json",
        "correctness_evidence": {
            "status": "verified",
            "path": "benchmarks/nw10/missing-correctness.json",
            "sha256": "0" * 64,
        },
        "files": [],
    }
    (output / "evidence-status.v1.json").write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    nw10_evidence.build_evidence(output)
    regenerated = json.loads(
        (output / "evidence-status.v1.json").read_text(encoding="utf-8")
    )
    assert regenerated["evidence_status"] == "pending_physical_windows_capture"
    assert regenerated["ada_correctness"] == "pending_unverified"
    assert regenerated["clean_system"] == "pending_unverified"
    assert "reused_physical_evidence" not in regenerated
    assert regenerated["superseded_physical_evidence"]
