#!/usr/bin/env python3
"""Author, export and verify the standalone Neon Breach concept demo."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

import audio_song
import nw03_compiler as compiler
import nw05_export as export
import nw05_optimizer as optimizer
import nwp_package as nwp

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "fixtures/nw12a"
GRAPH = ASSETS / "neon_breach.nwgraph"
SCORE = ASSETS / "development-score.json"
STORY = ASSETS / "story.json"
FINALE = ASSETS / "finale.txt"
WIDTH, HEIGHT = 1920, 1080
INSTANCE_COUNT = 1024
TICKS_PER_SECOND = 48000
FRAME_TICKS = 800
CUE_TAIL = 3200
BASELINE = "1c9e1559c8d43530c7b213461c6719b6aa43ae35"
BASELINE_TREE = "4b73007a5321e9c8ad89eac70de2f0219a542080"
# The effect transients are generated exclusively inside the same NWAS payload
# as the score, not played as independently retriggered sounds.
EFFECTS = {
    "teleport": ((3000, 1850000, 9500), (14500, 715000, 5200)),
    "door_impact": ((1900, 90000, 17500), (3000, 196000, 9600)),
    "shot_one": ((1450, 2130000, 15000),),
    "impact_one": ((2300, 128000, 15300),),
    "shot_two": ((1450, 1960000, 15100),),
    "impact_two": ((2300, 118000, 15300),),
    "escape": ((12000, 440000, 3800),),
    "exit": ((8800, 587330, 5100),),
    "disruptor": ((5500, 1190000, 11000),),
    "stun": ((8100, 108000, 12200), (4100, 381000, 6200)),
    "finale": ((9000, 146832, 4200),),
}
TITLES = (
    "NEON BREACH / AN ORBITAL INCIDENT",
    "01 / MOTHERSHIP - TELEPORT PREP",
    "02 / NEON HOTEL - ROOM 07",
    "03 / SECURITY - DOOR BREACHED",
    "04 / CRISIS - QUARTER SPEED",
    "05 / HOLD - MOTION FROZEN",
    "06 / ECHO - VISUAL REVERSE",
    "07 / FORWARD - LINE CLEAR",
    "08 / THIRD AGENT SLIPS OUT",
    "09 / HATCH SEALED - AGENT OUT",
    "10 / CORRIDOR - DISRUPTOR DRAWN",
    "11 / ROOM 07 - SIGNAL LOST",
)
CHAPTERS = {
    "orbit-atlas": ("orbit_atlas.nwgraph", 96000),
    "portal-particles": ("portal_particles.nwgraph", 3200),
    "hotel-echo": ("hotel_echo.nwgraph", 3200),
}
STORY_SHADER_FILES = {
    2: "neon_breach_instances.hlsl",
    6: "neon_breach_scene_vs.hlsl",
    7: "neon_breach_scene_ps.hlsl",
    9: "neon_breach_tonemap.hlsl",
}
STORY_OPERATORS = frozenset({
    "nw.texture.noise2d", "nw.residency.generate_texture", "nw.mesh.torus",
    "nw.residency.generate_mesh", "nw.scene.instances", "nw.residency.generate_instances",
    "nw.material.standard", "nw.camera.curve", "nw.light.directional", "nw.animation.twist",
    "nw.render.instanced", "nw.post.bloom", "nw.post.tonemap", "nw.render.present",
})


def load_story(path: Path = STORY) -> dict[str, Any]:
    story = json.loads(path.read_text(encoding="utf-8"))
    if set(story) != {"version", "duration_ticks", "visual_segments", "cues"} or story["version"] != 1:
        raise ValueError("invalid story version/fields")
    duration = story["duration_ticks"]
    if type(duration) is not int or not 35 * TICKS_PER_SECOND <= duration <= 60 * TICKS_PER_SECOND:
        raise ValueError("demo duration must be 35–60 seconds")
    prior = 0
    for segment in story["visual_segments"]:
        if set(segment) != {"start", "end", "rate_numerator", "rate_denominator"} or any(type(value) is not int for value in segment.values()):
            raise ValueError("invalid motion segment")
        n, d = segment["rate_numerator"], segment["rate_denominator"]
        length = segment["end"] - segment["start"]
        if (segment["start"] != prior or length <= 0 or d not in (1, 2, 4)
                or not -d <= n <= d or length % (FRAME_TICKS * d)):
            raise ValueError("motion needs contiguous, frame-exact, bounded segments")
        prior = segment["end"]
    if prior != duration:
        raise ValueError("motion does not span song duration")
    if [(x["rate_numerator"], x["rate_denominator"]) for x in story["visual_segments"]] != [
        (1, 1), (1, 4), (0, 1), (-1, 2), (1, 1),
    ]:
        raise ValueError("story must play forward, slow, freeze, reverse, then resume")
    if [cue["id"] for cue in story["cues"]] != list(EFFECTS):
        raise ValueError("cue table does not match scene and effects bindings")
    previous = -CUE_TAIL
    for cue in story["cues"]:
        tick = cue["tick"]
        if (set(cue) != {"id", "tick"} or type(tick) is not int
                or tick % FRAME_TICKS or tick < previous + CUE_TAIL or tick + CUE_TAIL > duration):
            raise ValueError("ambiguous, overlapping, unaligned, or out-of-duration cue")
        previous = tick
    return story


def _motion_ranges(story: dict[str, Any]):
    accumulated = 0
    for row in story["visual_segments"]:
        yield accumulated, row
        accumulated += ((row["end"] - row["start"]) * row["rate_numerator"]
                        // row["rate_denominator"])


def motion_tick(story: dict[str, Any], tick: int) -> int:
    if type(tick) is not int or not 0 <= tick <= story["duration_ticks"]:
        raise ValueError("invalid presentation tick")
    for start, row in _motion_ranges(story):
        if tick <= row["end"]:
            elapsed = (tick - row["start"]) // row["rate_denominator"]
            return start + elapsed * row["rate_numerator"]
    raise AssertionError("missing motion segment")


def _events(score: dict[str, Any], story: dict[str, Any], *, include_effects: bool):
    if (not isinstance(score, dict) or set(score) != {"version", "title", "kind", "duration_ticks", "notes"}
            or type(score["version"]) is not int or score["version"] != 1
            or score["kind"] not in {"original_development", "authorized_arrangement"}
            or not isinstance(score["title"], str) or not score["title"].strip()
            or type(score["duration_ticks"]) is not int or score["duration_ticks"] != story["duration_ticks"]
            or not isinstance(score["notes"], list) or not score["notes"]):
        raise ValueError("score must be a versioned, nonempty bounded note-event arrangement of the exact story duration")
    rows = []
    for note in score["notes"]:
        if not isinstance(note, list) or len(note) != 4 or any(type(v) is not int for v in note):
            raise ValueError("each score note is [absolute_tick, duration_ticks, millihertz, gain]")
        rows.append(note)
    if include_effects:
        for cue in story["cues"]:
            rows.extend([cue["tick"], length, pitch, gain] for length, pitch, gain in EFFECTS[cue["id"]])
    return [{"tick": event[0], "duration": event[1], "frequency_millihz": event[2], "gain": event[3]}
            for event in sorted(rows, key=lambda event: event[0])]


def song_payload(score_path: Path, story: dict[str, Any], *, include_effects: bool = True) -> bytes:
    score = json.loads(score_path.read_text(encoding="utf-8"))
    return audio_song.compile_song({"version": 1, "duration_ticks": story["duration_ticks"],
                                    "events": _events(score, story, include_effects=include_effects)})


def _generated_header(story: dict[str, Any], finale: Path) -> str:
    glyphs = {" ": [0] * 7}
    for row in (ASSETS / "glyphs.txt").read_text(encoding="ascii").splitlines():
        char, hex_rows = row.split()
        glyphs[char] = list(bytes.fromhex(hex_rows))
    charset = sorted(glyphs)
    words = finale.read_text(encoding="ascii").splitlines()
    if (len(words) != 4 or any(not 1 <= len(line) <= 46 or
                               any(char not in charset for char in line)
                               for line in words)):
        raise ValueError("finale requires four uppercase original/authorized lines, up to 46 characters each")
    titles = (*TITLES, *words)
    glyph_rows = [str(bits) + "u" for char in charset for bits in glyphs[char]]
    width = max(map(len, titles))
    letters = [str(charset.index(char)) + "u" for title in titles for char in title.ljust(width)]
    lines = ["static const uint CUE_" + e["id"].upper() + " = " + str(e["tick"]) + "u;" for e in story["cues"]]
    for name, row in zip(("SLOW", "FREEZE", "REVERSE", "FORWARD"), story["visual_segments"][1:]):
        lines.append(f'static const uint STORY_{name} = {row["start"]}u;')
    lines += [
        f'static const uint STORY_DURATION = {story["duration_ticks"]}u;',
        f"static const uint STORY_FINALE_TEXT = {len(TITLES)}u;",
        "float StorySeconds(uint tick) { return float(tick)/48000.0; }",
        "float CuePulse(uint tick,uint cue) {",
        f"    if (tick<cue || tick-cue>={CUE_TAIL}u) return 0.0;",
        f"    return 1.0-float(tick-cue)/{CUE_TAIL}.0;",
        "}",
        "uint StoryMotionTick(uint tick) {",
    ]
    for start, r in _motion_ranges(story):
        lines.append(f'    if(tick<={r["end"]}u) return uint({start} + int((tick-{r["start"]}u)/{r["rate_denominator"]}u)*({r["rate_numerator"]}));')
    lines += [
        f'    return {motion_tick(story, story["duration_ticks"])}u;',
        "}",
        f"static const uint StoryGlyphs[{len(glyph_rows)}] = {{ {','.join(glyph_rows)} }};",
        f"static const uint StoryLetters[{len(letters)}] = {{ {','.join(letters)} }};",
        "float StoryText(float2 uv, float2 origin, uint title) {",
        "    float2 p=(uv-origin)*float2(320.0,195.0);",
        f"    if(p.x<0.0 || p.y<0.0 || p.x>={width * 6}.0 || p.y>=7.0) return 0.0;",
        "    uint px=uint(p.x); uint row=uint(p.y);",
        f"    uint glyph=StoryLetters[title*{width}u + px/6u];",
        "    uint col=px%6u; if(col==5u) return 0.0;",
        "    return float((StoryGlyphs[glyph*7u+row]>>(4u-col)) & 1u);",
        "}",
    ]
    return "\n".join(lines) + "\n"


def story_sources(story: dict[str, Any], finale: Path = FINALE) -> dict[int, str]:
    """Author shader content using the existing Orbital Vortex specialization path.

    Every scene object uses the existing torus-generated vertex/index buffers,
    instance layout, indexed draw and depth buffer. No new runtime/operator ABI
    is introduced. Keep the generated cue constants inside the hashed sources.
    """
    doc = compiler.load_graph(GRAPH)
    optimized = optimizer.optimize_graph(doc)
    if {node.operator_id for node in optimized.nodes} != STORY_OPERATORS:
        raise ValueError("the 3D film must use the existing procedural scene operators")
    if export._graph_render_dimensions(doc, optimized) != (WIDTH, HEIGHT):
        raise ValueError("story presentation dimensions do not match the production")
    if float(optimized.node_map()["instances"].params["count"][0]) != INSTANCE_COUNT:
        raise ValueError("scene instance budget does not match the production")
    header = _generated_header(story, finale)
    common = (ASSETS / "neon_breach_scene.hlsli").read_text(encoding="ascii")
    result = {}
    for shader_id, filename in STORY_SHADER_FILES.items():
        content = (ASSETS / filename).read_text(encoding="ascii")
        content = content.replace('#include "neon_breach_scene.hlsli"', common)
        if "#include" in content:
            raise ValueError("scene shader has an unresolved source dependency")
        result[shader_id] = header + content
    return result


@contextmanager
def _story_specialization(story: dict[str, Any], finale: Path = FINALE):
    """Restore shared shader definitions even if compilation/export fails."""
    sources = story_sources(story, finale)
    module = export.nw06_export
    original = module.FIXED_SHADERS
    with tempfile.TemporaryDirectory(prefix="neon-breach-shaders-") as folder:
        paths = {}
        for shader_id, source in sources.items():
            path = Path(folder) / STORY_SHADER_FILES[shader_id]
            path.write_text(source, encoding="ascii", newline="\n")
            paths[shader_id] = path
        try:
            module.FIXED_SHADERS = tuple(
                replace(fixed, name=paths[fixed.shader_id].stem, source=str(paths[fixed.shader_id]))
                if fixed.shader_id in paths else fixed for fixed in original
            )
            yield
        finally:
            module.FIXED_SHADERS = original


def _settings(template: Path | None, *, song: bool = False) -> export.ExportSettings:
    return export.ExportSettings(
        byte_ceiling=64 * 1024 * 1024, memory_budget=512 * 1024 * 1024,
        player_template=str(template) if template else "", include_optional_artifacts=False,
        **({"audio_song_path": str(SCORE)} if song else {}),
    )


def _export_outputs(output: Path, *, score: Path, template: Path | None,
                    dxc: str | None, test: bool = False, finale: Path = FINALE):
    if (test and template is not None) or (not test and (template is None or dxc is None)):
        raise ValueError("release export needs the pinned Windows template and DXC; contract exports never stage EXEs")
    story = load_story()
    settings = _settings(template, song=True)
    with _story_specialization(story, finale):
        result = export.export_graph(
            GRAPH, output / "neon-breach", settings=settings, dxc_executable=dxc, allow_test_payloads=test,
            audio_song_payload=song_payload(score, story),
        )
    artifacts = {"neon-breach": result.artifacts[nwp.COMMON_PROFILE_ID]}
    for chapter, (name, _) in CHAPTERS.items():
        result = export.export_graph(ASSETS / name, output / chapter, settings=_settings(template),
                                     dxc_executable=dxc, allow_test_payloads=test)
        artifacts[chapter] = result.artifacts[nwp.COMMON_PROFILE_ID]
    return artifacts


def cue_frames(story: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": cue["id"], "tick": cue["tick"], "sample": cue["tick"],
             "output_frame": cue["tick"] // FRAME_TICKS} for cue in story["cues"]]


def _fixed_probe(executable: Path, tick: int, *, pixel: bool = False) -> str:
    args = [str(executable), "--headless", "--allow-software-adapter", "--tick", str(tick), "--emit-image-hash"]
    if pixel:
        args.extend(("--probe-first-pixel",))
    completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=90, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"headless probe at {tick} failed: {completed.stdout}\n{completed.stderr}")
    if f"NW05_EXPLICIT_TICK={tick}\n" not in completed.stdout or "NW05_LINK_MANIFEST_VERIFIED=1\n" not in completed.stdout:
        raise RuntimeError("generated player did not admit the exact embedded manifest/tick")
    if not re.search(r"(?m)^NW05_IMAGE_FNV64=[0-9a-f]{16}$", completed.stdout):
        raise RuntimeError("generated player produced no image readback")
    return completed.stdout


def _distribution_hashes(directory: Path) -> str:
    required = {"neon-breach.exe", "README.txt", "PLAY-NEON-BREACH.cmd", "CUE-TABLE.json",
                *(f"chapters/{name}.exe" for name in CHAPTERS)}
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    if actual - {"SHA256SUMS.txt"} != required:
        raise ValueError("unexpected or incomplete Windows demo distribution")
    return "".join(sha256((directory / filename).read_bytes()).hexdigest() + "  " +
                   filename + "\n" for filename in sorted(required))


def smoke_bundle(directory: Path) -> None:
    if (directory / "SHA256SUMS.txt").read_text(encoding="ascii") != _distribution_hashes(directory):
        raise ValueError("Windows demo distribution checksum mismatch")
    story = load_story()
    player = (directory / "neon-breach.exe").resolve()
    for cue in cue_frames(story):
        for delta, rgb in ((-FRAME_TICKS, "00000000"), (0, "3f800000")):
            text = _fixed_probe(player, cue["tick"] + delta, pixel=True)
            if f"NW05_FIRST_PIXEL={rgb},00000000,00000000,3f800000\n" not in text:
                raise RuntimeError(f"{cue['id']}: native cue pixel does not match exact frame transition")
    for name, (_, tick) in CHAPTERS.items():
        text = _fixed_probe((directory / "chapters" / (name + ".exe")).resolve(), tick)
        if name != "orbit-atlas" and f"NW05_HISTORY_SIMULATION_TICK={tick // 400}\n" not in text:
            raise RuntimeError(f"{name} did not execute native History ticks")


def build_bundle(output: Path, *, template: Path, dxc: str, score: Path, smoke: bool = True,
                 finale: Path = FINALE) -> None:
    if os.name != "nt":
        raise RuntimeError("standalone production build requires the pinned Windows environment")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".neon-breach-", dir=output.parent) as temp:
        root = Path(temp)
        # Use one immutable input snapshot for both the NWAS conversion and
        # the artifact's score-kind disclosure even if the source changes.
        score_bytes = score.read_bytes()
        score_snapshot = root / "score.json"
        score_snapshot.write_bytes(score_bytes)
        finale_bytes = finale.read_bytes()
        finale_snapshot = root / "finale.txt"
        finale_snapshot.write_bytes(finale_bytes)
        artifacts = _export_outputs(root / "builds", score=score_snapshot, template=template,
                                    dxc=dxc, finale=finale_snapshot)
        stage = root / "stage"
        (stage / "chapters").mkdir(parents=True)
        for name, artifact in artifacts.items():
            if artifact.executable_bytes is None:
                raise RuntimeError(f"{name} unexpectedly lacks its native production player")
            destination = stage / "neon-breach.exe" if name == "neon-breach" else stage / "chapters" / (name + ".exe")
            embedded, _ = export.extract_embedded_package(artifact.executable_bytes)
            if embedded != artifact.package_bytes:
                raise RuntimeError(f"{name} shipped bytes do not contain the expected package")
            destination.write_bytes(artifact.executable_bytes)
        shutil.copyfile(ASSETS / "README.txt", stage / "README.txt")
        shutil.copyfile(ASSETS / "PLAY-NEON-BREACH.cmd", stage / "PLAY-NEON-BREACH.cmd")
        story = load_story()
        score_metadata = json.loads(score_bytes)
        cue_manifest = {"format": "nodewerk-demo-cue-table", "version": 1,
                        "baseline": BASELINE, "baseline_tree": BASELINE_TREE,
                        "duration_ticks": story["duration_ticks"], "sample_rate": TICKS_PER_SECOND,
                        "frame_ticks": FRAME_TICKS, "score_kind": score_metadata["kind"],
                        "presentation": {"width": WIDTH, "height": HEIGHT, "instances": INSTANCE_COUNT,
                                         "renderer": "existing_indexed_instanced_3d", "rtx": False},
                        "scene_source_sha256": {str(key): sha256(value.encode("ascii")).hexdigest()
                                                for key, value in story_sources(story, finale_snapshot).items()},
                        "finale_sha256": sha256(finale_bytes).hexdigest(),
                        "nwas_sha256": sha256(artifacts["neon-breach"].package.audio_song).hexdigest(),
                        "cues": cue_frames(story)}
        (stage / "CUE-TABLE.json").write_text(json.dumps(cue_manifest, indent=2) + "\n", encoding="utf-8")
        (stage / "SHA256SUMS.txt").write_text(_distribution_hashes(stage), encoding="ascii", newline="\n")
        if smoke:
            smoke_bundle(stage)
        stage.rename(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    info = commands.add_parser("cues", help="print deterministic native 60 fps cue mapping")
    build = commands.add_parser("build", help="build and stage exact generated players")
    build.add_argument("--player-template", type=Path, required=True)
    build.add_argument("--dxc", required=True)
    build.add_argument("--score", type=Path, default=SCORE,
                       help="authorized note-event JSON, never a recording")
    build.add_argument("--finale-text", type=Path, default=FINALE,
                       help="four original/authorized uppercase lines, at most 46 characters each")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--build-only", action="store_true", help="defer native smoke to the Windows runtime job")
    smoke = commands.add_parser("smoke", help="probe the exact staged bundle without window or audio device")
    smoke.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "cues":
        print(json.dumps(cue_frames(load_story()), indent=2))
    elif args.command == "build":
        build_bundle(args.output, template=args.player_template, dxc=args.dxc,
                     score=args.score, finale=args.finale_text, smoke=not args.build_only)
    else:
        smoke_bundle(args.bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
