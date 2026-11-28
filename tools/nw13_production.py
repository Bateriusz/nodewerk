#!/usr/bin/env python3
"""NW-13 original hotel-room/corridor production orchestration.

The production binds one editable graph, geometry document, scene, song and
particle emitter.  This module validates the cross-document identities, renders
actual fixed-tick frames with declared tolerances/regions, and emits a contact
sheet plus an evidence manifest.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

import nw03_compiler
import nw05_export
import nw13_audio
import nw13_evidence
import nw13_film
import nw13_geometry
import nw13_particles
import nw13_pipeline
import nw13_render
import nw13_scene


FORMAT = "nw13.production"
VERSION = 1


class ProductionError(ValueError):
    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def load_production(repo_root: Path, path: str | Path) -> dict[str, Any]:
    production_path = _resolve(repo_root, str(path))
    document = json.loads(production_path.read_text(encoding="utf-8"))
    if document.get("format") != FORMAT or document.get("version") != VERSION:
        raise ProductionError("production must use nw13.production version 1")
    document["_path"] = str(production_path)
    return document


def measured_revision(repo_root: Path, expected: str | None = None, *, require_clean: bool = False) -> str | None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout.strip()

    revision = git("rev-parse", "--verify", "HEAD^{commit}")
    if expected is not None and expected != revision:
        raise ProductionError(f"expected measured commit {expected}, checked out {revision}")
    if git("status", "--porcelain", "--untracked-files=normal"):
        if require_clean or expected is not None:
            raise ProductionError("commit-bound evidence requires a clean measured worktree")
        return None
    return revision


def import_graph(repo_root: Path, production: Mapping[str, Any], destination: Path) -> None:
    geometry = json.loads(_resolve(repo_root, production["geometry"]).read_text(encoding="utf-8"))
    scene = json.loads(_resolve(repo_root, production["scene"]).read_text(encoding="utf-8"))
    destination.write_text(
        nw13_film.import_graph(geometry, scene, production, source_name=str(destination)),
        encoding="utf-8", newline="\n",
    )


def film_graph(repo_root: Path, production: Mapping[str, Any]):
    """Resolve the same editable graph used by Preview and standalone export."""
    graph = nw13_pipeline.load_graph(_resolve(repo_root, production["graph"]))
    roots = nw03_compiler._reachable_shader_roots(graph)
    if len(roots) != 1 or roots[0].operator_id != nw13_film.OPERATOR:
        raise ProductionError("production must select exactly one reachable film shader")
    return graph, nw13_film.read_film(graph, roots[0])


def playback_song(repo_root: Path, production: Mapping[str, Any], *, graph=None) -> bytes:
    if graph is None:
        graph, _ = film_graph(repo_root, production)
    return nw05_export.read_audio_song(str(_resolve(repo_root, production["song"])), film_document=graph)


def validate_production(repo_root: Path, production: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    graph_path = _resolve(repo_root, production["graph"])
    geometry_path = _resolve(repo_root, production["geometry"])
    scene_path = _resolve(repo_root, production["scene"])
    song_path = _resolve(repo_root, production["song"])
    for path in (graph_path, geometry_path, scene_path, song_path):
        if not path.exists():
            diagnostics.append({"category": "production_validation", "severity": "error", "code": "production_missing_input", "message": f"missing {path}"})
    if diagnostics:
        return {"ok": False, "diagnostics": diagnostics, "sources": {}}
    graph, (prepared, _, _, scene, settings, _) = film_graph(repo_root, production)
    song_document = json.loads(song_path.read_text(encoding="utf-8"))
    song = nw13_audio.compile_sequence(song_document)
    graph_dimensions = tuple(int(graph.settings[f"nw05_output_{key}"]) for key in ("width", "height"))
    evidence_dimensions = tuple(production["rendering"][key] for key in ("width", "height"))
    authored_dimensions = tuple(settings["rendering"][key] for key in ("width", "height"))
    if graph_dimensions != evidence_dimensions or graph_dimensions != authored_dimensions:
        raise ProductionError("authored graph, renderer and evidence dimensions must match")
    node_ids = set(graph.nodes)
    mapping = production.get("mappings", [])
    for item in mapping:
        for node_id in item.get("nodes", []):
            if node_id not in node_ids:
                diagnostics.append({"category": "production_validation", "severity": "error", "code": "production_unknown_node", "message": f"mapping references unknown graph node {node_id}"})
        for object_id in item.get("scene_objects", []):
            if object_id not in scene.objects:
                diagnostics.append({"category": "production_validation", "severity": "error", "code": "production_unknown_object", "message": f"mapping references unknown scene object {object_id}"})
        for track_id in item.get("audio_tracks", []):
            if not any(track.get("id") == track_id for track in song_document.get("tracks", [])):
                diagnostics.append({"category": "production_validation", "severity": "error", "code": "production_unknown_track", "message": f"mapping references unknown audio track {track_id}"})
        if item.get("provenance") not in {"independently_authored", "catalog_review"}:
            diagnostics.append({"category": "production_validation", "severity": "error", "code": "production_missing_provenance", "message": f"mapping '{item.get('feature')}' has no provenance label"})
        for field in ("source_page", "shot", "reachable_dependencies", "license"):
            if not item.get(field):
                diagnostics.append(
                    {
                        "category": "production_validation",
                        "severity": "error",
                        "code": "production_missing_provenance_detail",
                        "message": f"mapping '{item.get('feature')}' is missing provenance detail '{field}'",
                    }
                )
        for entry in item.get("catalog_entries", []):
            if not isinstance(entry, str) or not entry:
                diagnostics.append({"category": "production_validation", "severity": "error", "code": "production_invalid_catalog_entry", "message": "catalog entry must be a non-empty string"})
    # Validate the pinned catalog endpoints when the catalog is present.
    catalog_dir = repo_root / "references" / "operator_catalog"
    if catalog_dir.exists():
        known_entries = set()
        for name in ("werkkzeug3.json", "werkkzeug4.json"):
            path = catalog_dir / name
            if path.exists():
                known_entries.update(entry["key"] for entry in json.loads(path.read_text(encoding="utf-8"))["operators"])
        for item in mapping:
            for entry in item.get("catalog_entries", []):
                if entry not in known_entries:
                    diagnostics.append({"category": "production_validation", "severity": "error", "code": "production_unknown_catalog_entry", "message": f"mapping references unknown catalog entry {entry}"})
    return {
        "ok": not diagnostics,
        "diagnostics": diagnostics,
        "sources": {
            "graph": {"path": str(graph_path.relative_to(repo_root)), "sha256": _sha256_file(graph_path), "semantic_hash": nw13_pipeline.semantic_hash(graph)},
            "geometry": {"source": "graph", "semantic_hash": nw13_geometry.semantic_hash(prepared.document),
                         "bounds": prepared.bounds.to_dict(), "stats": prepared.stats},
            "scene": {"source": "graph", "sha256": nw13_evidence.sha256_text(
                nw13_evidence.canonical_bytes(scene.source).decode("ascii")),
                "objects": len(scene.objects), "shots": [shot.id for shot in scene.shots]},
            "song": {"path": str(song_path.relative_to(repo_root)), "sha256": _sha256_file(song_path),
                     "semantic_hash": nw13_audio.semantic_hash(song_document), "notes": len(song.notes),
                     "nwas_sha256": "sha256:" + hashlib.sha256(playback_song(repo_root, production, graph=graph)).hexdigest()},
            "import_inputs": {"geometry": {"path": str(geometry_path.relative_to(repo_root)), "sha256": _sha256_file(geometry_path)},
                              "scene": {"path": str(scene_path.relative_to(repo_root)), "sha256": _sha256_file(scene_path)}},
        },
    }


def _tone_image(production: Mapping[str, Any], image: list[float], width: int, height: int) -> list[float]:
    effects = production.get("effects", {})
    bloom_doc = effects.get("bloom") if isinstance(effects, Mapping) else None
    tonemap_doc = effects.get("tonemap") if isinstance(effects, Mapping) else None
    if isinstance(bloom_doc, Mapping):
        image = nw13_particles.bloom(
            image,
            width,
            height,
            threshold=float(bloom_doc.get("threshold", 0.7)),
            intensity=float(bloom_doc.get("intensity", 0.3)),
            radius=int(bloom_doc.get("radius", 2)),
        )
    if isinstance(tonemap_doc, Mapping):
        image = nw13_particles.tonemap(
            image,
            exposure=float(tonemap_doc.get("exposure", 1.0)),
            gamma=float(tonemap_doc.get("gamma", 2.2)),
            operator=str(tonemap_doc.get("operator", "reinhard")),
        )
    return image


def render_evidence(
    repo_root: Path,
    production: Mapping[str, Any],
    *,
    output_dir: str | Path,
    ticks: Sequence[int] | None = None,
) -> dict[str, Any]:
    validation = validate_production(repo_root, production)
    if not validation["ok"]:
        raise ProductionError("production validation failed", validation["diagnostics"])
    graph, (prepared, _, _, scene, settings, emitter) = film_graph(repo_root, production)
    rendering = settings["rendering"]
    expected = production["rendering"]
    width, height = int(graph.settings["nw05_output_width"]), int(graph.settings["nw05_output_height"])
    renderer = nw13_render.Renderer(scene, prepared.document, width=width, height=height, **{
        key: rendering[key] for key in ("max_steps", "samples", "epsilon", "max_distance",
                                       "shadows", "reflection_bounces", "miss_radiance")
    })
    output = _resolve(repo_root, str(output_dir))
    frames_dir = output / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    active_ticks = list(ticks if ticks is not None else production.get("fixed_ticks", [0, 96000, 144000, 192000, 288000]))
    frames = []
    region_checks: list[dict[str, Any]] = []
    images: list[list[float]] = []
    for frame_state in transport_trace(production, repo_root, ticks=active_ticks, timing_source=(scene, settings)):
        tick = frame_state["tick"]
        action_tick = frame_state["action_tick"]
        flash = settings.get("flash")
        light_gain = {flash["light"]: float(frame_state["flash_active"])} if flash else {}
        image, depth = renderer.render(tick, action_tick=action_tick, light_gain=light_gain)
        if emitter is not None:
            image = renderer.render_particles(image, depth, emitter.to_dict(), tick, action_tick=action_tick, film_cycle=True)
        image = _tone_image(settings, image, width, height)
        frame_path = frames_dir / f"tick-{int(tick):06d}.ppm"
        nw13_render.to_ppm(image, width, height, frame_path)
        images.append(image)
        camera = nw13_scene.camera_state(scene, tick, action_tick=action_tick)
        regions = {}
        checks = {}
        for region in expected.get("regions", []):
            rect = region.get("rect", [0.0, 0.0, 1.0, 1.0])
            x0 = int(rect[0] * width)
            y0 = int(rect[1] * height)
            x1 = int(rect[2] * width)
            y1 = int(rect[3] * height)
            value = nw13_render.luma(image, width, height, x0, y0, x1, y1)
            regions[region["name"]] = value
            passed = float(region.get("minimum_luma", 0.0)) <= value <= float(region.get("maximum_luma", 1.0e9))
            checks[region["name"]] = passed
            region_checks.append(
                {
                    "tick": int(tick),
                    "region": region["name"],
                    "value": value,
                    "expected": {
                        "minimum_luma": region.get("minimum_luma", 0.0),
                        "maximum_luma": region.get("maximum_luma", 1.0e9),
                    },
                    "passed": passed,
                }
            )
        frames.append(
            {
                **frame_state,
                "file": str(frame_path.relative_to(output)),
                "sha256": _sha256_file(frame_path),
                "shot_id": camera.shot_id,
                "cut": camera.cut,
                "regions": regions,
                "region_checks": checks,
            }
        )
    sheet_path = output / "contact_sheet.ppm"
    nw13_render.contact_sheet(images, width, height, columns=min(4, len(images)), path=sheet_path)
    manifest = {
        "format": "nw13.image-evidence",
        "version": 1,
        "production_id": production.get("id"),
        "revision": measured_revision(repo_root),
        "implementation_base": production.get("implementation_base"),
        "width": width,
        "height": height,
        "samples": int(rendering.get("samples", 1)),
        "depth_convention": "positive_view_space_linear",
        "alpha_convention": "straight",
        "tolerances": expected.get("tolerances", {"max_abs_channel_error": 0.02, "rmse": 0.01}),
        "render_cost": {
            "objects": len(scene.objects),
            "geometry_nodes": validation["sources"]["geometry"]["stats"]["nodes"],
            "estimated_evaluation_steps": validation["sources"]["geometry"]["stats"]["evaluation_steps"],
            "estimated_memory_bytes": validation["sources"]["geometry"]["stats"]["memory_bytes"],
            "estimated_acceleration_structure_bytes": len(scene.objects) * (512 + 64),
            "startup_ms": None,
            "startup_measurement_status": "unavailable_in_headless_reference",
        },
        "frames": frames,
        "region_checks": region_checks,
        "region_checks_passed": all(check["passed"] for check in region_checks),
        "contact_sheet": {"file": sheet_path.name, "sha256": _sha256_file(sheet_path)},
        "sources": validation["sources"],
    }
    manifest_path = output / "frames.v1.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_sha256"] = _sha256_file(manifest_path)
    return manifest


def transport_trace(production: Mapping[str, Any], repo_root: Path, *,
                    ticks: Sequence[int] | None = None, timing_source=None) -> list[dict[str, Any]]:
    if timing_source is None:
        _, (_, _, _, scene, settings, _) = film_graph(repo_root, production)
    else:
        scene, settings = timing_source
    timeline, onsets = nw13_film.film_timing(scene, settings)
    active = list(ticks if ticks is not None else production.get("fixed_ticks", [0, 96000, 144000, 192000]))
    flash = settings.get("flash")
    previous = -1
    result = []
    for tick in active:
        if type(tick) is not int or not max(0, previous) <= tick <= scene.duration_ticks:
            raise ProductionError("evidence ticks must be ordered presentation ticks within the film")
        result.append({
            "tick": tick,
            **timeline.at(tick),
            "flash_active": bool(flash and any(onset <= tick < onset + flash["duration_ticks"] for onset in onsets)),
            "events": [event for event in timeline.events if previous < event["presentation_tick"] <= tick],
        })
        previous = tick
    return result
