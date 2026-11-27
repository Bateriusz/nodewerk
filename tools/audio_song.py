"""Compile NWAS v1 and decode the shared NWAS v1/v2 audio containers."""
import argparse
import json
import struct
from pathlib import Path

NWAS_MAGIC = b"NWAS"
NWAS_V2_VERSION = 2
MAX_NWAS_EVENTS = 64
MAX_NWAS_INSTRUMENTS = 64
MAX_NWAS_AUTOMATION_BUSES = 8
MAX_NWAS_AUTOMATION_KEYS = 64
MAX_SONG_TICKS = 48000 * 3600
WAVEFORM_CODES = {"sine": 0, "square": 1, "saw": 2, "triangle": 3, "noise": 4}
WAVEFORM_NAMES = {value: key for key, value in WAVEFORM_CODES.items()}
BUS_CODES = {"music": 0, "effect": 1}
BUS_NAMES = {value: key for key, value in BUS_CODES.items()}
NWAS_V2_HEADER = struct.Struct("<4sIQIII")
NWAS_V2_INSTRUMENT = struct.Struct("<IIIiIIIIII")
NWAS_V2_NOTE = struct.Struct("<QIIIHBB")
NWAS_V2_AUTOMATION_HEADER = struct.Struct("<BBHI")
NWAS_V2_AUTOMATION_KEY = struct.Struct("<QII")

def compile_song(model):
    if not isinstance(model, dict) or set(model) != {"version", "duration_ticks", "events"}:
        raise ValueError("invalid song fields")
    def integer(value, low, high):
        if type(value) is not int or not low <= value <= high:
            raise ValueError("song integer out of range")
        return value
    integer(model["version"], 1, 1)
    duration = integer(model["duration_ticks"], 1, 48000 * 3600)
    events = model["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 64:
        raise ValueError("invalid event count")
    result = bytearray(struct.pack("<4sIQI", b"NWAS", 1, duration, len(events)))
    previous = 0
    for e in events:
        if not isinstance(e, dict) or set(e) != {"tick", "duration", "frequency_millihz", "gain"}:
            raise ValueError("invalid event fields")
        tick = integer(e["tick"], previous, duration - 1)
        length = integer(e["duration"], 1, duration - tick)
        frequency = integer(e["frequency_millihz"], 1, 20_000_000)
        gain = integer(e["gain"], 1, 32767)
        result += struct.pack("<QIII", tick, length, frequency, gain)
        previous = tick
    return bytes(result)

def _decode_v1(payload):
    if not isinstance(payload, bytes) or len(payload) < 20:
        raise ValueError("truncated NWAS payload")
    magic, version, duration, count = struct.unpack_from("<4sIQI", payload)
    if magic != b"NWAS" or not 1 <= count <= 64 or len(payload) != 20 + 20 * count:
        raise ValueError("invalid NWAS header or length")
    events = []
    for offset in range(20, len(payload), 20):
        tick, length, frequency, gain = struct.unpack_from("<QIII", payload, offset)
        events.append({"tick": tick, "duration": length,
                       "frequency_millihz": frequency, "gain": gain})
    model = {"version": version, "duration_ticks": duration, "events": events}
    if compile_song(model) != payload:
        raise ValueError("noncanonical NWAS payload")
    return model

def decode_v2(payload):
    """Decode the shared NWAS v2 instrument/sequence container."""
    if not isinstance(payload, bytes) or len(payload) < NWAS_V2_HEADER.size:
        raise ValueError("truncated NWAS v2 payload")
    magic, version, duration, note_count, instrument_count, automation_count = NWAS_V2_HEADER.unpack_from(payload)
    if magic != NWAS_MAGIC or version != NWAS_V2_VERSION:
        raise ValueError("invalid NWAS v2 header")
    if not 1 <= duration <= MAX_SONG_TICKS or not 1 <= note_count <= MAX_NWAS_EVENTS:
        raise ValueError("invalid NWAS v2 duration or note count")
    if not 1 <= instrument_count <= MAX_NWAS_INSTRUMENTS or automation_count > MAX_NWAS_AUTOMATION_BUSES:
        raise ValueError("invalid NWAS v2 instrument or automation count")
    offset = NWAS_V2_HEADER.size
    instruments = []
    for _ in range(instrument_count):
        if offset + NWAS_V2_INSTRUMENT.size > len(payload):
            raise ValueError("truncated NWAS v2 instrument")
        waveform, seed, gain_milli, pan_milli, attack, decay, sustain, release, cutoff, filter_type = NWAS_V2_INSTRUMENT.unpack_from(payload, offset)
        offset += NWAS_V2_INSTRUMENT.size
        if (waveform not in WAVEFORM_NAMES or filter_type != 0 or gain_milli > 1000
                or not -1000 <= pan_milli <= 1000 or sustain > 1000
                or attack > MAX_SONG_TICKS or decay > MAX_SONG_TICKS
                or release > MAX_SONG_TICKS or not 20 <= cutoff <= 96000):
            raise ValueError("invalid NWAS v2 instrument")
        instruments.append({
            "waveform": WAVEFORM_NAMES[waveform], "seed": seed,
            "gain_milli": gain_milli, "pan_milli": pan_milli,
            "attack_ticks": attack, "decay_ticks": decay,
            "sustain_milli": sustain, "release_ticks": release,
            "cutoff_hz": cutoff, "filter_type": "one_pole_lowpass",
        })
    notes = []
    previous = 0
    for _ in range(note_count):
        if offset + NWAS_V2_NOTE.size > len(payload):
            raise ValueError("truncated NWAS v2 note")
        tick, length, frequency, velocity_milli, instrument, bus, reserved = NWAS_V2_NOTE.unpack_from(payload, offset)
        offset += NWAS_V2_NOTE.size
        if (tick < previous or tick >= duration or not 0 < length <= duration - tick
                or not 0 < frequency <= 20_000_000 or velocity_milli > 8000
                or instrument >= instrument_count or bus not in BUS_NAMES or reserved != 0):
            raise ValueError("invalid NWAS v2 note")
        previous = tick
        notes.append({
            "tick": tick, "duration": length, "frequency_millihz": frequency,
            "velocity_milli": velocity_milli, "instrument": instrument,
            "bus": BUS_NAMES[bus],
        })
    automation = []
    seen_buses = set()
    for _ in range(automation_count):
        if offset + NWAS_V2_AUTOMATION_HEADER.size > len(payload):
            raise ValueError("truncated NWAS v2 automation")
        bus, reserved_a, reserved_b, key_count = NWAS_V2_AUTOMATION_HEADER.unpack_from(payload, offset)
        offset += NWAS_V2_AUTOMATION_HEADER.size
        if (bus not in BUS_NAMES or bus in seen_buses or reserved_a or reserved_b
                or key_count > MAX_NWAS_AUTOMATION_KEYS):
            raise ValueError("invalid NWAS v2 automation")
        seen_buses.add(bus)
        keys = []
        previous_key = 0
        for _ in range(key_count):
            if offset + NWAS_V2_AUTOMATION_KEY.size > len(payload):
                raise ValueError("truncated NWAS v2 automation key")
            tick, value_milli, reserved = NWAS_V2_AUTOMATION_KEY.unpack_from(payload, offset)
            offset += NWAS_V2_AUTOMATION_KEY.size
            if reserved != 0 or tick < previous_key or tick > duration or value_milli > 8000:
                raise ValueError("invalid NWAS v2 automation key")
            previous_key = tick
            keys.append({"tick": tick, "value_milli": value_milli})
        automation.append({"bus": BUS_NAMES[bus], "keys": keys})
    if offset != len(payload):
        raise ValueError("NWAS v2 payload has trailing bytes")
    return {
        "version": NWAS_V2_VERSION,
        "duration_ticks": duration,
        "instruments": instruments,
        "notes": notes,
        "automation": automation,
    }

def decode_song(payload):
    """Decode NWAS v1 (canonical) or the shared NWAS v2 container."""
    if not isinstance(payload, bytes) or len(payload) < 8 or payload[:4] != NWAS_MAGIC:
        raise ValueError("truncated NWAS payload")
    version = struct.unpack_from("<I", payload, 4)[0]
    if version == 1:
        return _decode_v1(payload)
    if version == NWAS_V2_VERSION:
        return decode_v2(payload)
    raise ValueError("unsupported NWAS version")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("song", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(compile_song(json.loads(args.song.read_text(encoding="utf-8"))))

if __name__ == "__main__":
    main()
