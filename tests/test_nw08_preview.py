from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw08_editor as editor  # noqa: E402
import nw08_preview as preview  # noqa: E402


PROBE = REPO_ROOT / "fixtures" / "nw08" / "shader_edit_probe.nwgraph"


def _pipeline(graph: Path | None = None) -> preview.PreviewPipeline:
    session = (
        editor.GraphEditorSession.open(graph)
        if graph is not None
        else editor.GraphEditorSession.create_nw07_proof()
    )
    return preview.PreviewPipeline(session, gpu_budget_bytes=512 * 1024 * 1024)


def test_pass_timings_and_size_attribution_are_reported() -> None:
    pipeline = _pipeline()
    assert pipeline.preview_now(tick=240_000).accepted
    record = pipeline.last_valid_preview
    assert record is not None
    names = {item.name for item in record.frame.pass_timings.timings}
    assert {"initialization_reference", "timeline_sample", "size_attribution"} <= names
    assert record.frame.pass_timings.total_milliseconds >= 0.0
    attribution = record.frame.size_attribution
    assert attribution["available"] is True
    assert attribution["resident_payload_bytes"] > 0
    assert attribution["baked_asset_bytes"] == 0
    assert attribution["texture"]["width"] == 256


def test_failed_shader_edit_retains_last_valid_and_links_diagnostic() -> None:
    pipeline = _pipeline(PROBE)
    assert pipeline.preview_now(tick=0).accepted
    previous = pipeline.last_valid_preview
    assert previous is not None
    previous_generation = pipeline.snapshot_generation

    invalid = pipeline.session.set_parameter("shader", "expression", "vec4(sin(")
    pipeline.apply_edit(invalid)
    assert invalid.valid is False
    assert pipeline.last_valid_preview is not None
    assert pipeline.last_valid_preview.frame.image_sha256 == previous.frame.image_sha256
    assert pipeline.snapshot_generation == previous_generation
    assert pipeline.last_diagnostics
    diagnostic = pipeline.last_diagnostics[0]
    assert diagnostic["code"] == "invalid_expression"
    assert diagnostic["source"]["node_id"] == "shader"
    assert diagnostic["source"]["line"] > 0
    assert diagnostic["editor_selection"] == {"node_id": "shader"}

    valid = pipeline.session.set_parameter(
        "shader",
        "expression",
        "vec4(mix(uv.x, sin(uv.y * 5.0 + time), 0.5), 0.25, 0.5, 1.0)",
    )
    pipeline.apply_edit(valid)
    assert valid.valid is True
    assert pipeline.snapshot_generation == previous_generation + 1
    assert pipeline.preview_now(tick=0).accepted
    assert (
        pipeline.last_valid_preview.frame.image_sha256
        != previous.frame.image_sha256
    )


def test_cancelled_and_out_of_order_jobs_never_replace_valid_preview() -> None:
    pipeline = _pipeline()
    assert pipeline.preview_now(tick=10).accepted
    original = pipeline.last_valid_preview
    assert original is not None

    pipeline.apply_parameter_edit("texture", "seed", "1297036694")
    first = pipeline.submit_preview(tick=100)
    second = pipeline.submit_preview(tick=200)
    assert pipeline.execute(second).accepted
    assert pipeline.last_valid_preview.frame.tick == 200
    rejected = pipeline.execute(first)
    assert rejected.accepted is False
    assert rejected.reason == "superseded_by_newer_job"
    assert pipeline.last_valid_preview.frame.tick == 200

    cancelled = pipeline.submit_preview(tick=300)
    assert pipeline.cancel(cancelled)
    assert pipeline.execute(cancelled).reason == "canceled"
    assert pipeline.last_valid_preview.frame.tick == 200


def test_obsolete_snapshot_completion_is_rejected() -> None:
    pipeline = _pipeline()
    token = pipeline.submit_preview(tick=10)
    pipeline.apply_parameter_edit("camera", "fov_degrees", "55")
    decision = pipeline.execute(token)
    assert decision.accepted is False
    assert decision.reason == "obsolete_snapshot_generation"
    assert pipeline.last_valid_preview is None


def test_device_loss_and_recreation_reject_old_jobs_and_realizations() -> None:
    pipeline = _pipeline()
    assert pipeline.preview_now(tick=10).accepted
    assert pipeline.cache.live_bytes > 0
    pending = pipeline.submit_preview(tick=20)
    pipeline.handle_device_loss()
    assert pipeline.last_valid_preview is None
    assert pipeline.cache.live_bytes == 0
    assert pipeline.cache.retiring_bytes == 0
    generation = pipeline.reinitialize_device()
    assert generation.epoch == 1
    decision = pipeline.execute(pending)
    assert decision.accepted is False
    assert decision.reason in {"obsolete_device_epoch", "device_lost"}
    snapshot_generation = pipeline.snapshot_generation
    assert pipeline.preview_now(tick=30).accepted
    assert pipeline.last_valid_preview.frame.tick == 30
    assert pipeline.last_valid_preview.token.device_epoch == 1
    assert pipeline.snapshot_generation == snapshot_generation


def test_windows_backend_uses_existing_player_command(tmp_path: Path) -> None:
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ")
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = preview.WindowsRuntimePreviewBackend(executable, runner=runner)
    session = editor.GraphEditorSession.create_nw07_proof()
    frame = backend.render(session.document, 480_000, width=1920, height=1080)
    assert calls
    assert "--capture-tick" in calls[0]
    assert "480000" in calls[0]
    assert frame.backend == "windows-package-player"
    assert frame.hardware_evidence is True
    assert frame.execution_kind == "physical_runtime"


def test_reference_backend_is_never_reported_as_hardware_evidence() -> None:
    pipeline = _pipeline()
    pipeline.preview_now(tick=0)
    frame = pipeline.last_valid_frame
    assert frame is not None
    assert frame.hardware_evidence is False
    assert frame.execution_kind == "contract_reference"

