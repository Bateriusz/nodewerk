from __future__ import annotations

import json
import struct
import sys
import wave
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import audio_physical_validation as physical
import audio_song
import nw03_compiler
import nw05_export
import nw05_optimizer
import nwp_package as nwp

GRAPH = ROOT / "fixtures/nw13/hotel_corridor.nwgraph"
SONG = ROOT / "fixtures/nw13/hotel_corridor.song.json"
MEDIA = ROOT / "fixtures/nw13/av-contract"
PLAYBACK_START = 4800
CAPTURE_END = 4 * 48000


@pytest.fixture
def film(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    graph = nw03_compiler.load_graph(GRAPH)
    song = nw05_export.read_audio_song(str(SONG), film_document=graph)
    artifact = nw05_export.export_graph(
        GRAPH, tmp_path / "export", allow_test_payloads=True, audio_song_payload=song,
    ).artifacts[nwp.COMMON_PROFILE_ID]
    schedule = artifact.manifest["audio"]["presentation_cues"]
    model = audio_song.decode_song(song)
    root = tmp_path / "bundle"
    root.mkdir()
    package, player, manifest = [root / name for name in ("film.nwp", "player.exe", "film.manifest.json")]
    package.write_bytes(artifact.package_bytes)
    manifest.write_bytes(nw05_optimizer.manifest_bytes(artifact.manifest))
    player.write_bytes(nw05_export.embed_package(b"MZ synthetic test player - not executable",
                         package.read_bytes(), manifest.read_bytes()))
    identity = {"executable_sha256": physical.sha256_file(player),
                "package_sha256": physical.sha256_file(package), "song_sha256": physical.sha256_bytes(song)}
    arms = {variant: {"package": package.name, "executable": player.name, "manifest": manifest.name,
                      **identity, "manifest_sha256": physical.sha256_file(manifest)}
            for variant in ("common", "fallback")}
    bundle = root / "film-physical.json"
    bundle.write_text(json.dumps({"format": physical.FILM_BUNDLE_FORMAT, "version": 1,
                      "revision": "a" * 40, "tree": "b" * 40, "arms": arms,
                      "graph_sha256": physical.sha256_file(GRAPH),
                      "files": {p.name: physical.sha256_file(p) for p in (package, player, manifest)}}),
                      encoding="utf-8")
    return SimpleNamespace(song=song, model=model, schedule=schedule, package=package, player=player,
                           bundle=bundle, manifest=manifest, tmp_path=tmp_path)


def _observation(film, delta=400):
    rows = [{"cue_id": cue["cue_id"], "presentation_tick": cue["presentation_tick"],
             "audio_tick": PLAYBACK_START + cue["presentation_tick"] + 40,
             "display_tick": PLAYBACK_START + cue["presentation_tick"] + 40 + delta}
            for cue in film.schedule["cues"]]
    return {"format": physical.OBSERVATION_FORMAT, "version": 2,
            "sample_rate": 48000, "impulses": rows,
            "capture_clock": {"audio_origin_tick": 0, "video_origin_tick": 0,
                              "playback_start_tick": PLAYBACK_START}}


def test_actual_film_uses_emitted_flash_not_action_tick_or_every_music_note(film):
    assert film.schedule["graph_sha256"] == physical.sha256_file(GRAPH)
    assert film.schedule["song_sha256"] == physical.sha256_bytes(film.song)
    assert len(film.model["notes"]) == 30
    assert len(film.schedule["cues"]) == 1
    planned = film.schedule["cues"][0]
    assert (planned["cue_id"], planned["presentation_tick"]) == ("flash", 148800)
    note = film.model["notes"][planned["effect_note_index"]]
    assert note["bus"] == "effect" and note["tick"] == 148800

    source_note = next(note for track in json.loads(SONG.read_bytes())["tracks"]
                       for note in track["notes"] if note["id"] == "flash")
    assert source_note["tick"] == 144000
    declared = dict(presentation_cues=film.schedule, song_sha256=physical.sha256_bytes(film.song))
    aligned = physical.evaluate_alignment(_observation(film), film.model, 120, **declared)
    assert aligned["impulse_count"] == 1 and aligned["within_one_displayed_frame"]
    assert not physical.evaluate_alignment(_observation(film, 401), film.model, 120,
                                          **declared)["within_one_displayed_frame"]
    raw = _observation(film)
    for invalid in (
        dict(raw, impulses=[]),
        dict(raw, impulses=[{**raw["impulses"][0], "presentation_tick": 144000}]),
        dict(raw, impulses=[{**raw["impulses"][0], "cue_id": "not-flash"}]),
        dict(raw, impulses=raw["impulses"] * len(film.model["notes"])),
        dict(raw, version=1),
    ):
        with pytest.raises(ValueError):
            physical.evaluate_alignment(invalid, film.model, 120, **declared)
    for invalid in (None, dict(film.schedule, cues=[]),
                    dict(film.schedule, song_sha256="0" * 64)):
        with pytest.raises(ValueError):
            physical.evaluate_alignment(raw, film.model, 120,
                                        presentation_cues=invalid,
                                        song_sha256=physical.sha256_bytes(film.song))


def _simulated_playback(film, monkeypatch):
    # Portable integration only: no Windows, external executable or real measurement.
    run_path = film.tmp_path / "private" / "run.json"
    launch = []
    stdout = (
        "AUDIO_SYNTHESIS_CPU_100NS=700\nAUDIO_SYNTHESIS_CPU_AVAILABLE=1\n"
        "AUDIO_WORKER_CPU_100NS=900\nAUDIO_WORKER_CPU_AVAILABLE=1\n"
        "AUDIO_OBSERVED_UNDERRUNS=0\nAUDIO_MISSING_SAMPLES_LOWER_BOUND=0\n"
        "AUDIO_CLOCK_DISCONTINUITIES=0\nAUDIO_STARVATION_OBSERVATIONS=0\n"
        f"AUDIO_PLAYBACK_EVIDENCE_V1={'e' * 64},{'d' * 64},120,1,240,288000\n"
    )
    monkeypatch.setattr(physical, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(physical, "subprocess", SimpleNamespace(
        run=lambda command, **kw: launch.append(command) or SimpleNamespace(
            returncode=0, stdout=stdout.encode("ascii"), stderr=b""),
    ))
    monkeypatch.setattr(physical, "probe_environment", lambda *, include_host_identity: {
        "status": "unavailable", "reason": "synthetic host", "audio_endpoint": None, "display": None,
        "os": {"version": "10.0.26200", "build": "26200", "product_type": 1},
        "host_id_sha256": "c" * 64,
    } if include_host_identity else pytest.fail("v2 must bind the host"))
    run = physical.run_playback(
        player=film.player, package=film.package, song=None, output=run_path, film_bundle=film.bundle,
        film_bundle_sha256=physical.sha256_file(film.bundle), variant="common",
    )
    assert launch == [[str(film.player), "--audio-evidence", "--maximized"]]
    observation, capture = run_path.with_name("observation.json"), run_path.with_name("synthetic.mp4")
    capture.write_bytes((MEDIA / "synthetic-timeline-mux.mp4").read_bytes())
    return run, run_path, observation, capture


def test_physical_film_record_rechecks_exact_embedded_bytes_source_and_capture(film, monkeypatch):
    run, path, observation, capture = _simulated_playback(film, monkeypatch)
    captured = dict(_observation(film), run_id=run["run_id"], binding=run["observation_binding"],
                    capture={k: physical.sha256_file(capture)
                             for k in ("audio_sha256", "video_sha256")})
    observation.write_text(json.dumps(captured), encoding="utf-8")
    output = path.with_name("evidence.json")
    kwargs = dict(player=film.player, package=film.package, song=None, output=output,
                  run_path=path, observation_path=observation,
                  audio_capture=capture, video_capture=capture)
    record = physical.record(**kwargs)
    assert (record["task"], record["version"]) == ("NW-13", 2)
    assert record["verification_status"] == "verified_physical"
    assert record["song_nwas_hex"] == film.song.hex()
    assert record["environment"]["host_id_sha256"] == "c" * 64
    assert record["measurements"]["physical_underruns"] == "unavailable"
    assert physical.load_verified_evidence(output, package_bytes=film.package.read_bytes(),
           executable_bytes=film.player.read_bytes(), song_bytes=film.song) == record
    public = physical.summarize_private_evidence(output)
    assert public["variant"] == "common" and public["satisfies_completion"] is False
    assert "host_id_sha256" not in json.dumps(public) and "song_nwas_hex" not in public
    assert physical.verify_evidence(record)["verification_status"] == "pending_physical"

    for key in ("source_commit", "source_tree", "variant", "host_id_sha256",
                "display_id_sha256", "audio_endpoint_id_sha256",
                "executable_sha256", "presentation_cues_sha256"):
        changed = deepcopy(captured)
        changed["binding"][key] = "fallback" if key == "variant" else "0" * 64
        observation.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ValueError, match="same source, variant, host, display and endpoint"):
            physical.record(**kwargs)
    observation.write_text(json.dumps(dict(captured, impulses=[])), encoding="utf-8")
    with pytest.raises(ValueError, match="one impulse per emitted presentation cue"):
        physical.record(**kwargs)
    observation.write_text(json.dumps(captured), encoding="utf-8")
    bad_report = dict(record, song_nwas_hex=(b"NWAS" + film.song[4:-1] + b"\xff").hex())
    with pytest.raises(ValueError):
        physical.verify_evidence(bad_report, evidence_path=output)
    stale = dict(record, presentation_cues=dict(film.schedule, graph_sha256="0" * 64))
    with pytest.raises(ValueError, match="attested artifact/source"):
        physical.verify_evidence(stale, evidence_path=output)
    observation.write_text(json.dumps({**captured, "impulses": _observation(film, 200)["impulses"]}),
                           encoding="utf-8")
    with pytest.raises(ValueError, match="original observation JSON"):
        physical.load_verified_evidence(output)


def test_film_preflight_rejects_stale_manifest_graph_or_exact_player(film):
    bundle_hash = physical.sha256_file(film.bundle)
    artifact = {"executable_sha256": physical.sha256_file(film.player),
                "package_sha256": physical.sha256_file(film.package),
                "song_sha256": physical.sha256_bytes(film.song)}
    args = dict(player=film.player, package_path=film.package, artifact=artifact,
                manifest_sha256=physical.sha256_file(film.manifest), schedule=film.schedule)
    with pytest.raises(ValueError, match="not staged"):
        physical._film_build_inputs(film.bundle, bundle_hash, "dxr", **args)
    with pytest.raises(ValueError, match="attested bundle"):
        physical._film_build_inputs(film.bundle, bundle_hash, "common",
                                    **dict(args, player=film.bundle.parent / "stranger.exe"))
    changed = json.loads(film.bundle.read_bytes())
    changed["graph_sha256"] = "0" * 64
    film.bundle.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="externally verified"):
        physical._film_build_inputs(film.bundle, bundle_hash, "common", **args)
    with pytest.raises(ValueError, match="staged graph and variant"):
        physical._film_build_inputs(film.bundle, physical.sha256_file(film.bundle), "common", **args)


def _write_observation(film, run, where, audio, video):
    captured = dict(_observation(film), run_id=run["run_id"], binding=run["observation_binding"],
                    capture={"audio_sha256": physical.sha256_file(audio),
                             "video_sha256": physical.sha256_file(video)})
    where.write_text(json.dumps(captured), encoding="utf-8")
    return captured


@pytest.mark.parametrize("filename,rate", [
    ("synthetic-aac-88200.mp4", 88200),
    ("synthetic-aac-96000.mp4", 96000),
    ("synthetic-alac-96000.mov", 96000),
    ("synthetic-alac-96000.mp4", 96000),
    ("synthetic-alac-192000.mp4", 192000),
])
def test_film_records_and_rechecks_high_rate_compressed_captures(film, monkeypatch, filename, rate):
    run, path, observation, capture = _simulated_playback(film, monkeypatch)
    capture = capture.with_name(filename)
    data = (MEDIA / filename).read_bytes()
    audio_entry = data.rindex(b"stsd") + 16
    if filename.endswith(".mp4"):
        assert data[audio_entry:audio_entry + 4] == (b"alac" if "-alac-" in filename else b"mp4a")
        assert struct.unpack_from(">H", data, audio_entry + 12)[0] == 0
        assert struct.unpack_from(">I", data, audio_entry + 28)[0] == 0
    else:
        assert data[audio_entry:audio_entry + 4] == b"alac"
        assert struct.unpack_from(">H", data, audio_entry + 12)[0] == 2
        assert struct.unpack_from(">d", data, audio_entry + 36)[0] == rate
    capture.write_bytes(data)
    archive = physical.inspect_capture(capture, {"audio", "video"}, check_container=True)
    assert archive.tracks["audio"].media_scale == rate
    assert archive.tracks["audio"].contains(CAPTURE_END - 1, 48000)
    assert not archive.tracks["audio"].contains(CAPTURE_END, 48000)
    _write_observation(film, run, observation, capture, capture)
    evidence = path.with_name("evidence.json")
    result = physical.record(player=film.player, package=film.package, song=None, output=evidence,
                             run_path=path, observation_path=observation,
                             audio_capture=capture, video_capture=capture)
    assert result["verification_status"] == "verified_physical"
    assert physical.load_verified_evidence(evidence) == result


@pytest.mark.parametrize("filename,offset,fmt,value,reason", [
    ("synthetic-mux.mp4", 28, ">I", 0, "AAC sample rate does not agree"),
    ("synthetic-alac-96000.mov", 36, ">d", float("nan"), "version 2 extension"),
    ("synthetic-alac-96000.mov", 36, ">d", 96001, "ALAC sample description disagrees"),
    ("synthetic-alac-96000.mov", 44, ">I", 2, "ALAC sample description disagrees"),
    ("synthetic-alac-96000.mov", 48, ">I", 0, "version 2 extension"),
    ("synthetic-alac-96000.mov", 10, ">H", 0, "self-contained sample description"),
])
def test_extended_audio_entries_still_require_matching_metadata(tmp_path, filename, offset, fmt, value, reason):
    data = bytearray((MEDIA / filename).read_bytes())
    audio_entry = data.rindex(b"stsd") + 16
    struct.pack_into(fmt, data, audio_entry + offset, value)
    capture = tmp_path / filename
    capture.write_bytes(data)
    with pytest.raises(ValueError, match=reason):
        physical.inspect_capture(capture, {"audio", "video"}, check_container=True)


@pytest.mark.parametrize("offset,fmt,value", [
    (24, ">I", 48000),
    (24, ">I", 0),
    (13, ">B", 1),
    (9, ">B", 24),
])
def test_zero_rate_alac_mp4_requires_matching_high_rate_codec_config(tmp_path, offset, fmt, value):
    data = bytearray((MEDIA / "synthetic-alac-96000.mp4").read_bytes())
    config = data.rindex(b"alac") + 4
    assert struct.unpack_from(">I", data, config + 24)[0] == 96000
    assert (data[config + 9], data[config + 13]) == (16, 2)
    struct.pack_into(fmt, data, config + offset, value)
    capture = tmp_path / "synthetic.mp4"
    capture.write_bytes(data)
    with pytest.raises(ValueError, match="ALAC sample description disagrees"):
        physical.inspect_capture(capture, {"audio", "video"}, check_container=True)


def _broken_audio_sample_table(kind):
    # For the two-track synthetic fixture, the last table of each type belongs
    # to audio. Keep each containing size and every untouched capture byte.
    original = (MEDIA / "synthetic-mux.mp4").read_bytes()
    recording = bytearray(original)
    stsc = original.rindex(b"stsc")
    stco = original.rindex(b"stco")
    stsd = original.rindex(b"stsd")
    dref = original.rindex(b"dref")
    mdat = original.index(b"mdat")
    mdat_end = mdat - 4 + struct.unpack_from(">I", original, mdat - 4)[0]
    assert struct.unpack_from(">I", original, stsc + 8)[0] == 2
    assert struct.unpack_from(">I", original, stco + 8)[0] == 3
    if kind == "missing map":
        recording[stsc:stsc + 4] = b"free"
    else:
        position, value = {
            "starts after first chunk": (stsc + 12, 2),
            "empty chunk mapping": (stsc + 16, 0),
            "invalid sample description index": (stsc + 20, 2),
            "duplicate chunk mapping": (stsc + 24, 1),
            "out of range chunk mapping": (stsc + 24, 4),
            "unmapped samples": (stsc + 28, 4),
            "overmapped samples": (stsc + 28, 6),
            "absent sample description": (stsd + 8, 2),
            "external data reference": (dref + 20, 0),
            "later chunk outside mdat": (stco + 16, mdat_end),
            "later chunk crosses mdat boundary": (stco + 20, mdat_end - 2),
            "later chunk overlaps video": (stco + 20, struct.unpack_from(
                ">I", original, original.index(b"stco") + 12)[0]),
        }[kind]
        struct.pack_into(">I", recording, position, value)
    return bytes(recording)


def test_physical_film_record_rejects_hash_matched_mux_without_audio_chunk_map(film, monkeypatch):
    run, path, observation, capture = _simulated_playback(film, monkeypatch)
    capture.write_bytes(_broken_audio_sample_table("missing map"))
    _write_observation(film, run, observation, capture, capture)
    output = path.with_name("evidence.json")
    with pytest.raises(ValueError, match="sample-to-chunk table"):
        physical.record(player=film.player, package=film.package, song=None, output=output,
                        run_path=path, observation_path=observation,
                        audio_capture=capture, video_capture=capture)
    assert not output.exists()


@pytest.mark.parametrize("mutation, reason", [
    ("starts after first chunk", "sample-to-chunk table"),
    ("empty chunk mapping", "sample-to-chunk table"),
    ("invalid sample description index", "sample-to-chunk table"),
    ("duplicate chunk mapping", "sample-to-chunk table"),
    ("out of range chunk mapping", "sample-to-chunk table"),
    ("unmapped samples", "sample count"),
    ("overmapped samples", "sample count"),
    ("absent sample description", "sample descriptions"),
    ("external data reference", "self-contained"),
    ("later chunk outside mdat", "outside its media payload"),
    ("later chunk crosses mdat boundary", "outside its media payload"),
    ("later chunk overlaps video", "overlap"),
])
def test_mux_requires_exact_sample_chunk_layout_and_ownership(tmp_path, mutation, reason):
    capture = tmp_path / "synthetic.mp4"
    capture.write_bytes(_broken_audio_sample_table(mutation))
    with pytest.raises(ValueError, match=reason):
        physical.inspect_capture(capture, {"audio", "video"}, check_container=True)


def _broken_sample_entry(mutation):
    data = bytearray((MEDIA / "synthetic-mux.mp4").read_bytes())
    audio = data.rindex(b"stsd") + 16
    video = data.index(b"stsd") + 16
    assert (data[audio:audio + 4], data[video:video + 4]) == (b"mp4a", b"mp4v")
    audio_config = data.rindex(b"esds")
    assert data[audio_config + 16] == 4
    where, broken = {
        "generic box in audio stsd": (audio, b"free"),
        "video type in audio stsd": (audio, b"avc1"),
        "audio without codec config": (audio_config, b"free"),
        "audio without decoder descriptor": (audio_config + 16, b"\x02"),
        "audio without channels": (audio + 20, b"\0\0"),
        "video without width": (video + 28, b"\0\0"),
        "video without codec config": (data.index(b"esds"), b"free"),
    }[mutation]
    data[where:where + len(broken)] = broken
    return bytes(data)


@pytest.mark.parametrize("mutation", [
    "generic box in audio stsd", "video type in audio stsd", "audio without codec config",
    "audio without decoder descriptor", "audio without channels", "video without width",
    "video without codec config",
])
def test_mux_requires_supported_structured_role_specific_entries(tmp_path, mutation):
    capture = tmp_path / "synthetic.mp4"
    capture.write_bytes(_broken_sample_entry(mutation))
    with pytest.raises(ValueError, match="MP4 sample description"):
        physical.inspect_capture(capture, {"audio", "video"}, check_container=True)


def _synthetic_wave(fmt, bits, extension=b""):
    frames, rate, width = 12, 24000, (bits + 7) // 8
    header = struct.pack("<HHIIHH", fmt, 1, rate, rate * width, width, bits) + extension
    chunks = b"fmt " + struct.pack("<I", len(header)) + header + b"data" + struct.pack(
        "<I", frames * width,
    ) + bytes(frames * width)
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


@pytest.mark.parametrize("fmt,bits,extension,reason", [
    (3, 8, b"", "floating-point sample width"),
    (1, 12, b"", "integer or floating-point sample width"),
    (0xfffe, 32, struct.pack("<HHI", 21, 32, 1) + bytes(16), "extension length"),
    (0xfffe, 32, struct.pack("<HHI", 22, 32, 1) + bytes(16), "subformat"),
    (0xfffe, 32, bytes(2), "truncated PCM WAV extensible"),
])
def test_wav_rejects_impossible_sample_format(tmp_path, fmt, bits, extension, reason):
    capture = tmp_path / "synthetic.wav"
    capture.write_bytes(_synthetic_wave(fmt, bits, extension))
    with pytest.raises(ValueError, match=reason):
        physical.inspect_capture(capture, {"audio"}, check_container=True)


def test_wav_accepts_float_and_extensible_pcm(tmp_path):
    capture = tmp_path / "synthetic.wav"
    pcm_guid = bytes.fromhex("0100000000001000800000aa00389b71")
    for fmt, bits, extension in ((3, 64, bytes(2)), (3, 32, bytes(2)),
                                 (0xfffe, 32, struct.pack("<HHI", 22, 24, 1) + pcm_guid)):
        capture.write_bytes(_synthetic_wave(fmt, bits, extension))
        timeline = physical.inspect_capture(capture, {"audio"}, check_container=True).tracks["audio"]
        assert timeline.contains(23, 48000)
        assert not timeline.contains(24, 48000)


def test_mp4_presentation_ranges_follow_media_edits_and_exclude_the_end(tmp_path):
    capture = tmp_path / "synthetic.mp4"
    original = bytearray((MEDIA / "synthetic-mux.mp4").read_bytes())
    capture.write_bytes(original)
    tracks = physical.inspect_capture(capture, {"audio", "video"}, check_container=True).tracks
    for role in ("audio", "video"):
        assert tracks[role].contains(0, 48000)
        assert tracks[role].contains(9599, 48000)
        assert not tracks[role].contains(9600, 48000)
        assert not tracks[role].contains(-1, 48000)

    edit = original.rindex(b"elst")
    # AAC already edits out 1024 priming samples. Moving its media start a
    # further 4800 samples forward shortens actual coverage without altering
    # any declared movie-level or edit-list duration.
    assert struct.unpack_from(">i", original, edit + 16)[0] == 1024
    struct.pack_into(">i", original, edit + 16, 5824)
    capture.write_bytes(original)
    tracks = physical.inspect_capture(capture, {"audio", "video"}, check_container=True).tracks
    assert tracks["audio"].contains(4799, 48000)
    assert not tracks["audio"].contains(4800, 48000)
    assert tracks["video"].contains(4800, 48000)
    struct.pack_into(">i", original, edit + 16, -1)
    capture.write_bytes(original)
    tracks = physical.inspect_capture(capture, {"audio"}, check_container=True).tracks
    assert not tracks["audio"].contains(0, 48000)


def test_film_rejects_outside_clock_and_range_onsets_at_record_and_recheck(film, monkeypatch):
    run, path, observation, capture = _simulated_playback(film, monkeypatch)
    original = _write_observation(film, run, observation, capture, capture)
    output = path.with_name("evidence.json")
    args = dict(player=film.player, package=film.package, song=None, output=output,
                run_path=path, observation_path=observation,
                audio_capture=capture, video_capture=capture)
    good = physical.record(**args)
    assert good["verification_status"] == "verified_physical"
    output.unlink()

    clock = original["capture_clock"]
    nominal = PLAYBACK_START + original["impulses"][0]["presentation_tick"]
    for change, reason in (
        ({"capture_clock": None}, "explicit audio and video capture clock origins"),
        ({"capture_clock": {"audio_origin_tick": 0, "video_origin_tick": 0}}, "explicit audio and video capture clock origins"),
        ({"capture_clock": dict(clock, playback_start_tick=-1)}, "non-negative integer"),
        ({"capture_clock": dict(clock, audio_origin_tick=False)}, "non-negative integer"),
        ({"capture_clock": dict(clock, audio_origin_tick=148800, video_origin_tick=148800)}, "first file timeline zero"),
        ({"capture_clock": dict(clock, audio_origin_tick=1)}, "single zero clock origin"),
        ({"capture_clock": dict(clock, playback_start_tick=CAPTURE_END)}, "measured playback start is outside"),
        ({"impulses": [{**original["impulses"][0], "audio_tick": nominal - 1}]}, "precedes its scheduled presentation cue"),
        ({"impulses": [{**original["impulses"][0], "audio_tick": CAPTURE_END,
                        "display_tick": CAPTURE_END}]}, "outside the retained audio presentation range"),
        ({"impulses": [{**original["impulses"][0], "audio_tick": CAPTURE_END - 200,
                        "display_tick": CAPTURE_END}]}, "outside the retained video presentation range"),
    ):
        changed = {**original, **change}
        observation.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ValueError, match=reason):
            physical.record(**args)
        assert not output.exists()

    copied_video = path.with_name("copy.mp4")
    copied_video.write_bytes(capture.read_bytes())
    observation.write_text(json.dumps(dict(original, capture_clock=dict(clock, video_origin_tick=40))),
                           encoding="utf-8")
    with pytest.raises(ValueError, match="single zero clock origin"):
        physical.record(**dict(args, video_capture=copied_video))

    # Before the launch-anchor check, these locally in-range peaks in the 0.2 s
    # clip passed for the graph's 3.1 s cue, both at record and archive recheck.
    capture.write_bytes((MEDIA / "synthetic-mux.mp4").read_bytes())
    short = _write_observation(film, run, observation, capture, capture)
    forged = dict(short, impulses=[{**short["impulses"][0], "audio_tick": 4800, "display_tick": 5200}])
    observation.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(ValueError, match="scheduled cue is outside the retained audio presentation range"):
        physical.record(**args)
    assert not output.exists()
    report = deepcopy(good)
    report["observation_sha256"] = physical.sha256_file(observation)
    report["alignment"]["impulses"] = forged["impulses"]
    report["alignment"]["capture"].update(forged["capture"], observation_sha256=report["observation_sha256"])
    output.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="precedes its scheduled presentation cue"):
        physical.load_verified_evidence(output)
    # The reported ticks can also be internally coherent yet outside the actual
    # retained clip. Test the exact raw recheck through the other entry points.
    forged["impulses"] = good["alignment"]["impulses"]
    observation.write_text(json.dumps(forged), encoding="utf-8")
    report["observation_sha256"] = physical.sha256_file(observation)
    report["alignment"]["impulses"] = forged["impulses"]
    report["alignment"]["capture"]["observation_sha256"] = report["observation_sha256"]
    output.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="scheduled cue is outside the retained audio presentation range"):
        physical.main(["verify", "--evidence", str(output)])
    public = output.with_name("summary.json")
    with pytest.raises(ValueError, match="scheduled cue is outside the retained audio presentation range"):
        physical.main(["summarize", "--evidence", str(output), "--output", str(public)])
    assert not public.exists()


def test_physical_film_record_rejects_hash_matched_empty_mux(film, monkeypatch):
    run, path, observation, capture = _simulated_playback(film, monkeypatch)
    capture.write_bytes(b"")
    _write_observation(film, run, observation, capture, capture)
    output = path.with_name("evidence.json")
    with pytest.raises(ValueError, match="empty.*capture|capture.*empty"):
        physical.record(player=film.player, package=film.package, song=None, output=output,
                        run_path=path, observation_path=observation,
                        audio_capture=capture, video_capture=capture)
    assert not output.exists()


def test_hash_matched_empty_or_non_media_archive_is_not_reverified_or_publishable(film, monkeypatch):
    run, path, observation, capture = _simulated_playback(film, monkeypatch)
    _write_observation(film, run, observation, capture, capture)
    output = path.with_name("evidence.json")
    evidence = physical.record(player=film.player, package=film.package, song=None, output=output,
                               run_path=path, observation_path=observation,
                               audio_capture=capture, video_capture=capture)
    assert evidence["verification_status"] == "verified_physical"
    for fake, expected in ((b"", "capture is empty"),
                           (b"not audio and not video", "invalid or unsupported recording container"),
                           ((MEDIA / "synthetic-video.mp4").read_bytes(), "data for each required track"),
                           (_broken_sample_entry("generic box in audio stsd"), "unsupported audio MP4 sample description"),
                           (_broken_audio_sample_table("missing map"), "sample-to-chunk table")):
        capture.write_bytes(fake)
        changed_observation = _write_observation(film, run, observation, capture, capture)
        # A report and observation previously prepared against these exact file
        # bytes agreed on both hashes. Revalidation must still reject each.
        changed_report = deepcopy(evidence)
        changed_report["observation_sha256"] = physical.sha256_file(observation)
        changed_report["alignment"]["capture"].update(
            changed_observation["capture"],
            observation_sha256=changed_report["observation_sha256"],
        )
        output.write_text(json.dumps(changed_report), encoding="utf-8")
        with pytest.raises(ValueError, match=expected):
            physical.load_verified_evidence(output)
        with pytest.raises(ValueError, match=expected):
            physical.main(["verify", "--evidence", str(output)])
        public = output.with_name("summary.json")
        with pytest.raises(ValueError, match=expected):
            physical.main(["summarize", "--evidence", str(output), "--output", str(public)])
        assert not public.exists()


def test_separate_film_capture_requires_nonempty_audio_and_video(film, monkeypatch):
    run, path, observation, video = _simulated_playback(film, monkeypatch)
    video.write_bytes((MEDIA / "synthetic-timeline-video.mp4").read_bytes())
    audio = video.with_name("synthetic.wav")
    with wave.open(str(audio), "wb") as recording:
        recording.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        recording.writeframes(b"\x01\x00" * 82000)
    captured = _write_observation(film, run, observation, audio, video)
    captured["capture_clock"]["audio_origin_tick"] = 100
    observation.write_text(json.dumps(captured), encoding="utf-8")
    output = path.with_name("evidence.json")
    kwargs = dict(player=film.player, package=film.package, song=None, output=output,
                  run_path=path, observation_path=observation,
                  audio_capture=audio, video_capture=video)
    good = physical.record(**kwargs)
    assert physical.verify_evidence(good, evidence_path=output) == good
    assert good["verification_status"] == "verified_physical"
    output.unlink()
    for reported_onset in (99, 164100):
        changed = deepcopy(captured)
        changed["impulses"][0]["audio_tick"] = reported_onset
        observation.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ValueError, match="outside the retained audio presentation range"):
            physical.record(**kwargs)
        assert not output.exists()
    for file, label in ((audio, "audio"), (video, "video")):
        actual = file.read_bytes()
        for broken, reason in (
            (b"", f"{label} capture is empty"),
            (actual[:-1], f"{label} capture has an invalid or unsupported recording container"),
        ):
            file.write_bytes(broken)
            _write_observation(film, run, observation, audio, video)
            with pytest.raises(ValueError, match=reason):
                physical.record(**kwargs)
            assert not output.exists()
        file.write_bytes(actual)
