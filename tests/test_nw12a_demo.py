"""NW-12A portable contracts; genuine DXIL/PE/WARP validation is in the Windows gate."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import wave

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import audio_demo as demo
import audio_song
import nw03_compiler as compiler
import nw05_optimizer as optimizer
import nwp_package as nwp

from test_nw05_export import _make_dxc


def test_motion_reconstruction_is_continuous_and_effects_use_the_absolute_clock():
    story = demo.load_story()
    clock = lambda tick: demo.motion_tick(story, tick)
    assert story["duration_ticks"] / 48000 == 48
    focus = [1056000, 1200000, 1296000, 1392000]
    for boundary in focus:
        assert abs(clock(boundary) - clock(boundary - 1)) <= 1
        assert abs(clock(boundary + 1) - clock(boundary)) <= 1
    start, freeze, reverse, forward = focus
    assert clock(start) == start
    assert clock(start + 800) == start + 200
    assert clock(freeze) == clock(freeze + 48000) == clock(reverse)
    assert clock(reverse + 800) == clock(reverse) - 400
    assert clock(forward + 800) == clock(forward) + 800
    cues = demo.cue_frames(story)
    assert [row["id"] for row in cues] == [
        "teleport", "door_impact", "shot_one", "impact_one", "shot_two", "impact_two",
        "escape", "exit", "disruptor", "stun", "finale",
    ]
    assert all(row["sample"] == row["output_frame"] * 800 == row["tick"] for row in cues)
    assert next(row["tick"] for row in cues if row["id"] == "escape") < 34 * 48000
    assert len({row["sample"] for row in cues}) == len(cues)
    cue_times = {cue["id"]: cue["tick"] for cue in cues}
    assert cue_times["escape"] < cue_times["exit"] < cue_times["disruptor"] < cue_times["stun"] < cue_times["finale"]
    shader = demo.story_shader(story).hlsl
    for row in cues:
        assert f"CUE_{row['id'].upper()} = {row['sample']}u;" in shader
    assert "NeonBreach(uv, uint(g_time), nw_texture2d(uv).r)" in shader


def test_score_override_accepts_notes_but_never_recordings(tmp_path):
    story = demo.load_story()
    sample = audio_song.decode_song(demo.song_payload(demo.SCORE, story))
    no_fx = audio_song.decode_song(demo.song_payload(demo.SCORE, story, include_effects=False))
    assert sample["duration_ticks"] == no_fx["duration_ticks"] == story["duration_ticks"]
    assert len(sample["events"]) - len(no_fx["events"]) == sum(map(len, demo.EFFECTS.values()))
    assert len(sample["events"]) <= 64
    file = tmp_path / "note-events.json"
    file.write_text(json.dumps({
        "version": 1, "title": "Local test score (synthetic)", "kind": "authorized_arrangement",
        "duration_ticks": story["duration_ticks"], "notes": [[800, 100, 440000, 1200]],
    }), encoding="utf-8")
    override = audio_song.decode_song(demo.song_payload(file, story))
    assert override["events"][0] == {"tick": 800, "duration": 100, "frequency_millihz": 440000, "gain": 1200}
    file.write_text(json.dumps({"audio_file": "music.wav"}), encoding="utf-8")
    with pytest.raises(ValueError, match="note-event"):
        demo.song_payload(file, story)
    file.write_bytes(b"RIFF\x00\x00")
    with pytest.raises(ValueError):
        demo.song_payload(file, story)


@pytest.mark.skipif(sys.platform == "win32", reason="native portable synth test is run on hosted Linux")
def test_each_scene_cue_starts_the_real_nw12_pcm_effect_on_its_capture_frame(tmp_path):
    assert shutil.which("c++"), "native offline compiler is required on portable CI"
    exe = tmp_path / "offline"
    subprocess.run(["c++", "-std=c++20", "-O2", "-I", str(ROOT / "src"),
                    str(ROOT / "src/audio/procedural_audio.cpp"), str(ROOT / "fixtures/audio/offline.cpp"),
                    "-o", str(exe)], check=True)
    story = demo.load_story()
    buffers = []
    for include in (False, True):
        data = tmp_path / "song.nwas"
        data.write_bytes(demo.song_payload(demo.SCORE, story, include_effects=include))
        wav, plan = tmp_path / "song.wav", tmp_path / "frames.csv"
        subprocess.run([str(exe), str(data), str(wav), str(plan)], check=True, timeout=20)
        with wave.open(str(wav), "rb") as stream:
            assert stream.getframerate() == 48000 and stream.getnframes() == story["duration_ticks"]
            buffers.append(stream.readframes(stream.getnframes()))
        plan_lines = plan.read_text(encoding="utf-8").splitlines()
        assert len(plan_lines) == 1 + story["duration_ticks"] // demo.FRAME_TICKS
    music, mix = buffers
    for cue in demo.cue_frames(story):
        tick = cue["sample"]
        assert music[(tick - 1) * 2:tick * 2] == mix[(tick - 1) * 2:tick * 2], cue
        assert struct.unpack_from("<h", music, tick * 2) != struct.unpack_from("<h", mix, tick * 2), cue
        assert plan_lines[1 + cue["output_frame"]] == f"{cue['output_frame']},{tick},{tick}"


def test_actual_exports_contain_live_main_audio_and_reachable_operator_chapters(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    dxc = str(_make_dxc(tmp_path / "test-dxc"))
    artifacts = demo._export_outputs(tmp_path / "export", score=demo.SCORE, template=None, dxc=dxc, test=True)
    assert list(artifacts) == ["neon-breach", "orbit-atlas", "portal-particles", "hotel-echo"]
    assert all(item.executable_bytes is None for item in artifacts.values()), "test output is not a native PE"
    main = artifacts["neon-breach"]
    song = demo.song_payload(demo.SCORE, demo.load_story())
    assert nwp.decode_package(main.package_bytes).audio_song == song
    assert main.manifest["audio"]["clock"] == "wasapi_endpoint"
    assert main.manifest["image_oracle_status"] == "unavailable"
    assert main.manifest["image_oracle_sha256"] == ""
    source_files = list((tmp_path / "export" / "neon-breach").glob("*.player.cpp"))
    assert len(source_files) == 1
    source = source_files[0].read_text(encoding="utf-8")
    assert "config.width = 640u" in source and "config.height = 360u" in source
    assert "audio_output.now_ticks()" in source
    assert "NW05_FIRST_PIXEL=" in source and "--headless" in source
    orbit_graph = compiler.load_graph(demo.ASSETS / "orbit_atlas.nwgraph")
    optimized = optimizer.optimize_graph(orbit_graph)
    operators = {
        "nw.texture.noise2d", "nw.residency.generate_texture", "nw.mesh.torus",
        "nw.residency.generate_mesh", "nw.scene.instances", "nw.residency.generate_instances",
        "nw.material.standard", "nw.camera.curve", "nw.animation.twist",
        "nw.render.instanced", "nw.post.bloom", "nw.post.tonemap",
    }
    assert operators <= {node.operator_id for node in optimized.nodes}
    assert len(artifacts["orbit-atlas"].package.shaders) >= 9
    for name, state in (("portal-particles", "nw.particles.v1"), ("hotel-echo", "nw.feedback.image.v1")):
        package = nwp.decode_package(artifacts[name].package_bytes)
        assert len(package.history) == 1 and package.history[0].state_type == state
        assert package.audio_song is None
        assert any(instruction.opcode == "frame.simulation_step" for instruction in package.frame)
        assert any(instruction.opcode == "frame.present" for instruction in package.frame)


def test_demo_workflow_builds_smokes_then_publishes_the_transfer():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    build = workflow.index("python tools/audio_demo.py build ")
    transfer = workflow.index("Upload current-head runtime-smoke inputs")
    smoke = workflow.index("python tools/audio_demo.py smoke")
    publish = workflow.index("name: nodewerk-nw12a-demo-windows")
    assert build < transfer < smoke < publish
    assert "windows-2025" in workflow[transfer:smoke]



def test_transfer_smoke_refuses_corrupt_or_extra_distribution_files(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "chapters").mkdir(parents=True)
    files = ("neon-breach.exe", "README.txt", "PLAY-NEON-BREACH.cmd", "CUE-TABLE.json",
             *(f"chapters/{name}.exe" for name in demo.CHAPTERS))
    for name in files:
        (bundle / name).write_bytes(name.encode("ascii") + b" (structural test only)")
    sums = demo._distribution_hashes(bundle)
    (bundle / "SHA256SUMS.txt").write_text(sums, encoding="ascii")
    assert demo._distribution_hashes(bundle) == sums

    (bundle / "neon-breach.exe").write_bytes(b"wrong native artifact")
    with pytest.raises(ValueError, match="checksum mismatch"):
        demo.smoke_bundle(bundle)
    (bundle / "music.wav").write_bytes(b"unexpected file")
    with pytest.raises(ValueError, match="unexpected or incomplete"):
        demo.smoke_bundle(bundle)



def test_finale_text_replacement_is_bounded_and_does_not_change_audio(tmp_path):
    story = demo.load_story()
    default = demo.story_shader(story).hlsl
    song = demo.song_payload(demo.SCORE, story)
    finale = tmp_path / "my-local-finale.txt"
    finale.write_text("THE SIGNAL IS LOST.\nTHE AGENT IS GONE.\nROOM 07 IS CLOSED.\nEND OF FILE.\n", encoding="ascii")
    assert demo.story_shader(story, finale).hlsl != default
    assert demo.song_payload(demo.SCORE, story) == song
    finale.write_text("ONLY ONE LINE\n", encoding="ascii")
    with pytest.raises(ValueError, match="four uppercase"):
        demo.story_shader(story, finale)
