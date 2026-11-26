import csv
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import wave

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from audio_song import compile_song

def fixture():
    return json.loads((ROOT / "fixtures/audio/sync_song.json").read_text(encoding="utf-8"))

def test_song_encoding_is_canonical_and_bounded():
    model = fixture()
    data = compile_song(model)
    assert data == compile_song(model)
    assert struct.unpack_from("<4sIQI", data) == (b"NWAS", 1, 192000, 4)
    assert len(data) == 20 + 20 * 4
    for field, value in (("duration_ticks", 0), ("version", True), ("events", [])):
        with pytest.raises(ValueError):
            compile_song({**model, field: value})
    for field, value in (("tick", -1), ("duration", 0), ("gain", 32768),
                         ("frequency_millihz", 20000001)):
        changed = fixture()
        changed["events"][0][field] = value
        with pytest.raises(ValueError):
            compile_song(changed)

@pytest.fixture(scope="module")
def native_tools(tmp_path_factory):
    if os.name == "nt":
        pytest.skip("portable native test is executed on hosted Linux; use NW_BUILD_AUDIO on Windows")
    compiler = shutil.which("c++")
    assert compiler, "native audio contract requires a C++ compiler"
    out = tmp_path_factory.mktemp("native-audio")
    for source, name in (("tests/audio_contract.cpp", "contract"),
                         ("fixtures/audio/offline.cpp", "offline")):
        subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                        "-fno-exceptions", "-fno-rtti", "-O2", "-I", str(ROOT / "src"),
                        str(ROOT / "src/audio/procedural_audio.cpp"), str(ROOT / source),
                        "-o", str(out / name)], check=True)
    return out

def test_native_audio_contract(native_tools):
    subprocess.run([str(native_tools / "contract")], check=True)

def test_offline_pcm_and_frame_plan_are_reproducible(native_tools, tmp_path):
    song = tmp_path / "song.nwas"
    song.write_bytes(compile_song(fixture()))
    results = []
    for i in range(2):
        wav = tmp_path / f"song{i}.wav"
        plan = tmp_path / f"plan{i}.csv"
        subprocess.run([str(native_tools / "offline"), str(song), str(wav), str(plan)],
                       check=True)
        results.append((wav.read_bytes(), plan.read_bytes()))
        with wave.open(str(wav), "rb") as stream:
            assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(),
                    stream.getnframes()) == (1, 2, 48000, 192000)
            pcm = struct.unpack("<192000h", stream.readframes(192000))
        assert pcm[47999] == 0 and pcm[48000] != 0
        assert pcm[50400] == 0
        rows = list(csv.DictReader(plan.read_text().splitlines()))
        assert len(rows) == 240
        assert rows[60] == {"frame": "60", "tick": "48000", "sample": "48000"}
    assert results[0] == results[1]
    for broken in (b"", song.read_bytes() + b"\x00", song.read_bytes()[:-1]):
        song.write_bytes(broken) if broken else song.write_bytes(b"")
        result = subprocess.run([str(native_tools / "offline"), str(song),
                                 str(tmp_path / "bad.wav"), str(tmp_path / "bad.csv")])
        assert result.returncode != 0

def test_audio_manifest_and_generated_source_are_opt_in():
    import nw05_export as export
    from hashlib import sha256
    song = compile_song(fixture())
    silent = {"retained": {"code": [], "data": []}}
    export._attach_audio_manifest(silent, b"")
    assert "audio" not in silent
    audio = {"retained": {"code": [], "data": []}}
    export._attach_audio_manifest(audio, song)
    assert audio["audio"]["song_sha256"] == sha256(song).hexdigest()
    assert audio["audio"]["initialization_failure"] == "fail"
    assert "feature_requirements" not in audio
    assert audio["retained"]["data"] == ["audio.song.nwas"]
    kwargs = {"package_sha256": "0" * 64, "manifest_sha256": "0" * 64}
    silent_source = export.generate_player_source(manifest=silent, **kwargs)
    audio_source = export.generate_player_source(manifest=audio, **kwargs)
    assert "WasapiOutput" not in silent_source
    assert "song_bytes[]" not in silent_source
    assert "WasapiOutput" in audio_source and "song_bytes[]" in audio_source
    assert "audio_output.now_ticks()" in audio_source
    with pytest.raises(export.ExportError):
        export._attach_audio_manifest({"timeline": {"duration_ticks": 1},
                                      "retained": {}}, song)

def test_offline_capture_plan_rejects_mismatched_samples(tmp_path):
    from audio_capture import frame_ticks
    plan = tmp_path / "frames.csv"
    plan.write_text("frame,tick,sample\n0,0,0\n1,800,800\n")
    assert frame_ticks(plan) == [0, 800]
    plan.write_text("frame,tick,sample\n0,0,0\n1,800,799\n")
    with pytest.raises(ValueError):
        frame_ticks(plan)

def test_studio_export_parser_accepts_audio():
    import nw08_studio
    args = nw08_studio.build_parser().parse_args(
        ["export", "--output", "build/audio", "--audio-song", "song.json"])
    assert args.audio_song == "song.json"
