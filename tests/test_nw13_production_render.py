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
    sources = manifest["sources"]
    assert sources["geometry"]["source"] == sources["scene"]["source"] == "graph"
    for source in (sources["graph"], sources["song"], *sources["import_inputs"].values()):
        path = REPO_ROOT / source["path"]
        assert source["sha256"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    for frame in manifest["frames"]:
        assert frame["sha256"] == "sha256:" + hashlib.sha256((evidence_dir / frame["file"]).read_bytes()).hexdigest()
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



def test_studio_song_uses_the_edited_film_cue_schedule(tmp_path):
    import nw03_compiler as compiler
    import nw05_export
    import nw08_editor
    import nw08_preview
    import nw13_audio

    document = production.load_production(REPO_ROOT, PRODUCTION)
    graph = compiler.load_graph(REPO_ROOT / document["graph"])
    song = REPO_ROOT / document["song"]
    exporter = nw08_preview.RuntimePackageExporter(
        settings=nw05_export.ExportSettings(audio_song_path=str(song))
    )
    def flash_at(payload):
        data = nw13_audio.decode_nwas(payload)
        return next(note["tick"] for note in data["notes"] if note["frequency_millihz"] == 1760000)

    assert flash_at(nw05_export.read_audio_song(str(song))) == 144000  # raw authored action time
    assert exporter.selected_audio_payload(graph) == production.playback_song(REPO_ROOT, document)
    assert flash_at(exporter.selected_audio_payload(graph)) == 148800

    root = graph.nodes["film"]
    scene = json.loads(root.params["scene"][0])
    next(cue for cue in scene["cues"] if cue["id"] == "flash")["tick"] = 0
    author = nw08_editor.GraphEditorSession(graph)
    assert author.set_parameter("film", "scene", [json.dumps(scene)]).valid
    authored = tmp_path / "edited.nwgraph"
    authored.write_text(nw08_editor.emit_graph_text(author.document), encoding="utf-8")
    assert flash_at(exporter.selected_audio_payload(author.document)) == 0
    modified = {**document, "graph": str(authored)}
    trace = production.transport_trace(modified, REPO_ROOT, ticks=[0, 1, 148800])
    assert trace[0]["flash_active"] and not trace[-1]["flash_active"]
    assert [event["presentation_tick"] for row in trace for event in row["events"]
            if event.get("cue_id") == "flash"] == [0]
    assert production.playback_song(REPO_ROOT, modified) == exporter.selected_audio_payload(author.document)


def test_generated_film_has_exact_tick_and_camera_transition_contract():
    import nw03_compiler as compiler
    import nw13_film

    document = production.load_production(REPO_ROOT, PRODUCTION)
    graph = compiler.load_graph(REPO_ROOT / document["graph"])
    root = graph.nodes["film"]
    source = json.loads(root.params["scene"][0])
    first, second = source["cameras"][0]["shots"]
    first["projection"]["fov_degrees"] = 40
    second["projection"]["fov_degrees"] = 70
    second["transition"], second["transition_ticks"] = "smoothstep", 24000
    root.params["scene"] = [json.dumps(source)]
    assert scene_module.camera_state(scene_module.compile_scene(source), second["start_tick"]).fov_degrees == 70
    shader = nw13_film.emit(graph, root, entry_point="Main", thread_group=(8, 8, 1), width=48, height=27)
    assert "uint presentationTick" in shader and "uint actionAt(uint t)" in shader
    assert "Camera prev=shot0(t)" in shader and "c.lens=lerp" not in shader
    assert "float4(70.0" in shader


def test_native_capture_rejects_truncated_or_nonfinite_readback(tmp_path):
    import struct
    import nw13_artifact_measure as measure
    import nw13_evidence

    path = tmp_path / "frame.rgba32f"
    path.write_bytes(struct.pack("<4f", 0.0, 0.2, 0.3, 1.0))
    with pytest.raises(nw13_evidence.EvidenceError, match="2x1"):
        measure._read_capture(path, (2, 1))
    path.write_bytes(struct.pack("<4f", 0.0, 0.2, float("nan"), 1.0))
    with pytest.raises(nw13_evidence.EvidenceError, match="nonfinite"):
        measure._read_capture(path, (1, 1))
    path.write_bytes(struct.pack("<4f", 0.0, 0.2, 0.3, 0.0))
    with pytest.raises(nw13_evidence.EvidenceError, match="alpha"):
        measure._read_capture(path, (1, 1))


def test_release_film_shader_validates_and_fits_declared_shader_budgets(tmp_path):
    import os
    import shutil
    import nw03_compiler as compiler
    import nw13_film
    import nw13_history
    import offline_shader_pack
    from dataclasses import replace

    dxc = shutil.which("dxc")
    if dxc is None:
        pytest.skip("real DXC is unavailable")
    document = production.load_production(REPO_ROOT, PRODUCTION)
    graph = compiler.load_graph(REPO_ROOT / document["graph"])
    shaders, diagnostics = compiler.lower_to_fused_hlsl(
        graph, thread_group=(8, 8, 1), dispatch_groups=(6, 4, 1), image_width=48, image_height=27)
    assert not diagnostics and len(shaders) == 1
    common = tmp_path / "common" / "shader.dxil"
    # The upstream Linux tarball reports its dev/commit identifier rather
    # than the release tag; Windows export still requires the exact pin.
    result = compiler.compile_generated_shader(shaders[0], common, dxc_executable=dxc,
                                                allow_unpinned_dxc=os.name != "nt")
    _, (_, _, _, scene, settings, _) = production.film_graph(REPO_ROOT, document)
    timing, _ = nw13_film.film_timing(scene, settings)
    feedback = nw13_history.read_feedback(graph)
    post = tmp_path / "post" / "shader.dxil"
    shader = replace(shaders[0], entry_point="FilmFeedback",
                     hlsl=nw13_history.emit_composite(feedback, scene, timing, 48, 27))
    compiler.compile_generated_shader(shader, post, dxc_executable=dxc, allow_unpinned_dxc=os.name != "nt")
    metadata = nw13_film.compile_inline_ray_payload(graph, tmp_path / "ray", dxc,
                                                   allow_unpinned_dxc=os.name != "nt")
    ray = tmp_path / "ray" / "film_inline_ray.dxil"
    assert result["blob"] == common.read_bytes()
    assert metadata["inline_shader_bytes"] == ray.stat().st_size
    assert metadata["bounds_count"] == 17
    with pytest.raises(ValueError, match="baseline requires"):
        offline_shader_pack.validate_dxil_container(ray.read_bytes())
    common_shader_bytes = common.stat().st_size + post.stat().st_size
    assert common_shader_bytes <= 48 * 1024
    assert common_shader_bytes + ray.stat().st_size <= 96 * 1024


def test_shared_export_packages_the_preview_cue_bytes_and_integer_clock(tmp_path):
    import nw03_compiler as compiler
    import nw05_export
    import nw08_preview
    import nwp_package
    from test_nw05_export import _make_dxc

    document = production.load_production(REPO_ROOT, PRODUCTION)
    graph = REPO_ROOT / document["graph"]
    settings = nw05_export.ExportSettings(audio_song_path=str(REPO_ROOT / document["song"]))
    preview = nw08_preview.RuntimePackageExporter(settings=settings)
    expected = preview.selected_audio_payload(compiler.load_graph(graph))
    result = nw05_export.export_graph(
        graph, tmp_path / "export", settings=settings, dxc_executable=str(_make_dxc(tmp_path)),
        allow_test_payloads=True,
    )
    package = result.artifacts[nwp_package.COMMON_PROFILE_ID].package
    assert package.audio_song == expected == production.playback_song(REPO_ROOT, document)
    assert next(parameter for parameter in package.parameters if parameter.name == "time").type == "uint"


def test_controlled_scene_probes_isolate_expected_shadow_and_reflection():
    import nw13_artifact_measure as measure

    document = production.load_production(REPO_ROOT, PRODUCTION)
    graph, _ = production.film_graph(REPO_ROOT, document)
    dimensions = tuple(document["rendering"][axis] for axis in ("width", "height"))
    verdicts = {}
    for name, tick, variant in measure._probe_specs(graph):
        _, delta, channels = measure._response_channels(name, graph, variant, tick, dimensions)
        verdicts[name] = max(abs(delta[i]) for i in channels)
    assert verdicts["moving_occluder_shadow"] > 0.03
    assert verdicts["off_camera_material_reflection"] > 0.15



def test_native_source_must_be_tracked_and_match_selected_revision(tmp_path):
    import subprocess
    import nw13_artifact_measure as measure
    import nw13_evidence

    location = tmp_path / "repo"
    location.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=location, check=True)
    (location / "film.nwgraph").write_bytes(b"authored")
    subprocess.run(["git", "add", "film.nwgraph"], cwd=location, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@invalid.local",
                    "commit", "-qm", "fixture"], cwd=location, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=location, text=True).strip()
    assert measure._tracked_source(location, "film.nwgraph", revision) == ("film.nwgraph", b"authored")
    (location / "film.nwgraph").write_bytes(b"unbound")
    with pytest.raises(nw13_evidence.EvidenceError, match="differs"):
        measure._tracked_source(location, "film.nwgraph", revision)
    with pytest.raises(nw13_evidence.EvidenceError, match="tracked"):
        measure._tracked_source(location, "../not-owned.nwgraph", revision)


def test_typed_scene_history_is_real_and_legacy_film_remains_stateless(tmp_path, monkeypatch):
    import nw03_compiler as compiler
    import nw05_export as export
    import nw13_history
    import nwp_package as nwp

    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    document = production.load_production(REPO_ROOT, PRODUCTION)
    source = REPO_ROOT / document["graph"]
    graph = compiler.load_graph(source)
    feedback = nw13_history.read_feedback(graph)
    assert feedback.source_id == "corridor_render" and feedback.film_id == "film"
    assert graph.nodes[feedback.node_id].inputs == {
        "color": (feedback.source_id, "color"), "depth": (feedback.source_id, "depth"),
        "camera": (feedback.source_id, "camera"),
    }

    song = production.playback_song(REPO_ROOT, document)
    artifact = export.export_graph(source, tmp_path / "stateful", allow_test_payloads=True,
                                   audio_song_payload=song).artifacts[nwp.COMMON_PROFILE_ID]
    package = nwp.decode_package(artifact.package_bytes)
    assert package.audio_song == song and len(package.history) == 1
    record = package.history[0]
    assert record.values_per_element == 4 and record.element_count == 48 * 27
    assert record.initial_state == b"" and record.state_type == nw13_history.STATE_TYPE
    assert package.frame[0].opcode == "frame.set_timeline"
    assert [op.value for op in package.frame[0].operands] == [package.parameters[0].parameter_id, 0]
    steps = [ins for ins in package.frame if ins.opcode == "frame.simulation_step"]
    assert len(steps) == 1 and steps[0].operands[-2].value == 1
    assert steps[0].operands[-1].value == record.state_id
    assert [shader.entry_point for shader in package.shaders] == ["FilmSample", "FilmFeedback"]
    assert [(b.name, b.slot) for b in package.root_signatures[1].bindings if b.usage == "srv"] == [
        ("input", 0), ("film_current", 1)
    ]
    assert artifact.manifest["stateful_extension"]["sampled_scene_history"]["max_predecessor_dispatches_per_frame"] == 1
    assert artifact.manifest["present_geometry"] == {"width": 48, "height": 27}
    source_text = (tmp_path / "stateful" / "common.player.cpp").read_text(encoding="utf-8")
    assert "config.width = 48u;" in source_text and "config.height = 27u;" in source_text

    from dataclasses import replace
    assert any(row["code"] == "package_history_sampled_schedule" for row in
               nwp.validate_package_model(replace(package, parameters=package.parameters[:-1])))
    assert any(row["code"] == "package_history_sampled_schedule" for row in
               nwp.validate_package_model(replace(package, history=(replace(record, initial_state=b"x"),))))

    assert any(row["code"] == "package_history_sampled_schedule" for row in
               nwp.validate_package_model(replace(package, frame=package.frame[1:])))
    for target in (1, 7):
        wrong_order = list(package.frame)
        wrong_order.insert(target, wrong_order.pop(4))
        assert any(row["code"] == "package_history_sampled_schedule" for row in
                   nwp.validate_package_model(replace(package, frame=wrong_order)))
    wrong_view = list(package.frame)
    wrong_view[12] = replace(wrong_view[12], operands=(*wrong_view[12].operands[:2], wrong_view[13].operands[2]))
    assert any(row["code"] == "package_history_sampled_schedule" for row in
               nwp.validate_package_model(replace(package, frame=wrong_view)))

    bad = compiler.load_graph(source)
    bad.nodes[feedback.node_id].inputs["depth"] = (feedback.source_id, "color")
    assert any(row["code"] == "port_type_mismatch" for row in compiler.validate_graph_document(bad))
    with pytest.raises(ValueError, match="same film frame"):
        nw13_history.read_feedback(bad)

    old = {**document, "effects": {key: value for key, value in document["effects"].items()
                                 if key != "temporal_feedback"}}
    old_source = tmp_path / "stateless.nwgraph"
    production.import_graph(REPO_ROOT, old, old_source)
    old_package = export.export_graph(old_source, tmp_path / "stateless", allow_test_payloads=True,
                                      dxc_executable=__import__("nw09_export")._test_dxc(tmp_path / "dxc")
                                      ).artifacts[nwp.COMMON_PROFILE_ID].package
    assert not old_package.history and len(old_package.shaders) == 1

    import nw08_editor
    unrelated = compiler.load_graph(REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph")
    for node in graph.nodes.values():
        if node.operator_id == "nw.render.present":
            continue
        renamed = replace(node, node_id=f"unreachable_{node.node_id}", inputs={
            port: (f"unreachable_{source}", output) for port, (source, output) in node.inputs.items()
        })
        unrelated.nodes[renamed.node_id] = renamed
    dead_source = tmp_path / "dead.nwgraph"
    dead_source.write_text(nw08_editor.emit_graph_text(unrelated), encoding="utf-8")
    dead_artifact = export.export_graph(dead_source, tmp_path / "dead", allow_test_payloads=True,
                                        dxc_executable=__import__("nw09_export")._test_dxc(tmp_path / "dxc")
                                        ).artifacts[nwp.COMMON_PROFILE_ID]
    assert not dead_artifact.package.history
    assert not {"nw.feedback.scene_image", "nw.render.film_frame"}.intersection(dead_artifact.manifest["retained"]["code"])


def test_scene_history_is_visible_before_cut_and_exactly_reset_after_seek():
    import nw13_film
    import nw13_history

    document = production.load_production(REPO_ROOT, PRODUCTION)
    graph, _ = production.film_graph(REPO_ROOT, document)
    oracle = nw13_film.FrameOracle(graph, graph.nodes["film"], width=48, height=27)
    effect = oracle.render(150400)
    assert render.image_difference(effect, oracle.sample(150400)[0])["max_abs_channel_error"] > 0.02
    for boundary in (192000, 240000):
        assert nw13_history.compatible_tick(boundary, oracle.resets) is None
        assert oracle.render(boundary) == oracle.sample(boundary)[0]
    assert oracle.render(150400) == effect
    assert [nw13_history.predecessor(x) for x in (0, 399, 400, 799, 800)] == [None, None, 0, 0, 400]


def test_scene_history_rejects_disoccluded_depth_before_blending():
    from dataclasses import replace
    import nw13_history

    document = production.load_production(REPO_ROOT, PRODUCTION)
    _graph, (_, _, _, scene, _, _) = production.film_graph(REPO_ROOT, document)
    camera = replace(scene_module.camera_state(scene, 0), position=(0.0, 0.0, 0.0),
                     forward=(0.0, 0.0, 1.0), up=(0.0, 1.0, 0.0), aspect=1.0)
    current = ([1.0, 0.0, 0.0, 1.0], [4.0], camera)
    stale = [0.0, 0.0, 1.0, 1.0]
    def project(depth):
        return nw13_history.composite(current, (stale, [depth], camera), width=1, height=1,
                                      weight=0.25, depth_tolerance=0.04)
    assert project(4.0) == [0.75, 0.0, 0.25, 1.0]
    assert project(2.0) == project(float("inf")) == current[0]


def test_portable_cpp_package_player_accepts_bounded_nonmonotonic_film_ticks(tmp_path, monkeypatch):
    import os
    import shutil
    import subprocess

    import nw05_export
    import nw09_export
    import nwp_package

    native = shutil.which("g++") or shutil.which("clang++")
    if os.name == "nt" or native is None:
        pytest.skip("portable C++ compiler required; Windows runs the actual readback proof")
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    document = production.load_production(REPO_ROOT, PRODUCTION)
    staged = nw05_export.export_graph(
        REPO_ROOT / document["graph"], tmp_path / "package", allow_test_payloads=True,
        dxc_executable=nw09_export._test_dxc(tmp_path / "dxc"),
    ).artifacts[nwp_package.COMMON_PROFILE_ID]
    package_path = tmp_path / "typed-film.nwp"
    package_path.write_bytes(staged.package_bytes)
    binary = tmp_path / "planner"
    sources = [
        "tests/nw13_scene_history_contract.cpp",
        "src/player/package_player.cpp", "src/player/package_loader.cpp", "src/player/diagnostics.cpp",
        "src/player/capability_requirements.cpp", "src/player/capability_check.cpp",
        "src/player/player_instrumentation.cpp", "src/player/resource_plan.cpp",
        "src/player/embedded_package.cpp", "src/platform/win32_window.cpp", "src/package/nwp_format.cpp",
    ]
    compile_result = subprocess.run([
        native, "-std=c++20", "-O1", "-DNW_PROFILE_DEV=1", "-ffunction-sections", "-fdata-sections",
        "-Wl,--gc-sections", "-I", "src", "-I", "src/player", "-I", "src/package", "-I", "src/platform",
        *sources, "-o", str(binary),
    ], cwd=REPO_ROOT, capture_output=True, text=True, timeout=90)
    assert compile_result.returncode == 0, compile_result.stderr
    result = subprocess.run([str(binary), str(package_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "7 bounded predecessor steps" in result.stdout
