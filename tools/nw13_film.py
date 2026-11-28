"""Lower the editable film graph into a bounded common-profile compute pass."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import nw13_geometry as geometry
import nw13_history as history
import nw13_particles as particles
import nw13_scene as scenes

OPERATOR = "nw.shader.film"
LIBRARY = "nw.geom.library"
MAX_FIELD_GRADIENT = 8.0
TEMPLATE = Path(__file__).resolve().parents[1] / "shaders" / "nw13" / "film.hlsl"
THREAD_GROUP = (8, 8, 1)  # Also the versioned typed-History package group.
WORK_PROFILES = {
    "verification-v1": (512, 288, 150_000_000),
    # Preserve the versioned sampled-History surface limits as well as the
    # independent player allocation budget. A larger raster needs a new proof.
    "inspection-v1": (512, 288, 6_500_000_000),
}


@dataclass(frozen=True)
class FilmDispatch:
    width: int
    height: int
    groups: tuple[int, int, int]
    profile: str

    @property
    def extent(self):
        return self.width, self.height

    @property
    def thread_group(self):
        return THREAD_GROUP


def resolve_dispatch(document, root, *, rendering=None, thread_group=None,
                     dispatch_groups=None, width=None, height=None):
    """One image and command shape, independent of window/presentation size."""
    if rendering is None:
        rendering = json.loads(root.params["production"][0])["rendering"]
    extent = tuple(rendering[key] for key in ("width", "height"))
    profile = rendering.get("work_profile", "verification-v1")
    if not isinstance(profile, str) or profile not in WORK_PROFILES:
        raise ValueError("film work_profile is not a supported bounded profile")
    max_width, max_height, _ = WORK_PROFILES[profile]
    if any(type(axis) is not int or not 1 <= axis <= limit
           for axis, limit in zip(extent, (max_width, max_height), strict=True)):
        raise ValueError(f"film raster dimensions must be integers within {max_width}x{max_height} for {profile}")
    keys = ("nw05_output_width", "nw05_output_height")
    if any(key in document.settings for key in keys):
        try:
            graph_extent = tuple(int(document.settings[key], 10) for key in keys)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("film graph must declare both integer raster axes or neither") from exc
        if graph_extent != extent:
            raise ValueError(f"film raster dimensions {extent} disagree with graph output {graph_extent}")
    if any(key in document.settings for key in ("nw07_output_width", "nw07_output_height")):
        raise ValueError("legacy NW-07 output settings cannot override the authored film raster")
    if (width is None) != (height is None) or (width is not None and (
            type(width) is not int or type(height) is not int or (width, height) != extent)):
        raise ValueError(f"requested film raster dimensions {(width, height)} disagree with authored raster {extent}")
    if thread_group is not None and (any(type(size) is not int for size in thread_group)
                                     or tuple(thread_group) != THREAD_GROUP):
        raise ValueError(f"film thread group must be exactly {THREAD_GROUP}")
    groups = tuple((size + group - 1) // group for size, group in zip(extent, THREAD_GROUP[:2], strict=True)) + (1,)
    if dispatch_groups is not None and (any(type(size) is not int for size in dispatch_groups)
                                        or tuple(dispatch_groups) != groups):
        raise ValueError(f"film dispatch must cover the raster in exactly {groups} groups")
    return FilmDispatch(*extent, groups, profile)


def _workload(dispatch, recipes, scene, rendering, emitter, effects, feedback):
    def field_cost(recipe):
        factor = math.prod(recipe["parameters"]["counts"]) if recipe["operator"] == "nw.geom.repeat" else 1
        weight = 32 if recipe["operator"] == "nw.geom.displace" else 1
        return weight + factor * sum(field_cost(child) for child in recipe["children"])

    # HLSL shades a clipped tile including the bloom halo. Count every
    # in-bounds sample, including samples shared by adjacent groups. All
    # rounded dispatch lanes still execute setup and the tile barrier.
    def samples_on_axis(size, group_count, lanes):
        return sum(min(size, (index + 1) * lanes + radius) - max(0, index * lanes - radius)
                   for index in range(group_count))

    radius = effects.get("bloom", {}).get("radius", 0)
    gx, gy, _ = dispatch.groups
    tx, ty, _ = dispatch.thread_group
    group_count = gx * gy
    lanes = group_count * tx * ty
    pixels = dispatch.width * dispatch.height
    samples = samples_on_axis(dispatch.width, gx, tx) * samples_on_axis(dispatch.height, gy, ty)
    passes = 2 if feedback else 1
    rays_per_sample = (1 + rendering["reflection_bounces"]) * (1 + (len(scene.lights) if rendering["shadows"] else 0))
    steps = rendering["max_steps"]
    fields_per_step = sum(field_cost(recipes[obj.geometry]) for obj in scene.objects.values())
    # A hit may evaluate six additional field samples for its gradient.
    field_units = passes * samples * rays_per_sample * (steps + 6) * fields_per_step
    # Keep other repeated shader work explicitly bounded and visible. Units
    # are deterministic admission weights, not instructions/ticks or timing.
    tile_slots = group_count * (tx + 2 * radius) * (ty + 2 * radius)
    setup = passes * (group_count if dispatch.profile == "inspection-v1" else lanes) * len(scene.objects)
    box_tests = passes * samples * rays_per_sample * len(scene.objects)
    particles = passes * samples * (emitter.count if emitter else 0)
    bloom_taps = passes * pixels * (1 + 2 * radius) ** 2
    tonemap_pixels = passes * pixels if "tonemap" in effects else 0
    history_lanes = lanes if feedback else 0
    history_copy_bytes = 2 * pixels * 16 if feedback else 0
    units = (field_units + 32 * setup + 8 * box_tests + 16 * particles + 4 * bloom_taps
             + 20 * tonemap_pixels + 32 * history_lanes + history_copy_bytes // 4
             + passes * (16 * lanes + 4 * tile_slots))
    return {
        "profile": dispatch.profile, "raster": list(dispatch.extent), "thread_group": list(dispatch.thread_group),
        "dispatch_groups": list(dispatch.groups), "rounded_lanes_per_pass": lanes,
        "tile_slots_per_pass": tile_slots,
        "scene_samples_per_pass": samples, "max_scene_passes": passes,
        "max_rays_per_sample": rays_per_sample, "max_weighted_field_operations": field_units,
        "max_object_frame_initializations": setup, "max_ray_object_candidates": box_tests,
        "max_particle_evaluations": particles, "max_bloom_taps": bloom_taps, "max_tonemap_pixels": tonemap_pixels,
        "max_history_composite_lanes": history_lanes, "max_history_copy_bytes": history_copy_bytes,
        "rgba32f_surface_bytes": pixels * 16,
        "work_units": units, "work_limit": WORK_PROFILES[dispatch.profile][2],
    }


def _literal(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return f"float{len(value)}(" + ",".join(_literal(x) for x in value) + ")"
    value = float(value)
    if not math.isfinite(value) or abs(value) > 1e12:
        raise ValueError("film constant outside the finite shader range")
    text = format(value, ".9g")
    return text if "." in text or "e" in text else text + ".0"


def graph_geometry(document, root) -> tuple[dict, dict]:
    """Resolve the geometry library through actual graph edges."""
    recipes: dict[str, dict] = {}
    visiting: set[str] = set()

    def visit(identifier: str, depth: int) -> dict:
        if depth > geometry.Budget().max_depth:
            raise ValueError(f"{identifier}: film geometry recursion limit exceeded")
        if identifier in visiting:
            raise ValueError(f"{identifier}: film geometry cycle")
        if identifier in recipes:
            return recipes[identifier]
        visiting.add(identifier)
        node = document.nodes[identifier]
        if not node.operator_id.startswith("nw.geom."):
            raise ValueError(f"{identifier}: expected a film geometry operator")
        parameters = {}
        for name, tokens in node.params.items():
            if name == "axis":
                parameters[name] = tokens[0]
            elif name in {"offset", "scale", "spacing", "half_extents"}:
                parameters[name] = [float(v) for v in tokens]
            elif name == "counts":
                values = [float(v) for v in tokens]
                if not all(v.is_integer() for v in values):
                    raise ValueError(f"{identifier}: repetition counts must be integers")
                parameters[name] = [int(v) for v in values]
            elif name == "seed":
                parameters[name] = int(float(tokens[0]))
            else:
                parameters[name] = float(tokens[0])
        children = [visit(source, depth + 1) for _, (source, _) in sorted(node.inputs.items())]
        recipe = {"id": identifier, "operator": node.operator_id, "version": node.version,
                  "parameters": parameters, "children": children}
        visiting.remove(identifier)
        recipes[identifier] = recipe
        return recipe

    tree = visit(root.inputs["geometry"][0], 0)
    return tree, recipes


def admit_film(document, root, **dispatch_options):
    tree, recipes = graph_geometry(document, root)
    settings = json.loads(root.params["production"][0])
    scene_doc = json.loads(root.params["scene"][0])
    budget = settings.get("geometry_budget", {})
    expanded_nodes = 0

    def counted_tree(recipe):
        nonlocal expanded_nodes
        expanded_nodes += 1
        if expanded_nodes > geometry.Budget().max_nodes:
            raise ValueError("film geometry library expansion exceeds its node budget")
        return {**recipe,
                "operator": "nw.geom.union" if recipe["operator"] == LIBRARY else recipe["operator"],
                "children": [counted_tree(child) for child in recipe["children"]]}

    # The aggregate budget accounts for the whole library, including repeated
    # references. The library itself has no field evaluation in the shader.
    prepared = geometry.prepare({"schema_version": 1, "root": counted_tree(tree), "budget": budget})
    bounds = {key: geometry.bounds_node(value) for key, value in recipes.items()
              if value["operator"] != LIBRARY}
    scene = scenes.compile_scene(scene_doc, geometry_bounds={
        key: (value.minimum, value.maximum) for key, value in bounds.items()
    })
    if (scene.shots[0].start_tick != 0 or scene.shots[-1].end_tick != scene.duration_ticks or any(
            a.end_tick != b.start_tick for a, b in zip(scene.shots, scene.shots[1:]))):
        raise ValueError("film shots must be a contiguous, non-overlapping presentation sequence")
    rendering = settings["rendering"]
    dispatch = resolve_dispatch(document, root, rendering=rendering, **dispatch_options)
    steps = rendering["max_steps"]
    ray_length = float(rendering["max_distance"])
    epsilon = float(rendering["epsilon"])
    if not (type(steps) is int and 1 <= steps <= 128
            and 0 < ray_length <= 128 and 1e-5 <= epsilon <= 0.05):
        raise ValueError("film ray steps, length or epsilon exceed the common limits")
    if (type(rendering["samples"]) is not int or rendering["samples"] != 1
            or type(rendering["shadows"]) is not bool or type(rendering["reflection_bounces"]) is not int
            or rendering["reflection_bounces"] not in (0, 1)):
        raise ValueError("film common pass supports one sample and zero or one reflection bounce")
    if not (1 <= len(scene.objects) <= 32 and len(scene.lights) <= 8):
        raise ValueError("film common pass supports 1..32 objects and at most 8 lights")
    if any(geometry.field_lipschitz(recipes[obj.geometry], bounds[obj.geometry].expanded(4 * epsilon)) > MAX_FIELD_GRADIENT
           for obj in scene.objects.values()):
        raise ValueError("film solid gradient bound exceeds the supported trace budget")
    if sum(len(t.keys) for obj in scene.objects.values() for t in obj.tracks.values()) > 128:
        raise ValueError("film common pass animation supports at most 128 object keys")
    emitter = None
    if "emitter" in settings.get("particles", {}):
        emitter = particles.compile_emitter(settings["particles"]["emitter"])
        if emitter.count > 128 or emitter.lifetime_steps > 240 or emitter.material not in scene.materials:
            raise ValueError("film particle count, reconstruction window or material exceeds the common contract")
        if emitter.origin_track and (len(emitter.origin_track.keys) > 16 or any(
            not scene.contains_point(key.value) for key in emitter.origin_track.keys
        )):
            raise ValueError("film emitter exceeds its animation key or scene bounds")
    effects = settings.get("effects", {})
    radius = effects.get("bloom", {}).get("radius", 0)
    if type(radius) is not int or not 0 <= radius <= 2:
        raise ValueError("film common pass requires a bloom radius in 0..2")
    feedback = history.read_feedback(document)
    if feedback is not None and feedback.film_id != root.node_id:
        raise ValueError("the selected feedback must bind this exact film root")
    workload = _workload(dispatch, recipes, scene, rendering, emitter, effects, feedback)
    if workload["work_units"] > workload["work_limit"]:
        raise ValueError(f"film per-frame work {workload['work_units']} exceeds the {dispatch.profile} "
                         f"evaluation budget {workload['work_limit']} for {dispatch.width}x{dispatch.height}")
    return (prepared, recipes, bounds, scene, settings, emitter), dispatch, workload


def read_film(document, root):
    return admit_film(document, root)[0]


def project_inspection_rasters(document, root, dimensions):
    """Static comparisons of the same scene, including disallowed candidates."""
    (_, recipes, _, scene, settings, emitter), _, _ = admit_film(document, root)
    feedback = history.read_feedback(document)
    profile = "inspection-v1"
    max_width, max_height, _ = WORK_PROFILES[profile]
    output = []
    for width, height in dimensions:
        if type(width) is not int or type(height) is not int or not (1 <= width <= 1920 and 1 <= height <= 1080):
            raise ValueError("film raster projections are bounded to 1920x1080")
        groups = ((width + THREAD_GROUP[0] - 1) // THREAD_GROUP[0],
                  (height + THREAD_GROUP[1] - 1) // THREAD_GROUP[1], 1)
        model = _workload(FilmDispatch(width, height, groups, profile), recipes, scene, settings["rendering"],
                          emitter, settings.get("effects", {}), feedback)
        model["within_raster_limits"] = 1 <= width <= max_width and 1 <= height <= max_height
        model["within_work_limit"] = model["work_units"] <= model["work_limit"]
        output.append(model)
    return output


def _sdf(recipe, names):
    p = recipe["parameters"]
    c = [names[x["id"]] for x in recipe["children"]]
    call = lambda i, v="p": f"{c[i]}({v})"
    n = _literal
    op = recipe["operator"].split(".")[-1]
    if op == "sphere":
        return f"return length(p)-{n(p['radius'])};"
    if op == "box":
        return f"return sdBox(p,{n(p['half_extents'])});"
    if op == "cylinder":
        return (f"float2 q=abs(float2(length(p.xz),p.y))-float2({n(p['radius'])},{n(p['half_height'])});"
                "return length(max(q,0))+min(max(q.x,q.y),0);")
    if op == "capsule":
        h = n(p["half_height"])
        return f"p.y-=clamp(p.y,-{h},{h});return length(p)-{n(p['radius'])};"
    if op == "torus":
        return f"return length(float2(length(p.xz)-{n(p['major_radius'])},p.y))-{n(p['minor_radius'])};"
    if op == "translate":
        return f"return {call(0, 'p-' + n(p['offset']))};"
    if op == "scale":
        return f"return {n(min(p['scale']))}*{call(0, 'p/' + n(p['scale']))};"
    if op == "rotate":
        axis = "xyz".index(p["axis"])
        return f"return {call(0, f'rotateAxis(p,{axis},-radians({n(p["degrees"])}))')};"
    if op in {"union", "intersection", "subtract", "smooth_union"}:
        a, b = call(0), call(1)
        expr = {"union": f"min({a},{b})", "intersection": f"max({a},{b})",
                "subtract": f"max({a},-{b})", "smooth_union": f"smoothMin({a},{b},{n(p.get('k', 0))})"}
        return f"return {expr[op]};"
    if op == "repeat":
        x, y, z = p["counts"]
        center = f"float3({x//2},{y//2},{z//2})"
        return (f"float d=1e20; for(uint z=0;z<{z};++z)for(uint y=0;y<{y};++y)for(uint x=0;x<{x};++x){{"
                f"d=min(d,{call(0, f'p-(float3(x,y,z)-{center})*{n(p["spacing"])}')});}}return d;")
    if op in {"twist", "bend"}:
        axis, component, extent = (1, "y", "half_height") if op == "twist" else (2, "x", "half_width")
        angle = f"-radians({n(p['max_degrees'])})*clamp(p.{component}/{n(p[extent])},-1,1)"
        return f"return {call(0, f'rotateAxis(p,{axis},{angle})')};"
    if op == "displace":
        return f"return {call(0)}+{n(p['amplitude'])}*(2*noise3(p*{n(p['frequency'])},{p['seed']}u)-1);"
    raise ValueError(f"{recipe['id']}: unsupported film solid {op}")


def _track(track, default, tick):
    if track is None:
        return _literal(default)
    result = _literal(track.keys[-1].value)
    intervals = list(zip(track.keys, track.keys[1:]))
    for i, (a, b) in reversed(list(enumerate(intervals))):
        if b.interpolation == "step" or b.tick == a.tick:
            value = _literal(a.value)
        else:
            weight = f"saturate(float({tick}-{a.tick}u)/{b.tick-a.tick}.0)"
            if b.interpolation == "smoothstep":
                weight = f"smoothstep(0,1,{weight})"
            value = f"lerp({_literal(a.value)},{_literal(b.value)},{weight})"
        compare = "<" if i == len(intervals) - 1 else "<="
        result = f"({tick}{compare}{b.tick}u?{value}:{result})"
    return f"({tick}<={track.keys[0].tick}u?{_literal(track.keys[0].value)}:{result})"


def film_timing(scene, settings):
    timeline = scenes.compile_film_timeline(scene, settings.get("transport_script", {}))
    flash = settings.get("flash")
    if not flash:
        return timeline, ()
    if (not isinstance(flash, dict) or flash.get("light") not in scene.lights
            or type(flash.get("duration_ticks")) is not int or flash["duration_ticks"] <= 0
            or not any(cue["id"] == flash.get("cue") for cue in scene.cues)):
        raise ValueError("film flash requires a scene cue, light and positive duration")
    return timeline, timeline.cue_onsets(flash["cue"])


def compile_film_song(song, document):
    """Compile the song against the same authored graph as the visual lowering."""
    import nw03_compiler
    import nw13_audio

    active = {node.node_id for node in history.active_nodes(document)}
    roots = [node for node in nw03_compiler._reachable_shader_roots(document)
             if node.operator_id == OPERATOR and node.node_id in active]
    if not roots:
        return nw13_audio.compile_to_nwas(song)
    if len(roots) != 1:
        raise ValueError("procedural film audio requires exactly one reachable film graph")
    _, _, _, scene, settings, _ = read_film(document, roots[0])
    timeline, _ = film_timing(scene, settings)
    bindings = settings.get("audio_cue_bindings", {})
    if not isinstance(bindings, dict) or any(
        not isinstance(note_id, str) or not isinstance(cue_id, str)
        for note_id, cue_id in bindings.items()
    ):
        raise ValueError("film audio cue bindings must map note IDs to cue IDs")
    # Diagnose malformed source before inspecting IDs or cloning tracks.
    nw13_audio.compile_sequence(song)
    seen = set()
    tracks = []
    for track in song["tracks"]:
        notes = []
        for note in track["notes"]:
            note_id = note.get("id")
            if note_id not in bindings:
                notes.append(note)
                continue
            if track.get("bus") != "effect" or note_id in seen:
                raise ValueError(f"film audio binding for {note_id!r} requires one unambiguous effect note")
            seen.add(note_id)
            cue_id = bindings[note_id]
            onsets = timeline.cue_onsets(cue_id)
            if not onsets:
                raise ValueError(f"film audio cue {cue_id!r} is not reached by the authored transport")
            notes.extend({**note, "tick": onset,
                          "id": note_id if len(onsets) == 1 else f"{note_id}@{onset}"} for onset in onsets)
        tracks.append({**track, "notes": notes})
    if bindings.keys() - seen:
        raise ValueError(f"film audio bindings reference missing notes: {sorted(bindings.keys() - seen)}")
    return nw13_audio.compile_to_nwas({**song, "tracks": tracks})


def _camera(scene):
    if len(scene.shots) > 8:
        raise ValueError("film common pass supports at most eight camera shots")
    lines = []
    for i, shot in enumerate(scene.shots):
        eye = _track(shot.position_track, shot.position, "t")
        target = _track(shot.target_track, shot.target, "t")
        lens = (shot.fov_degrees, shot.aspect, shot.near, shot.far)
        lines.append(f"Camera shot{i}(uint t){{Camera c;"
                     f"c.eye={eye};c.target={target};c.up={_literal(shot.up)};"
                     f"c.lens={_literal(lens)};return c;}}")
    lines.append("Camera cameraAt(uint t){")
    for i in reversed(range(1, len(scene.shots))):
        shot = scene.shots[i]
        lines.append(f"if(t>={shot.start_tick}u){{Camera c=shot{i}(t);")
        if shot.transition != "cut" and shot.transition_ticks:
            t = f"saturate(float(t-{shot.start_tick}u)/{shot.transition_ticks}.0)"
            if shot.transition == "smoothstep":
                t = f"smoothstep(0,1,{t})"
            lines.append(f"if(t<{shot.start_tick+shot.transition_ticks}u){{Camera prev=shot{i-1}(t);float w={t};"
                         "c.eye=lerp(prev.eye,c.eye,w);c.target=lerp(prev.target,c.target,w);}")
        lines.append("return c;}")
    lines.append("return shot0(t);}")
    return "\n".join(lines)


def _objects(scene, bounds, recipes, names, epsilon, *, shared=False):
    objects = list(scene.objects.values())
    indices = {obj.id: i for i, obj in enumerate(objects)}
    lines = [f"#define OBJECT_COUNT {len(objects)}"]
    for i, obj in enumerate(objects):
        pos, rot, scale = (_track(obj.tracks.get(f"transform.{attr}"), getattr(obj, attr), "t")
                           for attr in ("position", "rotation", "scale"))
        lines.append(f"float4x4 local{i}(uint t){{return localInverse({pos},{rot},{scale});}}")
    if shared:
        lines.append("ObjectFrame frameAt(uint index,uint t){ObjectFrame frame=(ObjectFrame)0;switch(index){")
    else:
        lines.append("void setupObjects(uint t,out ObjectFrame objects[OBJECT_COUNT]){")
    for i, obj in enumerate(objects):
        chain = f"local{i}(t)"
        parent = obj.parent
        while parent is not None:
            chain = f"mul({chain},local{indices[parent]}(t))"
            parent = scene.objects[parent].parent
        bound = bounds[obj.geometry].expanded(4 * epsilon)
        lipschitz = geometry.field_lipschitz(recipes[obj.geometry], bound)
        target = "frame" if shared else f"objects[{i}]"
        lines.append((f"case {i}:" if shared else "") +
                     f"{target}.inverse={chain};"
                     f"{target}.low={_literal(bound.minimum)};{target}.high={_literal(bound.maximum)};"
                     f"{target}.stepScale={_literal(0.95/lipschitz)};" + ("break;" if shared else ""))
    lines.append("}return frame;}" if shared else "}")
    # Scalar noinline signatures are valid in DXIL; keeping the shared field
    # specialization out of line controls size across primary/shadow/reflection
    # rays and normal samples without changing runtime dependencies.
    lines.append("[noinline] float objectFieldScalar(uint index,float x,float y,float z){"
                 "float3 p=float3(x,y,z);switch(index){")
    lines.extend(f"case {i}:return {names[obj.geometry]}(p);" for i, obj in enumerate(objects))
    lines.append("default:return 1e20;}}")
    lines.append("float objectField(uint i,float3 p){return objectFieldScalar(i,p.x,p.y,p.z);}")
    lines.append("Material materialAt(uint index){Material m;switch(index){")
    for i, obj in enumerate(objects):
        m = scene.materials[obj.material]
        lines.append(f"case {i}:m.base={_literal(m.base_color[:3])};m.emission={_literal(m.emission)};"
                     f"m.response={_literal((m.roughness,m.metallic,m.specular))};return m;")
    lines.append("default:m.base=0;m.emission=0;m.response=0;return m;}}")
    return "\n".join(lines)


def _lights(scene, settings, onsets):
    lines = [f"#define LIGHT_COUNT {len(scene.lights)}", "Light lightAt(uint index,uint t){Light light;switch(index){"]
    flash = settings.get("flash")
    for i, light in enumerate(scene.lights.values()):
        intensity = _literal(light.intensity)
        if flash and light.id == flash["light"]:
            duration = flash["duration_ticks"]
            active = " || ".join(f"(t>={onset}u && t<{min(onset+duration, scene.duration_ticks+1)}u)" for onset in onsets)
            intensity = f"(({active})?{intensity}:0)" if active else "0"
        lines.append(f"case {i}:light.position={_literal(light.position)};light.color={_literal(light.color)};"
                     f"light.intensity={intensity};light.range={_literal(light.range)};return light;")
    lines.append("default:light.position=0;light.color=0;light.intensity=0;light.range=1;return light;}}")
    return "\n".join(lines)


def _particle_code(emitter, scene):
    if emitter is None:
        return "float3 dust(float3 color,float2 uv,float depth,uint action,Camera cam){return color;}"
    # The seeded launch data is generated once. The film shader reconstructs
    # motion directly from the bounded lifetime and its absolute action tick.
    velocities = [_literal(particles._spawn(emitter, i, 0).velocity) for i in range(emitter.count)]
    tint = scene.materials[emitter.material].base_color
    origin = _track(emitter.origin_track, emitter.origin, "spawnTick")
    return f"""
static const float3 launchVelocity[{emitter.count}]={{{','.join(velocities)}}};
float3 dust(float3 color,float2 uv,float depth,uint action,Camera cam){{
    float3 forward=normalize(cam.target-cam.eye), right=normalize(cross(forward,cam.up));
    float3 up=normalize(cross(right,forward));
    float tangent=tan(radians(cam.lens.x)*0.5);
    float alpha=0;
    uint step=action/400u;
    for(uint i=0;i<{emitter.count};++i){{
        uint ageSteps=(step+i)%{emitter.lifetime_steps}u;
        float age=float(ageSteps)/120.0;
        uint spawnTick=(step>=ageSteps?step-ageSteps:0)*400u;
        float3 p={origin}+launchVelocity[i]*age*exp(-{_literal(emitter.drag)}*age)
                 +0.5*{_literal(emitter.gravity)}*age*age-cam.eye;
        float z=dot(p,forward);
        if(z<cam.lens.z || z>cam.lens.w || z>depth+{_literal(emitter.depth_bias)})continue;
        float2 center=0.5+float2(dot(p,right)/cam.lens.y,-dot(p,up))/(2*tangent*z);
        float size=lerp({_literal(emitter.size_start)},{_literal(emitter.size_end)},age/({_literal(emitter.lifetime_steps/120)}));
        float2 delta=(uv-center)*float2(FRAME_WIDTH,FRAME_HEIGHT)/max(0.6,size*FRAME_WIDTH/z);
        alpha=max(alpha,0.8*saturate(1-dot(delta,delta)));
    }}
    return lerp(color,{_literal(tint[:3])},alpha);
}}
"""


def emit(document, root, *, entry_point, thread_group=None, dispatch_groups=None, width=None, height=None):
    (_, recipes, bounds, scene, settings, emitter), dispatch, _ = admit_film(
        document, root, thread_group=thread_group, dispatch_groups=dispatch_groups, width=width, height=height,
    )
    feedback = history.read_feedback(document)
    shared = dispatch.profile == "inspection-v1"
    # The recipe library organizes scene geometry. Only object-bound solids
    # and their dependencies enter the shader.
    solid_ids = set()
    def mark(recipe):
        if recipe["id"] not in solid_ids:
            solid_ids.add(recipe["id"])
            for child in recipe["children"]:
                mark(child)
    for obj in scene.objects.values():
        mark(recipes[obj.geometry])
    names = {key: f"field{i}" for i, key in enumerate(sorted(solid_ids))}
    solid_functions = "\n".join(
        f"float {names[key]}(float3 p){{{_sdf(recipe,names)}}}"
        for key, recipe in recipes.items() if key in solid_ids
    )
    timing, onsets = film_timing(scene, settings)
    def action_expression(span):
        offset = f"(t-{span.start}u)"
        return f"{span.action}u" + ({-1: "-" + offset, 0: "", 1: "+" + offset}[span.speed])
    timeline = "\n".join([
        "uint actionAt(uint t) {",
        *(f"if(t<{span.end}u)return {action_expression(span)};" for span in timing.spans),
        f"return {timing.end_state.action}u; }}",
    ])
    rendering = settings["rendering"]
    effects = settings.get("effects", {})
    tone = effects.get("tonemap", {})
    bloom = effects.get("bloom", {})
    if tone.get("operator", "reinhard") != "reinhard" or not 0.1 <= float(tone.get("gamma", 2.2)) <= 5:
        raise ValueError("film common pass requires Reinhard and gamma in 0.1..5")
    template = TEMPLATE.read_text(encoding="utf-8")
    return template.replace("/* FILM_SETTINGS */", "\n".join(
        f"#define {key} {val}" for key, val in {
            "FRAME_WIDTH": dispatch.width, "FRAME_HEIGHT": dispatch.height, "TRACE_STEPS": rendering["max_steps"],
            "FILM_DURATION_TICKS": scene.duration_ticks,
            "FILM_CAPTURE_DEPTH": int(feedback is not None),
            "FILM_SHARED_OBJECTS": int(shared),
            "GROUP_X": dispatch.thread_group[0], "GROUP_Y": dispatch.thread_group[1],
            "BLOOM_RADIUS": int(bloom.get("radius", 0)),
            "TRACE_LIMIT": _literal(rendering["max_distance"]), "EPS": _literal(rendering["epsilon"]),
            "SHADOWS": int(rendering["shadows"]), "REFLECTIONS": rendering["reflection_bounces"],
            "MISS_RADIANCE": _literal(rendering["miss_radiance"]), "EXPOSURE": _literal(tone.get("exposure", 1)),
            "GAMMA": _literal(tone.get("gamma", 2.2)), "TONEMAP_ENABLED": int("tonemap" in effects),
            "BLOOM_GAIN": _literal(bloom.get("intensity", 0)),
            "BLOOM_THRESHOLD": _literal(bloom.get("threshold", 1)),
        }.items()
    )).replace("/* FILM_DATA */", "\n".join((
        solid_functions, timeline, _camera(scene),
        _objects(scene, bounds, recipes, names, rendering["epsilon"], shared=shared),
        _lights(scene, settings, onsets), _particle_code(emitter, scene),
    ))).replace("FILM_ENTRY", entry_point)


class FrameOracle:
    """One authored CPU renderer shared by Studio and evidence."""
    def __init__(self, document, root, *, width=None, height=None):
        import nw13_render

        data, dispatch, _ = admit_film(document, root, width=width, height=height)
        prepared, _, _, self.scene, self.settings, self.emitter = data
        self.width, self.height = dispatch.extent
        self.timing, self.onsets = film_timing(self.scene, self.settings)
        self.feedback = history.read_feedback(document)
        self.resets = history.reset_ticks(self.scene, self.timing)
        rendering = self.settings["rendering"]
        self.renderer = nw13_render.Renderer(self.scene, prepared.document, width=self.width, height=self.height, **{
            key: rendering[key] for key in ("max_steps", "samples", "epsilon", "max_distance",
                                            "shadows", "reflection_bounces", "miss_radiance")
        })

    def light_gains(self, tick):
        flash = self.settings.get("flash")
        if flash:
            return {flash["light"]: float(any(onset <= tick < onset + flash["duration_ticks"] for onset in self.onsets))}
        return {}

    def sample(self, tick):
        import nw13_production

        tick = max(0, min(tick, self.scene.duration_ticks))
        action = self.timing.at(tick)["action_tick"]
        image, depth = self.renderer.render(tick, action_tick=action, light_gain=self.light_gains(tick))
        if self.emitter is not None:
            image = self.renderer.render_particles(image, depth, self.emitter.to_dict(), tick, action_tick=action, film_cycle=True)
        return (nw13_production._tone_image(self.settings, image, self.width, self.height),
                depth, scenes.camera_state(self.scene, tick, action_tick=action))

    def render(self, tick):
        tick = max(0, min(tick, self.scene.duration_ticks))
        current = self.sample(tick)
        if not self.feedback or not self.feedback.weight:
            return current[0]
        predecessor = history.compatible_tick(tick, self.resets)
        previous = self.sample(predecessor) if predecessor is not None else None
        return history.composite(current, previous, width=self.width, height=self.height,
                                 weight=self.feedback.weight, depth_tolerance=self.feedback.depth_tolerance)


def reference_image(document, root, tick, *, width=None, height=None):
    return FrameOracle(document, root, width=width, height=height).render(tick)


def import_graph(geometry_document, scene_document, production, *, source_name):
    """Import the public film documents into a self-contained editable graph."""
    import nw03_compiler as compiler
    import nw08_editor as editor
    import nwp_package as nwp

    geometry.prepare(geometry_document)
    camera_scene = scenes.compile_scene(scene_document)
    visible = {obj.geometry for obj in camera_scene.objects.values()}
    nodes = {}
    layout = {}

    def add(identifier, operator, *, params=None, inputs=None, frequency="initialization", residency="logical"):
        if identifier in nodes:
            raise ValueError(f"duplicate film node {identifier}")
        nodes[identifier] = compiler.GraphNode(
            identifier, operator, "1.0.0", frequency, residency,
            params=params or {}, inputs={port: (source, "out") for port, source in (inputs or {}).items()},
        )
        return identifier

    def visit(recipe, depth=0, solid=False):
        identifier = recipe["id"]
        solid = solid or identifier in visible
        children = [visit(child, depth + 1, solid) for child in recipe.get("children", [])]
        operator = recipe["operator"]
        if operator == "nw.geom.union" and not solid:
            operator = LIBRARY
        # Public geometry permits n-ary CSG. The graph ABI uses binary ports.
        if len(children) > 2:
            for i, child in enumerate(children[1:-1], 1):
                name = f"{identifier}_part{i}"
                params = recipe.get("parameters", {})
                children[i] = add(name, operator, params=encode(params), inputs={"a": children[i-1], "b": child})
            children = children[-2:]
        params = encode(recipe.get("parameters", {}))
        ports = ("child",) if len(children) == 1 else ("a", "b")
        node_id = add(identifier, operator, params=params, inputs=dict(zip(ports, children)))
        layout[node_id] = (max(0, 1000 - 200 * depth), len(layout) * 75)
        return node_id

    def encode(params):
        return {name: [str(value) for value in values] if isinstance(values, list) else [str(values)]
                for name, values in params.items()}

    root = visit(geometry_document["root"])
    settings = {key: production[key] for key in ("rendering", "particles", "effects", "transport_script", "flash", "audio_cue_bindings")
                if key in production}
    temporal = settings.get("effects", {}).get("temporal_feedback")
    if temporal is not None:
        if not isinstance(temporal, dict) or set(temporal) != {"weight", "depth_tolerance"}:
            raise ValueError("temporal feedback requires a weight and depth_tolerance")
        settings["effects"] = {key: value for key, value in settings["effects"].items() if key != "temporal_feedback"}
    settings["geometry_budget"] = geometry_document.get("budget", {})
    params = {key: [json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)]
              for key, value in {"scene": scene_document, "production": settings}.items()}
    add("film", OPERATOR, params=params, inputs={"geometry": root}, frequency="frame")
    if temporal is not None:
        add("corridor_render", history.SOURCE, inputs={"shader": "film"}, frequency="frame", residency="gpu")
        add("scene_feedback", history.OPERATOR, params=encode(temporal), frequency="history", residency="gpu")
        nodes["scene_feedback"].inputs = {port: ("corridor_render", port) for port in history.PORTS}
        nodes["scene_feedback"].history = compiler.HistoryDeclaration(history.STATE_TYPE, history.INITIAL, "on-seek")
        image = ("scene_feedback", "out")
        layout["scene_feedback"] = (1660, 300)
    else:
        add("corridor_render", "nw.render.film", inputs={"shader": "film"}, frequency="frame", residency="gpu")
        image = ("corridor_render", "image")
    add("hotel_screen", "nw.render.present", frequency="frame", residency="gpu")
    nodes["hotel_screen"].inputs["image"] = image
    layout.update({"film": (1220, 300), "corridor_render": (1440, 300), "hotel_screen": (1880 if temporal else 1660, 300)})
    rendering = production["rendering"]
    document = compiler.GraphDocument(1, nwp.COMMON_PROFILE_ID, nwp.COMMON_REQUIREMENTS_DIGEST, {
        "optimization": "O2", "deterministic": "true", "frequency_policy": "strict",
        "nw05_output_width": str(rendering["width"]), "nw05_output_height": str(rendering["height"]),
    }, nodes, source_name, "", layout)
    return editor.emit_graph_text(document)


def compile_inline_ray_payload(document, output, dxc, *, allow_unpinned_dxc=False):
    """Compile the admitted AABB candidate query specialization and its bounds."""
    from dataclasses import replace
    import hashlib
    import nw03_compiler as compiler

    roots = compiler._reachable_shader_roots(document)
    if len(roots) != 1 or roots[0].operator_id != OPERATOR:
        raise ValueError("inline ray lowering requires one reachable film root")
    root = roots[0]
    (_, _, bounds, scene, settings, _), dispatch, workload = admit_film(document, root)
    rendering = settings["rendering"]
    if rendering["max_distance"] > 64.0:
        raise ValueError("inline ray lowering admits rays of at most 64 units")
    shaders, diagnostics = compiler.lower_to_fused_hlsl(
        document, target="cs_6_5", thread_group=dispatch.thread_group,
        dispatch_groups=dispatch.groups, image_width=dispatch.width, image_height=dispatch.height,
    )
    if diagnostics or len(shaders) != 1 or shaders[0].specialization_id != root.node_id:
        raise compiler.CompilerError("film inline ray lowering requires a single compute shader", diagnostics)
    shader = replace(shaders[0], hlsl="#define INLINE_RAY 1\n" + shaders[0].hlsl)
    output.mkdir(parents=True, exist_ok=True)
    dxil_path = output / "film_inline_ray.dxil"
    compiler.compile_generated_shader(shader, dxil_path, dxc_executable=str(dxc),
                                      qualified_shader_model="cs_6_5",
                                      allow_unpinned_dxc=allow_unpinned_dxc)
    dxil = dxil_path.read_bytes()
    tuples = []
    for obj in scene.objects.values():
        local = bounds[obj.geometry].expanded(rendering["epsilon"] * 8)
        low, high = scenes.swept_object_bounds(scene, obj, (local.minimum, local.maximum))
        tuples.append("{" + ",".join(_literal(value) + "f" for value in (*low, *high)) + "}")
    header = output / "nw13_inline_ray_payload.h"
    hex_rows = [",".join(f"0x{value:02x}" for value in dxil[start:start+24])
                for start in range(0, len(dxil), 24)]
    header.write_text(
        "#pragma once\n#include <d3d12.h>\n"
        "inline constexpr D3D12_RAYTRACING_AABB kFilmRayBounds[]={\n" + ",\n".join(tuples) + "\n};\n"
        "inline constexpr unsigned char kFilmRayDxil[]={\n" + ",\n".join(hex_rows) + "\n};\n",
        encoding="utf-8", newline="\n",
    )
    return {
        "inline_shader_bytes": len(dxil),
        "inline_shader_sha256": "sha256:" + hashlib.sha256(dxil).hexdigest(),
        "payload_sha256": "sha256:" + hashlib.sha256(header.read_bytes()).hexdigest(),
        "bounds_count": len(tuples),
        "film_workload": workload,
        "acceleration_builds": {"bottom": 1, "top": 1, "per_frame": 0},
    }
