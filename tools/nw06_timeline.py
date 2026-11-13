#!/usr/bin/env python3
"""NW-06 integer-tick timeline, bounded curves, and event cursor semantics.

The timeline is deliberately audio-independent.  A clock-provider protocol is
reserved for a later audio implementation, but no audio library is imported or
linked.  The authoritative time unit is one integer tick at 48,000 ticks per
second.  Frame stepping is rational and camera/curve sampling takes an absolute
tick, so a stateless scene does not depend on display frame rate or accumulated
floating-point deltas.
"""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Callable, Iterable, Protocol, Sequence, runtime_checkable


TICKS_PER_SECOND = 48_000
NANOSECONDS_PER_SECOND = 1_000_000_000
MAX_TICKS = (1 << 64) - 1


class TimelineError(ValueError):
    """Raised for malformed timelines, curves, events, or overflow."""


# Compatibility names used by consumers that distinguish overflow from other
# malformed-input diagnostics.
TickConversionError = TimelineError
TickOverflowError = TimelineError
CurveValidationError = TimelineError
EventValidationError = TimelineError


def _error(code: str, message: str, **fields: Any) -> TimelineError:
    diagnostic = {
        "category": "timeline_validation",
        "severity": "error",
        "code": code,
        "message": message,
    }
    diagnostic.update(fields)
    exc = TimelineError(message)
    exc.diagnostics = [diagnostic]  # type: ignore[attr-defined]
    return exc


def _as_fraction(value: float | int | Fraction | Decimal | str) -> Fraction:
    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, Decimal):
        result = Fraction(value)
    elif isinstance(value, int):
        result = Fraction(value, 1)
    elif isinstance(value, str):
        try:
            result = Fraction(Decimal(value))
        except (InvalidOperation, ValueError) as exc:
            raise _error("invalid_time", f"cannot parse time '{value}'") from exc
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise _error("non_finite_time", "time must be finite")
        # Decimal spelling makes common decimal inputs exact and independent of
        # binary formatting noise while preserving the caller's intended value.
        result = Fraction(Decimal(str(value)))
    else:
        raise _error("invalid_time", f"unsupported time type {type(value).__name__}")
    if result < 0:
        raise _error("negative_time", "time must be non-negative")
    return result


def checked_seconds_to_ticks(
    seconds: float | int | Fraction | Decimal | str,
) -> int:
    """Convert seconds to integer ticks with checked overflow."""
    value = _as_fraction(seconds) * TICKS_PER_SECOND
    ticks = value.numerator // value.denominator
    if ticks > MAX_TICKS:
        raise _error("tick_overflow", f"time {seconds!r} exceeds the uint64 tick range")
    return ticks


def checked_ticks_to_seconds(ticks: int) -> float:
    if not 0 <= int(ticks) <= MAX_TICKS:
        raise _error("tick_range", f"tick {ticks} is outside the uint64 range")
    return int(ticks) / TICKS_PER_SECOND


def checked_ticks_from_monotonic_ns(nanoseconds: int) -> int:
    """Checked integer conversion from a monotonic clock's nanoseconds."""
    if not isinstance(nanoseconds, int):
        raise _error("invalid_clock_value", "monotonic nanoseconds must be an integer")
    if nanoseconds < 0:
        raise _error("negative_clock_value", "monotonic nanoseconds must be non-negative")
    numerator = nanoseconds * TICKS_PER_SECOND
    if numerator // NANOSECONDS_PER_SECOND > MAX_TICKS:
        raise _error("tick_overflow", "monotonic clock value exceeds the tick range")
    return numerator // NANOSECONDS_PER_SECOND


def checked_add_ticks(left: int, right: int) -> int:
    if left < 0 or right < 0 or left > MAX_TICKS or right > MAX_TICKS:
        raise _error("tick_range", "ticks must be non-negative uint64 values")
    result = left + right
    if result > MAX_TICKS:
        raise _error("tick_overflow", f"{left} + {right} exceeds the tick range")
    return result


def checked_sub_ticks(left: int, right: int) -> int:
    if left < 0 or right < 0 or left > MAX_TICKS or right > MAX_TICKS:
        raise _error("tick_range", "ticks must be non-negative uint64 values")
    if right > left:
        raise _error("negative_duration", f"{left} - {right} is negative")
    return left - right


def duration_ticks(
    seconds: float | int | Fraction | Decimal | str,
    *,
    tick_rate: int = TICKS_PER_SECOND,
) -> int:
    if tick_rate <= 0:
        raise _error("invalid_tick_rate", "tick rate must be positive")
    value = _as_fraction(seconds) * tick_rate
    ticks = value.numerator // value.denominator
    if ticks > MAX_TICKS:
        raise _error("duration_overflow", "duration exceeds the tick range")
    return ticks


@runtime_checkable
class ClockProvider(Protocol):
    """Audio-independent clock seam reserved for later synchronization."""

    @property
    def tick_rate(self) -> int: ...

    def now_ticks(self) -> int: ...

    def now_seconds(self) -> float: ...


@dataclass(frozen=True)
class MonotonicClockProvider:
    """Monotonic clock backed only by the standard library."""

    tick_rate: int = TICKS_PER_SECOND
    nanosecond_source: Callable[[], int] = time.monotonic_ns

    def now_ticks(self) -> int:
        nanoseconds = int(self.nanosecond_source())
        if self.tick_rate != TICKS_PER_SECOND:
            value = nanoseconds * self.tick_rate
            ticks = value // NANOSECONDS_PER_SECOND
            if ticks > MAX_TICKS:
                raise _error("tick_overflow", "monotonic clock exceeds the tick range")
            return ticks
        return checked_ticks_from_monotonic_ns(nanoseconds)

    def now_seconds(self) -> float:
        return self.now_ticks() / self.tick_rate


@dataclass
class FakeClockProvider:
    """Deterministic clock provider for tests and offline capture."""

    ticks: int = 0
    tick_rate: int = TICKS_PER_SECOND

    def now_ticks(self) -> int:
        return self.ticks

    def now_seconds(self) -> float:
        return self.ticks / self.tick_rate

    def advance_ticks(self, ticks: int) -> int:
        self.ticks = checked_add_ticks(self.ticks, ticks)
        return self.ticks


@dataclass
class RationalFrameStep:
    """Exact rational capture cadence expressed in integer ticks per frame.

    ``tick_for_frame`` is pure.  ``advance`` also carries the absolute frame
    index reached by the previous sequential call so repeated stepping cannot
    drop a fractional tick.  An explicit ``frame_index`` makes the operation
    stateless for callers that seek or evaluate directly.
    """

    numerator: int
    denominator: int = 1
    _next_frame_index: int = field(
        default=0, init=False, repr=False, compare=False
    )
    _last_advance_tick: int | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.denominator == 0:
            raise _error("invalid_frame_step", "frame-step denominator must be non-zero")
        if self.numerator <= 0 or self.denominator < 0:
            raise _error("invalid_frame_step", "frame step must be positive")

    @property
    def ticks_per_frame(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def tick_for_frame(self, frame_index: int) -> int:
        if frame_index < 0:
            raise _error("invalid_frame_index", "frame index must be non-negative")
        value = self.numerator * frame_index
        ticks = value // self.denominator
        if ticks > MAX_TICKS:
            raise _error("tick_overflow", "frame index exceeds the tick range")
        return ticks

    def remainder_for_frame(self, frame_index: int) -> int:
        if frame_index < 0:
            raise _error("invalid_frame_index", "frame index must be non-negative")
        return (self.numerator * frame_index) % self.denominator

    def advance(
        self,
        current_tick: int,
        frames: int = 1,
        *,
        frame_index: int | None = None,
    ) -> int:
        """Advance a tick position.

        A rational cadence cannot be reconstructed from the floored tick after
        a partial step.  Callers stepping a capture must therefore pass the
        absolute destination frame index via ``frame_index`` (or use
        :class:`RationalFrameCadence`).  Without it this method is an
        independent single-step helper and intentionally starts from
        ``current_tick`` rather than carrying hidden mutable state.
        """
        return self.advance_to(
            current_tick, frames, frame_index=frame_index
        )

    def advance_to(
        self,
        current_tick: int,
        frames: int = 1,
        *,
        frame_index: int | None = None,
    ) -> int:
        """Advance and preserve the exact rational cadence.

        When ``frame_index`` is supplied, the result is exactly
        ``tick_for_frame(frame_index + frames)``.  This keeps repeated capture
        stepping identical to direct rational evaluation for non-integral
        ticks-per-frame values.
        """
        if frames < 0:
            raise _error("invalid_frame_index", "frame count must be non-negative")
        if frame_index is not None:
            if frame_index < 0:
                raise _error("invalid_frame_index", "frame index must be non-negative")
            destination = frame_index + frames
        elif (
            self._last_advance_tick is not None
            and current_tick == self._last_advance_tick
        ):
            destination = self._next_frame_index + frames
        elif current_tick == 0:
            destination = frames
        else:
            # Restart from an arbitrary absolute tick.  The first frame whose
            # floored tick reaches it is the unique exact reconstruction point
            # for cadences at or above one tick per frame.
            destination = (
                (current_tick * self.denominator + self.numerator - 1)
                // self.numerator
            ) + frames
        result = self.tick_for_frame(destination)
        self._next_frame_index = destination
        self._last_advance_tick = result
        return result


@dataclass
class RationalFrameCadence:
    """Stateful exact rational capture cadence.

    The state is the absolute frame index, never a floating-point or floored
    tick delta.  Repeated ``advance()`` calls therefore cannot accumulate
    cadence error and always agree with ``tick_for_frame``.
    """

    step: RationalFrameStep
    frame_index: int = 0

    def __post_init__(self) -> None:
        if self.frame_index < 0:
            raise _error("invalid_frame_index", "frame index must be non-negative")

    @property
    def tick(self) -> int:
        return self.step.tick_for_frame(self.frame_index)

    def advance(self, frames: int = 1) -> int:
        if frames < 0:
            raise _error("invalid_frame_index", "frame count must be non-negative")
        self.frame_index += frames
        return self.step.tick_for_frame(self.frame_index)

    def seek(self, frame_index: int) -> int:
        if frame_index < 0:
            raise _error("invalid_frame_index", "frame index must be non-negative")
        self.frame_index = frame_index
        return self.step.tick_for_frame(frame_index)


@dataclass(frozen=True)
class CurveKey:
    tick: int
    value: float


def _validate_curve_keys(keys: Sequence[CurveKey]) -> tuple[CurveKey, ...]:
    if not keys:
        raise _error("empty_curve", "curve must contain at least one key")
    previous_tick = -1
    for key in keys:
        if not isinstance(key.tick, int) or key.tick < 0 or key.tick > MAX_TICKS:
            raise _error("invalid_curve_tick", f"curve tick {key.tick!r} is invalid")
        if key.tick <= previous_tick:
            raise _error("invalid_curve_range", "curve ticks must be strictly increasing")
        if not math.isfinite(key.value):
            raise _error("non_finite_curve_value", "curve values must be finite")
        previous_tick = key.tick
    return tuple(keys)


def sample_curve(
    keys: Sequence[CurveKey],
    tick: int,
    *,
    interpolation: str = "linear",
) -> float:
    checked = _validate_curve_keys(keys)
    if tick < 0 or tick > MAX_TICKS:
        raise _error("invalid_curve_tick", f"sample tick {tick!r} is invalid")
    if interpolation not in {"linear", "step"}:
        raise _error("invalid_interpolation", f"unsupported curve interpolation '{interpolation}'")
    if tick <= checked[0].tick:
        return checked[0].value
    if tick >= checked[-1].tick:
        return checked[-1].value
    ticks = [key.tick for key in checked]
    index = bisect.bisect_right(ticks, tick) - 1
    left = checked[index]
    right = checked[index + 1]
    if interpolation == "step":
        return left.value
    fraction = (tick - left.tick) / (right.tick - left.tick)
    return left.value + (right.value - left.value) * fraction


@dataclass(frozen=True)
class Curve:
    keys: tuple[CurveKey, ...]
    interpolation: str = "linear"
    value_min: float | None = None
    value_max: float | None = None

    def __post_init__(self) -> None:
        checked = _validate_curve_keys(self.keys)
        object.__setattr__(self, "keys", checked)
        if self.interpolation not in {"linear", "step"}:
            raise _error("invalid_interpolation", f"unsupported interpolation '{self.interpolation}'")
        minimum = (
            min(key.value for key in checked)
            if self.value_min is None
            else self.value_min
        )
        maximum = (
            max(key.value for key in checked)
            if self.value_max is None
            else self.value_max
        )
        object.__setattr__(self, "value_min", minimum)
        object.__setattr__(self, "value_max", maximum)
        if not math.isfinite(minimum) or not math.isfinite(maximum):
            raise _error("invalid_curve_range", "curve bounds must be finite")
        if minimum > maximum:
            raise _error("invalid_curve_range", "curve minimum exceeds maximum")
        for key in checked:
            if not minimum <= key.value <= maximum:
                raise _error(
                    "curve_value_out_of_range",
                    f"curve value {key.value} is outside [{minimum}, {maximum}]",
                )

    def sample(self, tick: int) -> float:
        return sample_curve(self.keys, tick, interpolation=self.interpolation)


@dataclass(frozen=True)
class QuantizationRecord:
    original: tuple[float, ...]
    quantized: tuple[float, ...]
    bits: int | None
    step: float
    maximum_absolute_error: float
    declared_tolerance: float
    within_tolerance: bool
    full_precision: bool

    def recover_full_precision(self) -> tuple[float, ...]:
        return self.original

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": list(self.original),
            "quantized": list(self.quantized),
            "bits": self.bits,
            "step": self.step,
            "maximum_absolute_error": self.maximum_absolute_error,
            "declared_tolerance": self.declared_tolerance,
            "within_tolerance": self.within_tolerance,
            "full_precision_escape_hatch": self.full_precision,
            "recoverable": True,
        }


@dataclass(frozen=True)
class QuantizedCurve:
    curve: Curve
    record: QuantizationRecord

    @property
    def maximum_error(self) -> float:
        return self.record.maximum_absolute_error

    def sample(self, tick: int) -> float:
        if self.record.full_precision or self.record.bits is None:
            return self.curve.sample(tick)
        # The GPU stores the quantized key values.  Sampling must interpolate
        # those encoded values rather than re-quantizing the interpolated
        # original curve; the two differ for non-constant signed curves (for
        # example 0 -> 1 with one bit), which is exactly what the recorded
        # maximum error is meant to describe.
        quantized_keys = tuple(
            CurveKey(key.tick, value)
            for key, value in zip(
                self.curve.keys, self.record.quantized
            )
        )
        return sample_curve(
            quantized_keys,
            tick,
            interpolation=self.curve.interpolation,
        )

    def recover_full_precision(self) -> Curve:
        return self.curve


def quantize_curve(
    curve: Curve,
    *,
    bits: int | None = 10,
    tolerance: float = 0.0,
    full_precision: bool = False,
) -> QuantizedCurve:
    """Quantize curve values and record the measured maximum error.

    ``bits=None`` and ``full_precision=True`` are explicit escape hatches.  The
    source curve is retained and always recoverable.
    """
    if full_precision or bits is None:
        record = QuantizationRecord(
            original=tuple(key.value for key in curve.keys),
            quantized=tuple(key.value for key in curve.keys),
            bits=None,
            step=0.0,
            maximum_absolute_error=0.0,
            declared_tolerance=tolerance,
            within_tolerance=tolerance >= 0.0,
            full_precision=True,
        )
        return QuantizedCurve(curve=curve, record=record)
    if not isinstance(bits, int) or not 1 <= bits <= 24:
        raise _error("invalid_quantization", "quantization bits must be in 1..24")
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise _error("invalid_quantization", "quantization tolerance must be finite and non-negative")
    span = curve.value_max - curve.value_min
    levels = (1 << bits) - 1
    step = span / levels if levels else 0.0
    original = tuple(key.value for key in curve.keys)
    quantized: list[float] = []
    maximum_error = 0.0
    for value in original:
        if step == 0.0:
            quantized_value = curve.value_min
        else:
            quantized_value = curve.value_min + round((value - curve.value_min) / step) * step
        quantized_value = min(curve.value_max, max(curve.value_min, quantized_value))
        maximum_error = max(maximum_error, abs(quantized_value - value))
        quantized.append(quantized_value)
    if maximum_error > tolerance:
        raise _error(
            "curve_quantization_tolerance_exceeded",
            f"maximum error {maximum_error} exceeds tolerance {tolerance}",
        )
    record = QuantizationRecord(
        original=original,
        quantized=tuple(quantized),
        bits=bits,
        step=step,
        maximum_absolute_error=maximum_error,
        declared_tolerance=tolerance,
        within_tolerance=True,
        full_precision=False,
    )
    return QuantizedCurve(curve=curve, record=record)


@dataclass(frozen=True)
class CameraCurve:
    """Absolute-tick camera path.  No frame-rate state is retained."""

    angle_curve: Curve
    radius_curve: Curve
    height_curve: Curve
    fov_degrees: float = 60.0
    aspect: float = 16.0 / 9.0
    near: float = 0.1
    far: float = 1000.0

    def sample(self, tick: int) -> dict[str, float]:
        if not (math.isfinite(self.fov_degrees) and 1.0 <= self.fov_degrees <= 179.0):
            raise _error("invalid_camera", "camera fov is outside 1..179 degrees")
        if not (math.isfinite(self.aspect) and self.aspect > 0.0):
            raise _error("invalid_camera", "camera aspect must be positive")
        if not (math.isfinite(self.near) and math.isfinite(self.far) and 0.0 < self.near < self.far):
            raise _error("invalid_camera", "camera near/far range is invalid")
        return {
            "tick": float(tick),
            "angle_radians": self.angle_curve.sample(tick),
            "radius": self.radius_curve.sample(tick),
            "height": self.height_curve.sample(tick),
            "fov_degrees": self.fov_degrees,
            "aspect": self.aspect,
            "near": self.near,
            "far": self.far,
        }


@dataclass(frozen=True, order=True)
class Event:
    tick: int
    event_id: int
    kind: str = "event"
    payload: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tick, int) or self.tick < 0 or self.tick > MAX_TICKS:
            raise _error("invalid_event_tick", f"event tick {self.tick!r} is invalid")
        if not isinstance(self.event_id, int) or self.event_id < 0:
            raise _error("invalid_event_id", "event id must be a non-negative integer")
        if not self.kind:
            raise _error("invalid_event_kind", "event kind must be non-empty")
        object.__setattr__(
            self,
            "payload",
            tuple(sorted(self.payload, key=lambda item: item[0])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "event_id": self.event_id,
            "kind": self.kind,
            "payload": {name: value for name, value in self.payload},
        }


def ordered_events(events: Iterable[Event]) -> tuple[Event, ...]:
    """Validate unique IDs and return events in (tick, event_id) order."""
    event_list = list(events)
    event_list.sort(key=lambda event: (event.tick, event.event_id))
    ids = [event.event_id for event in event_list]
    if len(set(ids)) != len(ids):
        raise _error("duplicate_event_id", "event ids must be unique")
    for previous, current in zip(ids, ids[1:]):
        if current <= previous:
            raise _error(
                "unordered_event_ids",
                "event ids must be strictly increasing in tick/id order",
            )
    for previous, current in zip(event_list, event_list[1:]):
        if current.event_id < previous.event_id:
            raise _error(
                "unordered_event_ids",
                "event ids must increase with time",
            )
    return tuple(event_list)


class EventCursor:
    """Ordered event delivery over half-open intervals ``(previous, current]``.

    ``seek(tick)`` resets the cursor so the next advance delivers only events
    strictly after ``tick``.  ``seek(0, replay_from_start=True)`` explicitly
    requests a replay from the beginning.  Seeking never mutates timeline data
    or stateless scene state.
    """

    def __init__(
        self,
        events: Iterable[Event],
        *,
        previous_tick: int = 0,
    ) -> None:
        self.events = ordered_events(events)
        self._ticks = [event.tick for event in self.events]
        self.reset(previous_tick=previous_tick)

    @property
    def previous_tick(self) -> int:
        return self._previous_tick

    @property
    def current_tick(self) -> int:
        return self._previous_tick

    def _index_after(self, tick: int) -> int:
        return bisect.bisect_right(self._ticks, tick)

    def reset(self, *, previous_tick: int = 0) -> None:
        if not 0 <= previous_tick <= MAX_TICKS:
            raise _error("invalid_event_tick", "event cursor tick is invalid")
        self._previous_tick = previous_tick
        self._index = self._index_after(previous_tick)

    def seek(self, tick: int, *, replay_from_start: bool = False) -> tuple[Event, ...]:
        if not 0 <= tick <= MAX_TICKS:
            raise _error("invalid_event_tick", "seek tick is invalid")
        if replay_from_start:
            self._previous_tick = 0
            self._index = 0
            # A seek is not an advance: event delivery is explicitly requested
            # by the caller through replay_through().
            return ()
        self._previous_tick = tick
        self._index = self._index_after(tick)
        return ()

    def replay_through(self, tick: int) -> tuple[Event, ...]:
        self._previous_tick = 0
        self._index = 0
        return self.advance(tick)

    def replay(self, tick: int) -> tuple[Event, ...]:
        return self.replay_through(tick)

    def events_between(self, previous_tick: int, current_tick: int) -> tuple[Event, ...]:
        if previous_tick < 0 or current_tick < previous_tick:
            raise _error("invalid_event_interval", "event interval is invalid")
        start = bisect.bisect_right(self._ticks, previous_tick)
        end = bisect.bisect_right(self._ticks, current_tick)
        return self.events[start:end]

    def advance(self, current_tick: int) -> tuple[Event, ...]:
        if not 0 <= current_tick <= MAX_TICKS:
            raise _error("invalid_event_tick", "advance tick is invalid")
        if current_tick < self._previous_tick:
            raise _error(
                "event_time_reversal",
                "event cursor cannot move backwards; use seek/reset explicitly",
            )
        end = bisect.bisect_right(self._ticks, current_tick)
        delivered = self.events[self._index : end]
        self._index = end
        self._previous_tick = current_tick
        return delivered

    def pending(self) -> tuple[Event, ...]:
        return self.events[self._index :]


@dataclass(frozen=True)
class Timeline:
    duration: int
    frame_step: RationalFrameStep = field(
        default_factory=lambda: RationalFrameStep(1, 1)
    )
    tick_rate: int = TICKS_PER_SECOND

    def __post_init__(self) -> None:
        if self.tick_rate != TICKS_PER_SECOND:
            raise _error("invalid_tick_rate", "NW-06 fixes the timeline at 48,000 ticks/second")
        if self.duration < 0 or self.duration > MAX_TICKS:
            raise _error("invalid_duration", "timeline duration is invalid")

    def frame_tick(self, frame_index: int) -> int:
        tick = self.frame_step.tick_for_frame(frame_index)
        if tick > self.duration:
            raise _error("frame_outside_timeline", f"frame tick {tick} exceeds duration {self.duration}")
        return tick

    def capture_ticks(self) -> tuple[int, ...]:
        ticks: list[int] = []
        frame = 0
        while True:
            tick = self.frame_step.tick_for_frame(frame)
            if tick > self.duration:
                break
            ticks.append(tick)
            frame += 1
        return tuple(ticks)


def stateless_camera_samples(
    camera: CameraCurve,
    ticks: Sequence[int],
) -> tuple[dict[str, float], ...]:
    return tuple(camera.sample(tick) for tick in ticks)


class StatelessSceneEvaluator:
    """Absolute-tick evaluator used to prove seek/evaluation equivalence.

    No previous tick, frame delta, or accumulated integer remainder influences
    the returned state.  Event cursors are optional and are reset on seek.
    """

    def __init__(
        self,
        camera: CameraCurve,
        *,
        events: Iterable[Event] | None = None,
    ) -> None:
        self.camera = camera
        self.event_cursor = EventCursor(events or ())

    def evaluate(self, tick: int) -> dict[str, float]:
        return self.camera.sample(tick)

    def seek(self, tick: int) -> dict[str, float]:
        self.event_cursor.seek(tick)
        return self.evaluate(tick)


def seek_matches_evaluation(
    evaluator: StatelessSceneEvaluator,
    tick: int,
) -> bool:
    expected = evaluator.evaluate(tick)
    seeked = evaluator.seek(tick)
    return seeked == expected


__all__ = [
    "TICKS_PER_SECOND",
    "MAX_TICKS",
    "TimelineError",
    "TickConversionError",
    "TickOverflowError",
    "CurveValidationError",
    "EventValidationError",
    "ClockProvider",
    "MonotonicClockProvider",
    "FakeClockProvider",
    "RationalFrameStep",
    "RationalFrameCadence",
    "CurveKey",
    "Curve",
    "QuantizationRecord",
    "QuantizedCurve",
    "CameraCurve",
    "Event",
    "EventCursor",
    "Timeline",
    "checked_seconds_to_ticks",
    "checked_ticks_to_seconds",
    "checked_ticks_from_monotonic_ns",
    "checked_add_ticks",
    "checked_sub_ticks",
    "duration_ticks",
    "sample_curve",
    "quantize_curve",
    "ordered_events",
    "stateless_camera_samples",
    "StatelessSceneEvaluator",
    "seek_matches_evaluation",
]
