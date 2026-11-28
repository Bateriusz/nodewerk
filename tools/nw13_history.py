"""Typed scene feedback on the shared fixed-step History scheduling contract."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import nw03_compiler as compiler
import nw13_scene as scenes

SOURCE = "nw.render.film_frame"
OPERATOR = "nw.feedback.scene_image"
STATE_TYPE = "nw.feedback.scene_image.v1"
INITIAL = "rebuild-previous-sample"
STEP_TICKS = compiler.NW09_TIMELINE_TICKS_PER_SIMULATION_TICK
PORTS = ("color", "depth", "camera")
TEMPLATE = Path(__file__).resolve().parents[1] / "shaders" / "nw13" / "scene_feedback.hlsl"


@dataclass(frozen=True)
class Feedback:
    node_id: str
    source_id: str
    film_id: str
    weight: float
    depth_tolerance: float


def active_nodes(document):
    roots = [node for node in document.nodes.values() if node.operator_id == "nw.render.present"]
    if not roots:
        return tuple(document.nodes.values())
    pending = [node.node_id for node in roots]
    seen = set()
    while pending:
        identifier = pending.pop()
        node = document.nodes.get(identifier)
        if node is None or identifier in seen:
            continue
        seen.add(identifier)
        pending.extend(source for source, _ in node.inputs.values())
    return tuple(node for node in document.nodes.values() if node.node_id in seen)


def node_present(document):
    return any(node.operator_id in {SOURCE, OPERATOR} for node in active_nodes(document))


def read_feedback(document) -> Feedback | None:
    active = active_nodes(document)
    producers = [node for node in active if node.operator_id == SOURCE]
    feedback = [node for node in active if node.operator_id == OPERATOR]
    if not producers and not feedback:
        return None
    if len(producers) != 1 or len(feedback) != 1:
        raise ValueError("scene feedback requires one frame producer and one feedback consumer")
    producer, node = producers[0], feedback[0]
    if any(node.inputs.get(port) != (producer.node_id, port) for port in PORTS):
        raise ValueError("scene feedback color, depth and camera must be views from the same film frame")
    film_id, port = producer.inputs.get("shader", (None, None))
    film = document.nodes.get(film_id)
    if film is None or film.operator_id != "nw.shader.film" or port != "out":
        raise ValueError("scene feedback frame producer requires a procedural film shader")
    present = [item for item in active if item.operator_id == "nw.render.present"]
    if len(present) != 1 or present[0].inputs.get("image") != (node.node_id, "out"):
        raise ValueError("the bounded film must present the selected feedback output")
    if any(item.node_id != node.node_id and item.history is not None for item in active):
        raise ValueError("one-sample scene feedback cannot share a schedule with recursive History")
    if node.history is None or (node.history.state_type, node.history.initial_value,
                                 node.history.reset_policy, node.history.step_rate_hz) != (
                                     STATE_TYPE, INITIAL, "on-seek", 120):
        raise ValueError("scene feedback needs the bounded on-seek History declaration")
    try:
        weight = float(node.params["weight"][0])
        depth_tolerance = float(node.params["depth_tolerance"][0])
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise ValueError("scene feedback requires typed weight and depth_tolerance") from exc
    if not 0 <= weight <= 0.5 or not 0.0001 <= depth_tolerance <= 0.25:
        raise ValueError("scene feedback weight or depth tolerance is out of bounds")
    return Feedback(node.node_id, producer.node_id, film.node_id, weight, depth_tolerance)


def predecessor(tick: int) -> int | None:
    """The only prior sample that can affect a presentation frame."""
    return (tick // STEP_TICKS - 1) * STEP_TICKS if tick >= STEP_TICKS else None


def reset_ticks(scene, timing):
    # Reversal and restarts invalidate a prior echo rather than showing an
    # unrelated state. A freeze/resume is not an action discontinuity.
    return tuple(sorted({
        *scene.cuts,
        *(shot.start_tick for shot in scene.shots[1:] if shot.transition == "cut"),
        *(event["presentation_tick"] for event in timing.events if event["kind"] == "reverse"),
    }))


def compatible_tick(tick: int, boundaries) -> int | None:
    previous = predecessor(tick)
    if previous is None or any(previous < boundary <= tick for boundary in boundaries):
        return None
    return previous


def _basis(camera):
    forward = camera.forward
    right = scenes._normalize(scenes._cross(forward, camera.up))
    up = scenes._normalize(scenes._cross(right, forward))
    return forward, right, up, math.tan(math.radians(camera.fov_degrees) * 0.5)


def composite(current, previous, *, width, height, weight, depth_tolerance):
    """Reproject one raw scene sample. Reject disocclusion and absent depth."""
    current_pixels, current_depth, camera_now = current
    if previous is None or weight == 0:
        return current_pixels
    previous_pixels, previous_depth, camera_before = previous
    f, r, up, tan = _basis(camera_now)
    pf, pr, pu, ptan = _basis(camera_before)
    result = list(current_pixels)
    for y in range(height):
        vertical = (1.0 - 2.0 * (y + 0.5) / height) * tan
        for x in range(width):
            pixel = y * width + x
            depth = current_depth[pixel]
            if not camera_now.near <= depth <= camera_now.far:
                continue
            horizontal = (2.0 * (x + 0.5) / width - 1.0) * tan * camera_now.aspect
            point = tuple(camera_now.position[i] + depth * (f[i] + horizontal*r[i] + vertical*up[i])
                          - camera_before.position[i] for i in range(3))
            z = scenes._dot(point, pf)
            if not camera_before.near <= z <= camera_before.far:
                continue
            old_x = 0.5 + scenes._dot(point, pr) / (2.0 * ptan * z * camera_before.aspect)
            old_y = 0.5 - scenes._dot(point, pu) / (2.0 * ptan * z)
            if not 0 <= old_x < 1 or not 0 <= old_y < 1:
                continue
            old = int(old_y * height) * width + int(old_x * width)
            if abs(previous_depth[old] - z) > depth_tolerance:
                continue
            for channel in range(3):
                i = pixel*4 + channel
                result[i] = current_pixels[i] * (1.0 - weight) + previous_pixels[old*4+channel] * weight
    return result


def emit_composite(spec, scene, timing, width, height):
    import nw13_film

    boundaries = " || ".join(f"(past<{tick}u && tick>={tick}u)" for tick in reset_ticks(scene, timing)) or "false"
    return TEMPLATE.read_text(encoding="utf-8").replace(
        "/* SETTINGS */", "\n".join((
            f"#define WIDTH {width}", f"#define HEIGHT {height}",
            f"#define DURATION {scene.duration_ticks}u", f"#define STEP_TICKS {STEP_TICKS}u",
            f"#define WEIGHT {nw13_film._literal(spec.weight)}",
            f"#define DEPTH_TOLERANCE {nw13_film._literal(spec.depth_tolerance)}",
            f"bool resetBetween(uint past, uint tick) {{ return {boundaries}; }}",
        )),
    ).replace("/* CAMERA */", nw13_film._camera(scene))


def build_stateful_package(document, *, optimized, allow_test_payloads, frames_in_flight,
                           packaged_checkpoint_ticks, dxc_executable, optimization,
                           allow_unpinned_dxc, cache_dir):
    """Lower the typed views into the existing History + frame-command ABI."""
    from dataclasses import replace
    import struct
    import tempfile

    import nw09_export
    import nw13_film
    import nwp_package as nwp

    if frames_in_flight < 2 or packaged_checkpoint_ticks:
        raise nw09_export.StatefulExportError("scene feedback uses two or more frame slots and no baked checkpoints")

    try:
        spec = read_feedback(document)
        root_node = document.nodes[spec.film_id]
        _, _, _, scene, settings, _ = nw13_film.read_film(document, root_node)
        width, height = (int(document.settings[f"nw05_output_{key}"]) for key in ("width", "height"))
        if (width, height) != tuple(settings["rendering"][key] for key in ("width", "height")):
            raise ValueError("film frame views and export dimensions must agree")
        timing, _ = nw13_film.film_timing(scene, settings)
        group = (8, 8, 1)
        generated = [
            compiler.GeneratedShader(
                specialization_id="nw13.0.sample." + spec.film_id, entry_point="FilmSample", target="cs_6_0",
                family=compiler.PASS_FAMILY,
                hlsl=nw13_film.emit(document, root_node, entry_point="FilmSample", thread_group=group,
                                    width=width, height=height),
                source_map=(), root_expression="", root_node_ids=(spec.film_id, spec.source_id), thread_group=group,
            ),
            compiler.GeneratedShader(
                specialization_id="nw13.1.compose." + spec.node_id, entry_point="FilmFeedback", target="cs_6_0",
                family=compiler.PASS_FAMILY, hlsl=emit_composite(spec, scene, timing, width, height),
                source_map=(), root_expression="", root_node_ids=(spec.node_id,), thread_group=group,
            ),
        ]
        with tempfile.TemporaryDirectory(prefix="nw13-history-base-") as scratch:
            path = Path(scratch) / "base.nwp"
            dxc = nw09_export.resolve_history_dxc(path, dxc_executable, allow_test_payloads=allow_test_payloads)
            compiled = compiler.compile_package(
                document.source_name, path, document=document,
                generated_shaders_override=generated,
                semantic_digest_override=optimized.materialized_semantic_digest(),
                semantic_digest_test_identity=allow_test_payloads,
                allow_test_payloads=allow_test_payloads, allow_unpinned_dxc=allow_unpinned_dxc,
                dxc_executable=dxc, optimization=optimization, cache_dir=cache_dir,
            )
            package = nwp.decode_package(path.read_bytes())
    except (ValueError, KeyError, TypeError, OSError, compiler.CompilerError) as exc:
        raise nw09_export.StatefulExportError(str(exc), getattr(exc, "diagnostics", ())) from exc

    # Ordinary image producer and feedback consumer each bind their own root.
    # The optional AS extends only the producer root; it cannot replace or
    # steal either of the feedback pass's SRVs.
    sample_id = next(shader.shader_id for shader in package.shaders if shader.entry_point == "FilmSample")
    feedback_id = next(shader.shader_id for shader in package.shaders if shader.entry_point == "FilmFeedback")
    if (sample_id, feedback_id) != (1, 2):
        raise nw09_export.StatefulExportError("film shaders are not at the versioned ABI IDs")
    base_root = package.root_signatures[0]
    feedback_bindings = base_root.bindings + (
        nwp.RootBinding("input", "srv", 0, 0, 2),
        nwp.RootBinding("film_current", "srv", 1, 0, 3),
    )
    feedback_root = replace(base_root, root_signature_id=2, dword_cost=7, bindings=feedback_bindings,
                            blob=nwp.make_root_signature_blob(base_root.family, feedback_bindings))
    shaders = tuple(replace(shader, root_signature_id=1 if shader.shader_id == sample_id else 2)
                    for shader in package.shaders)
    roots = (base_root, feedback_root)
    state_bytes = width * height * 16

    def buffer(identifier, name, usage, size, *, binding=0, format="DXGI_FORMAT_R32_UINT"):
        return nwp.ResourceDescriptor(resource_id=identifier, name=name, kind="buffer",
                                      format=format, usage=usage, pass_family=compiler.PASS_FAMILY,
                                      binding_slot=binding, binding_space=0, size_bytes=size)

    # The sampled surface temporarily owns RGB + positive view depth. After
    # the scheduler copies the predecessor out, the same UAV renders the
    # actual presentation tick; its immutable SRV copy is the current view.
    params, state_uav, previous_srv, current_srv, present_uav = 1, 2, 3, 4, 5
    resources = (
        buffer(params, "params", "cbv", compiler.PARAMETER_BUFFER_BYTES, format="DXGI_FORMAT_R32G32B32A32_FLOAT"),
        buffer(state_uav, "output", "uav", state_bytes),
        buffer(previous_srv, "input", "srv", state_bytes),
        buffer(current_srv, "film_current", "srv", state_bytes, binding=1),
        buffer(present_uav, "output", "uav", state_bytes, format="DXGI_FORMAT_R32G32B32A32_FLOAT"),
    )
    parameters = [
        replace(package.parameters[0], type="uint", value=bytes(4)),
        nwp.ParameterRecord(2, "nw05.present.width", "uint", "initialization", 1, struct.pack("<I", width)),
        nwp.ParameterRecord(3, "nw05.present.height", "uint", "initialization", 1, struct.pack("<I", height)),
        nwp.ParameterRecord(4, "nw13.film.duration_ticks", "uint", "initialization", 1,
                            struct.pack("<I", scene.duration_ticks)),
    ]

    def ins(opcode, *operands):
        return nwp.Instruction(opcode, tuple(nwp.InstructionOperand(kind, value) for kind, value in operands))

    initialization = (
        *(ins("init.create_resource", ("resource_ref", item.resource_id)) for item in resources),
        *(ins("init.create_root_signature", ("root_signature_ref", item.root_signature_id)) for item in roots),
        ins("init.upload_resource", ("resource_ref", params)),
        *(ins("init.create_pipeline", ("shader_ref", shader.shader_id), ("root_signature_ref", shader.root_signature_id))
          for shader in shaders),
    )
    frame = nwp.sampled_scene_frame(width, height, parameters[0].parameter_id, 1)
    declared_history = document.nodes[spec.node_id].history
    state = nwp.HistoryRecord(
        1, spec.node_id, OPERATOR, STATE_TYPE, INITIAL, "on-seek", 120,
        declared_history.state_layout_hash(), 4, width * height, state_uav, previous_srv,
        frames_in_flight=frames_in_flight, initial_state=b"",
    )
    capability = replace(
        package.capability, **nwp.derive_capability_requirements(resources, initialization, frame, shaders, roots),
        required_init_instructions=tuple(sorted({item.opcode for item in initialization})),
        required_frame_instructions=tuple(sorted({item.opcode for item in frame})),
    )
    stateful = replace(
        package, capability=replace(capability, profile_fingerprint=capability.computed_profile_fingerprint()),
        resources=resources, initialization=initialization, frame=frame, parameters=parameters,
        shaders=shaders, root_signatures=roots, history=(state,), flags=1,
    )
    diagnostics = nwp.validate_package_model(stateful)
    if diagnostics:
        raise nw09_export.StatefulExportError("typed scene History package is invalid", diagnostics)
    return stateful, {
        "format": nw09_export.NW09_EXPORT_FORMAT, "version": nw09_export.NW09_EXPORT_VERSION,
        "stateful": True, "fixed_step": {
            "simulation_hz": 120, "timeline_ticks_per_second": 48_000, "timeline_ticks_per_simulation_tick": STEP_TICKS,
        },
        "present_geometry": {"width": width, "height": height},
        "timeline_duration_ticks": scene.duration_ticks,
        "pending_retirement_bytes": state.state_bytes * frames_in_flight,
        "packaged_checkpoint_byte_cost": 0,
        "history": [state.to_dict()],
        "sampled_scene_history": {
            "views": list(PORTS), "storage": "rgb32f+positive_view_depth_r32f",
            "max_predecessor_dispatches_per_frame": 1,
            "reset_policy": "reject across shot cuts or directional discontinuities; rebuild on seek",
            "producer_shader_id": sample_id, "consumer_shader_id": feedback_id,
        },
        "compile_evidence": compiled,
    }
