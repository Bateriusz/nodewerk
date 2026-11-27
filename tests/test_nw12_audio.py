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


def _test_physical_roundtrip_args(*, rate="60,1"):
    import audio_physical_validation as physical
    package_bytes, song = _audio_package_bytes()
    provenance = {
        "source_revision": "d" * 40, "source_tree": "e" * 40,
        "tracked_files_clean": True,
        **physical.provenance_snapshot(include_source=False),
    }
    build_identity = {
        "format": "nodewerk-audio-physical-inputs", "version": 1,
        "source_commit": provenance["source_revision"], "source_tree": provenance["source_tree"],
        "artifact": {"package_sha256": physical.sha256_bytes(package_bytes),
                     "executable_sha256": physical.sha256_bytes(b"test player"),
                     "song_sha256": physical.sha256_bytes(song)},
    }
    observation = _observation()
    output = (
        "AUDIO_SYNTHESIS_CPU_100NS=700\nAUDIO_SYNTHESIS_CPU_AVAILABLE=1\n"
        "AUDIO_WORKER_CPU_100NS=900\nAUDIO_WORKER_CPU_AVAILABLE=1\n"
        "AUDIO_OBSERVED_UNDERRUNS=0\nAUDIO_MISSING_SAMPLES_LOWER_BOUND=0\n"
        "AUDIO_CLOCK_DISCONTINUITIES=0\nAUDIO_STARVATION_OBSERVATIONS=0\n"
        f"AUDIO_PLAYBACK_EVIDENCE_V1={'c' * 64},{'d' * 64},{rate},240,192000\n"
    )
    return dict(package_bytes=package_bytes, song_bytes=song,
                source_head_sha=provenance["source_revision"], executable_bytes=b"test player",
                telemetry_text=output, observation=observation,
                environment={"status": "unavailable", "reason": "", "audio_endpoint": None,
                             "display": None, "os": {"version": "10.0.26200",
                                                      "build": "26200", "product_type": 1}},
                provenance=provenance, build_identity=build_identity,
                run_id="a" * 32, player_returncode=0)


def test_physical_evidence_verified_requires_all_observations(tmp_path):
    from audio_physical_validation import verify_evidence
    args = _test_physical_roundtrip_args()
    package_bytes, song = args["package_bytes"], args["song_bytes"]
    evidence, path, archive = _record_test_capture(tmp_path, args)
    assert evidence["verification_status"] == "verified_physical"
    assert evidence["alignment"]["within_one_displayed_frame"] is True
    assert evidence["unavailable"] == []
    assert {item["field"] for item in evidence["unavailable_telemetry"]} == {
        "measurements.physical_underruns"
    }
    assert verify_evidence(evidence, package_bytes=package_bytes, song_bytes=song,
                           evidence_path=path)["verification_status"] == "verified_physical"
    detached = verify_evidence(evidence)
    assert detached["verification_status"] == "pending_physical"
    assert detached["satisfies_completion"] is False
    assert detached["source_head_sha"] is None
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
    path = ROOT / "benchmarks/nw12/audio-physical-evidence.v1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    from schema_validator import validate_file_against_schema
    validate_file_against_schema(path, ROOT / "schemas/nw12-audio-physical-evidence.schema.json")
    if record["format"] == physical.PUBLIC_SUMMARY_FORMAT:
        assert record["verification_status"] == "reported_historical"
        assert record["archive_rechecked"] is False
        assert record["satisfies_completion"] is False
        assert physical.load_verified_evidence(path) is None
        return

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
    ).strip() == (
        record["implementation_base_tree"] if "implementation_base_tree" in record
        else record["provenance"]["implementation_base_tree"]
    )
    message = subprocess.check_output(
        ["git", "log", "-1", "--format=%s", base], cwd=ROOT, text=True
    )
    assert "NW-11" in message


def test_verified_physical_evidence_merges_only_for_matching_artifact(tmp_path, monkeypatch):
    import audio_physical_validation as physical
    args = _test_physical_roundtrip_args()
    package_bytes, song = args["package_bytes"], args["song_bytes"]
    evidence, path, archive = _record_test_capture(tmp_path, args)
    monkeypatch.setenv("NW12_PHYSICAL_EVIDENCE", str(path))
    assert physical.load_verified_evidence(
        package_bytes=package_bytes, song_bytes=song
    ) is not None
    assert physical.load_verified_evidence(
        package_bytes=package_bytes, song_bytes=song, source_head_sha="f" * 40
    ) is None
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


def test_runtime_refresh_not_other_monitor_determines_acceptance(tmp_path):
    from audio_physical_validation import build_evidence
    args = _test_physical_roundtrip_args(rate="144,1")
    args["environment"]["display"] = [{"refresh_hz": 60}]
    args["observation"]["impulses"][2]["display_tick"] -= 300
    args["observation"]["impulses"][3]["display_tick"] += 800
    record, _, _ = _record_test_capture(tmp_path / "outside", args)
    assert record["verification_status"] == "pending_physical"
    assert record["alignment"]["max_abs_delta_ticks"] == 500
    assert record["alignment"]["display_refresh_hz"] == 144
    args["observation"]["impulses"][2]["display_tick"] -= 167
    record, _, _ = _record_test_capture(tmp_path / "within", args)
    assert record["verification_status"] == "verified_physical"
    args["observation"]["impulses"][2]["display_tick"] += 1
    record, _, _ = _record_test_capture(tmp_path / "boundary", args)
    assert record["verification_status"] == "pending_physical"

    args["telemetry_text"] = args["telemetry_text"].split("AUDIO_PLAYBACK_EVIDENCE_V1")[0]
    assert build_evidence(**args)["environment"]["status"] == "unavailable"


def test_complete_observation_without_build_identity_stays_pending(tmp_path):
    from audio_physical_validation import build_evidence
    args = _test_physical_roundtrip_args()
    pending, _, _ = _record_test_capture(tmp_path, dict(args, build_identity=None))
    assert pending["verification_status"] == "pending_physical"
    assert pending["source_head_sha"] is None
    assert any(item["field"] == "build_identity" for item in pending["unavailable"])
    with pytest.raises(ValueError, match="artifact SHA-256"):
        build_evidence(**dict(args, executable_bytes=b"other player"))


def test_shared_verification_recomputes_observations_and_bindings(tmp_path):
    from copy import deepcopy
    import audio_physical_validation as physical
    from schema_validator import SchemaValidationError, validate_file_against_schema

    good, path, archive = _record_test_capture(tmp_path, _test_physical_roundtrip_args())
    schema = ROOT / "schemas/nw12-audio-physical-evidence.schema.json"
    path.write_text(json.dumps(good))
    validate_file_against_schema(path, schema)
    assert physical.load_verified_evidence(path) is not None

    for section, key, value in (
        ("alignment", "max_abs_delta_ticks", 100),
        ("alignment", "impulse_count", 3),
        ("alignment", "impulses", [{**row, "display_tick": row["display_tick"] + 2000}
                                   for row in good["alignment"]["impulses"]]),
        ("artifact", "executable_sha256", None),
        ("environment", "display", None),
        ("measurements", "clock_discontinuities", -1),
        ("measurements", "synthesis_cpu_ms", 0),
        ("", "run_id", "f" * 32),
        ("", "player_returncode", 81),
    ):
        bad = deepcopy(good)
        (bad[section] if section else bad)[key] = value
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            physical.load_verified_evidence(path)
        with pytest.raises(SchemaValidationError):
            validate_file_against_schema(path, schema)


def _record_test_capture(tmp_path, args):
    """Synthetic bytes for contract tests only, never hardware evidence."""
    from copy import deepcopy
    import audio_physical_validation as physical

    tmp_path.mkdir(parents=True, exist_ok=True)
    run_path, obs_path, audio, video, package, song, player, output = [
        tmp_path / name for name in ("run.json", "obs.json", "audio.wav", "video.mp4",
                                     "audio.nwp", "song.nwas", "player.exe", "report.json")
    ]
    audio.write_bytes(b"synthetic audio for test")
    video.write_bytes(b"synthetic video for test")
    package.write_bytes(args["package_bytes"])
    song.write_bytes(args["song_bytes"])
    player.write_bytes(args["executable_bytes"])
    run = dict(format="nodewerk-audio-physical-run", version=1, run_id=args["run_id"],
               returncode=args["player_returncode"], stdout=args["telemetry_text"],
               artifact={"package_sha256": physical.sha256_file(package),
                         "song_sha256": physical.sha256_file(song),
                         "executable_sha256": physical.sha256_file(player)},
               build_identity=args["build_identity"], environment=args["environment"],
               provenance=args["provenance"])
    run_path.write_text(json.dumps(run), encoding="utf-8")
    observation = deepcopy(args["observation"])
    observation["run_id"] = run["run_id"]
    observation["capture"].update(audio_sha256=physical.sha256_file(audio),
                                  video_sha256=physical.sha256_file(video))
    obs_path.write_text(json.dumps(observation), encoding="utf-8")
    recorded = physical.record(player=player, package=package, song=song, output=output,
                               run_path=run_path, observation_path=obs_path,
                               audio_capture=audio, video_capture=video)
    return recorded, output, physical.ArchiveFiles(run_path, obs_path, audio, video)


def test_recorder_verified_evidence_archive_rechecks_original_measurements(tmp_path, capsys):
    from copy import deepcopy
    import audio_physical_validation as physical
    from schema_validator import SchemaValidationError, validate_file_against_schema

    recorded, output, archive = _record_test_capture(tmp_path, _test_physical_roundtrip_args())
    assert recorded["verification_status"] == "verified_physical"
    assert recorded["observation_sha256"] == physical.sha256_file(archive.observation)
    assert physical.verify_evidence(recorded, archive_files=archive) == recorded

    # An older report without an observation-byte digest cannot be promoted.
    unbound = deepcopy(recorded)
    unbound.pop("observation_sha256")
    unbound["alignment"]["capture"].pop("observation_sha256")
    assert physical.verify_evidence(unbound, archive_files=archive)["verification_status"] == "pending_physical"

    # Shift both onsets while preserving every report-local delta and count.
    changed = deepcopy(recorded)
    changed["alignment"]["impulses"][1]["audio_tick"] += 100
    changed["alignment"]["impulses"][1]["display_tick"] += 100
    output.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="original measured onsets"):
        physical.load_verified_evidence(output)
    schema = ROOT / "schemas/nw12-audio-physical-evidence.schema.json"
    with pytest.raises(SchemaValidationError, match="original measured onsets"):
        validate_file_against_schema(output, schema)
    output.write_text(json.dumps(recorded), encoding="utf-8")

    # Even identical observations reserialized to different bytes aren't original.
    original = archive.observation.read_bytes()
    archive.observation.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="original observation JSON SHA-256"):
        physical.load_verified_evidence(output)
    archive.observation.write_bytes(original)

    archive.observation.unlink()
    assert physical.load_verified_evidence(output) is None
    assert physical.main(["verify", "--evidence", str(output)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "valid": True, "verification_status": "pending_physical", "archive_rechecked": False,
    }
    with pytest.raises(SchemaValidationError, match="original capture archive"):
        validate_file_against_schema(output, schema)
    archive.observation.write_bytes(original)

    command = ["verify", "--evidence", str(output), "--run", str(archive.run),
               "--observation", str(archive.observation), "--capture-audio", str(archive.audio),
               "--capture-video", str(archive.video)]
    assert physical.main(command) == 0
    assert json.loads(capsys.readouterr().out)["archive_rechecked"] is True
    archive.audio.write_bytes(b"changed media")
    with pytest.raises(ValueError, match="audio capture SHA-256"):
        physical.verify_evidence(recorded, archive_files=archive)


def test_physical_archive_discovery_cannot_escape_report_directory(tmp_path):
    from audio_physical_validation import ArchiveFiles

    root = tmp_path / "archive"
    root.mkdir()
    external = tmp_path / "external"
    external.write_text("unrelated")
    invalid = ["../external", str(external), "C:external"]
    try:
        (root / "link").symlink_to(external)
    except OSError:  # Windows may restrict symlink creation for test users.
        pass
    else:
        invalid.append("link")
    for bad in invalid:
        with pytest.raises(ValueError, match="must remain relative"):
            ArchiveFiles.from_report(root / "report.json", {
                "archive_files": {"run": bad, "observation": "obs.json",
                                  "audio": "recording.mp4", "video": "recording.mp4"},
            })


def test_studio_same_path_song_edit_replaces_prepared_payload(tmp_path, test_dxc):
    import nw03_compiler
    import nw05_export
    import nwp_package
    from nw08_preview import RuntimePackageExporter, WindowsRuntimePreviewBackend

    path = tmp_path / "selected.json"
    first, second = fixture(), fixture()
    second["events"][0]["gain"] = 1000
    path.write_text(json.dumps(first))
    host = tmp_path / "test-preview-host"
    host.touch()

    def export_from_snapshot(*args, **kwargs):
        # File changes after the preview key was built, before export.
        path.write_text(json.dumps(second))
        return nw05_export.export_graph(*args, **kwargs)

    exporter = RuntimePackageExporter(
        settings=nw05_export.ExportSettings(audio_song_path=str(path)),
        dxc_executable=test_dxc, allow_test_payloads=True,
        export_function=export_from_snapshot,
    )
    backend = WindowsRuntimePreviewBackend(host, exporter=exporter)
    graph = nw03_compiler.load_graph(ROOT / "fixtures/audio/sync_flash.nwgraph")
    try:
        key_a, bundle_a, _ = backend._ensure_bundle(graph, width=256, height=256)
        assert nwp_package.decode_package(bundle_a.package_path.read_bytes()).audio_song == compile_song(first)
        key_b, bundle_b, _ = backend._ensure_bundle(graph, width=256, height=256)
        assert key_b != key_a
        assert nwp_package.decode_package(bundle_b.package_path.read_bytes()).audio_song == compile_song(second)
        path.write_text(json.dumps(second, indent=4))
        key_c, bundle_c, _ = backend._ensure_bundle(graph, width=256, height=256)
        assert key_c == key_b and bundle_c is bundle_b
    finally:
        backend.close()


def test_package_rejects_unsupported_header_flags_generically():
    from dataclasses import replace
    from hashlib import sha256
    import nwp_package as nwp

    raw, _ = _audio_package_bytes()
    model = nwp.decode_package(raw)
    with pytest.raises(nwp.PackageValidationError):
        nwp.encode_package(replace(model, flags=model.flags | 8))
    bad = bytearray(raw)
    struct.pack_into("<I", bad, 16, model.flags | 8)
    bad[-32:] = sha256(bad[:-32]).digest()
    with pytest.raises(nwp.PackageValidationError, match="unsupported package header flags"):
        nwp.decode_package(bytes(bad))


def test_public_projection_is_allowlisted_historical_and_has_no_acceptance_authority(tmp_path):
    from copy import deepcopy
    import audio_physical_validation as physical
    from schema_validator import SchemaValidationError, validate_file_against_schema

    args = _test_physical_roundtrip_args()
    private_detail = "/home/private-user/machine-serial/session"
    args["observation"]["capture"]["operator_note"] = private_detail
    private, original, archive = _record_test_capture(tmp_path / "private", args)
    assert private_detail in original.read_text(encoding="utf-8")
    public = tmp_path / "summary.json"
    assert physical.main(["summarize", "--evidence", str(original), "--output", str(public)]) == 0
    summary = json.loads(public.read_text(encoding="utf-8"))
    assert summary["verification_status"] == "reported_historical"
    assert summary["reported_decision"] == "verified_physical"
    assert summary["archive_rechecked"] is False and summary["satisfies_completion"] is False
    assert summary["artifact"] == private["artifact"]
    assert summary["measured_source_commit"] == private["source_head_sha"]
    assert summary["measured_source_tree"] == private["provenance"]["source_tree"]
    assert summary["archive_digests"]["observation_sha256"] == private["observation_sha256"]
    assert summary["archive_digests"]["run_sha256"] == private["run_sha256"]
    public_text = public.read_text(encoding="utf-8")
    for secret in (private_detail, archive.observation.name, archive.run.name, archive.audio.name,
                   private["run_id"], "audio_endpoint", "operator_note"):
        assert secret not in public_text
    public_schema = ROOT / "schemas" / physical.PUBLIC_SUMMARY_SCHEMA
    repo_slot_schema = ROOT / "schemas/nw12-audio-physical-evidence.schema.json"
    validate_file_against_schema(public, public_schema)
    validate_file_against_schema(public, repo_slot_schema)
    assert physical.load_verified_evidence(public) is None
    with pytest.raises(ValueError, match="unexpected NW-12 physical evidence format"):
        physical.verify_evidence(summary, archive_files=archive)

    ref = physical.evidence_reference("not a physical measurement", verified=private,
                                      playback_artifact_matches=True)
    assert ref["status"] == "reported_historical" and ref["summary"] == summary
    assert "evidence" not in ref and ref["artifact_match"] == "executable_package_song"

    # The public route cannot smuggle private details or claim verification.
    for bad in (
        {**summary, "archive_files": private["archive_files"]},
        {**summary, "archive_rechecked": True},
        {**summary, "satisfies_completion": True},
        {**summary, "verification_status": "verified_physical"},
        {**summary, "environment": {**summary["environment"], "host": private_detail}},
    ):
        public.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(SchemaValidationError):
            validate_file_against_schema(public, repo_slot_schema)

    detached = tmp_path / "detached.json"
    detached.write_text(json.dumps(private), encoding="utf-8")
    with pytest.raises(ValueError, match="requires a verified private decision"):
        physical.summarize_private_evidence(detached)
    assert physical.load_verified_evidence(detached) is None

    original_observation = archive.observation.read_bytes()
    archive.observation.write_bytes(original_observation + b"\n")
    with pytest.raises(ValueError, match="original observation JSON SHA-256"):
        physical.summarize_private_evidence(original)


def test_public_benchmark_registry_refuses_private_archive_even_when_complete(tmp_path, monkeypatch):
    from shutil import copyfile
    import audio_physical_validation as physical
    import schema_validator

    private_schema = "schemas/nw12-audio-physical-evidence.schema.json"
    for relative in (private_schema, f"schemas/{physical.PUBLIC_SUMMARY_SCHEMA}"):
        dst = tmp_path / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        copyfile(ROOT / relative, dst)
    slot = tmp_path / physical.EVIDENCE_RECORD
    args = _test_physical_roundtrip_args()
    args["observation"]["capture"]["operator_note"] = "/home/synthetic-test/private-audit"
    private, record, _ = _record_test_capture(slot.parent, args)
    record.replace(slot)
    monkeypatch.setattr(schema_validator, "ARTIFACT_SCHEMA_MAPPINGS", [
        (physical.EVIDENCE_RECORD, private_schema),
    ])

    assert physical.verify_evidence(private, evidence_path=slot) == private
    schema_validator.validate_file_against_schema(slot, tmp_path / private_schema)
    with pytest.raises(schema_validator.SchemaValidationError, match="sanitized historical summary"):
        schema_validator.validate_all_repository_artifacts(tmp_path)

    summary = physical.summarize_private_evidence(slot)
    slot.write_text(json.dumps(summary), encoding="utf-8")
    assert schema_validator.validate_all_repository_artifacts(tmp_path)["validated_count"] == 1

    copyfile(ROOT / physical.EVIDENCE_RECORD, slot)
    assert schema_validator.validate_all_repository_artifacts(tmp_path)["validated_count"] == 1
