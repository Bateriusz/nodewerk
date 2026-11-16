from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw08_editor as editor  # noqa: E402
import nw08_preview as preview  # noqa: E402
import nw09_temporal as temporal  # noqa: E402


PARTICLES = REPO_ROOT / "fixtures" / "nw09" / "particles_seeded.nwgraph"
TICK = temporal.TIMELINE_TICKS_PER_SIMULATION_TICK


@pytest.fixture(autouse=True)
def _test_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")


def _session() -> editor.GraphEditorSession:
    return editor.GraphEditorSession.open(PARTICLES)


def test_preview_backend_uses_fixed_step_state() -> None:
    session = _session()
    backend = temporal.TemporalReferencePreviewBackend()
    frame_a = backend.render(session.document, TICK * 5, width=64, height=32)
    frame_b = backend.render(session.document, TICK * 5, width=64, height=32)
    assert frame_a.image_sha256 == frame_b.image_sha256
    assert frame_a.execution_kind == "contract_reference"
    assert frame_a.hardware_evidence is False
    assert frame_a.content_digest.startswith("sha256:v1:")
    # Presentation cadence does not change the fixed-step state digest.
    backend.render(session.document, TICK * 3, width=64, height=32)
    cadence = backend.render(session.document, TICK * 5, width=64, height=32)
    assert cadence.content_digest == frame_a.content_digest


def test_pipeline_cancellation_keeps_last_valid_temporal_preview() -> None:
    session = _session()
    backend = temporal.TemporalReferencePreviewBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted
    previous = pipeline.last_valid_preview
    assert previous is not None
    token = pipeline.submit_preview(tick=TICK * 10)
    assert pipeline.cancel(token) is True
    decision = pipeline.execute(token)
    assert decision.accepted is False
    assert pipeline.last_valid_preview is previous


def test_device_recreation_requires_explicit_preview_boundary() -> None:
    session = _session()
    backend = temporal.TemporalReferencePreviewBackend(
        device_lifetime=temporal.DeviceLifetime("device-a", 0)
    )
    pipeline = preview.PreviewPipeline(session, backend=backend)
    controller = temporal.TemporalPreviewSession(
        session,
        pipeline,
        device_lifetime=temporal.DeviceLifetime("device-a", 0),
    )
    assert controller.seek(TICK * 4).ok
    checkpoint = controller.runtime.checkpoint(residency="device")
    controller.reinitialize_device(temporal.DeviceLifetime("device-a", 1))
    assert not controller.runtime.checkpoint_compatibility(checkpoint).compatible
    with pytest.raises(temporal.CheckpointError):
        controller.runtime.restore_checkpoint(
            checkpoint, allow_device_replay=True
        )


def test_temporal_nodes_are_not_pure_cached_outputs() -> None:
    session = _session()
    backend = temporal.TemporalReferencePreviewBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    record = pipeline.preview_now(tick=TICK * 2)
    assert record.accepted
    assert pipeline.last_valid_preview is not None
    node = session.document.nodes["particles"]
    assert node.frequency == "history"
    # The realization key must include the exact tick/state, so the same node
    # at two ticks is not a byte-identical pure cache entry.
    frame_a = backend.render(session.document, TICK * 2, width=64, height=32)
    frame_b = backend.render(session.document, TICK * 3, width=64, height=32)
    assert frame_a.content_digest != frame_b.content_digest

