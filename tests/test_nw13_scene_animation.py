from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw13_scene as scene_module  # noqa: E402
from schema_validator import validate_instance  # noqa: E402


SCENE_PATH = REPO_ROOT / "fixtures" / "nw13" / "hotel_corridor.scene.json"
SCHEMA_PATH = REPO_ROOT / "schemas" / "nw13-scene.v1.json"


@pytest.fixture()
def scene() -> scene_module.Scene:
    return scene_module.compile_scene(json.loads(SCENE_PATH.read_text(encoding="utf-8")))


def _track(channel: str, keys: list[tuple[int, list[float], str]]) -> dict:
    return {
        "channel": channel,
        "value_type": "vec3",
        "keys": [
            {"tick": tick, "value": value, "interpolation": interpolation}
            for tick, value, interpolation in keys
        ],
    }


def test_scene_schema_and_fixture_validate(scene: scene_module.Scene) -> None:
    document = json.loads(SCENE_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validate_instance(document, schema, schema)
    assert scene.duration_ticks == 288000
    assert {shot.id for shot in scene.shots} == {"corridor_approach", "room_turn"}


def test_rigid_parent_child_transforms_and_prop_follows_parent(scene: scene_module.Scene) -> None:
    body0 = scene_module.object_world_position(scene, "character_body", 0)
    prop0 = scene_module.object_world_position(scene, "held_prop", 0)
    body1 = scene_module.object_world_position(scene, "character_body", 144000)
    prop1 = scene_module.object_world_position(scene, "held_prop", 144000)
    assert body0 != body1
    assert prop0 != prop1
    # The prop's offset from its parent stays rigid apart from the parent's
    # rotation; its distance from the parent is preserved.
    distance0 = math.dist(body0, prop0)
    distance1 = math.dist(body1, prop1)
    assert distance0 == pytest.approx(distance1, abs=1e-9)


def test_hinged_door_handle_rotates_about_the_hinge(scene: scene_module.Scene) -> None:
    closed = scene_module.object_world_position(scene, "handle", 96000)
    open_position = scene_module.object_world_position(scene, "handle", 144000)
    assert math.dist(closed, open_position) > 0.2
    door_closed = scene_module.object_world_position(scene, "door", 96000)
    door_open = scene_module.object_world_position(scene, "door", 144000)
    # Rigid rotation preserves the handle-to-door distance.
    assert math.dist(door_closed, closed) == pytest.approx(math.dist(door_open, open_position), abs=1e-9)


def test_typed_tracks_and_interpolation_modes() -> None:
    linear = scene_module.TypedTrack(
        "transform.position",
        "vec3",
        (
            scene_module.TrackKey(0, (0.0, 0.0, 0.0), "linear"),
            scene_module.TrackKey(100, (10.0, 0.0, 0.0), "linear"),
        ),
    )
    step = scene_module.TypedTrack(
        "transform.position",
        "vec3",
        (
            scene_module.TrackKey(0, (0.0, 0.0, 0.0), "linear"),
            scene_module.TrackKey(100, (10.0, 0.0, 0.0), "step"),
        ),
    )
    smooth = scene_module.TypedTrack(
        "transform.position",
        "vec3",
        (
            scene_module.TrackKey(0, (0.0, 0.0, 0.0), "linear"),
            scene_module.TrackKey(100, (10.0, 0.0, 0.0), "smoothstep"),
        ),
    )
    assert linear.value_at(50)[0] == pytest.approx(5.0)
    assert step.value_at(50)[0] == pytest.approx(0.0)
    assert 0.0 < smooth.value_at(25)[0] < 2.5
    for channel in ("transform.position", "transform.rotation", "transform.scale"):
        track = scene_module.TypedTrack(
            channel,
            "vec3",
            (
                scene_module.TrackKey(0, (1.0, 1.0, 1.0), "linear"),
                scene_module.TrackKey(100, (2.0, 2.0, 2.0), "linear"),
            ),
        )
        assert track.value_at(50) == pytest.approx((1.5, 1.5, 1.5))


def test_camera_pose_target_projection_cut_and_action_independence(scene: scene_module.Scene) -> None:
    first = scene_module.camera_state(scene, 0, action_tick=0)
    second = scene_module.camera_state(scene, 192000, action_tick=0)
    assert first.shot_id == "corridor_approach"
    assert second.shot_id == "room_turn"
    assert second.cut is True
    assert first.position != second.position
    assert first.near > 0.0 and first.far > first.near
    # Camera motion is independent of action time.
    assert scene_module.camera_state(scene, 96000, action_tick=0) == scene_module.camera_state(scene, 96000, action_tick=200000)
    early_action = scene_module.action_state(scene, 0)
    late_action = scene_module.action_state(scene, 200000)
    assert early_action["objects"]["character_body"]["position"] != late_action["objects"]["character_body"]["position"]


def test_bounded_camera_transition_blends_between_shots() -> None:
    document = json.loads(SCENE_PATH.read_text(encoding="utf-8"))
    document["cameras"][0]["shots"][0]["end_tick"] = 120000
    document["cameras"][0]["shots"][1]["start_tick"] = 120000
    document["cameras"][0]["shots"][1]["end_tick"] = 288000
    document["cameras"][0]["shots"][1]["transition"] = "linear"
    document["cameras"][0]["shots"][1]["transition_ticks"] = 24000
    document["cameras"][0]["shots"][1]["position_track"]["keys"][0]["tick"] = 120000
    document["cameras"][0]["shots"][1]["position_track"]["keys"][1]["tick"] = 288000
    document["cuts"] = []
    compiled = scene_module.compile_scene(document)
    mid = scene_module.camera_state(compiled, 132000)
    assert mid.shot_id == "room_turn"
    assert 0.0 < mid.transition_weight < 1.0


def test_scene_diagnostics_cover_cycles_transforms_cameras_clipping_and_bounds() -> None:
    document = json.loads(SCENE_PATH.read_text(encoding="utf-8"))

    cycle = copy.deepcopy(document)
    for obj in cycle["objects"]:
        if obj["id"] == "room_shell":
            obj["parent"] = "corridor_wall"
        if obj["id"] == "corridor_wall":
            obj["parent"] = "room_shell"
    with pytest.raises(scene_module.SceneError) as exc:
        scene_module.compile_scene(cycle)
    assert any(item["code"] == "scene_hierarchy_cycle" for item in exc.value.diagnostics)

    bad_scale = copy.deepcopy(document)
    next(obj for obj in bad_scale["objects"] if obj["id"] == "room_shell")["transform"]["scale"] = [0, 1, 1]
    with pytest.raises(scene_module.SceneError) as exc:
        scene_module.compile_scene(bad_scale)
    assert any(item["code"] == "scene_invalid_transform" for item in exc.value.diagnostics)

    bad_clip = copy.deepcopy(document)
    bad_clip["cameras"][0]["shots"][0]["projection"]["far"] = 0.01
    with pytest.raises(scene_module.SceneError) as exc:
        scene_module.compile_scene(bad_clip)
    assert any(item["code"] == "scene_invalid_camera" for item in exc.value.diagnostics)

    out_of_bounds = copy.deepcopy(document)
    next(obj for obj in out_of_bounds["objects"] if obj["id"] == "room_shell")["transform"]["position"] = [100, 0, 0]
    with pytest.raises(scene_module.SceneError) as exc:
        scene_module.compile_scene(out_of_bounds)
    assert any(item["code"] == "scene_bounds_exceeded" for item in exc.value.diagnostics)

    degenerate = copy.deepcopy(document)
    degenerate["cameras"][0]["shots"][0]["target"] = degenerate["cameras"][0]["shots"][0]["position"]
    compiled = scene_module.compile_scene(degenerate)
    with pytest.raises(scene_module.SceneError) as exc:
        scene_module.camera_state(compiled, 0)
    assert any(item["code"] == "scene_degenerate_camera" for item in exc.value.diagnostics)


def test_fixed_tick_transport_freeze_reverse_resume_cut_and_one_shot_cues(scene: scene_module.Scene) -> None:
    transport = scene_module.Transport(scene)
    transport.step(96001)
    assert transport.tick == 96001
    assert any(event.get("cue_id") == "door_hinge" for event in transport.events)
    cue_count = sum(1 for event in transport.events if event.get("cue_id") == "door_hinge")
    assert cue_count == 1

    transport.freeze()
    frozen_tick = transport.tick
    produced = transport.step(2)
    assert transport.tick == frozen_tick
    assert produced[-1]["kind"] == "frozen"
    transport.resume()
    assert not transport.frozen

    transport.reverse()
    assert transport.direction == -1
    transport.step(1001)
    transport.reverse()
    transport.step(1001)
    assert sum(1 for event in transport.events if event.get("cue_id") == "door_hinge") == 1

    transport.step(192000 - transport.tick)
    assert any(event["kind"] == "cut" and event["cut_tick"] == 192000 for event in transport.events)

    transport.step(288000 - transport.tick + 1)
    loop_events = [event for event in transport.events if event["kind"] == "loop"]
    assert loop_events and loop_events[-1]["generation"] == 1
    transport.step(96001)
    # A new loop generation re-arms the declared one-shot cue.
    assert sum(1 for event in transport.events if event.get("cue_id") == "door_hinge") == 2

