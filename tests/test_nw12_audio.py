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
    assert "WasapiOutput" in audio_source and "player.package().audio_song" in audio_source
    assert "song_hex" not in audio["audio"]
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

@pytest.fixture
def test_dxc(tmp_path, monkeypatch):
    # Use the established test-only DXIL producer and graph identity seam on
    # portable CI. The real Windows export test must still use pinned DXC.
    from test_nw05_export import _make_dxc
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    return str(_make_dxc(tmp_path / "toolchain"))

def test_package_audio_and_flash_compile(tmp_path, test_dxc):
    import nw05_export as export
    import nw03_compiler as compiler
    import nw03_oracle as oracle
    import nwp_package as nwp
    graph = ROOT / "fixtures/audio/sync_flash.nwgraph"
    document = compiler.load_graph(graph)
    shaders, diagnostics = compiler.lower_to_fused_hlsl(document)
    assert not diagnostics and shaders
    source = next(s.root_expression for s in shaders if "48000" in s.root_expression)
    for event in fixture()["events"]:
        tick = event["tick"]
        for delta, expected in ((0, 1.0), (800, 1.0), (3200, 0.0)):
            value = oracle.evaluate_text(source, variables={"uv": (0.5, 0.5),
                                                            "time": tick + delta, "seed": 0},
                                         float32=True)
            assert value[:3] == (expected,) * 3
    result = export.export_graph(
        graph, tmp_path / "export", allow_test_payloads=True, dxc_executable=test_dxc,
        settings=export.ExportSettings(audio_song_path=str(ROOT / "fixtures/audio/sync_song.json"),
                                       byte_ceiling=64 * 1024 * 1024))
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    decoded = nwp.decode_package(artifact.package_bytes)
    assert decoded.version_minor == 2
    assert decoded.audio_song == compile_song(fixture())
    assert nwp.encode_package(decoded) == artifact.package_bytes
    assert "AUDI" in export.section_sizes(artifact.package_bytes)
    from dataclasses import replace
    with pytest.raises(nwp.PackageValidationError):
        nwp.encode_package(replace(decoded, audio_song=b"NWAS"))

def test_pcm_header_and_duration(native_tools, tmp_path):
    from audio_capture import validate_pcm, frame_ticks
    song = tmp_path / "song.nwas"
    song.write_bytes(compile_song(fixture()))
    wav, plan = tmp_path / "audio.wav", tmp_path / "frames.csv"
    subprocess.run([str(native_tools / "offline"), str(song), str(wav), str(plan)], check=True)
    validate_pcm(wav, 192000)
    assert len(frame_ticks(plan, 192000)) == 240
    wav.write_bytes(wav.read_bytes()[:-2])
    with pytest.raises(ValueError):
        validate_pcm(wav, 192000)
    plan.write_text("frame,tick,sample\n0,0,0\n")
    with pytest.raises(ValueError):
        frame_ticks(plan, 192000)

def test_editor_transport_uses_native_session_and_reconstructs_before_start():
    from nw08_preview import AuthoringRuntimeSession
    from types import SimpleNamespace
    import threading
    commands = []
    session = SimpleNamespace(
        _lock=threading.RLock(), _persistent=True, _lost=False,
        _device_running=True, _last_render_tick=1000,
        _write=commands.append,
        _read_response=lambda **kwargs: {"event": commands[-1].split()[0],
                                         "ok": True, "tick": 100},
        _stop_process=lambda: commands.append("closed"),
    )
    AuthoringRuntimeSession.audio_command(session, "audio-stop")
    AuthoringRuntimeSession.audio_command(session, "audio-start 100")
    assert commands == ["audio-stop", "audio-start 100"]
    assert session._last_render_tick == 100
    session._read_response = lambda **kwargs: {"event": "audio-start", "ok": False}
    from nw08_preview import RuntimeSessionError
    with pytest.raises(RuntimeSessionError):
        AuthoringRuntimeSession.audio_command(session, "audio-start 200")
    assert commands[-1] == "closed"

def test_coupled_capture_protocol(native_tools, tmp_path, monkeypatch, test_dxc):
    import audio_capture
    import nw05_export as export
    import nwp_package as nwp
    song = tmp_path / "song.nwas"
    song.write_bytes(compile_song(fixture()))
    result = export.export_graph(
        ROOT / "fixtures/audio/sync_flash.nwgraph", tmp_path / "export",
        allow_test_payloads=True, dxc_executable=test_dxc, settings=export.ExportSettings(
            audio_song_path=str(ROOT / "fixtures/audio/sync_song.json"), byte_ceiling=64 * 1024 * 1024))
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    package = tmp_path / "export" / (export._profile_file_stem(artifact.profile_id) + ".nwp")
    assert nwp.decode_package(package.read_bytes()).audio_song == song.read_bytes()
    host = tmp_path / "host.exe"
    host.write_bytes(b"controlled native protocol stand-in")
    real_run = subprocess.run
    def controlled_run(command, **kwargs):
        if command[0] != str(host):
            return real_run(command, **kwargs)
        directory = Path(command[command.index("--capture-dir") + 1])
        messages = [{"event": "ready", "package_sha256": audio_capture.sha256(package)}]
        for line in kwargs["input"].splitlines():
            if line == "quit":
                messages.append({"event": "shutdown", "ok": True})
            else:
                tick = int(line.split()[1])
                level = float(tick % 48000 < 2400)
                (directory / f"tick_{tick}.rgba32f").write_bytes(struct.pack("<4f", level, level, level, 1))
                messages.append({"event": "rendered", "ok": True, "tick": tick, "capture": True})
        return subprocess.CompletedProcess(command, 0, stdout="\n".join(map(json.dumps, messages)))
    monkeypatch.setattr(audio_capture.subprocess, "run", controlled_run)
    report = audio_capture.capture(native_tools / "offline", host, package, song,
                                   tmp_path / "capture", width=1, height=1)
    assert len(report["images"]) == 240
    assert report["physical_alignment"] == "not_measured"
    song.write_bytes(compile_song({**fixture(), "duration_ticks": 192001}))
    with pytest.raises(ValueError, match="does not match"):
        audio_capture.capture(native_tools / "offline", host, package, song,
                              tmp_path / "invalid", width=1, height=1)
    assert not (tmp_path / "invalid").exists()
