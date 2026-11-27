#!/usr/bin/env python3
"""NW-13 versioned instrument/sequence payload and deterministic offline audio.

The public authoring payload is ``nw13.instrument-sequence`` version 2.  It
compiles to the preserved, canonical NWAS v1 event container for the shared
PackagePlayer/procedural-audio path.  The richer waveform/envelope/filter/pan
reference is deterministic and independent of GPU capabilities; its sample
clock is the authoritative 48 kHz timeline.

This module never changes NWAS v1: ``tools.audio_song.compile_song`` and
``decode_song`` remain the normative v1 reader/writer.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import audio_song


FORMAT = "nw13.instrument-sequence"
VERSION = 2
TICK_RATE_HZ = 48_000
MAX_NWAS_EVENTS = 64


class AudioError(ValueError):
    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _diagnostic(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result = {
        "category": "audio_validation",
        "severity": "error",
        "code": code,
        "message": message,
    }
    result.update(fields)
    return result


def _number(value: Any, name: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise AudioError(f"{name} must be finite")
    value = float(value)
    if minimum is not None and value < minimum:
        raise AudioError(f"{name} is below its minimum")
    if maximum is not None and value > maximum:
        raise AudioError(f"{name} is above its maximum")
    return value


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AudioError(f"{name} is outside [{minimum}, {maximum}]")
    return value


WAVEFORMS = {"sine", "square", "saw", "triangle", "noise"}
FILTER_TYPES = {"one_pole_lowpass"}
BUSES = {"music", "effect"}


@dataclass(frozen=True)
class Envelope:
    attack_ticks: int = 240
    decay_ticks: int = 960
    sustain: float = 0.7
    release_ticks: int = 2400

    def level(self, age_samples: int, total_samples: int, sample_rate: int) -> float:
        attack = max(1, int(self.attack_ticks * sample_rate / TICK_RATE_HZ))
        decay = max(1, int(self.decay_ticks * sample_rate / TICK_RATE_HZ))
        release = max(1, int(self.release_ticks * sample_rate / TICK_RATE_HZ))
        if age_samples < attack:
            return age_samples / attack
        if age_samples < attack + decay:
            ratio = (age_samples - attack) / decay
            return 1.0 + (self.sustain - 1.0) * ratio
        remaining = total_samples - age_samples
        if remaining < release:
            return self.sustain * max(0.0, remaining / release)
        return self.sustain


@dataclass(frozen=True)
class Instrument:
    id: str
    waveform: str
    seed: int
    gain: float
    pan: float
    envelope: Envelope
    filter_type: str
    cutoff_hz: float


@dataclass(frozen=True)
class Note:
    tick: int
    duration: int
    frequency_millihz: int
    velocity: float
    instrument: str
    bus: str
    id: str


@dataclass(frozen=True)
class AutomationKey:
    tick: int
    value: float


@dataclass
class Sequence:
    duration_ticks: int
    instruments: dict[str, Instrument]
    notes: tuple[Note, ...]
    automation: dict[str, tuple[AutomationKey, ...]]
    source: dict[str, Any]

    def automation_at(self, bus: str, tick: float) -> float:
        keys = self.automation.get(bus)
        if not keys:
            return 1.0
        if tick <= keys[0].tick:
            return keys[0].value
        if tick >= keys[-1].tick:
            return keys[-1].value
        for index in range(1, len(keys)):
            previous = keys[index - 1]
            current = keys[index]
            if tick <= current.tick:
                if current.tick == previous.tick:
                    return current.value
                alpha = (tick - previous.tick) / (current.tick - previous.tick)
                return previous.value + (current.value - previous.value) * alpha
        return keys[-1].value


def _compile_instrument(document: Mapping[str, Any]) -> Instrument:
    identifier = document.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise AudioError("instrument id must be a non-empty string")
    waveform = document.get("waveform", "sine")
    if waveform not in WAVEFORMS:
        raise AudioError(
            f"instrument {identifier} has unsupported waveform '{waveform}'",
            [_diagnostic("audio_invalid_instrument", "unsupported waveform", instrument_id=identifier)],
        )
    filter_doc = document.get("filter", {})
    if not isinstance(filter_doc, Mapping):
        raise AudioError(f"instrument {identifier}.filter must be an object")
    filter_type = filter_doc.get("type", "one_pole_lowpass")
    if filter_type not in FILTER_TYPES:
        raise AudioError(f"instrument {identifier} has unsupported filter '{filter_type}'")
    envelope_doc = document.get("envelope", {})
    if not isinstance(envelope_doc, Mapping):
        raise AudioError(f"instrument {identifier}.envelope must be an object")
    envelope = Envelope(
        attack_ticks=_integer(envelope_doc.get("attack_ticks", 240), "attack_ticks", minimum=0, maximum=3600 * TICK_RATE_HZ),
        decay_ticks=_integer(envelope_doc.get("decay_ticks", 960), "decay_ticks", minimum=0, maximum=3600 * TICK_RATE_HZ),
        sustain=_number(envelope_doc.get("sustain", 0.7), "sustain", minimum=0.0, maximum=1.0),
        release_ticks=_integer(envelope_doc.get("release_ticks", 2400), "release_ticks", minimum=0, maximum=3600 * TICK_RATE_HZ),
    )
    return Instrument(
        id=identifier,
        waveform=str(waveform),
        seed=_integer(document.get("seed", 0), "seed", minimum=0, maximum=4294967295),
        gain=_number(document.get("gain", 0.5), "gain", minimum=0.0, maximum=1.0),
        pan=_number(document.get("pan", 0.0), "pan", minimum=-1.0, maximum=1.0),
        envelope=envelope,
        filter_type=str(filter_type),
        cutoff_hz=_number(filter_doc.get("cutoff_hz", 12000.0), "cutoff_hz", minimum=20.0, maximum=96000.0),
    )


def compile_sequence(document: Mapping[str, Any]) -> Sequence:
    if not isinstance(document, Mapping):
        raise AudioError("sequence must be an object")
    if document.get("format") != FORMAT or document.get("version") != VERSION:
        raise AudioError(
            "sequence must use nw13.instrument-sequence version 2",
            [_diagnostic("audio_schema_version", "sequence must use nw13.instrument-sequence version 2")],
        )
    if document.get("tick_rate_hz", TICK_RATE_HZ) != TICK_RATE_HZ:
        raise AudioError("the authoritative audio tick rate is 48000 Hz")
    duration = _integer(document.get("duration_ticks"), "duration_ticks", minimum=1, maximum=TICK_RATE_HZ * 3600)
    instruments_doc = document.get("instruments", [])
    if not isinstance(instruments_doc, list) or not 1 <= len(instruments_doc) <= 64:
        raise AudioError("sequence requires 1..64 instruments")
    instruments: dict[str, Instrument] = {}
    for entry in instruments_doc:
        if not isinstance(entry, Mapping):
            raise AudioError("instrument entries must be objects")
        instrument = _compile_instrument(entry)
        if instrument.id in instruments:
            raise AudioError(f"duplicate instrument id '{instrument.id}'")
        instruments[instrument.id] = instrument
    notes: list[Note] = []
    tracks_doc = document.get("tracks", [])
    if not isinstance(tracks_doc, list):
        raise AudioError("tracks must be an array")
    for track in tracks_doc:
        if not isinstance(track, Mapping):
            raise AudioError("track entries must be objects")
        bus = track.get("bus", "music")
        if bus not in BUSES:
            raise AudioError(f"track bus '{bus}' is unsupported")
        notes_doc = track.get("notes", [])
        if not isinstance(notes_doc, list):
            raise AudioError("track notes must be an array")
        for index, note_doc in enumerate(notes_doc):
            if not isinstance(note_doc, Mapping):
                raise AudioError("note entries must be objects")
            instrument_id = note_doc.get("instrument")
            if instrument_id not in instruments:
                raise AudioError(f"note references unknown instrument '{instrument_id}'")
            tick = _integer(note_doc.get("tick"), "note.tick", minimum=0, maximum=duration - 1)
            note_duration = _integer(note_doc.get("duration"), "note.duration", minimum=1, maximum=duration - tick)
            frequency = _integer(note_doc.get("frequency_millihz"), "note.frequency_millihz", minimum=1, maximum=20_000_000)
            notes.append(
                Note(
                    tick=tick,
                    duration=note_duration,
                    frequency_millihz=frequency,
                    velocity=_number(note_doc.get("velocity", 1.0), "note.velocity", minimum=0.0, maximum=8.0),
                    instrument=str(instrument_id),
                    bus=str(bus),
                    id=str(note_doc.get("id", f"{track.get('id', 'track')}:{index}")),
                )
            )
    notes.sort(key=lambda note: (note.tick, note.id))
    automation: dict[str, tuple[AutomationKey, ...]] = {}
    for entry in document.get("automation", []):
        if not isinstance(entry, Mapping):
            raise AudioError("automation entries must be objects")
        bus = entry.get("bus", "music")
        if bus not in BUSES:
            raise AudioError(f"automation bus '{bus}' is unsupported")
        keys_doc = entry.get("keys", [])
        keys: list[AutomationKey] = []
        for key in keys_doc:
            if not isinstance(key, Mapping):
                raise AudioError("automation keys must be objects")
            keys.append(
                AutomationKey(
                    tick=_integer(key.get("tick"), "automation.tick", minimum=0, maximum=duration),
                    value=_number(key.get("value"), "automation.value", minimum=0.0, maximum=8.0),
                )
            )
        keys.sort(key=lambda key: key.tick)
        automation[str(bus)] = tuple(keys)
    sequence = Sequence(duration, instruments, tuple(notes), automation, dict(document))
    _validate_budget(sequence, document.get("budget", {}))
    return sequence


def _validate_budget(sequence: Sequence, budget: Any) -> None:
    if not isinstance(budget, Mapping):
        budget = {}
    max_voices = budget.get("max_voices", 64)
    if not isinstance(max_voices, int) or max_voices <= 0:
        raise AudioError("audio max_voices budget must be positive")
    # Sweep note boundaries to find the maximum simultaneous voice count.
    boundaries = sorted({note.tick for note in sequence.notes} | {note.tick + note.duration for note in sequence.notes})
    maximum = 0
    for tick in boundaries:
        active = sum(1 for note in sequence.notes if note.tick <= tick < note.tick + note.duration)
        maximum = max(maximum, active)
    if maximum > max_voices:
        raise AudioError(
            "audio voice overflow",
            [_diagnostic("audio_voice_overflow", "simultaneous voices exceed the declared budget", voices=maximum, max_voices=max_voices)],
        )
    memory_budget = budget.get("max_memory_bytes", 16 * 1024 * 1024)
    estimated = len(sequence.notes) * 128 + len(sequence.instruments) * 256 + 4096
    if not isinstance(memory_budget, int) or estimated > memory_budget:
        raise AudioError(
            "audio memory overflow",
            [_diagnostic("audio_memory_overflow", "audio payload exceeds the declared memory budget", bytes=estimated)],
        )


def compile_to_nwas(sequence: Sequence | Mapping[str, Any]) -> bytes:
    """Compile the versioned sequence to the preserved NWAS v1 container."""
    if not isinstance(sequence, Sequence):
        sequence = compile_sequence(sequence)
    if not sequence.notes:
        raise AudioError(
            "audio event underflow",
            [_diagnostic("audio_event_underflow", "NWAS v1 requires at least one event")],
        )
    if len(sequence.notes) > MAX_NWAS_EVENTS:
        raise AudioError(
            "audio event overflow",
            [_diagnostic("audio_event_overflow", "NWAS v1 accepts at most 64 events", events=len(sequence.notes))],
        )
    events = []
    previous_tick = 0
    for note in sequence.notes:
        if note.tick < previous_tick:
            raise AudioError("notes are not ordered")
        previous_tick = note.tick
        instrument = sequence.instruments[note.instrument]
        gain = max(1, min(32767, int(round(note.velocity * instrument.gain * 24000.0))))
        events.append(
            {
                "tick": note.tick,
                "duration": note.duration,
                "frequency_millihz": note.frequency_millihz,
                "gain": gain,
            }
        )
    return audio_song.compile_song(
        {"version": 1, "duration_ticks": sequence.duration_ticks, "events": events}
    )


def decode_nwas(payload: bytes) -> dict[str, Any]:
    """Explicitly preserve the NWAS v1 reader as the authoritative decoder."""
    return audio_song.decode_song(payload)


def _waveform(instrument: Instrument, phase: float, sample_index: int) -> float:
    if instrument.waveform == "sine":
        return math.sin(math.tau * phase)
    if instrument.waveform == "square":
        return 1.0 if phase < 0.5 else -1.0
    if instrument.waveform == "saw":
        return phase * 2.0 - 1.0
    if instrument.waveform == "triangle":
        return 1.0 - 4.0 * abs(phase - 0.5)
    if instrument.waveform == "noise":
        value = (sample_index + instrument.seed * 0x9E3779B9) & 0xFFFFFFFF
        value = (value ^ (value >> 16)) * 0x7FEB352D & 0xFFFFFFFF
        value = (value ^ (value >> 15)) * 0x846CA68B & 0xFFFFFFFF
        value ^= value >> 16
        return (value / 4294967295.0) * 2.0 - 1.0
    raise AudioError(f"unsupported waveform '{instrument.waveform}'")


def _pan_gains(pan: float) -> tuple[float, float]:
    angle = (pan + 1.0) * math.pi * 0.25
    return math.cos(angle), math.sin(angle)


def render_pcm(
    sequence: Sequence | Mapping[str, Any],
    *,
    sample_rate: int = TICK_RATE_HZ,
    first_sample: int = 0,
    sample_count: int | None = None,
    channels: int = 2,
) -> list[int]:
    """Render signed PCM16, identical across chunk partitions."""
    if not isinstance(sequence, Sequence):
        sequence = compile_sequence(sequence)
    if sample_rate != TICK_RATE_HZ:
        raise AudioError("the authoritative sample clock is 48000 Hz")
    if channels not in {1, 2}:
        raise AudioError("channels must be 1 or 2")
    if first_sample < 0:
        raise AudioError("first_sample must be non-negative")
    duration_samples = int(sequence.duration_ticks * sample_rate / TICK_RATE_HZ)
    if sample_count is None:
        sample_count = max(0, duration_samples - first_sample)
    if sample_count < 0 or first_sample + sample_count > duration_samples:
        raise AudioError("render range exceeds the sequence duration")
    voices = []
    for note in sequence.notes:
        start = int(note.tick * sample_rate / TICK_RATE_HZ)
        end = int((note.tick + note.duration) * sample_rate / TICK_RATE_HZ)
        if end <= first_sample or start >= first_sample + sample_count:
            continue
        instrument = sequence.instruments[note.instrument]
        voices.append(
            {
                "note": note,
                "instrument": instrument,
                "start": start,
                "end": max(start + 1, end),
                "left_gain": _pan_gains(instrument.pan)[0],
                "right_gain": _pan_gains(instrument.pan)[1],
                "filter_state": 0.0,
            }
        )
    # Reconstruct the bounded one-pole filter state for voices that started
    # before the requested chunk.  This makes an arbitrary chunk partition
    # sample-identical to a single continuous render.
    for voice in voices:
        start = voice["start"]
        if start >= first_sample:
            continue
        note = voice["note"]
        instrument = voice["instrument"]
        dt = 1.0 / sample_rate
        rc = 1.0 / (math.tau * instrument.cutoff_hz)
        alpha = dt / (rc + dt)
        state = 0.0
        for sample in range(start, first_sample):
            phase = (sample - start) * note.frequency_millihz / 1000.0 / sample_rate % 1.0
            raw = _waveform(instrument, phase, sample)
            state += alpha * (raw - state)
        voice["filter_state"] = state
    output: list[int] = []
    for frame in range(sample_count):
        sample = first_sample + frame
        left = 0.0
        right = 0.0
        for voice in voices:
            start = voice["start"]
            end = voice["end"]
            if sample < start or sample >= end:
                continue
            note = voice["note"]
            instrument = voice["instrument"]
            age = sample - start
            total = end - start
            phase = (age * note.frequency_millihz / 1000.0 / sample_rate) % 1.0
            raw = _waveform(instrument, phase, sample)
            envelope = instrument.envelope.level(age, total, sample_rate)
            dt = 1.0 / sample_rate
            rc = 1.0 / (math.tau * instrument.cutoff_hz)
            alpha = dt / (rc + dt)
            voice["filter_state"] += alpha * (raw - voice["filter_state"])
            state = voice["filter_state"]
            if not math.isfinite(state) or abs(state) > 1.0e6:
                raise AudioError(
                    "audio filter state overflow",
                    [_diagnostic("audio_filter_state_overflow", "filter state became non-finite or unbounded", note_id=note.id)],
                )
            bus = sequence.automation_at(note.bus, sample * TICK_RATE_HZ / sample_rate)
            amplitude = state * envelope * note.velocity * instrument.gain * bus
            left += amplitude * voice["left_gain"]
            right += amplitude * voice["right_gain"]
        left = max(-1.0, min(1.0, left))
        right = max(-1.0, min(1.0, right))
        if channels == 1:
            output.append(int(round((left + right) * 0.5 * 32767.0)))
        else:
            output.append(int(round(left * 32767.0)))
            output.append(int(round(right * 32767.0)))
    return output


def render_in_chunks(
    sequence: Sequence | Mapping[str, Any],
    chunk_sizes: Iterable[int],
    *,
    sample_rate: int = TICK_RATE_HZ,
    channels: int = 2,
) -> list[int]:
    """Render using explicit chunk partitions; must equal one-shot rendering."""
    if not isinstance(sequence, Sequence):
        sequence = compile_sequence(sequence)
    output: list[int] = []
    first = 0
    for size in chunk_sizes:
        if size <= 0:
            raise AudioError("chunk sizes must be positive")
        output.extend(render_pcm(sequence, sample_rate=sample_rate, first_sample=first, sample_count=size, channels=channels))
        first += size
    return output


def cue_samples(sequence: Sequence | Mapping[str, Any], *, sample_rate: int = TICK_RATE_HZ) -> list[dict[str, Any]]:
    if not isinstance(sequence, Sequence):
        sequence = compile_sequence(sequence)
    result = []
    for note in sequence.notes:
        result.append(
            {
                "id": note.id,
                "tick": note.tick,
                "sample": (note.tick * sample_rate + TICK_RATE_HZ - 1) // TICK_RATE_HZ,
                "bus": note.bus,
            }
        )
    return result


def audiovisual_alignment(
    sequence: Sequence | Mapping[str, Any],
    *,
    frame_rate: int = 60,
    sample_rate: int = TICK_RATE_HZ,
) -> dict[str, Any]:
    if not isinstance(sequence, Sequence):
        sequence = compile_sequence(sequence)
    samples_per_frame = sample_rate / frame_rate
    deltas = []
    for cue in cue_samples(sequence, sample_rate=sample_rate):
        frame_index = round(cue["tick"] * frame_rate / TICK_RATE_HZ)
        frame_sample = round(frame_index * samples_per_frame)
        deltas.append(abs(cue["sample"] - frame_sample))
    maximum = max(deltas, default=0.0)
    return {
        "maximum_delta_samples": maximum,
        "within_one_frame": maximum <= samples_per_frame + 1.0e-9,
        "samples_per_frame": samples_per_frame,
    }


@dataclass
class TransportCues:
    generation: int = 0
    emitted: set[tuple[int, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.emitted is None:
            self.emitted = set()

    def reset(self) -> None:
        self.generation += 1
        self.emitted.clear()

    def cue(self, cue_id: str, once: bool = True) -> bool:
        key = (self.generation, cue_id)
        if once and key in self.emitted:
            return False
        self.emitted.add(key)
        return True


def canonical_bytes(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def semantic_hash(document: Mapping[str, Any]) -> str:
    return "sha256:v1:" + hashlib.sha256(canonical_bytes(document)).hexdigest()

