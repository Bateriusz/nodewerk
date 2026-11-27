from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw13_geometry as geometry  # noqa: E402
import nw13_production as production  # noqa: E402
import nw13_render as render  # noqa: E402
import nw13_scene as scene_module  # noqa: E402


PRODUCTION = REPO_ROOT / "fixtures" / "nw13" / "hotel_corridor.production.json"


def _geometry_document(children: list[dict]) -> dict:
    return {
        "format": "nw13.geometry",
        "schema_version": 1,
        "budget": {
            "max_nodes": 32,
            "max_depth": 8,
            "max_repetition_instances": 16,
            "max_generated_primitives": 16,
            "max_evaluation_steps": 65536,
            "max_memory_bytes": 1048576,
        },
        "root": {"id": "root", "operator": "nw.geom.union", "version": "1.0.0", "children": children},
    }


def _scene_document(objects: list[dict], materials: list[dict], lights: list[dict], shot: dict) -> dict:
    return {
        "format": "nw13.scene",
        "version": 1,
        "tick_rate_hz": 48000,
        "duration_ticks": 192000,
        "bounds": {"minimum": [-20, -10, -20], "maximum": [20, 10, 20]},
        "materials": materials,
        "lights": lights,
        "objects": objects,
        "cameras": [{"id": "main", "shots": [shot]}],
        "cuts": [],
        "cues": [],
    }


def test_production_validation_and_evidence_manifest(tmp_path: Path) -> None:
    document = production.load_production(REPO_ROOT, PRODUCTION)
    validation = production.validate_production(REPO_ROOT, document)
    assert validation["ok"], validation["diagnostics"]
    assert validation["sources"]["scene"]["objects"] == 17
    assert validation["sources"]["song"]["notes"] == 30
    assert validation["sources"]["geometry"]["stats"]["evaluation_steps"] > 0
    assert validation["sources"]["geometry"]["stats"]["memory_bytes"] > 0
    assert all(mapping["license"] for mapping in document["mappings"])
    assert all(mapping["reachable_dependencies"] for mapping in document["mappings"])

    manifest = production.render_evidence(
        REPO_ROOT,
        document,
        output_dir=tmp_path / "evidence",
        ticks=[0, 96000, 148800, 192000, 288000],
    )
    assert len(manifest["frames"]) == 5
    assert manifest["contact_sheet"]["file"].endswith(".ppm")
    assert manifest["tolerances"]["max_abs_channel_error"] == 0.02
    assert manifest["region_checks_passed"] is True
    assert len(manifest["region_checks"]) == len(manifest["frames"]) * 3
    assert manifest["render_cost"]["estimated_evaluation_steps"] > 0
    assert manifest["render_cost"]["estimated_memory_bytes"] > 0
    assert manifest["render_cost"]["startup_ms"] is None
    for frame in manifest["frames"]:
        path = tmp_path / "evidence" / frame["file"]
        assert path.exists()
        assert frame["sha256"] == "sha256:" + __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        assert frame["regions"]["center"] >= 0.0
    assert (tmp_path / "evidence" / "frames.v1.json").exists()
    assert (tmp_path / "evidence" / "contact_sheet.ppm").exists()


def test_committed_fixed_tick_frames_and_contact_sheet_are_retained() -> None:
    evidence_dir = REPO_ROOT / "fixtures" / "nw13" / "evidence"
    manifest_path = evidence_dir / "frames.v1.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["frames"]) >= 6
    assert (evidence_dir / manifest["contact_sheet"]["file"]).exists()
    for frame in manifest["frames"]:
        path = evidence_dir / frame["file"]
        assert path.exists()
        assert path.read_bytes().startswith(b"P6\n")
    assert {frame["shot_id"] for frame in manifest["frames"]} == {"corridor_approach", "room_turn"}
    assert any(frame["tick"] == 192000 for frame in manifest["frames"])
    assert manifest["region_checks_passed"] is True
    import hashlib
    for source in manifest["sources"].values():
        path = REPO_ROOT / source["path"]
        assert source["sha256"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("frames.v1.json", "transport.v1.json"):
        sidecar = evidence_dir / f"{name}.sha256"
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8").strip() == hashlib.sha256((evidence_dir / name).read_bytes()).hexdigest()


def test_reference_renderer_does_not_claim_optional_execution() -> None:
    document = json.loads((REPO_ROOT / "fixtures/nw13/geometry_room.json").read_text())
    scene = scene_module.compile_scene(json.loads((REPO_ROOT / "fixtures/nw13/hotel_corridor.scene.json").read_text()))
    with pytest.raises(render.RenderError, match="native"):
        render.render_backend(scene, document, "dxr", width=24, height=14)


def test_moving_occluder_changes_the_expected_shadow_region() -> None:
    document = _geometry_document(
        [
            {"id": "receiver", "operator": "nw.geom.box", "version": "1.0.0", "parameters": {"half_extents": [4, 0.1, 4]}},
            {"id": "occluder", "operator": "nw.geom.box", "version": "1.0.0", "parameters": {"half_extents": [0.4, 0.1, 0.4]}},
        ]
    )
    materials = [
        {"id": "white", "base_color": [1, 1, 1, 1], "roughness": 0.9, "metallic": 0.0, "specular": 0.1, "emission": [0, 0, 0]},
        {"id": "black", "base_color": [0.02, 0.02, 0.02, 1], "roughness": 0.9, "metallic": 0.0, "specular": 0.0, "emission": [0, 0, 0]},
    ]
    lights = [{"id": "l", "type": "point", "position": [0, 4, 0], "color": [1, 1, 1], "intensity": 50.0, "range": 20.0}]
    objects = [
        {"id": "receiver", "geometry": "receiver", "material": "white", "parent": None, "transform": {"position": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]}},
        {
            "id": "occluder",
            "geometry": "occluder",
            "material": "black",
            "parent": None,
            "transform": {"position": [-1.5, 1.5, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]},
            "tracks": [
                {
                    "channel": "transform.position",
                    "value_type": "vec3",
                    "keys": [
                        {"tick": 0, "value": [-1.5, 1.5, 0], "interpolation": "linear"},
                        {"tick": 96000, "value": [1.5, 1.5, 0], "interpolation": "linear"},
                    ],
                }
            ],
        },
    ]
    shot = {"id": "top", "start_tick": 0, "end_tick": 192000, "position": [0, 5, 0.01], "target": [0, 0, 0], "up": [0, 0, -1], "projection": {"fov_degrees": 60, "near": 0.01, "far": 50, "aspect": 1.0}}
    scene = scene_module.compile_scene(_scene_document(objects, materials, lights, shot))
    renderer = render.Renderer(scene, document, width=48, height=48, max_steps=96, shadows=True, reflection_bounces=0)
    left_occluder, _ = renderer.render(0)
    right_occluder, _ = renderer.render(96000)
    assert render.luma(left_occluder, 48, 48, 0, 0, 24, 48) < render.luma(left_occluder, 48, 48, 24, 0, 48, 48)
    assert render.luma(right_occluder, 48, 48, 0, 0, 24, 48) > render.luma(right_occluder, 48, 48, 24, 0, 48, 48)
    assert render.image_difference(left_occluder, right_occluder)["max_abs_channel_error"] > 0.1


def test_off_camera_reflected_object_material_changes_nearest_hit_response() -> None:
    document = _geometry_document(
        [
            {"id": "mirror", "operator": "nw.geom.box", "version": "1.0.0", "parameters": {"half_extents": [3, 2, 0.05]}},
            {"id": "reflected", "operator": "nw.geom.box", "version": "1.0.0", "parameters": {"half_extents": [0.5, 0.5, 0.5]}},
        ]
    )
    materials = [
        {"id": "mirror", "base_color": [0.85, 0.85, 0.9, 1], "roughness": 0.02, "metallic": 0.95, "specular": 0.9, "emission": [0, 0, 0]},
        {"id": "red", "base_color": [0.95, 0.05, 0.05, 1], "roughness": 0.7, "metallic": 0.0, "specular": 0.1, "emission": [0, 0, 0]},
        {"id": "green", "base_color": [0.05, 0.95, 0.05, 1], "roughness": 0.7, "metallic": 0.0, "specular": 0.1, "emission": [0, 0, 0]},
    ]
    lights = [{"id": "l", "type": "point", "position": [0, 3, 3], "color": [1, 1, 1], "intensity": 20.0, "range": 30.0}]
    object_document = [
        {"id": "mirror", "geometry": "mirror", "material": "mirror", "parent": None, "transform": {"position": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]}},
        {"id": "reflected_object", "geometry": "reflected", "material": "red", "parent": None, "transform": {"position": [1, 0.5, 6], "rotation": [0, 0, 0], "scale": [1.5, 0.8, 1]}},
    ]
    shot = {"id": "front", "start_tick": 0, "end_tick": 192000, "position": [0, 1, 4], "target": [0, 0, 0], "up": [0, 1, 0], "projection": {"fov_degrees": 70, "near": 0.01, "far": 50, "aspect": 1.0}}
    red_scene = scene_module.compile_scene(_scene_document(object_document, materials, lights, shot))
    green_objects = [object_document[0], {**object_document[1], "material": "green"}]
    green_scene = scene_module.compile_scene(_scene_document(green_objects, materials, lights, shot))
    red_renderer = render.Renderer(red_scene, document, width=48, height=48, max_steps=96, reflection_bounces=1)
    green_renderer = render.Renderer(green_scene, document, width=48, height=48, max_steps=96, reflection_bounces=1)
    red_image, _ = red_renderer.render(0)
    green_image, _ = green_renderer.render(0)
    difference = render.image_difference(red_image, green_image)
    assert difference["max_abs_channel_error"] > 0.05
    # The reflected object is behind the camera, so its change can only be
    # observed through the mirror's one-bounce reflection.
    forward = (0.0, -1.0, -4.0)
    direct = red_renderer.trace((0.0, 1.0, 4.0), forward)
    assert direct is not None and direct.object_id == "mirror"


def test_renderer_bound_validation() -> None:
    document = json.loads((REPO_ROOT / "fixtures" / "nw13" / "geometry_room.json").read_text(encoding="utf-8"))
    scene = scene_module.compile_scene(json.loads((REPO_ROOT / "fixtures" / "nw13" / "hotel_corridor.scene.json").read_text(encoding="utf-8")))
    with pytest.raises(render.RenderError):
        render.Renderer(scene, document, max_steps=1024)
    with pytest.raises(render.RenderError):
        render.Renderer(scene, document, reflection_bounces=2)
    with pytest.raises(render.RenderError):
        render.Renderer(scene, document, samples=16)


def test_production_transport_trace_reports_cut_and_one_shot_cues() -> None:
    document = production.load_production(REPO_ROOT, PRODUCTION)
    trace = production.transport_trace(document, REPO_ROOT, ticks=[0, 96000, 148800, 192000, 288000])
    assert [entry["tick"] for entry in trace] == [0, 96000, 148800, 192000, 288000]
    assert any(event.get("cue_id") == "door_hinge" for event in trace[1]["events"])
    assert any(event.get("cue_id") == "flash" for event in trace[2]["events"])
    assert any(event.get("kind") == "cut" and event.get("cut_tick") == 192000 for event in trace[3]["events"])
    assert sum(1 for entry in trace for event in entry["events"] if event.get("cue_id") == "door_hinge") == 1

    declared = production.transport_trace(document, REPO_ROOT)
    states = {entry["tick"]: entry for entry in declared}
    assert states[120000]["action_tick"] == states[123200]["action_tick"] == 120000
    assert states[252000]["action_tick"] < states[240000]["action_tick"]
    assert states[288000]["action_tick"] > states[264000]["action_tick"]
    assert states[148800]["flash_active"]
    assert not states[144000]["flash_active"]
    reverse_entry = next(entry for entry in declared if entry["direction"] == -1)
    assert reverse_entry["tick"] == 240000
    assert any(event.get("kind") == "reverse" and event.get("direction") == -1 for event in reverse_entry["events"])
    assert any(event.get("kind") == "reverse" and event.get("direction") == 1 for entry in declared for event in entry["events"])
    assert any(event.get("kind") == "freeze" for entry in declared for event in entry["events"])
    assert any(event.get("kind") == "resume" for entry in declared for event in entry["events"])

    retained = json.loads((REPO_ROOT / "fixtures" / "nw13" / "evidence" / "transport.v1.json").read_text(encoding="utf-8"))
    retained_events = [event["kind"] for entry in retained["trace"] for event in entry["events"]]
    assert "freeze" in retained_events
    assert "resume" in retained_events
    assert "cut" in retained_events
    assert "reverse" in retained_events
    assert any(entry["direction"] == -1 for entry in retained["trace"])



def test_scaled_nearest_hit_and_shadow_limit_are_world_distances() -> None:
    sphere = {"id": "ball", "version": "1.0.0", "operator": "nw.geom.sphere", "parameters": {"radius": 1}}
    made = {"schema_version": 1, "root": sphere}
    material = {"id": "white", "base_color": [1, 1, 1, 1], "roughness": 1,
                "metallic": 0, "specular": 0, "emission": [0, 0, 0]}
    shot = {"id": "front", "start_tick": 0, "end_tick": 192000, "position": [0, 0, 5],
            "target": [0, 0, 0], "up": [0, 1, 0],
            "projection": {"fov_degrees": 90, "near": 0.05, "far": 30, "aspect": 1}}
    objects = [
        {"id": "large", "geometry": "ball", "material": "white",
         "transform": {"position": [0, 0, 0], "scale": [2, 1, 2]}},
        {"id": "farther", "geometry": "ball", "material": "white",
         "transform": {"position": [0, 0, -3], "scale": [0.5, 0.5, 0.5]}},
    ]
    scene = scene_module.compile_scene(_scene_document(objects, [material], [], shot))
    renderer = render.Renderer(scene, made, epsilon=1e-4)
    hit = renderer.trace((0, 0, 5), (0, 0, -1))
    assert hit is not None and hit.object_id == "large"
    assert hit.distance == pytest.approx(3, abs=0.0003)
    assert hit.position == pytest.approx((0, 0, 2), abs=0.0003)
    assert renderer.trace((0, 0, 5), (0, 0, -1), max_distance=2.9) is None
    assert renderer._shadowed((0, 0, 5), (0, 0, 1), "")
    assert not renderer._shadowed((0, 0, 5), (0, 0, 2.2), "")


def test_off_axis_particle_uses_the_scene_view_depth() -> None:
    wall = {"id": "wall", "version": "1.0.0", "operator": "nw.geom.box",
            "parameters": {"half_extents": [4, 4, 0.1]}}
    material = {"id": "surface", "base_color": [1, 1, 1, 1], "roughness": 1,
                "metallic": 0, "specular": 0, "emission": [0, 0, 0]}
    obj = {"id": "wall", "geometry": "wall", "material": "surface",
           "transform": {"position": [0, 0, -2.1]}}
    shot = {"id": "view", "start_tick": 0, "end_tick": 192000, "position": [0, 0, 0],
            "target": [0, 0, -1], "up": [0, 1, 0],
            "projection": {"fov_degrees": 90, "near": 0.05, "far": 30, "aspect": 1}}
    scene = scene_module.compile_scene(_scene_document([obj], [material], [], shot))
    renderer = render.Renderer(scene, {"schema_version": 1, "root": wall}, width=4, height=4)
    image, depth = renderer.render(0)
    assert depth[2 * 4 + 3] == pytest.approx(2, abs=0.005)
    emitter = {"id": "dust", "seed": 1, "count": 1, "origin": [1.65, -0.55, -2.2],
               "size_start": 0.16, "speed_min": 0, "speed_max": 0}
    assert renderer.render_particles(image, depth, emitter, 0) == image
    emitter["origin"] = [1.35, -0.45, -1.8]
    assert renderer.render_particles(image, depth, emitter, 0) != image


def test_film_import_and_typed_studio_edit_reach_preview_and_offline_lowering(tmp_path) -> None:
    import nw03_compiler as compiler
    import nw08_editor
    import nw08_preview

    document = production.load_production(REPO_ROOT, PRODUCTION)
    graph = tmp_path / "film.nwgraph"
    production.import_graph(REPO_ROOT, document, graph)
    assert graph.read_bytes() == (REPO_ROOT / document["graph"]).read_bytes()
    author = nw08_editor.GraphEditorSession(compiler.load_graph(graph))
    viewer = nw08_preview.ReferencePreviewBackend()

    def preview():
        return viewer.render(author.document, 0, width=32, height=18)

    def lower():
        shaders, diagnostics = compiler.lower_to_fused_hlsl(author.document, image_width=48, image_height=27)
        assert not diagnostics
        assert len(shaders) == 1 and "mirror_panel" in shaders[0].root_node_ids
        return shaders[0]

    initial, first_shader = preview(), lower()
    result = author.set_parameter("mirror_panel", "half_extents", [0.15, 0.15, 0.04])
    assert result.valid
    edited, edited_shader = preview(), lower()
    assert initial.has_image and edited.image_data != initial.image_data
    assert first_shader.specialization_key != edited_shader.specialization_key
    assert "localInverse" in edited_shader.hlsl


def test_transport_flash_drives_reference_light_and_exported_transient() -> None:
    import nw13_audio

    document = production.load_production(REPO_ROOT, PRODUCTION)
    trace = production.transport_trace(document, REPO_ROOT, ticks=[148800, 149600])
    assert trace[0]["action_tick"] == 144000
    assert sum(event.get("cue_id") == "flash" for frame in trace for event in frame["events"]) == 1
    decoded = nw13_audio.decode_nwas(production.playback_song(REPO_ROOT, document))
    flash = next(note for note in decoded["notes"] if note["frequency_millihz"] == 1760000)
    assert flash["tick"] == 148800

    geometry_doc = json.loads((REPO_ROOT / document["geometry"]).read_text())
    scene = scene_module.compile_scene(json.loads((REPO_ROOT / document["scene"]).read_text()))
    renderer = render.Renderer(scene, geometry_doc, width=24, height=14)
    lit, _ = renderer.render(148800, action_tick=144000)
    dark, _ = renderer.render(148800, action_tick=144000, light_gain={"flash_light": 0})
    assert render.image_difference(lit, dark)["max_abs_channel_error"] > 0.02
