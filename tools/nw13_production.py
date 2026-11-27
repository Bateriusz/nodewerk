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
from pathlib import Path
from typing import Any, Mapping, Sequence

import nw13_audio
import nw13_evidence
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


def _geometry_index(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    nw13_render._walk_nodes(document["root"], index)
    return index


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
    graph = nw13_pipeline.load_graph(graph_path)
    geometry_document = json.loads(geometry_path.read_text(encoding="utf-8"))
    prepared = nw13_geometry.prepare(geometry_document)
    scene = nw13_scene.compile_scene(json.loads(scene_path.read_text(encoding="utf-8")))
    song_document = json.loads(song_path.read_text(encoding="utf-8"))
    song = nw13_audio.compile_sequence(song_document)
    geometry_index = _geometry_index(geometry_document)
    for obj in scene.objects.values():
        if obj.geometry not in geometry_index:
            diagnostics.append({"category": "production_validation", "severity": "error", "code": "production_unknown_geometry", "message": f"object {obj.id} references unknown geometry {obj.geometry}"})
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
        for name in ("werkkzeug3.inventory.v1.json", "werkkzeug4.inventory.v1.json"):
            path = catalog_dir / name
            if path.exists():
                known_entries.update(entry["id"] for entry in json.loads(path.read_text(encoding="utf-8"))["entries"])
        for item in mapping:
            for entry in item.get("catalog_entries", []):
                if entry not in known_entries:
                    diagnostics.append({"category": "production_validation", "severity": "error", "code": "production_unknown_catalog_entry", "message": f"mapping references unknown catalog entry {entry}"})
    return {
        "ok": not diagnostics,
        "diagnostics": diagnostics,
        "sources": {
            "graph": {"path": str(graph_path.relative_to(repo_root)), "sha256": _sha256_file(graph_path), "semantic_hash": nw13_pipeline.semantic_hash(graph)},
            "geometry": {"path": str(geometry_path.relative_to(repo_root)), "sha256": _sha256_file(geometry_path), "semantic_hash": nw13_geometry.semantic_hash(geometry_document), "bounds": prepared.bounds.to_dict(), "stats": prepared.stats},
            "scene": {"path": str(scene_path.relative_to(repo_root)), "sha256": _sha256_file(scene_path), "objects": len(scene.objects), "shots": [shot.id for shot in scene.shots]},
            "song": {"path": str(song_path.relative_to(repo_root)), "sha256": _sha256_file(song_path), "semantic_hash": nw13_audio.semantic_hash(song_document), "notes": len(song.notes), "nwas_sha256": "sha256:" + hashlib.sha256(nw13_audio.compile_to_nwas(song)).hexdigest()},
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
    rendering = production.get("rendering", {})
    width = int(rendering.get("width", 48))
    height = int(rendering.get("height", 27))
    renderer = nw13_render.Renderer(
        nw13_scene.compile_scene(json.loads(_resolve(repo_root, production["scene"]).read_text(encoding="utf-8"))),
        json.loads(_resolve(repo_root, production["geometry"]).read_text(encoding="utf-8")),
        width=width,
        height=height,
        max_steps=int(rendering.get("max_steps", 96)),
        samples=int(rendering.get("samples", 1)),
        epsilon=float(rendering.get("epsilon", 0.002)),
        max_distance=float(rendering.get("max_distance", 48.0)),
        shadows=bool(rendering.get("shadows", True)),
        reflection_bounces=int(rendering.get("reflection_bounces", 1)),
        miss_radiance=rendering.get("miss_radiance", [0.035, 0.045, 0.065]),
    )
    scene = renderer.scene
    output = _resolve(repo_root, str(output_dir))
    frames_dir = output / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    active_ticks = list(ticks if ticks is not None else production.get("fixed_ticks", [0, 96000, 144000, 192000, 288000]))
    frames = []
    region_checks: list[dict[str, Any]] = []
    images: list[list[float]] = []
    for tick in active_ticks:
        image, depth = renderer.render(int(tick))
        emitter_doc = production.get("particles", {}).get("emitter")
        if isinstance(emitter_doc, Mapping):
            image = renderer.render_particles(image, depth, emitter_doc, int(tick))
        image = _tone_image(production, image, width, height)
        frame_path = frames_dir / f"tick-{int(tick):06d}.ppm"
        nw13_render.to_ppm(image, width, height, frame_path)
        images.append(image)
        camera = nw13_scene.camera_state(scene, int(tick), action_tick=int(tick))
        regions = {}
        checks = {}
        for region in rendering.get("regions", []):
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
                "tick": int(tick),
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
        "revision": production.get("revision"),
        "width": width,
        "height": height,
        "samples": int(rendering.get("samples", 1)),
        "depth_convention": "positive_view_space_linear",
        "alpha_convention": "straight",
        "tolerances": rendering.get("tolerances", {"max_abs_channel_error": 0.02, "rmse": 0.01}),
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


def transport_trace(production: Mapping[str, Any], repo_root: Path, *, ticks: Sequence[int] | None = None) -> list[dict[str, Any]]:
    scene = nw13_scene.compile_scene(json.loads(_resolve(repo_root, production["scene"]).read_text(encoding="utf-8")))
    transport = nw13_scene.Transport(scene)
    active = list(ticks if ticks is not None else production.get("fixed_ticks", [0, 96000, 144000, 192000]))
    script = production.get("transport_script", {})
    if not isinstance(script, Mapping):
        raise ProductionError("transport_script must be an object")
    freeze_at = script.get("freeze_at")
    resume_at = script.get("resume_at")
    reverse_at = script.get("reverse_at")
    freeze_applied = False
    reverse_applied = False
    reverse_pending = False

    def step_to(target: int) -> None:
        while transport.tick < target:
            transport.step(min(4800, target - transport.tick))

    result = []
    cursor = 0
    for tick in active:
        tick = int(tick)
        if tick < transport.tick:
            transport.seek(tick)
        while transport.tick < tick:
            if reverse_pending:
                # A reverse state is observable at the fixed tick where it was
                # declared; resume forward before traversing to the next target.
                transport.reverse()
                reverse_pending = False
            if transport.frozen:
                if not isinstance(resume_at, int) or resume_at > tick:
                    break
                # Model frozen wall-clock time without emitting seek history.
                transport.tick = resume_at
                transport.resume()
                continue
            if isinstance(freeze_at, int) and not freeze_applied and transport.tick < freeze_at <= tick:
                step_to(freeze_at)
                transport.freeze()
                freeze_applied = True
                continue
            if isinstance(reverse_at, int) and not reverse_applied and transport.tick < reverse_at < tick:
                step_to(reverse_at)
                transport.reverse()
                reverse_applied = True
                reverse_pending = True
                continue
            step_to(min(tick, transport.tick + 4800))
        if isinstance(freeze_at, int) and not freeze_applied and transport.tick == freeze_at:
            transport.freeze()
            freeze_applied = True
        if isinstance(reverse_at, int) and not reverse_applied and transport.tick >= reverse_at:
            transport.reverse()
            reverse_applied = True
            reverse_pending = True
        if transport.tick > tick:
            transport.seek(tick)
        interval_events = transport.events[cursor:]
        cursor = len(transport.events)
        result.append(
            {
                "tick": tick,
                "frozen": transport.frozen,
                "direction": transport.direction,
                "generation": transport.generation,
                "events": interval_events,
            }
        )
    return result

