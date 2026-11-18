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
from schema_validator import validate_file_against_schema  # noqa: E402


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
    assert ledger["all_required_bytes_accounted"] is True
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
    system_path = REPO_ROOT / "benchmarks" / "nw10" / "system-validation.v1.json"
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
        system_path, REPO_ROOT / "schemas" / "nw10-system-validation.schema.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["evidence_status"] == "informational_contract_only"
    assert evidence["physical_certification"] == "pending_physical_windows_capture"
    assert evidence["reference_capture"] == "pending_reference_capture"
    assert evidence["proof"]["reproducibility"]["matching"] is True
    assert evidence["tiers"]["compact_target_bytes"] == 65_536
    assert evidence["tiers"]["stretch_bytes"] == 131_072
    assert evidence["tiers"]["proof_bytes"] == 262_144
    assert evidence["tiers"]["standard_bytes"] == 1_048_576
    assert evidence["decision"]["custom_compressor_started"] is False
    assert evidence["decision"]["universal_64k_claim"] is False
    current_inputs = contract.compute_build_inputs()
    assert evidence["nw10_build_inputs"] == current_inputs
    assert evidence["nw10_build_inputs_digest"] == contract.build_inputs_digest(
        current_inputs
    )
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["contract_evidence"] == "verified_by_pytest"
    performance_path = (
        REPO_ROOT
        / "benchmarks"
        / "nw10"
        / "performance-preservation-rtx4060ti.v1.json"
    )
    if performance_path.is_file():
        compact_physical_path = (
            REPO_ROOT
            / "benchmarks"
            / "nw10"
            / "windows-compact-feasibility.v1.json"
        )
        if compact_physical_path.is_file():
            assert (
                status["evidence_status"]
                == "verified_complete"
            )
            assert status["compact_feasibility"]["status"] == "verified_windows_runtime"
        else:
            assert status["evidence_status"] == "performance_verified_correctness_pending"
        assert status["physical_evidence"] == "performance_correctness_and_system_verified"
        assert status["clean_system"] == "verified"
        assert status["startup"] == "verified"
        assert status["memory"] == "verified"
        system = json.loads(system_path.read_text(encoding="utf-8"))
        assert system["status"] == "verified"
        assert system["final_artifact"]["executable_sha256"] == "76c13bcc21da3a17b10b912f5e8ff4270f64749c74b347f174b519cbdd8c96bb"
        assert system["startup"]["process_start_to_first_present_ms"] == 240.839
        assert system["memory"]["peak_engine_committed_gpu_bytes"] == 42_926_080
        assert system["clean_system"]["hardware"]["adapter_description"] == "NVIDIA GeForce RTX 5070 Ti"
        assert system["privacy"]["local_paths_included"] is False
        assert status["reference_capture"] == "verified_reference_capture"
        assert status["frame_time"] == "verified_preserved"
        correctness_path = (
            REPO_ROOT
            / "benchmarks"
            / "nw10"
            / "common-artifact-correctness.v1.json"
        )
        correctness = json.loads(correctness_path.read_text(encoding="utf-8"))
        assert correctness["format"] == "nw10-common-artifact-correctness-summary"
        assert correctness["status"] == "verified"
        assert correctness["common_artifact"]["package_sha256"] == (
            status["correctness_evidence"]["package_sha256"]
        )
        assert correctness["ada"]["status"] == "verified"
        assert correctness["blackwell"]["status"] == "verified"
        assert correctness["comparison"]["fixed_tick_status"] == "passed"
        assert correctness["comparison"]["ada_loop_boundary_status"] == "passed"
        assert correctness["comparison"]["blackwell_loop_boundary_status"] == "passed"
        assert correctness["comparison"]["worst_absolute_error"] == 0.0
        assert correctness["comparison"]["worst_relative_error"] == 0.0
        assert correctness["privacy"]["raw_capture_bytes_embedded"] is False
        assert correctness["privacy"]["local_paths_included"] is False
        preservation = json.loads(performance_path.read_text(encoding="utf-8"))
        assert preservation["format"] == "nw10-performance-preservation"
        assert preservation["status"] == "verified_preserved"
        assert preservation["matched_environment"] is True
        assert preservation["comparison"]["same_canonical_package_sha256"] is True
        assert preservation["comparison"]["faster_substitute_used"] is False
        assert preservation["privacy"]["raw_samples_included"] is False
        assert preservation["privacy"]["local_paths_included"] is False
        validate_file_against_schema(
            performance_path,
            REPO_ROOT / "schemas" / "nw10-performance-preservation.schema.json",
        )
    else:
        assert status["evidence_status"] == "pending_physical_windows_capture"
        assert status["reference_capture"] == "pending_reference_capture"
        assert status["frame_time"] == "pending_unverified"
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
    assert record["target_bytes"] == 65_536
    assert record["target_met"] is False
    assert record["classification"] == "continue"
    assert record["universal_64k_claim"] is False
    assert record["measurements"]["minimum_measured_candidate_bytes"] == 219_710
    assert record["compact_candidate"]["runtime_status"] == "rejected_runtime_failure"
    assert record["compact_candidate"]["exit_code"] == 70
    assert record["compact_candidate"]["failure_classification"] == "embedded_package_invalid"
    assert record["compact_candidate"]["counted_as_validated_runnable"] is False
    assert record["measurements"]["minimum_validated_runnable_bytes"] == 222_058
    assert record["measurements"]["validated_mode"] == "canonical_fallback"
    assert record["canonical_fallback"]["runtime_status"] == "validated_runnable"
    assert record["canonical_fallback"]["exit_code"] == 0
    assert record["canonical_fallback"]["dependency_closure"]["closure_verified"] is True
    assert record["canonical_fallback"]["dependency_closure"]["sidecar_files"] == []
    assert record["source"]["run_id"] == 44_181_057_725
    assert record["privacy"]["raw_stdout_stderr_included"] is False
    assert record["privacy"]["runner_paths_included"] is False
    assert record["privacy"]["user_profile_paths_included"] is False
    text = json.dumps(record).casefold()
    for token in ("c:\\\\users\\\\", "/home/", "micha", "michal", "matyl"):
        assert token not in text

    status = json.loads(
        (REPO_ROOT / "benchmarks" / "nw10" / "evidence-status.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["evidence_status"] == "verified_complete"
    assert status["compact_feasibility"]["status"] == "verified_windows_runtime"
    assert status["compact_feasibility"]["target_met"] is False
    assert status["compact_feasibility"]["minimum_measured_candidate_bytes"] == 219_710
    assert status["compact_feasibility"]["minimum_validated_runnable_bytes"] == 222_058
    assert status["ada_correctness"] == "verified"
    assert status["blackwell_correctness"] == "verified"
