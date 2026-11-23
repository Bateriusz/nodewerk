from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_compact as compact  # noqa: E402
import nw10_contract as contract  # noqa: E402
import nw10_intro as intro  # noqa: E402
import nwp_package as nwp  # noqa: E402


def test_restricted_intro_profile_is_frozen_and_complete() -> None:
    record = intro.validate_intro_graph()
    assert record["profile_id"] == "nw10.intro.sdf20.v1"
    assert record["loop_ticks"] == 960_000
    assert record["loop_seconds"] == 20
    assert (record["width"], record["height"]) == (1920, 1080)
    assert record["frame_rate_hz"] == 60
    assert record["audio"] is False
    assert record["mesh_pipeline"] is False
    assert record["hardware_ray_tracing_required"] is False
    assert record["runtime_dxc"] is False
    assert record["external_assets"] is False
    assert "sphere_sdf" in record["features"]
    assert "torus_sdf" in record["features"]
    assert "seeded_procedural_material" in record["features"]
    assert "camera_curve" in record["features"]
    assert "aces_tonemapping" in record["features"]
    assert record["fixed_tick_replay"] is True
    assert record["performance_contract"] == "nw07_perf_ref_v1"
    assert record["performance_protocol"] == "nw07-perf-run.v1"


def test_restricted_intro_shader_uses_smooth_union_not_intersection() -> None:
    shader = (REPO_ROOT / contract.INTRO_SHADER).read_text(encoding="utf-8")
    assert "float h = saturate(0.5 + 0.5 * (torus - sphere) / blend);" in shader
    assert "float h = saturate(0.5 + 0.5 * (sphere - torus) / blend);" not in shader
    assert "return lerp(torus, sphere, h) - blend * h * (1.0 - h);" in shader


def test_intro_operator_set_excludes_audio_mesh_and_hardware_rt() -> None:
    record = intro.validate_intro_graph()
    operators = set(record["operators"])
    assert operators == set(contract.INTRO_OPERATORS)
    assert "nw.mesh.torus" not in operators
    assert "nw.render.instanced" not in operators
    assert "nw.particles.system" not in operators
    assert "nw.feedback.image" not in operators


def test_profile_boundaries_preserve_all_configured_tiers() -> None:
    tiers = contract.budget_tiers()
    assert tiers["compact_target_bytes"] == 65_536
    assert tiers["stretch_bytes"] == 131_072
    assert tiers["proof_bytes"] == 262_144
    assert tiers["standard_bytes"] == 1_048_576
    assert tiers["larger_configured_budgets_supported"] is True
    assert tiers["compact_target_is_universal_feasibility_claim"] is False


def test_preserved_nw07_gates_and_tolerances_are_not_weakened() -> None:
    limits = contract.authoritative_limits()
    assert limits["complete_distribution_bytes_ceiling"] == 262_144
    assert limits["stretch_distribution_bytes_ceiling"] == 131_072
    assert limits["process_start_to_first_present_ms_ceiling"] == 2_000.0
    assert limits["peak_engine_committed_gpu_bytes_ceiling"] == 536_870_912
    assert limits["p99_gpu_frame_time_ms"]["threshold"] == 16.67
    assert limits["p99_cpu_submit_ms"]["threshold"] == 16.67
    assert limits["p99_gpu_frame_time_ms"]["operator"] == "<"
    assert limits["p99_cpu_submit_ms"]["operator"] == "<"
    assert limits["p99_gpu_frame_time_ms"]["runs"] == 3
    assert limits["p99_gpu_frame_time_ms"]["warmup_seconds"] == 5
    assert limits["p99_gpu_frame_time_ms"]["run_seconds"] == 60
    tolerances = contract.image_tolerances()
    assert tolerances["numeric"]["absolute"] == 1e-5
    assert tolerances["numeric"]["relative"] == 1e-4
    assert tolerances["image"]["absolute"] == 1e-4
    assert tolerances["image"]["relative"] == 1e-4


def test_unknown_codec_layout_and_parameter_identifiers_fail() -> None:
    for codec_id, layout_id, parameter_id in (
        ("nwc.codec.unknown", contract.LAYOUT_AOS_ID, contract.PARAM_LAYOUT_CANONICAL_ID),
        (contract.CODEC_STORED_ID, "nwc.layout.unknown", contract.PARAM_LAYOUT_CANONICAL_ID),
        (contract.CODEC_STORED_ID, contract.LAYOUT_AOS_ID, "nwc.params.unknown"),
    ):
        with pytest.raises(ValueError):
            contract.validate_codec_identifiers(
                codec_id, layout_id, parameter_layout_id=parameter_id
            )


def test_physical_evidence_classification_never_promotes_contract_only() -> None:
    assert (
        contract.classify_measurement(
            {"status": "contract_only", "test_payload": True}
        )
        == "informational_contract_only"
    )
    assert (
        contract.classify_measurement(
            {"status": "verified_physical", "artifact_sha256": "a" * 64}
        )
        == "pending_physical"
    )
    assert (
        contract.classify_measurement(
            {"status": "failed", "test_payload": True}
        )
        == "failed"
    )
    assert (
        contract.classify_measurement(
            {
                "status": "verified_physical",
                "execution_kind": "physical",
                "artifact_sha256": "a" * 64,
                "environment_match": True,
                "protocol": "nw07-perf-run.v1",
            }
        )
        == "verified_physical"
    )


def test_intro_export_is_deterministic_and_keeps_physical_evidence_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    first = intro.export_intro(
        tmp_path / "first", contracts_only=True, report_path=tmp_path / "first.json"
    )
    second = intro.export_intro(
        tmp_path / "second", contracts_only=True, report_path=tmp_path / "second.json"
    )
    assert first["canonical_artifact"]["executable_sha256"] == (
        second["canonical_artifact"]["executable_sha256"]
    )
    assert first["compact_artifact"]["selected_executable_sha256"] == (
        second["compact_artifact"]["selected_executable_sha256"]
    )
    assert first["canonical_artifact"]["package_sha256"] == (
        second["canonical_artifact"]["package_sha256"]
    )
    assert first["release_grade"] is False
    assert first["test_payload"] is True
    assert first["performance_validation"]["status"] == "pending_physical_windows_capture"
    assert first["visual_validation"]["status"] == "pending_physical_windows_capture"
    assert first["decision"]["physical_target_met"] is False
    assert first["decision"]["universal_64k_claim"] is False
    assert first["decision"]["classification"] == "continue"
    assert first["release_shader_binding"]["entry_point"] == "SDFIntroCS"
    assert first["release_shader_binding"]["verified_in_package"] is True
    packages = list((tmp_path / "first").glob("*.nwp"))
    assert len(packages) == 1
    package = nwp.decode_package(packages[0].read_bytes(), allow_dev=True)
    assert [shader.entry_point for shader in package.shaders] == ["SDFIntroCS"]
    # Compression that increases total distribution is rejected rather than
    # relabelled as a win.
    compact_ledger = first["ledgers"]["compact"]
    if compact_ledger["compression_rejected_for_increasing_distribution"]:
        assert compact_ledger["distribution_delta_bytes"] == 0
    else:
        assert (
            compact_ledger["complete_runnable_distribution_bytes"]
            <= first["ledgers"]["canonical"]["complete_runnable_distribution_bytes"]
        )


def test_intro_profile_rejects_mutated_graph_settings(tmp_path: Path) -> None:
    source = (REPO_ROOT / contract.INTRO_GRAPH).read_text(encoding="utf-8")
    mutations = (
        ("setting nw10_intro_loop_ticks 960000", "setting nw10_intro_loop_ticks 480000"),
        ("setting nw10_intro_loop_seconds 20", "setting nw10_intro_loop_seconds 10"),
        ("setting nw10_intro_profile nw10.intro.sdf20.v1", "setting nw10_intro_profile nw10.intro.sdf10.v1"),
        ("setting nw07_output_width 1920", "setting nw07_output_width 1280"),
    )
    for original, replacement in mutations:
        text = source.replace(original, replacement)
        assert text != source
        path = tmp_path / "mutated.nwgraph"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(intro.IntroContractError):
            intro.validate_intro_graph(path)


def test_intro_report_is_json_serializable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    intro.export_intro(
        tmp_path / "artifact",
        contracts_only=True,
        report_path=tmp_path / "report.json",
    )
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["format"] == "nw10-intro-report"
    assert report["ledgers"]["compact"]["all_required_bytes_accounted"] is False
    assert "decoder_code_bytes" in report["ledgers"]["compact"]["unaccounted_components"]
    assert "cabinet.dll" in {
        item["name"].lower()
        for item in report["ledgers"]["compact"]["dependencies"]
    }
    assert report["ledgers"]["compact"]["budget_tiers"]["compact_target_bytes"] == 65_536
