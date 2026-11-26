"""Compile the bounded NWAS v1 procedural song payload, independently of GPU capabilities."""
import argparse
import json
import struct
from pathlib import Path

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

def decode_song(payload):
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

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("song", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(compile_song(json.loads(args.song.read_text(encoding="utf-8"))))

if __name__ == "__main__":
    main()
