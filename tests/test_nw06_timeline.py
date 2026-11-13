from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw06_timeline as timeline  # noqa: E402


def test_fixed_tick_rate_and_checked_conversions() -> None:
    assert timeline.TICKS_PER_SECOND == 48_000
    assert timeline.checked_seconds_to_ticks("0.5") == 24_000
    assert timeline.checked_seconds_to_ticks(1) == 48_000
    assert timeline.checked_ticks_to_seconds(48_000) == 1.0
    assert timeline.checked_ticks_from_monotonic_ns(1_000_000_000) == 48_000
    with pytest.raises(timeline.TimelineError):
        timeline.checked_seconds_to_ticks(float("inf"))
    with pytest.raises(timeline.TimelineError):
        timeline.checked_seconds_to_ticks(-1)
    with pytest.raises(timeline.TimelineError):
        timeline.checked_add_ticks(timeline.MAX_TICKS, 1)
    with pytest.raises(timeline.TimelineError):
        timeline.duration_ticks("1e30")


def test_clock_provider_is_audio_independent_and_integer_backed() -> None:
    clock = timeline.FakeClockProvider()
    assert clock.now_ticks() == 0
    clock.advance_ticks(48_000)
    assert clock.now_seconds() == 1.0
    monotonic = timeline.MonotonicClockProvider(
        nanosecond_source=lambda: 2_000_000_000
    )
    assert monotonic.now_ticks() == 96_000
    assert isinstance(monotonic, timeline.ClockProvider)


def test_rational_frame_steps_do_not_accumulate_float_error() -> None:
    step = timeline.RationalFrameStep(1001, 30_000)
    assert step.tick_for_frame(30_000) == 1001
    assert step.tick_for_frame(60_000) == 2002
    assert step.advance(0, 30_000) == 1001
    with pytest.raises(timeline.TimelineError):
        timeline.RationalFrameStep(1, 0)


def test_rational_cadence_preserves_fractional_ticks_across_advances() -> None:
    step = timeline.RationalFrameStep(3, 2)
    cadence = timeline.RationalFrameCadence(step)
    assert cadence.advance() == 1
    assert cadence.advance() == 3
    assert cadence.tick == step.tick_for_frame(2)
    # The explicit frame index keeps a stateless caller exact as well.
    assert step.advance(0, 1, frame_index=0) == 1
    assert step.advance(1, 1, frame_index=1) == 3
    assert step.advance(0, 2, frame_index=0) == 3
    sequential = timeline.RationalFrameStep(3, 2)
    assert sequential.advance(0, 1) == 1
    assert sequential.advance(1, 1) == 3
    fresh = timeline.RationalFrameStep(3, 2)
    assert fresh.advance(5, frames=0) == 5
    assert fresh.advance(3, 1) == 4
    with pytest.raises(timeline.TimelineError):
        fresh.advance(-1)
    with pytest.raises(timeline.TimelineError):
        timeline.RationalFrameStep(3, 2).advance(5, 1)
    assert (
        timeline.RationalFrameStep(1, 3).advance(
            0, 1, frame_index=0
        )
        == 0
    )


def test_curve_sampling_quantization_bound_and_full_precision_escape() -> None:
    curve = timeline.Curve(
        (
            timeline.CurveKey(0, 0.0),
            timeline.CurveKey(24_000, 0.25),
            timeline.CurveKey(48_000, 1.0),
        ),
        value_min=0.0,
        value_max=1.0,
    )
    assert curve.sample(12_000) == pytest.approx(0.125)
    quantized = timeline.quantize_curve(curve, bits=8, tolerance=0.01)
    assert quantized.record.maximum_absolute_error <= 0.01
    assert quantized.record.within_tolerance
    assert quantized.recover_full_precision() is curve
    escape = timeline.quantize_curve(curve, full_precision=True)
    assert escape.maximum_error == 0.0
    assert escape.record.full_precision
    with pytest.raises(timeline.TimelineError):
        timeline.quantize_curve(curve, bits=4, tolerance=0.0)
    with pytest.raises(timeline.TimelineError):
        timeline.Curve(
            (timeline.CurveKey(0, 0.0),),
            value_min=1.0,
            value_max=0.0,
        )


def test_quantized_curve_samples_the_encoded_keys_not_the_original() -> None:
    curve = timeline.Curve(
        (timeline.CurveKey(0, 0.0), timeline.CurveKey(48_000, 1.0)),
        value_min=0.0,
        value_max=1.0,
    )
    quantized = timeline.quantize_curve(curve, bits=1, tolerance=0.0)
    assert quantized.record.maximum_absolute_error == 0.0
    assert quantized.sample(24_000) == pytest.approx(0.5)

    constant = timeline.Curve((timeline.CurveKey(0, 0.25),))
    constant_quantized = timeline.quantize_curve(
        constant, bits=4, tolerance=0.0
    )
    assert constant_quantized.sample(0) == pytest.approx(0.25)
    assert constant_quantized.sample(48_000) == pytest.approx(0.25)


def test_event_interval_is_previous_exclusive_current_inclusive() -> None:
    events = [
        timeline.Event(10, 1),
        timeline.Event(20, 2),
        timeline.Event(30, 3),
    ]
    cursor = timeline.EventCursor(events)
    assert [event.event_id for event in cursor.advance(10)] == [1]
    assert [event.event_id for event in cursor.advance(20)] == [2]
    assert cursor.advance(20) == ()
    assert [event.event_id for event in cursor.advance(30)] == [3]
    assert cursor.events_between(10, 20) == (events[1],)
    with pytest.raises(timeline.TimelineError):
        cursor.advance(29)


def test_seek_and_replay_reset_event_cursor_explicitly() -> None:
    events = [
        timeline.Event(10, 1),
        timeline.Event(20, 2),
        timeline.Event(30, 3),
    ]
    cursor = timeline.EventCursor(events)
    cursor.advance(15)
    cursor.seek(25)
    assert [event.event_id for event in cursor.advance(30)] == [3]
    cursor.seek(0, replay_from_start=True)
    assert [event.event_id for event in cursor.advance(20)] == [1, 2]
    assert [event.event_id for event in cursor.replay_through(30)] == [1, 2, 3]
    with pytest.raises(timeline.TimelineError):
        timeline.ordered_events([timeline.Event(1, 1), timeline.Event(2, 1)])


def test_stateless_camera_sample_depends_only_on_absolute_tick() -> None:
    angle = timeline.Curve(
        (timeline.CurveKey(0, 0.0), timeline.CurveKey(48_000, 6.2831853)),
        value_min=0.0,
        value_max=6.2831854,
    )
    radius = timeline.Curve(
        (timeline.CurveKey(0, 4.0),), value_min=0.0, value_max=10.0
    )
    height = timeline.Curve(
        (timeline.CurveKey(0, 1.0),), value_min=0.0, value_max=10.0
    )
    camera = timeline.CameraCurve(angle, radius, height)
    thirty = timeline.stateless_camera_samples(camera, [24_000] * 2)
    sixty = timeline.stateless_camera_samples(camera, [12_000, 24_000, 36_000])
    assert thirty[0] == camera.sample(24_000)
    assert sixty[1] == camera.sample(24_000)
    assert thirty[0] == thirty[1]
    evaluator = timeline.StatelessSceneEvaluator(
        camera, events=[timeline.Event(1_000, 1)]
    )
    assert timeline.seek_matches_evaluation(evaluator, 24_000)
