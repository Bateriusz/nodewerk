from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw09_temporal as temporal  # noqa: E402


PARTICLES = REPO_ROOT / "fixtures" / "nw09" / "particles_seeded.nwgraph"
FEEDBACK = REPO_ROOT / "fixtures" / "nw09" / "feedback_rect.nwgraph"
TICK = temporal.TIMELINE_TICKS_PER_SIMULATION_TICK


@pytest.fixture(autouse=True)
def _test_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")


def _runtime(path: Path) -> temporal.TemporalRuntime:
    return temporal.TemporalRuntime(nw03.load_graph(path))


@pytest.mark.parametrize("fixture", [PARTICLES, FEEDBACK])
def test_fixed_step_is_120hz_and_exactly_400_ticks(fixture: Path) -> None:
    runtime = _runtime(fixture)
    assert temporal.FIXED_STEP_HZ == 120
    assert temporal.TIMELINE_TICKS_PER_SIMULATION_TICK == 400
    result = runtime.advance_to(TICK * 25)
    assert result.processed_simulation_ticks == 25
    assert result.pending_simulation_ticks == 0
    assert result.presentation_timeline_tick == TICK * 25


@pytest.mark.parametrize("fixture", [PARTICLES, FEEDBACK])
@pytest.mark.parametrize(
    "presentation_ticks",
    [
        [TICK * 10],
        [TICK + TICK // 3, TICK * 5, TICK * 10],
        [0, 137, 999, TICK * 3 + 17, TICK * 10],
    ],
)
def test_presentation_cadence_never_changes_semantic_state(
    fixture: Path, presentation_ticks: list[int]
) -> None:
    direct = _runtime(fixture)
    direct.advance_to(TICK * 10)
    presented = _runtime(fixture)
    presented.advance_frames(presentation_ticks)
    assert presented.state_digest() == direct.state_digest()


@pytest.mark.parametrize("fixture", [PARTICLES, FEEDBACK])
def test_reset_full_replay_and_checkpoint_replay_agree(fixture: Path) -> None:
    target = TICK * 40
    reference = _runtime(fixture)
    reference.advance_to(target)
    expected = reference.state_digest()

    reference.reset()
    assert reference.seek(target).ok
    assert reference.state_digest() == expected

    checkpoint_runtime = _runtime(fixture)
    checkpoint_runtime.advance_to(TICK * 20)
    checkpoint = checkpoint_runtime.checkpoint()
    replay = _runtime(fixture)
    result = replay.seek(target, checkpoint=checkpoint)
    assert result.ok
    assert result.from_checkpoint
    assert replay.state_digest() == expected


@pytest.mark.parametrize("fixture", [PARTICLES, FEEDBACK])
def test_work_cap_lags_presentation_without_dropping_ticks(fixture: Path) -> None:
    runtime = _runtime(fixture)
    first = runtime.advance_to(TICK * 30, work_cap=7)
    assert first.processed_simulation_ticks == 7
    assert first.pending_simulation_ticks == 23
    assert first.presentation_lagging
    while first.pending_simulation_ticks:
        first = runtime.advance_to(TICK * 30, work_cap=7)
    assert runtime.simulation_tick == 30
    direct = _runtime(fixture)
    direct.advance_to(TICK * 30)
    assert runtime.state_digest() == direct.state_digest()


@pytest.mark.parametrize("fixture", [PARTICLES, FEEDBACK])
def test_deliberate_long_frame_processes_every_tick_in_order(fixture: Path) -> None:
    runtime = _runtime(fixture)
    result = runtime.advance_to(TICK * 120)
    assert result.processed_simulation_ticks == 120
    assert result.pending_simulation_ticks == 0
    assert runtime.simulation_tick == 120


def test_backward_seek_resets_cursor_without_duplicate_events() -> None:
    events = (
        temporal.TemporalEvent("a", 400, {}),
        temporal.TemporalEvent("b", 800, {}),
    )
    runtime = temporal.TemporalRuntime(nw03.load_graph(PARTICLES), events=events)
    first = runtime.advance_to(1600)
    assert [event.event_id for event in first.events] == ["a", "b"]
    second = runtime.seek(0)
    assert second.ok
    third = runtime.advance_to(1600)
    assert third.events == ()
    assert runtime.event_cursor.loop_generation == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", temporal.CHECKPOINT_VERSION + 1),
        ("tick", 1234),
        ("semantic_digest", "sha256:v1:deadbeef"),
        ("state_layout_hash", "sha256:v1:deadbeef"),
        ("capability_profile_id", "nw.other.profile.v1"),
        ("capability_requirements_digest", "sha256:v1:deadbeef"),
    ],
)
def test_checkpoint_compatibility_varies_each_field_independently(
    field: str, value: object
) -> None:
    runtime = _runtime(PARTICLES)
    runtime.advance_to(TICK * 5)
    checkpoint = runtime.checkpoint()
    assert runtime.checkpoint_compatibility(checkpoint).compatible
    incompatible = dataclasses.replace(checkpoint, **{field: value})
    assert not runtime.checkpoint_compatibility(incompatible).compatible


def test_invalid_checkpoint_falls_back_to_initial_state_replay() -> None:
    runtime = _runtime(PARTICLES)
    runtime.advance_to(TICK * 10)
    bad = dataclasses.replace(runtime.checkpoint(), semantic_digest="sha256:v1:bad")
    target = TICK * 20
    result = runtime.replay(target, checkpoint=bad)
    assert result.ok
    assert result.fallback_to_initial_state
    direct = _runtime(PARTICLES)
    direct.advance_to(target)
    assert runtime.state_digest() == direct.state_digest()


def test_device_checkpoint_binds_to_lifetime_and_recreation_epoch() -> None:
    lifetime = temporal.DeviceLifetime("device-1", 0)
    runtime = temporal.TemporalRuntime(
        nw03.load_graph(PARTICLES), device_lifetime=lifetime
    )
    runtime.advance_to(TICK * 3)
    checkpoint = runtime.checkpoint(residency="device")
    assert runtime.checkpoint_compatibility(checkpoint).compatible
    runtime.reinitialize_device(temporal.DeviceLifetime("device-1", 1))
    assert not runtime.checkpoint_compatibility(checkpoint).compatible
    with pytest.raises(temporal.CheckpointError):
        runtime.restore_checkpoint(checkpoint)
    runtime.confirm_preview_reinitialization()
    with pytest.raises(temporal.CheckpointError):
        runtime.restore_checkpoint(checkpoint, allow_device_replay=True)


def test_replay_error_identifies_operator_and_exact_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(PARTICLES)
    original = temporal.step_history_state

    def fail(spec, node, previous, simulation_tick):
        if simulation_tick == 3:
            raise ValueError("synthetic failure")
        return original(spec, node, previous, simulation_tick)

    monkeypatch.setattr(temporal, "step_history_state", fail)
    with pytest.raises(temporal.ReplayError) as excinfo:
        runtime.advance_to(TICK * 5)
    error = excinfo.value
    assert error.operator_id == "nw.particles.system"
    assert error.node_id == "particles"
    assert error.simulation_tick == 3
    assert runtime.simulation_tick == 2


def test_loop_reset_restarts_declared_initial_state() -> None:
    runtime = temporal.TemporalRuntime(
        nw03.load_graph(FEEDBACK),
        events=(temporal.TemporalEvent("loop", 800, {}),),
    )
    loop_ticks = runtime.loop_ticks
    assert loop_ticks is not None
    runtime.advance_to(loop_ticks - TICK)
    before = runtime.state_digest()
    runtime.advance_to(loop_ticks)
    assert runtime.loop_generation == 1
    assert runtime.simulation_tick * TICK == loop_ticks
    # The state is the declared initial state again.
    fresh = temporal.TemporalRuntime(nw03.load_graph(FEEDBACK))
    assert runtime.state_digest() != before
    assert runtime.state("feedback").values == fresh.state("feedback").values


def test_declared_zero_initial_feedback_state_is_zero() -> None:
    runtime = _runtime(FEEDBACK)
    values = runtime.state("feedback").values
    assert set(values) == {0.0}


def test_loop_reset_policy_is_per_node(tmp_path: Path) -> None:
    text = (
        "nwgraph 1\n"
        "profile nw.common.d3d12.fl12_0.sm6_0.v1 "
        "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a\n"
        "setting nw09_loop_ticks 800\n"
        "node particles nw.particles.system@1.0.0 frequency=history\n"
        "  param count 2\n"
        "  param seed 1\n"
        "  param gravity -0.5\n"
        "  param initial_velocity 1.0\n"
        "  param lifetime_ticks 800\n"
        "  param width 8\n"
        "  param height 8\n"
        '  history state-type nw.particles.v1 initial "mode=seeded;count=2;seed=1" '
        "reset on-loop step-rate 120\n"
        "node feedback nw.feedback.image@1.0.0 frequency=history\n"
        "  param width 2\n"
        "  param height 2\n"
        "  param seed 2\n"
        "  param decay 0.9\n"
        "  param gain 0.2\n"
        '  history state-type nw.feedback.image.v1 initial "mode=seeded;width=2;height=2;seed=2" '
        "reset never step-rate 120\n"
        "node screen nw.render.present@1.0.0 frequency=frame residency=gpu\n"
        "  input image feedback.out\n"
    )
    graph = tmp_path / "two.nwgraph"
    graph.write_text(text, encoding="utf-8")
    runtime = temporal.TemporalRuntime(nw03.load_graph(graph))
    runtime.advance_to(400)
    feedback_before = runtime.state("feedback").values
    runtime.advance_to(800)
    assert runtime.loop_generation == 1
    fresh = temporal.TemporalRuntime(nw03.load_graph(graph))
    assert runtime.state("particles").values == fresh.state("particles").values
    # The non-reset node is still stepped through the loop-boundary tick.
    assert runtime.state("feedback").values != feedback_before
    assert runtime.state("feedback").simulation_tick == 2
    checkpoint = runtime.checkpoint()
    assert all(
        state["simulation_tick"] == runtime.simulation_tick
        for state in checkpoint.states
    )
    restored = temporal.TemporalRuntime(nw03.load_graph(graph))
    restored.restore_checkpoint(checkpoint)
    assert restored.state_digest() == runtime.state_digest()


def test_seek_reset_policy_is_per_node(tmp_path: Path) -> None:
    text = (
        "nwgraph 1\n"
        "profile nw.common.d3d12.fl12_0.sm6_0.v1 "
        "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a\n"
        "node particles nw.particles.system@1.0.0 frequency=history\n"
        "  param count 2\n"
        "  param seed 1\n"
        "  param gravity -0.5\n"
        "  param initial_velocity 1.0\n"
        "  param lifetime_ticks 800\n"
        "  param width 8\n"
        "  param height 8\n"
        '  history state-type nw.particles.v1 initial "mode=seeded;count=2;seed=1" '
        "reset on-seek step-rate 120\n"
        "node feedback nw.feedback.image@1.0.0 frequency=history\n"
        "  param width 2\n"
        "  param height 2\n"
        "  param seed 2\n"
        "  param decay 0.9\n"
        "  param gain 0.2\n"
        '  history state-type nw.feedback.image.v1 initial "mode=seeded;width=2;height=2;seed=2" '
        "reset never step-rate 120\n"
        "node screen nw.render.present@1.0.0 frequency=frame residency=gpu\n"
        "  input image feedback.out\n"
    )
    graph = tmp_path / "seek-policies.nwgraph"
    graph.write_text(text, encoding="utf-8")
    runtime = temporal.TemporalRuntime(nw03.load_graph(graph))
    runtime.advance_to(400 * 3)
    feedback_before = runtime.state("feedback").values
    assert runtime.seek(0).ok
    fresh = temporal.TemporalRuntime(nw03.load_graph(graph))
    assert runtime.state("particles").values == fresh.state("particles").values
    assert runtime.state("feedback").values == feedback_before
    checkpoint = runtime.checkpoint()
    restored = temporal.TemporalRuntime(nw03.load_graph(graph))
    restored.restore_checkpoint(checkpoint)
    assert restored.state_digest() == runtime.state_digest()
    # The manual/never state is not stepped as if its old value were tick
    # zero; it stays frozen until replay reaches its recorded tick.
    runtime.advance_to(400 * 3)
    assert runtime.state("feedback").values == feedback_before
    runtime.advance_to(400 * 4)
    assert runtime.state("feedback").values != feedback_before


@pytest.mark.parametrize(
    "name,path",
    [("particles", PARTICLES), ("feedback", FEEDBACK)],
)
def test_fixture_golden_states_match_fixed_ticks(name: str, path: Path) -> None:
    golden = json.loads(
        (REPO_ROOT / "fixtures" / "nw09" / "expected_state.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = _runtime(path)
    for entry in golden["fixtures"][name]:
        runtime.advance_to(entry["tick"])
        assert runtime.state_digest() == entry["state_digest"]
        assert runtime.simulation_tick == entry["simulation_tick"]

