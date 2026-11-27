"""Lower the editable film graph into a bounded common-profile compute pass."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import nw13_geometry as geometry
import nw13_particles as particles
import nw13_scene as scenes

OPERATOR = "nw.shader.film"
LIBRARY = "nw.geom.library"
TEMPLATE = Path(__file__).resolve().parents[1] / "shaders" / "nw13" / "film.hlsl"


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


def read_film(document, root):
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
    rendering = settings["rendering"]
    width, height = int(rendering["width"]), int(rendering["height"])
    steps = int(rendering["max_steps"])
    ray_length = float(rendering["max_distance"])
    epsilon = float(rendering["epsilon"])
    if not (1 <= width <= 512 and 1 <= height <= 288 and 1 <= steps <= 128
            and 0 < ray_length <= 128 and 1e-5 <= epsilon <= 0.05):
        raise ValueError("film image dimensions, ray steps, length or epsilon exceed the common limits")
    if rendering["samples"] != 1 or rendering["reflection_bounces"] not in (0, 1):
        raise ValueError("film common pass supports one sample and zero or one reflection bounce")
    if not (1 <= len(scene.objects) <= 32 and len(scene.lights) <= 8):
        raise ValueError("film common pass supports 1..32 objects and at most 8 lights")
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
    radius = settings.get("effects", {}).get("bloom", {}).get("radius", 0)
    if type(radius) is not int or not 0 <= radius <= 2:
        raise ValueError("film common pass requires a bloom radius in 0..2")
    traces = ((width + 7) // 8) * ((height + 7) // 8) * (8 + 2 * radius) ** 2
    def field_cost(recipe):
        factor = math.prod(recipe["parameters"]["counts"]) if recipe["operator"] == "nw.geom.repeat" else 1
        weight = 32 if recipe["operator"] == "nw.geom.displace" else 1
        return weight + factor * sum(field_cost(child) for child in recipe["children"])

    traces *= steps * sum(field_cost(recipes[obj.geometry]) for obj in scene.objects.values())
    traces *= max(1, len(scene.lights) + 1)
    traces *= 1 + rendering["reflection_bounces"]
    if traces > 150_000_000:
        raise ValueError("film per-frame trace work exceeds the common evaluation budget")
    return prepared, recipes, bounds, scene, settings, emitter


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
            t = f"saturate(({tick}-{a.tick}.0)/{b.tick-a.tick}.0)"
            if b.interpolation == "smoothstep":
                t = f"smoothstep(0,1,{t})"
            value = f"lerp({_literal(a.value)},{_literal(b.value)},{t})"
        compare = "<" if i == len(intervals) - 1 else "<="
        result = f"({tick}{compare}{b.tick}.0?{value}:{result})"
    return result


def _timeline(scene, settings):
    commands = settings.get("transport_script", {})
    spans = []
    for name, speed in (("freeze_at", 0), ("resume_at", 1), ("reverse_at", -1), ("forward_at", 1)):
        if name not in commands:
            continue
        tick = commands[name]
        if type(tick) is not int or not 0 <= tick <= scene.duration_ticks:
            raise ValueError(f"invalid film transport tick for {name}")
        spans.append((tick, speed))
    spans.sort()
    spans.append((scene.duration_ticks, 0))
    action, presentation, speed = 0, 0, 1
    flash = settings.get("flash")
    cue_tick = None
    onset = None
    if flash:
        cue = next((cue for cue in scene.cues if cue["id"] == flash["cue"]), None)
        if cue is None or flash["light"] not in scene.lights or type(flash["duration_ticks"]) is not int or flash["duration_ticks"] <= 0:
            raise ValueError("film flash requires a scene cue, light and positive duration")
        cue_tick = cue["tick"]
    segments = []
    for end, next_speed in spans:
        stop = action + (end-presentation)*speed
        if not 0 <= min(action, stop) <= max(action, stop) <= scene.duration_ticks:
            raise ValueError("film transport would leave its bounded action interval")
        if onset is None and cue_tick is not None and speed and min(stop, action) <= cue_tick <= max(stop, action):
            onset = presentation + (cue_tick-action)*speed
        segments.append((presentation, end, action, speed))
        action, presentation, speed = stop, end, next_speed
    return segments, action, onset


def _camera(scene):
    if len(scene.shots) > 8:
        raise ValueError("film common pass supports at most eight camera shots")
    lines = []
    for i, shot in enumerate(scene.shots):
        eye = _track(shot.position_track, shot.position, "t")
        target = _track(shot.target_track, shot.target, "t")
        lens = (shot.fov_degrees, shot.aspect, shot.near, shot.far)
        lines.append(f"Camera shot{i}(float t){{Camera c;"
                     f"c.eye={eye};c.target={target};c.up={_literal(shot.up)};"
                     f"c.lens={_literal(lens)};return c;}}")
    lines.append("Camera cameraAt(float t){")
    for i in reversed(range(1, len(scene.shots))):
        shot = scene.shots[i]
        lines.append(f"if(t>={shot.start_tick}.0){{Camera c=shot{i}(t);")
        if shot.transition != "cut" and shot.transition_ticks:
            t = f"saturate((t-{shot.start_tick}.0)/{shot.transition_ticks}.0)"
            if shot.transition == "smoothstep":
                t = f"smoothstep(0,1,{t})"
            lines.append(f"Camera prev=shot{i-1}({shot.start_tick}.0);float w={t};"
                         "c.eye=lerp(prev.eye,c.eye,w);c.target=lerp(prev.target,c.target,w);"
                         "c.up=lerp(prev.up,c.up,w);c.lens=lerp(prev.lens,c.lens,w);")
        lines.append("return c;}")
    lines.append("return shot0(t);}")
    return "\n".join(lines)


def _objects(scene, bounds, names, epsilon):
    objects = list(scene.objects.values())
    indices = {obj.id: i for i, obj in enumerate(objects)}
    lines = [f"#define OBJECT_COUNT {len(objects)}"]
    for i, obj in enumerate(objects):
        pos, rot, scale = (_track(obj.tracks.get(f"transform.{attr}"), getattr(obj, attr), "t")
                           for attr in ("position", "rotation", "scale"))
        lines.append(f"float4x4 local{i}(float t){{return localInverse({pos},{rot},{scale});}}")
    lines.append("void setupObjects(float t,out ObjectFrame objects[OBJECT_COUNT]){")
    for i, obj in enumerate(objects):
        chain = f"local{i}(t)"
        parent = obj.parent
        while parent is not None:
            chain = f"mul({chain},local{indices[parent]}(t))"
            parent = scene.objects[parent].parent
        bound = bounds[obj.geometry].expanded(4 * epsilon)
        lines.append(f"objects[{i}].inverse={chain};"
                     f"objects[{i}].low={_literal(bound.minimum)};objects[{i}].high={_literal(bound.maximum)};")
    lines.append("}")
    lines.append("float objectField(uint index,float3 p){switch(index){")
    lines.extend(f"case {i}:return {names[obj.geometry]}(p);" for i, obj in enumerate(objects))
    lines.append("default:return 1e20;}}")
    lines.append("Material materialAt(uint index){Material m;switch(index){")
    for i, obj in enumerate(objects):
        m = scene.materials[obj.material]
        lines.append(f"case {i}:m.base={_literal(m.base_color[:3])};m.emission={_literal(m.emission)};"
                     f"m.response={_literal((m.roughness,m.metallic,m.specular))};return m;")
    lines.append("default:m.base=0;m.emission=0;m.response=0;return m;}}")
    return "\n".join(lines)


def _lights(scene, settings, onset):
    lines = [f"#define LIGHT_COUNT {len(scene.lights)}", "Light lightAt(uint index,float t){Light light;switch(index){"]
    flash = settings.get("flash")
    for i, light in enumerate(scene.lights.values()):
        intensity = _literal(light.intensity)
        if flash and light.id == flash["light"]:
            intensity = "0" if onset is None else f"((t>={onset}.0 && t<{onset+flash['duration_ticks']}.0)?{intensity}:0)"
        lines.append(f"case {i}:light.position={_literal(light.position)};light.color={_literal(light.color)};"
                     f"light.intensity={intensity};light.range={_literal(light.range)};return light;")
    lines.append("default:light.position=0;light.color=0;light.intensity=0;light.range=1;return light;}}")
    return "\n".join(lines)


def _particle_code(emitter, scene):
    if emitter is None:
        return "float3 dust(float3 color,float2 uv,float depth,float action,Camera cam){return color;}"
    # The seeded launch data is generated once. The film shader reconstructs
    # motion directly from the bounded lifetime and its absolute action tick.
    velocities = [_literal(particles._spawn(emitter, i, 0).velocity) for i in range(emitter.count)]
    tint = scene.materials[emitter.material].base_color
    origin = _track(emitter.origin_track, emitter.origin, "spawnTick")
    return f"""
static const float3 launchVelocity[{emitter.count}]={{{','.join(velocities)}}};
float3 dust(float3 color,float2 uv,float depth,float action,Camera cam){{
    float3 forward=normalize(cam.target-cam.eye), right=normalize(cross(forward,cam.up));
    float3 up=normalize(cross(right,forward));
    float tangent=tan(radians(cam.lens.x)*0.5);
    float alpha=0;
    for(uint i=0;i<{emitter.count};++i){{
        float age=fmod(floor(action/400.0)+i,{emitter.lifetime_steps}.0)/120.0;
        float spawnTick=max(0,(floor(action/400.0)-age*120.0)*400.0);
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


def emit(document, root, *, entry_point, thread_group, width, height):
    _, recipes, bounds, scene, settings, emitter = read_film(document, root)
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
    segments, final_action, onset = _timeline(scene, settings)
    timeline = "\n".join([
        "float actionAt(float t) {",
        *(f"if(t<{end}.0)return {action}.0+(t-{start}.0)*({speed}.0);"
          for start, end, action, speed in segments),
        f"return {final_action}.0; }}",
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
            "FRAME_WIDTH": width, "FRAME_HEIGHT": height, "TRACE_STEPS": int(rendering["max_steps"]),
            "GROUP_X": thread_group[0], "GROUP_Y": thread_group[1],
            "BLOOM_RADIUS": int(bloom.get("radius", 0)),
            "TRACE_LIMIT": _literal(rendering["max_distance"]), "EPS": _literal(rendering["epsilon"]),
            "SHADOWS": int(rendering["shadows"]), "REFLECTIONS": rendering["reflection_bounces"],
            "MISS_RADIANCE": _literal(rendering["miss_radiance"]), "EXPOSURE": _literal(tone.get("exposure", 1)),
            "GAMMA": _literal(tone.get("gamma", 2.2)), "BLOOM_GAIN": _literal(bloom.get("intensity", 0)),
            "BLOOM_THRESHOLD": _literal(bloom.get("threshold", 1)),
        }.items()
    )).replace("/* FILM_DATA */", "\n".join((
        solid_functions, timeline, _camera(scene), _objects(scene, bounds, names, rendering["epsilon"]),
        _lights(scene, settings, onset), _particle_code(emitter, scene),
    ))).replace("FILM_ENTRY", entry_point)


def reference_image(document, root, tick, *, width, height):
    import nw13_production
    import nw13_render

    prepared, _, _, scene, settings, emitter = read_film(document, root)
    segments, action, onset = _timeline(scene, settings)
    for start, end, initial, speed in segments:
        if start <= tick < end:
            action = initial + (tick - start) * speed
            break
    rendering = settings["rendering"]
    renderer = nw13_render.Renderer(scene, prepared.document, width=width, height=height, **{
        key: rendering[key] for key in ("max_steps", "samples", "epsilon", "max_distance",
                                       "shadows", "reflection_bounces", "miss_radiance")
    })
    flash = settings.get("flash")
    light_gain = {}
    if flash:
        light_gain[flash["light"]] = float(onset is not None and onset <= tick < onset + flash["duration_ticks"])
    image, depth = renderer.render(tick, action_tick=action, light_gain=light_gain)
    if emitter is not None:
        image = renderer.render_particles(image, depth, emitter.to_dict(), tick, action_tick=action, film_cycle=True)
    return nw13_production._tone_image(settings, image, width, height)


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
    settings = {key: production[key] for key in ("rendering", "particles", "effects", "transport_script", "flash")
                if key in production}
    settings["geometry_budget"] = geometry_document.get("budget", {})
    params = {key: [json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)]
              for key, value in {"scene": scene_document, "production": settings}.items()}
    add("film", OPERATOR, params=params, inputs={"geometry": root}, frequency="frame")
    add("corridor_render", "nw.render.film", inputs={"shader": "film"}, frequency="frame", residency="gpu")
    add("hotel_screen", "nw.render.present", frequency="frame", residency="gpu")
    nodes["hotel_screen"].inputs["image"] = ("corridor_render", "image")
    layout.update({"film": (1220, 300), "corridor_render": (1440, 300), "hotel_screen": (1660, 300)})
    rendering = production["rendering"]
    document = compiler.GraphDocument(1, nwp.COMMON_PROFILE_ID, nwp.COMMON_REQUIREMENTS_DIGEST, {
        "optimization": "O2", "deterministic": "true", "frequency_policy": "strict",
        "nw05_output_width": str(rendering["width"]), "nw05_output_height": str(rendering["height"]),
    }, nodes, source_name, "", layout)
    return editor.emit_graph_text(document)


def compile_inline_ray_payload(document, output, dxc):
    """Compile the admitted AABB candidate query specialization and its bounds."""
    from dataclasses import replace
    import hashlib
    import nw03_compiler as compiler

    root = document.nodes["film"]
    _, _, bounds, scene, settings, _ = read_film(document, root)
    rendering = settings["rendering"]
    if rendering["max_distance"] > 64.0:
        raise ValueError("inline ray lowering admits rays of at most 64 units")
    width, height = int(document.settings["nw05_output_width"]), int(document.settings["nw05_output_height"])
    shaders, diagnostics = compiler.lower_to_fused_hlsl(
        document, target="cs_6_5", thread_group=(8, 8, 1),
        dispatch_groups=((width + 7) // 8, (height + 7) // 8, 1), image_width=width, image_height=height,
    )
    if diagnostics or len(shaders) != 1 or shaders[0].specialization_id != root.node_id:
        raise compiler.CompilerError("film inline ray lowering requires a single compute shader", diagnostics)
    shader = replace(shaders[0], hlsl="#define INLINE_RAY 1\n" + shaders[0].hlsl)
    output.mkdir(parents=True, exist_ok=True)
    dxil_path = output / "film_inline_ray.dxil"
    compiler.compile_generated_shader(shader, dxil_path, dxc_executable=str(dxc))
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
        "acceleration_builds": {"bottom": 1, "top": 1, "per_frame": 0},
    }
