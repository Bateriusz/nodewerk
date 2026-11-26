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
    assert isinstance(decoded.audio_song, bytes)
    assert decoded.audio_song == compile_song(fixture())
    from audio_song import decode_song
    assert decode_song(decoded.audio_song) == fixture()
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
    physical = report["physical_alignment"]
    assert physical["status"] == "unavailable"
    assert physical["evidence_record"] == "benchmarks/nw12/audio-physical-evidence.v1.json"
    assert physical["required_observation"]
    song.write_bytes(compile_song({**fixture(), "duration_ticks": 192001}))
    with pytest.raises(ValueError, match="does not match"):
        audio_capture.capture(native_tools / "offline", host, package, song,
                              tmp_path / "invalid", width=1, height=1)
    assert not (tmp_path / "invalid").exists()


def test_nw06_optional_profile_export_keeps_audio_injection_after_compile(
    tmp_path, test_dxc,
):
    # Regression: the NW-06 specialized path must not reference the caller's
    # audio payload. export_graph injects AUDI after _compile_profile_package,
    # so every optional profile must still export without a NameError.
    import nw05_export as export
    import nwp_package as nwp
    settings = export.ExportSettings(
        byte_ceiling=64 * 1024 * 1024,
        memory_budget=256 * 1024 * 1024,
        optional_profiles=(nwp.OPTIONAL_PROFILE_WAVE_OPS,),
        allow_optional_profiles=True,
    )
    result = export.export_graph(
        ROOT / "fixtures/nw06/procedural_scene.nwgraph",
        tmp_path / "export",
        allow_test_payloads=True,
        dxc_executable=test_dxc,
        settings=settings,
    )
    assert set(result.artifacts) == {
        nwp.COMMON_PROFILE_ID,
        nwp.OPTIONAL_PROFILE_WAVE_OPS,
    }
    optional = result.artifacts[nwp.OPTIONAL_PROFILE_WAVE_OPS]
    assert optional.package.capability.optional_profile_id == nwp.OPTIONAL_PROFILE_WAVE_OPS
    assert optional.package.audio_song is None
    assert "AUDI" not in export.section_sizes(optional.package_bytes)


def test_stateful_audio_export_embeds_payload_and_manifest(tmp_path, test_dxc):
    # Regression: stateful NW-09 content with an audio song must embed AUDI in
    # the package (minor 2 with HIST+CKPT+audio), not only advertise it in the
    # manifest. Otherwise the generated WASAPI player rejects its own package.
    import nw05_export as export
    import nwp_package as nwp
    from audio_song import decode_song
    song = compile_song(fixture())
    report_path = tmp_path / "export" / "stateful-report.json"
    result = export.export_graph(
        ROOT / "fixtures/nw09/feedback_rect.nwgraph",
        tmp_path / "export",
        allow_test_payloads=True,
        dxc_executable=test_dxc,
        report_path=report_path,
        settings=export.ExportSettings(
            byte_ceiling=64 * 1024 * 1024,
            memory_budget=256 * 1024 * 1024,
            audio_song_path=str(ROOT / "fixtures/audio/sync_song.json"),
        ),
    )
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    decoded = nwp.decode_package(artifact.package_bytes)
    assert decoded.history
    assert decoded.version_minor == 2
    assert decoded.audio_song == song
    assert decode_song(decoded.audio_song) == fixture()
    assert "AUDI" in export.section_sizes(artifact.package_bytes)
    assert artifact.manifest["audio"]["song_sha256"] == __import__("hashlib").sha256(
        song
    ).hexdigest()
    assert artifact.size_report["audio"]["song_payload_bytes"] == len(song)
    assert artifact.size_report["stateful"]["package_minor"] == 2
    assert artifact.manifest["stateful_extension"]["package_minor"] == 2
    # The written report and the in-memory result must follow the combined
    # package minor rather than the stateful-only value from the base build.
    assert result.report["package_version"] == {"major": 1, "minor": 2}
    written_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert written_report["package_version"] == {"major": 1, "minor": 2}
    from schema_validator import validate_file_against_schema
    validate_file_against_schema(
        report_path, ROOT / "schemas" / "nw09-export-report.schema.json"
    )
    assert nwp.encode_package(decoded) == artifact.package_bytes
    source = export.generate_player_source(
        package_sha256="0" * 64,
        manifest_sha256="sha256:v1:" + "0" * 64,
        manifest=artifact.manifest,
    )
    assert "WasapiOutput" in source
    assert "player.package().audio_song" in source


def _audio_package_bytes():
    from dataclasses import replace
    import nwp_package as nwp
    song = compile_song(fixture())
    base = nwp.decode_package(
        (ROOT / "fixtures/nw04/compute_display.nwp").read_bytes()
    )
    package = replace(
        base, audio_song=song, flags=base.flags | nwp.AUDIO_EXTENSION_FLAG
    )
    return nwp.encode_package(package), song


def _observation(deltas=(0, 0, 800, -800)):
    events = fixture()["events"]
    return {
        "format": "nodewerk-audio-display-observation",
        "version": 1,
        "sample_rate": 48000,
        "capture": {
            "audio_sha256": "0" * 64,
            "video_sha256": "1" * 64,
            "duration_ticks": 192000,
            "protocol": "single external A/V capture of the same desktop run",
        },
        "impulses": [
            {"event_tick": event["tick"], "audio_tick": event["tick"], "display_tick": event["tick"] + delta}
            for event, delta in zip(events, deltas)
        ],
    }


def test_alignment_is_bounded_by_one_displayed_frame():
    from audio_physical_validation import evaluate_alignment
    song = fixture()
    within = evaluate_alignment(_observation(), song, 60)
    assert within["max_abs_delta_ticks"] == 800
    assert within["within_one_displayed_frame"] is True
    assert evaluate_alignment(_observation((0, 0, 0, 0)), song, 60)[
        "max_abs_delta_ticks"
    ] == 0
    shifted = _observation((0, 0, 0, 0))
    for impulse in shifted["impulses"]:
        impulse["audio_tick"] += 2000
        impulse["display_tick"] += 2100
    assert evaluate_alignment(shifted, song, 60)["max_abs_delta_ticks"] == 100
    # 120 Hz shortens the displayed-frame tolerance to 400 ticks.
    assert evaluate_alignment(_observation(), song, 120)[
        "within_one_displayed_frame"
    ] is False
    with pytest.raises(ValueError, match="one impulse per song event"):
        evaluate_alignment({**_observation(), "impulses": []}, song)
    bad = _observation()
    bad["impulses"][0]["event_tick"] = 1
    with pytest.raises(ValueError, match="does not match song onset"):
        evaluate_alignment(bad, song)


def test_player_telemetry_never_invents_unavailable_cpu():
    from audio_physical_validation import parse_player_telemetry
    available = parse_player_telemetry(
        "AUDIO_SYNTHESIS_CPU_100NS=700\n"
        "AUDIO_SYNTHESIS_CPU_AVAILABLE=1\n"
        "AUDIO_WORKER_CPU_100NS=900\n"
        "AUDIO_WORKER_CPU_AVAILABLE=1\n"
        "AUDIO_OBSERVED_UNDERRUNS=0\n"
        "AUDIO_MISSING_SAMPLES_LOWER_BOUND=0\n"
        "AUDIO_CLOCK_DISCONTINUITIES=0\n"
        "AUDIO_STARVATION_OBSERVATIONS=3\n"
        "AUDIO_PHYSICAL_UNDERRUNS=unavailable\n"
    )
    assert available["synthesis_cpu_100ns"] == 700
    assert available["worker_cpu_100ns"] == 900
    assert available["observed_underruns"] == 0
    assert available["physical_underruns"] == "unavailable"
    unavailable = parse_player_telemetry(
        "AUDIO_SYNTHESIS_CPU_AVAILABLE=0\n"
        "AUDIO_WORKER_CPU_AVAILABLE=0\n"
    )
    assert unavailable["synthesis_cpu_100ns"] is None
    assert unavailable["worker_cpu_100ns"] is None
    assert unavailable["synthesis_cpu_available"] is False


def test_physical_evidence_stays_pending_without_real_observation():
    from audio_physical_validation import build_evidence, verify_evidence
    package_bytes, song = _audio_package_bytes()
    evidence = build_evidence(
        package_bytes=package_bytes,
        song_bytes=song,
        source_head_sha="0" * 40,
        executable_bytes=b"player",
        telemetry_text=(
            "AUDIO_SYNTHESIS_CPU_100NS=700\nAUDIO_SYNTHESIS_CPU_AVAILABLE=1\n"
            "AUDIO_WORKER_CPU_100NS=900\nAUDIO_WORKER_CPU_AVAILABLE=1\n"
            "AUDIO_OBSERVED_UNDERRUNS=1\nAUDIO_MISSING_SAMPLES_LOWER_BOUND=480\n"
            "AUDIO_CLOCK_DISCONTINUITIES=0\nAUDIO_STARVATION_OBSERVATIONS=0\n"
        ),
        environment={"status": "unavailable", "reason": "no Windows endpoint"},
    )
    assert evidence["verification_status"] == "pending_physical"
    assert evidence["source_head_sha"] is None
    assert evidence["satisfies_completion"] is False
    fields = {item["field"] for item in evidence["unavailable"]}
    assert {"environment", "alignment"} <= fields
    verify_evidence(evidence, package_bytes=package_bytes, song_bytes=song)
    with pytest.raises(ValueError, match="package bytes"):
        verify_evidence(evidence, package_bytes=b"other")


def test_physical_evidence_verified_requires_all_observations():
    from audio_physical_validation import build_evidence, verify_evidence
    package_bytes, song = _audio_package_bytes()
    environment = {
        "status": "available",
        "reason": "",
        "os": {"caption": "Microsoft Windows 11 Pro", "version": "10.0.26200",
               "build": "26200"},
        "audio_endpoint": [{"name": "Speakers", "status": "OK", "device_id": "x"}],
        "display": [{"name": "Display", "driver_version": "1", "width": 1920,
                     "height": 1080, "refresh_hz": 60}],
    }
    telemetry = (
        "AUDIO_SYNTHESIS_CPU_100NS=700\nAUDIO_SYNTHESIS_CPU_AVAILABLE=1\n"
        "AUDIO_WORKER_CPU_100NS=900\nAUDIO_WORKER_CPU_AVAILABLE=1\n"
        "AUDIO_OBSERVED_UNDERRUNS=0\nAUDIO_MISSING_SAMPLES_LOWER_BOUND=0\n"
        "AUDIO_CLOCK_DISCONTINUITIES=0\nAUDIO_STARVATION_OBSERVATIONS=0\n"
    )
    evidence = build_evidence(
        package_bytes=package_bytes, song_bytes=song, source_head_sha="0" * 40,
        executable_bytes=b"player", telemetry_text=telemetry,
        observation=_observation(), environment=environment,
    )
    assert evidence["verification_status"] == "verified_physical"
    assert evidence["alignment"]["within_one_displayed_frame"] is True
    assert evidence["unavailable"] == []
    assert {item["field"] for item in evidence["unavailable_telemetry"]} == {
        "measurements.physical_underruns"
    }
    verify_evidence(evidence, package_bytes=package_bytes, song_bytes=song)
    # A forged verified record without the required observations must fail.
    forged = dict(evidence)
    forged["verification_status"] = "verified_physical"
    forged["source_head_sha"] = "0" * 40
    forged["satisfies_completion"] = True
    forged["unavailable"] = [
        {"field": "alignment", "reason": "missing"}
    ]
    with pytest.raises(ValueError, match="unavailable"):
        verify_evidence(forged)
    pending_without_reasons = dict(evidence)
    pending_without_reasons["verification_status"] = "pending_physical"
    pending_without_reasons["source_head_sha"] = None
    pending_without_reasons["satisfies_completion"] = False
    pending_without_reasons["unavailable"] = []
    with pytest.raises(ValueError, match="must list unavailable"):
        verify_evidence(pending_without_reasons)


def test_published_physical_evidence_binds_base_and_stays_honest():
    import audio_physical_validation as physical
    record = json.loads(
        (ROOT / "benchmarks/nw12/audio-physical-evidence.v1.json").read_text(
            encoding="utf-8"
        )
    )
    physical.verify_evidence(record)
    assert record["implementation_base"] == "8ade0b74c78113c18c977afbde0f3e2b98c4fcf2"
    assert record["nw11_incorporated"] is True
    provenance = record["provenance"]
    assert provenance["implementation_base"] == record["implementation_base"]
    assert provenance["nw11_merge_revision"] == record["implementation_base"]
    assert provenance["nw11_incorporated"] is True
    for path, digest in provenance["build_inputs"].items():
        assert __import__("hashlib").sha256(
            (ROOT / path).read_bytes()
        ).hexdigest() == digest
    if record["verification_status"] != "verified_physical":
        # A pending aggregate record must say exactly what is missing and must
        # never present invented CPU/underrun/alignment values.
        assert record["source_head_sha"] is None
        assert record["satisfies_completion"] is False
        assert record["unavailable"]
        assert record["measurements"] == {}
        assert record["alignment"] is None
        assert {item["field"] for item in record["unavailable_telemetry"]} >= {
            "measurements.physical_underruns"
        }
    from schema_validator import validate_file_against_schema
    validate_file_against_schema(
        ROOT / "benchmarks/nw12/audio-physical-evidence.v1.json",
        ROOT / "schemas/nw12-audio-physical-evidence.schema.json",
    )


def test_recorded_implementation_base_includes_nw11():
    import subprocess
    record = json.loads(
        (ROOT / "benchmarks/nw12/audio-physical-evidence.v1.json").read_text(
            encoding="utf-8"
        )
    )
    base = record["implementation_base"]
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, "HEAD"], cwd=ROOT
    ).returncode == 0
    assert subprocess.check_output(
        ["git", "rev-parse", f"{base}^{{tree}}"], cwd=ROOT, text=True
    ).strip() == record["provenance"]["implementation_base_tree"]
    message = subprocess.check_output(
        ["git", "log", "-1", "--format=%s", base], cwd=ROOT, text=True
    )
    assert "NW-11" in message


def test_verified_physical_evidence_merges_only_for_matching_artifact(tmp_path, monkeypatch):
    import audio_physical_validation as physical
    package_bytes, song = _audio_package_bytes()
    environment = {
        "status": "available", "reason": "",
        "os": {"caption": "Microsoft Windows 11 Pro", "version": "10.0.26200",
               "build": "26200"},
        "audio_endpoint": [{"name": "Speakers", "status": "OK", "device_id": "x"}],
        "display": [{"name": "Display", "driver_version": "1", "width": 1920,
                     "height": 1080, "refresh_hz": 60}],
    }
    evidence = physical.build_evidence(
        package_bytes=package_bytes, song_bytes=song, source_head_sha="0" * 40,
        executable_bytes=b"player",
        telemetry_text=(
            "AUDIO_SYNTHESIS_CPU_100NS=700\nAUDIO_SYNTHESIS_CPU_AVAILABLE=1\n"
            "AUDIO_WORKER_CPU_100NS=900\nAUDIO_WORKER_CPU_AVAILABLE=1\n"
            "AUDIO_OBSERVED_UNDERRUNS=0\nAUDIO_MISSING_SAMPLES_LOWER_BOUND=0\n"
            "AUDIO_CLOCK_DISCONTINUITIES=0\nAUDIO_STARVATION_OBSERVATIONS=0\n"
        ),
        observation=_observation(), environment=environment,
    )
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    monkeypatch.setenv("NW12_PHYSICAL_EVIDENCE", str(path))
    assert physical.load_verified_evidence(
        package_bytes=package_bytes, song_bytes=song
    ) is not None
    with pytest.raises(ValueError, match="package bytes"):
        physical.load_verified_evidence(package_bytes=b"other", song_bytes=song)
    pending = json.loads((ROOT / "benchmarks/nw12/audio-physical-evidence.v1.json")
                         .read_text(encoding="utf-8"))
    path.write_text(json.dumps(pending), encoding="utf-8")
    assert physical.load_verified_evidence() is None


def test_physical_capture_binds_one_run_and_raw_files(tmp_path):
    from audio_physical_validation import validate_capture_binding, sha256_file
    audio = tmp_path / "audio.wav"
    video = tmp_path / "video.mp4"
    audio.write_bytes(b"audio capture")
    video.write_bytes(b"video capture")
    run = {"format": "nodewerk-audio-physical-run", "version": 1, "run_id": "a" * 32}
    observation = _observation()
    observation["run_id"] = run["run_id"]
    observation["capture"].update(audio_sha256=sha256_file(audio), video_sha256=sha256_file(video))
    validate_capture_binding(run, observation, audio, video)
    with pytest.raises(ValueError, match="recorded playback run"):
        validate_capture_binding(dict(run, run_id="b" * 32), observation, audio, video)
    video.write_bytes(b"different recording")
    with pytest.raises(ValueError, match="video capture SHA-256 mismatch"):
        validate_capture_binding(run, observation, audio, video)


def test_ci_physical_inputs_bind_artifact_and_source():
    from audio_physical_validation import validate_physical_inputs
    artifact = {"executable_sha256": "a" * 64, "package_sha256": "b" * 64,
                "song_sha256": "c" * 64}
    provenance = {"source_revision": "d" * 40, "source_tree": "e" * 40}
    identity = {"format": "nodewerk-audio-physical-inputs", "version": 1,
                "source_commit": provenance["source_revision"],
                "source_tree": provenance["source_tree"], "artifact": artifact}
    validate_physical_inputs(identity, artifact, provenance)
    with pytest.raises(ValueError, match="artifact SHA-256"):
        validate_physical_inputs(identity, dict(artifact, executable_sha256="f" * 64), provenance)
    with pytest.raises(ValueError, match="source_commit"):
        validate_physical_inputs(identity, artifact, dict(provenance, source_revision="f" * 40))
    with pytest.raises(ValueError, match="source_tree"):
        validate_physical_inputs(identity, artifact, dict(provenance, source_tree="f" * 40))
