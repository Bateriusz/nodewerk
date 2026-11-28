"""Single preregistered NW-13 Work Graphs workload and decision contract."""
from __future__ import annotations

from fractions import Fraction
from functools import cache
import math
from typing import Any

CRITERION = "median GPU dispatch time below 95% of conventional, exact output, backing allocation at most 32 MiB"
THRESHOLD_PERCENT = 95
CANDIDATES = 1024
TICK = 148800
SAMPLES_PER_ARM = 9
WARMUP_PER_ARM = 1
MAX_BACKING_BYTES = 32 * 1024 * 1024
UINT64_MAX = (1 << 64) - 1
MASK32 = (1 << 32) - 1


class ObservationError(ValueError):
    """Raw measurements cannot support a decision."""


def _u32(value: int) -> int:
    return value & MASK32


@cache
def oracle_hash() -> int:
    """Independently checksum all CPU-oracle values in the declared workload."""
    def mix(value: int) -> int:
        value = _u32((value ^ (value >> 16)) * 0x7FEB352D)
        value = _u32((value ^ (value >> 15)) * 0x846CA68B)
        return _u32(value ^ (value >> 16))

    result = 2166136261
    for index in range(CANDIDATES):
        value = 0
        if mix(index ^ TICK) & 7 < 3:
            value = mix(index + TICK)
            for step in range(24):
                value = mix(value ^ step)
        result = _u32((result ^ value) * 16777619)
    return result


def median_decision(conventional: int | float, work_graph: int | float) -> str:
    for value in (conventional, work_graph):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ObservationError("dispatch medians must be finite and positive")
    # Exact for native integer timestamps, including the strict 95% boundary.
    return "adopt" if Fraction(str(work_graph)) * 100 < Fraction(str(conventional)) * THRESHOLD_PERCENT else "reject"


def integer(value: Any, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ObservationError(f"{label} is outside its integer bounds")
    return value


def check_oracle(value: dict) -> None:
    if not isinstance(value, dict):
        raise ObservationError("missing per-dispatch oracle comparison")
    integer(value.get("oracle_slots_checked"), CANDIDATES, CANDIDATES, "oracle slots")
    expected = oracle_hash()
    integer(value.get("output_hash"), expected, expected, "oracle output hash")


def recompute(observation: dict, *, conventional_shader_bytes: int, work_graph_shader_bytes: int) -> dict:
    """Use raw paired ticks, never launcher-selected medians or labels."""
    if observation.get("status") != "measured":
        raise ObservationError("native run did not produce a completed measurement")
    for label, expected in (
        ("candidates", CANDIDATES), ("tick", TICK), ("samples_per_arm", SAMPLES_PER_ARM),
        ("conventional_shader_bytes", conventional_shader_bytes),
        ("work_graph_shader_bytes", work_graph_shader_bytes),
    ):
        integer(observation.get(label), expected, expected, label)
    integer(observation.get("work_graph_tier"), 1, 255, "Work Graphs tier")
    integer(observation.get("shader_model"), 0x68, 0x6F, "queried shader model")
    frequency = integer(observation.get("timestamp_frequency_hz"), 1, UINT64_MAX, "timestamp frequency")
    allocated = integer(observation.get("backing_bytes"), 0, MAX_BACKING_BYTES, "backing allocation")
    required = integer(observation.get("backing_required_bytes"), 0, allocated, "required backing")
    if (allocated == 0) != (required == 0):
        raise ObservationError("backing reservation and allocation disagree")
    pairs = observation.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != SAMPLES_PER_ARM:
        raise ObservationError("raw matched sample pairs are missing")
    arms = ("conventional", "work_graph")
    series: dict[str, list[int]] = {name: [] for name in arms}
    last_end = 0

    def consume(row: dict, order: tuple[str, str], *, measured: bool) -> None:
        nonlocal last_end
        if not isinstance(row, dict):
            raise ObservationError("missing sample pair")
        for arm in order:
            sample = row.get(arm)
            check_oracle(sample)
            begin = integer(sample.get("begin_ticks"), last_end, UINT64_MAX - 1, "dispatch start")
            end = integer(sample.get("end_ticks"), begin + 1, UINT64_MAX, "dispatch end")
            last_end = end
            if measured:
                series[arm].append(end - begin)

    consume(observation.get("warmup"), arms, measured=False)
    for index, row in enumerate(pairs):
        order = arms if index % 2 == 0 else arms[::-1]
        if not isinstance(row, dict) or type(row.get("pair")) is not int or row["pair"] != index or row.get("first") != order[0]:
            raise ObservationError("paired dispatch order does not match the prespecified order")
        consume(row, order, measured=True)
    medians = {name: sorted(values)[SAMPLES_PER_ARM // 2] for name, values in series.items()}
    typical, variant = medians["conventional"], medians["work_graph"]
    return {
        "decision": median_decision(typical, variant),
        "satisfies_completion": True,
        "criterion": CRITERION,
        "dispatch_median_ticks": medians,
        "dispatch_median_us": {name: ticks * 1_000_000 / frequency for name, ticks in medians.items()},
        "measured_ratio": variant / typical,
        "backing_bytes": allocated,
        "all_output_slots_match_oracle": True,
    }
