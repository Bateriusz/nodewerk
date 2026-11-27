from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import audio_song  # noqa: E402
import nw13_audio as audio  # noqa: E402
from schema_validator import validate_instance  # noqa: E402


SONG_PATH = REPO_ROOT / "fixtures" / "nw13" / "hotel_corridor.song.json"
SCHEMA_PATH = REPO_ROOT / "schemas" / "nw13-instrument-sequence.v2.json"


def _instrument(identifier: str, waveform: str, *, pan: float = 0.0, seed: int = 1) -> dict:
    return {
        "id": identifier,
        "waveform": waveform,
        "seed": seed,
        "gain": 0.5,
        "pan": pan,
        "envelope": {"attack_ticks": 240, "decay_ticks": 960, "sustain": 0.7, "release_ticks": 2400},
        "filter": {"type": "one_pole_lowpass", "cutoff_hz": 12000.0},
    }


def _note(identifier: str, instrument: str, *, tick: int = 0, duration: int = 4800, frequency: int = 440000, velocity: float = 1.0) -> dict:
    return {
        "id": identifier,
        "tick": tick,
        "duration": duration,
        "frequency_millihz": frequency,
        "velocity": velocity,
        "instrument": instrument,
    }


def _sequence(instruments: list[dict], notes: list[dict], *, duration: int = 48000, automation: list[dict] | None = None) -> dict:
    return {
        "format": "nw13.instrument-sequence",
        "version": 2,
        "tick_rate_hz": 48000,
        "duration_ticks": duration,
        "instruments": instruments,
        "tracks": [{"id": "music.test", "bus": "music", "notes": notes}],
        "automation": automation or [],
    }


def test_schema_and_public_payload_validate() -> None:
    document = json.loads(SONG_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validate_instance(document, schema, schema)
    compiled = audio.compile_sequence(document)
    assert len(compiled.notes) == 30
    assert set(compiled.instruments) == {"lead", "bass", "percussion", "event"}


def test_nwas_v1_decoding_is_preserved_and_canonical() -> None:
    v1_events = [
        {"tick": 0, "duration": 4800, "frequency_millihz": 440000, "gain": 12000},
        {"tick": 9600, "duration": 4800, "frequency_millihz": 660000, "gain": 9000},
    ]
    v1 = audio_song.compile_song({"version": 1, "duration_ticks": 48000, "events": v1_events})
    decoded_v1 = audio.decode_nwas(v1)
    assert decoded_v1["version"] == 1
    assert decoded_v1["events"] == v1_events
    assert audio_song.compile_song(decoded_v1) == v1


def test_shared_v2_container_carries_instrument_filter_pan_bus_and_automation() -> None:
    document = json.loads(SONG_PATH.read_text(encoding="utf-8"))
    sequence = audio.compile_sequence(document)
    payload = audio.compile_to_nwas(sequence)
    assert payload[:4] == b"NWAS"
    assert int.from_bytes(payload[4:8], "little") == 2
    decoded = audio.decode_nwas(payload)
    assert decoded["version"] == 2
    assert decoded["duration_ticks"] == 288000
    assert len(decoded["notes"]) == 30
    assert len(decoded["instruments"]) == 4
    assert len(decoded["automation"]) == 2
    lead = decoded["instruments"][0]
    assert lead["waveform"] == "saw"
    assert lead["seed"] == 11
    assert lead["pan_milli"] == -250
    assert lead["cutoff_hz"] == 4200
    buses = {entry["bus"] for entry in decoded["automation"]}
    assert buses == {"music", "effect"}
    assert {note["bus"] for note in decoded["notes"]} == {"music", "effect"}
    assert audio.decode_nwas(payload) == decoded
    # The preserved NWAS v1 reader remains byte-canonical for v1 payloads.
    v1_single = audio_song.compile_song({"version": 1, "duration_ticks": 48000,
                                         "events": [{"tick": 0, "duration": 4800,
                                                     "frequency_millihz": 440000, "gain": 12000}]})
    assert audio_song.compile_song(audio_song.decode_song(v1_single)) == v1_single


@pytest.mark.parametrize("waveform", ["sine", "square", "saw", "triangle", "noise"])
def test_waveforms_render_bounded_deterministic_pcm(waveform: str) -> None:
    sequence = _sequence([_instrument("i", waveform, seed=7)], [_note("n", "i", duration=9600)])
    first = audio.render_pcm(sequence, sample_count=9600)
    second = audio.render_pcm(sequence, sample_count=9600)
    assert first == second
    assert len(first) == 19200
    assert all(-32768 <= sample <= 32767 for sample in first)
    assert any(sample != 0 for sample in first)


def test_distinct_instruments_have_distinct_render_signatures() -> None:
    signatures = {}
    for waveform, frequency in (("saw", 523251), ("square", 110000), ("noise", 90000), ("sine", 1760000)):
        sequence = _sequence([_instrument(waveform, waveform)], [_note(waveform, waveform, duration=4800, frequency=frequency)])
        samples = audio.render_pcm(sequence, sample_count=4800, channels=1)
        crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
        signatures[waveform] = (round(sum(abs(sample) for sample in samples) / len(samples)), crossings)
    assert len(set(signatures.values())) == len(signatures)
    assert signatures["sine"][1] > signatures["square"][1] * 0.2


def test_envelope_filter_gain_and_pan() -> None:
    instrument = _instrument("i", "sine")
    instrument["envelope"] = {"attack_ticks": 2400, "decay_ticks": 2400, "sustain": 0.4, "release_ticks": 2400}
    sequence = _sequence([instrument], [_note("n", "i", duration=24000)])
    mono = audio.render_pcm(sequence, sample_count=24000, channels=1)
    start = sum(abs(value) for value in mono[:1200]) / 1200
    middle = sum(abs(value) for value in mono[7000:9000]) / 2000
    end = sum(abs(value) for value in mono[-1200:]) / 1200
    assert start < middle
    assert end < middle

    left = _sequence([_instrument("l", "sine", pan=-1.0)], [_note("n", "l", duration=9600)])
    right = _sequence([_instrument("r", "sine", pan=1.0)], [_note("n", "r", duration=9600)])
    left_pcm = audio.render_pcm(left, sample_count=9600, channels=2)
    right_pcm = audio.render_pcm(right, sample_count=9600, channels=2)
    assert sum(abs(left_pcm[index]) for index in range(0, len(left_pcm), 2)) > 0
    assert sum(abs(left_pcm[index]) for index in range(1, len(left_pcm), 2)) == 0
    assert sum(abs(right_pcm[index]) for index in range(0, len(right_pcm), 2)) == 0
    assert sum(abs(right_pcm[index]) for index in range(1, len(right_pcm), 2)) > 0


def test_music_and_effect_buses_automation_are_separate() -> None:
    base = json.loads(SONG_PATH.read_text(encoding="utf-8"))
    loud = json.loads(json.dumps(base))
    quiet = json.loads(json.dumps(base))
    for document, value in ((loud, 1.0), (quiet, 0.25)):
        document["automation"] = [
            {"bus": "music", "keys": [{"tick": 0, "value": value}, {"tick": 288000, "value": value}]},
            {"bus": "effect", "keys": [{"tick": 0, "value": 1.0}, {"tick": 288000, "value": 1.0}]},
        ]
    loud_pcm = audio.render_pcm(loud, sample_count=4800, channels=1)
    quiet_pcm = audio.render_pcm(quiet, sample_count=4800, channels=1)
    loud_rms = math.sqrt(sum(value * value for value in loud_pcm) / len(loud_pcm))
    quiet_rms = math.sqrt(sum(value * value for value in quiet_pcm) / len(quiet_pcm))
    assert loud_rms > quiet_rms


def test_chunk_partitions_are_sample_identical() -> None:
    sequence = audio.compile_sequence(json.loads(SONG_PATH.read_text(encoding="utf-8")))
    whole = audio.render_pcm(sequence, first_sample=0, sample_count=48000, channels=2)
    chunks = audio.render_in_chunks(sequence, [1, 17, 333, 2048, 4096, 8192, 48000 - 1 - 17 - 333 - 2048 - 4096 - 8192], channels=2)
    assert chunks == whole


def test_event_voice_filter_and_memory_overflow_diagnostics() -> None:
    instruments = [_instrument("i", "sine")]
    many = _sequence(instruments, [_note(f"n{index}", "i", tick=index * 100, duration=100) for index in range(65)], duration=10000)
    compiled = audio.compile_sequence(many)
    with pytest.raises(audio.AudioError) as exc:
        audio.compile_to_nwas(compiled)
    assert any(item["code"] == "audio_event_overflow" for item in exc.value.diagnostics)

    voiced = _sequence(
        [_instrument("i", "sine"), _instrument("j", "saw")],
        [_note("n", "i", duration=4800), _note("m", "j", duration=4800)],
    )
    voiced["budget"] = {"max_voices": 1}
    with pytest.raises(audio.AudioError) as exc:
        audio.compile_sequence(voiced)
    assert any(item["code"] == "audio_voice_overflow" for item in exc.value.diagnostics)

    tight = _sequence(instruments, [_note("n", "i", duration=4800)])
    tight["budget"] = {"max_memory_bytes": 1024}
    with pytest.raises(audio.AudioError) as exc:
        audio.compile_sequence(tight)
    assert any(item["code"] == "audio_memory_overflow" for item in exc.value.diagnostics)


def test_filter_state_overflow_is_diagnosed(monkeypatch: pytest.MonkeyPatch) -> None:
    sequence = _sequence([_instrument("i", "noise")], [_note("n", "i", duration=4800)])
    monkeypatch.setattr(audio, "_waveform", lambda instrument, phase, sample_index: math.inf)
    with pytest.raises(audio.AudioError) as exc:
        audio.render_pcm(sequence, sample_count=4800)
    assert any(item["code"] == "audio_filter_state_overflow" for item in exc.value.diagnostics)


def test_audiovisual_cues_align_within_one_frame_without_duplicate_transport_cues() -> None:
    sequence = audio.compile_sequence(json.loads(SONG_PATH.read_text(encoding="utf-8")))
    alignment = audio.audiovisual_alignment(sequence, frame_rate=60)
    assert alignment["within_one_frame"] is True
    cues = audio.cue_samples(sequence)
    assert len(cues) == len(sequence.notes)
    transport = audio.TransportCues()
    assert transport.cue("flash") is True
    assert transport.cue("flash") is False
    transport.reset()
    assert transport.cue("flash") is True
    assert audio.semantic_hash(json.loads(SONG_PATH.read_text(encoding="utf-8"))) == audio.semantic_hash(json.loads(SONG_PATH.read_text(encoding="utf-8")))


def _compile_native(tmp_path: Path, source: str, name: str) -> Path:
    compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("shared procedural-audio contract requires a C++20 compiler")
    output = tmp_path / name
    subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fno-exceptions",
            "-fno-rtti",
            "-O2",
            "-I",
            str(REPO_ROOT / "src"),
            str(REPO_ROOT / "src" / "audio" / "procedural_audio.cpp"),
            str(REPO_ROOT / "tests" / source),
            "-o",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return output


def test_shared_cpp_decoder_reaches_v2_instruments_filter_pan_bus_and_automation(
    tmp_path: Path,
) -> None:
    executable = _compile_native(tmp_path, "nw13_audio_payload.cpp", "nw13_audio_payload")
    sequence = audio.compile_sequence(json.loads(SONG_PATH.read_text(encoding="utf-8")))
    payload = tmp_path / "hotel.nwas"
    payload.write_bytes(audio.compile_to_nwas(sequence))
    completed = subprocess.run([str(executable), str(payload)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert "version=2 instruments=4 notes=30 automation=2" in completed.stdout
    assert "chunked_equal=1" in completed.stdout
    assert "pcm_nonzero=" in completed.stdout
    hash_line = next(line for line in completed.stdout.splitlines() if line.startswith("pcm_fnv64="))
    assert hash_line != "pcm_fnv64=0000000000000000"


def test_shared_cpp_contract_selects_waveforms_pan_envelope_filter_and_automation(
    tmp_path: Path,
) -> None:
    executable = _compile_native(tmp_path, "nw13_audio_contract.cpp", "nw13_audio_contract")
    completed = subprocess.run([str(executable)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr

