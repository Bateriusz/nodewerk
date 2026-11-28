#!/usr/bin/env python3
"""Build and verify NW-13 artifacts from a clean measured revision."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
import subprocess
import struct
import tempfile
from pathlib import Path
from typing import Any, Sequence

import dependency_check
import nw03_compiler
import nw05_export
import nw08_editor
import nw13_evidence as evidence
import nw13_film
import nw13_history
import nw13_render
import nw13_scene
import nw13_production as production
import nwp_package as nwp


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _description(result: nw05_export.ExportResult) -> dict[str, Any]:
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    if artifact.executable_bytes is None:
        raise evidence.EvidenceError("export did not produce a standalone executable")
    executable = artifact.executable_bytes
    pe = dependency_check.parse_pe64_sections_and_imports(executable)
    imports = [
        f"{library.lower()}!{symbol}"
        for key in ("imports_by_dll", "delay_imports_by_dll")
        for library, symbols in pe[key].items() for symbol in symbols
    ]
    accounting = nw05_export.size_observation(
        artifact.size_report, executable, profile=artifact.profile_id,
        accounting_phase="stateful-informational-finalized" if artifact.package.history else "stateless-finalized",
    )
    distribution = accounting["distribution"]
    directory = Path(result.output_directory)
    package_file = "common.nwp" if artifact.package.history else f"{Path(artifact.executable_name).stem}.nwp"
    manifest_file = accounting["distribution_accounting"]["counted_manifest_path"]
    files = {package_file: distribution["package_bytes"], manifest_file: distribution["manifest_bytes"],
             artifact.executable_name: distribution["executable_bytes"], **distribution["artifact_files"]}
    for name, expected in files.items():
        if not (directory / name).is_file() or (directory / name).stat().st_size != expected:
            raise evidence.EvidenceError(f"exported file byte count does not match the size record: {name}")
    actual_payloads = {package_file: artifact.package_bytes, artifact.executable_name: executable,
                       manifest_file: nw05_export.optimizer.manifest_bytes(artifact.manifest)}
    if any((directory / name).read_bytes() != contents for name, contents in actual_payloads.items()):
        raise evidence.EvidenceError("exported executable, package or manifest differs from measured build bytes")
    total = sum(files.values())
    if total != distribution["total_distributed_bytes"]:
        raise evidence.EvidenceError("exported file byte counts do not sum to the reported distribution")
    lowering = artifact.manifest.get("film_lowering", {})
    return {
        "distribution_bytes": total,
        "size_accounting": {
            "status": "informational",
            "export_files": dict(sorted(files.items())),
            "package_bytes": distribution["package_bytes"],
            "executable": accounting["executable"],
        },
        "release_code_bytes": sum(section["virtual_size_bytes"] for section in pe["sections"]
                                  if section["characteristics_flags"] & 0x20000000),
        "shader_bytes": int(artifact.size_report["content"]["unique_shader_blob_bytes"]) + int(lowering.get("inline_shader_bytes", 0)),
        "sections": {
            section: (value["size_bytes"], value["records"])
            for section, value in nw05_export.section_sizes(artifact.package_bytes).items()
        },
        "imports": sorted(imports),
        "executable_sha256": _sha256(executable),
        "package_sha256": _sha256(artifact.package_bytes),
        "audio_song_sha256": _sha256(artifact.package.audio_song) if artifact.package.audio_song else None,
        "manifest_sha256": artifact.manifest["manifest_sha256"],
        "film_lowering": lowering or None,
        "manifest_file_sha256": _sha256(nw05_export.optimizer.manifest_bytes(artifact.manifest)),
        "dependency_identities": sorted(
            (item["name"], item["kind"], item["distributed_bytes"])
            for item in artifact.size_report["dependencies"] if item["included"]
        ),
    }


def _read_capture(path, dimensions):
    first = path.read_bytes()
    expected = dimensions[0] * dimensions[1] * 16
    if len(first) != expected:
        raise evidence.EvidenceError(f"capture does not contain {dimensions[0]}x{dimensions[1]} RGBA32F pixels")
    pixels = struct.unpack(f"<{expected // 4}f", first)
    if any(not math.isfinite(channel) for channel in pixels):
        raise evidence.EvidenceError("capture contains a nonfinite pixel channel")
    if any(alpha != 1.0 for alpha in pixels[3::4]):
        raise evidence.EvidenceError("film capture has invalid opaque presentation alpha")
    return first, pixels


def _capture(executable, directory, dimensions, *, tick, adapter=None):
    directory.mkdir(parents=True, exist_ok=True)
    frame = directory / f"tick-{tick:06d}.rgba32f"
    frame.unlink(missing_ok=True)
    environment = {**os.environ, "NODEWERK_ADAPTER_LUID": adapter} if adapter else None
    observation = evidence.smoke_observation(
        str(executable.resolve()),
        ["--tick", str(tick), "--headless", "--allow-software-adapter", "--film-capture", str(frame.resolve())],
        environment=environment,
    )
    if observation["status"] == "pass":
        try:
            raw, _ = _read_capture(frame, dimensions)
        except (OSError, evidence.EvidenceError) as exc:
            observation.update(status="error", error=f"incomplete film readback: {exc.__class__.__name__}")
        else:
            observation["capture"] = frame.name
            observation["capture_sha256"] = _sha256(raw)
    return observation


def _smoke(result, directory, dimensions, *, tick=148800, adapter=None):
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    return _capture(directory / artifact.executable_name, directory, dimensions, tick=tick, adapter=adapter)


def _export(graph, directory, build, template, dxc, audio_payload=None, *, selection=None, revision=None):
    extra = {}
    if selection:
        payload = build / "ray"
        try:
            shader = nw13_film.compile_inline_ray_payload(nw03_compiler.load_graph(graph), payload, dxc)
        except (ValueError, OSError) as error:
            raise evidence.EvidenceError(str(error)) from error
        extra = {"film_optional_provenance": {
            **selection, **shader, "revision": revision,
            "command_abi_profile": nwp.COMMON_PROFILE_ID, "runtime_dependencies": ["d3d12", "dxgi"],
        }}
    return nw05_export.export_graph(
        graph, directory,
        settings=nw05_export.ExportSettings(
            byte_ceiling=None,
            memory_budget=64 * 1024 * 1024, player_template=str(template),
            specialized_build_root=str(build / "native"),
            film_inline_ray_payload_dir=str(payload) if selection else "",
        ),
        dxc_executable=str(dxc), cache_dir=str(build / "dxc-cache"),
        audio_song_payload=audio_payload, **extra,
    )


def _independent_observations(graph, output, build, template, dxc, audio_payload, dimensions, *,
                              selection=None, adapter=None, revision=None):
    descriptions = []
    for arm in ("a", "b"):
        directory = output / f"independent-{arm}"
        result = _export(graph, directory, build / arm, template, dxc, audio_payload,
                         selection=selection, revision=revision)
        description = _description(result)
        description["smoke"] = _smoke(result, directory, dimensions, adapter=adapter)
        for metric in ("startup_ms", "committed_memory_bytes"):
            description[metric] = description["smoke"].get(metric)
        if description["audio_song_sha256"] != _sha256(audio_payload):
            raise evidence.EvidenceError("standalone package song differs from the compiled film song")
        descriptions.append(description)
    first, second = descriptions
    identities = ("executable_sha256", "package_sha256", "manifest_file_sha256", "dependency_identities")
    different = [key for key in identities if first[key] != second[key]]
    if different:
        raise evidence.EvidenceError("independent clean builds differ in " + ", ".join(different))
    for item in descriptions:
        if item["smoke"]["status"] != "pass":
            raise evidence.EvidenceError(f"independently rebuilt standalone failed smoke: {item['smoke']}")
    if first["smoke"]["capture_sha256"] != second["smoke"]["capture_sha256"]:
        raise evidence.EvidenceError("independently rebuilt standalones disagree at the same tick")
    return first, {"a": first, "b": second}


def _compare_pixels(first, second, tolerance):
    if not first or len(first) != len(second):
        raise evidence.EvidenceError("matched images must have identical nonzero dimensions")
    if any(not math.isfinite(channel) for image in (first, second) for channel in image):
        raise evidence.EvidenceError("matched images contain nonfinite channels")
    differences = [abs(a - b) for a, b in zip(first, second, strict=True)]
    maximum = max(differences)
    rmse = math.sqrt(sum(value * value for value in differences) / len(differences))
    return {
        "image_max_abs_error": maximum, "image_rmse": rmse,
        "image_within_tolerance": maximum <= tolerance["max_abs_channel_error"] and rmse <= tolerance["rmse"],
    }


def _summary(comparisons):
    if not comparisons:
        raise evidence.EvidenceError("film comparison needs at least one retained frame")
    return {
        "image_max_abs_error": max(row["image_max_abs_error"] for row in comparisons),
        "image_rmse": max(row["image_rmse"] for row in comparisons),
        "image_within_tolerance": all(row["image_within_tolerance"] for row in comparisons),
    }


def _standalone(directory):
    return directory / (nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID) + ".exe")


def _native_frame_set(document, parsed_graph, source, directory, adapter, *, common_frames=None):
    """Retain real readbacks of all declared key frames, including the exact cut."""
    root = next(node for node in nw03_compiler._reachable_shader_roots(parsed_graph)
                if node.operator_id == nw13_film.OPERATOR)
    _, _, _, scene, settings, _ = nw13_film.read_film(parsed_graph, root)
    width, height = dimensions = tuple(document["rendering"][axis] for axis in ("width", "height"))
    tolerance = document["rendering"]["tolerances"]
    timeline, onsets = nw13_film.film_timing(scene, settings)
    ticks = sorted({*document["fixed_ticks"], *(event["presentation_tick"] for event in timeline.events
                    if event["kind"] in {"cut", "freeze", "reverse", "resume"}), *onsets})
    if not ticks or len(ticks) > 32:
        raise evidence.EvidenceError("film requires 1..32 distinct native proof ticks")
    rows, pixels = [], {}
    for tick in ticks:
        observation = _capture(_standalone(source), directory, dimensions, tick=tick, adapter=adapter)
        if observation["status"] != "pass":
            raise evidence.EvidenceError(f"native film capture failed at tick {tick}")
        capture = directory / observation["capture"]
        _, image = _read_capture(capture, dimensions)
        if common_frames is None:
            expected = nw13_film.reference_image(parsed_graph, root, tick, width=width, height=height)
            reference_kind = "graph_cpu_oracle"
        else:
            _, expected = _read_capture(common_frames / capture.name, dimensions)
            reference_kind = "matched_common_gpu"
        numbers = _compare_pixels(image, expected, tolerance)
        rows.append({"tick": tick, **timeline.at(tick), "reference": reference_kind,
                     "file": capture.name, "sha256": observation["capture_sha256"],
                     "shot": nw13_scene.camera_state(scene, tick).shot_id, **numbers})
        pixels[tick] = image
    nw13_render.contact_sheet(list(pixels.values()), width, height, columns=min(4, len(pixels)),
                             path=directory / "contact-sheet.ppm")
    result = {"status": "measured_native", "width": width, "height": height, "frames": rows,
              "contact_sheet": {"file": "contact-sheet.ppm", "sha256": _sha256((directory / "contact-sheet.ppm").read_bytes())},
              **_summary(rows)}
    (directory / "native-frames.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not result["image_within_tolerance"]:
        divergent = [row["tick"] for row in rows if not row["image_within_tolerance"]]
        raise evidence.EvidenceError(f"{reference_kind} tolerance failed for native film ticks {divergent}")
    return result


def _unreachable_observations(repo_root, film_graph, output, build, template, dxc):
    unrelated = repo_root / "fixtures" / "nw05" / "procedural_image.nwgraph"
    original = nw03_compiler.load_graph(unrelated)
    film = nw03_compiler.load_graph(film_graph)
    for identifier, node in film.nodes.items():
        if node.operator_id in {"nw.render.present", "nw.render.film"}:
            continue
        node.node_id = "dead_film_" + identifier
        node.inputs = {port: ("dead_film_" + source, output) for port, (source, output) in node.inputs.items()}
        original.nodes[node.node_id] = node
    edited = build / "source" / unrelated.name
    edited.parent.mkdir(parents=True)
    edited.write_text(nw08_editor.emit_graph_text(original), encoding="utf-8")
    first = _description(_export(unrelated, output / "unrelated-a", build / "a", template, dxc))
    second = _description(_export(edited, output / "unrelated-b", build / "b", template, dxc))
    compared = ("release_code_bytes", "shader_bytes", "imports", "sections", "dependency_identities")
    different = [key for key in compared if first[key] != second[key]]
    if different:
        raise evidence.EvidenceError("unreachable film adds release weight in " + ", ".join(different))
    return {
        **evidence.unreachable_incremental(False, observations={
            "additional_release_code_bytes": second["release_code_bytes"] - first["release_code_bytes"],
            "additional_shader_bytes": second["shader_bytes"] - first["shader_bytes"],
            "additional_imports": sorted(set(second["imports"]) - set(first["imports"])),
            "additional_package_sections": sorted(second["sections"].keys() - first["sections"].keys()),
            "additional_dependencies": sorted(set(second["dependency_identities"]) - set(first["dependency_identities"])),
        }),
        "arms": {"baseline": first, "with_unreachable_film": second},
    }


def _native_edit_proof(document, graph, output, build, template, dxc, audio, adapter):
    """Exercise the same inspector edit against the shared reference preview and two standalones."""
    import nw08_preview

    source = nw03_compiler.load_graph(graph)
    author = nw08_editor.GraphEditorSession(source)
    change = author.set_parameter("mirror_panel", "half_extents", [0.15, 0.15, 0.04])
    if not change.valid:
        raise evidence.EvidenceError("film edit validation failed", change.diagnostics)
    authored = build / "source" / graph.name
    authored.parent.mkdir(parents=True)
    authored.write_text(nw08_editor.emit_graph_text(author.document), encoding="utf-8")

    width, height = document["rendering"]["width"], document["rendering"]["height"]
    preview = nw08_preview.ReferencePreviewBackend()
    reference = [preview.render(doc, 0, width=width, height=height) for doc in (source, author.document)]
    if reference[0].image_sha256 == reference[1].image_sha256:
        raise evidence.EvidenceError("the production geometry edit had no visible effect in Studio preview")

    before_dir = output / "independent-a"
    before_pixels = before_dir / "tick-000000.rgba32f"
    baseline = _capture(_standalone(before_dir), before_dir, (width, height), tick=0, adapter=adapter)
    after_dir = output / "edited"
    result = _export(authored, after_dir, build / "edited", template, dxc, audio)
    after = _smoke(result, after_dir, (width, height), tick=0, adapter=adapter)
    if baseline["status"] != "pass" or not before_pixels.is_file() or after["status"] != "pass":
        raise evidence.EvidenceError("geometry edit standalone smoke failed")
    before = before_pixels.read_bytes()
    edited = (after_dir / after["capture"]).read_bytes()
    if len(before) != width * height * 16 or len(edited) != len(before) or before == edited:
        raise evidence.EvidenceError("standalone capture does not show the geometry edit")
    before_values, edited_values = (struct.unpack(f"<{width*height*4}f", value) for value in (before, edited))
    regions = [nw13_render.luma(image, width, height, width//3, height//3, width, height)
               for image in (before_values, edited_values)]
    return {
        "tick": 0,
        "node": "mirror_panel",
        "preview_images": [frame.image_sha256 for frame in reference],
        "standalone_images": [_sha256(before), _sha256(edited)],
        "reflection_region_luma": regions,
        "standalone_difference": nw13_render.image_difference(before_values, edited_values),
        "invalidated_nodes": list(change.recomputed_nodes),
    }


def _edit_scene_node(graph, operation):
    editor = nw08_editor.GraphEditorSession(deepcopy(graph))
    source = json.loads(editor.document.nodes["film"].params["scene"][0])
    operation(source)
    change = editor.set_parameter("film", "scene", [evidence.canonical_bytes(source).decode("ascii")])
    if not change.valid:
        raise evidence.EvidenceError("film scene Inspector edit failed", change.diagnostics)
    return editor.document


def _move_occluder(scene):
    obj = next(item for item in scene["objects"] if item["id"] == "occluder")
    obj["transform"]["position"][0] -= 1.0
    for track in obj["tracks"]:
        if track["channel"] == "transform.position":
            for key in track["keys"]:
                key["value"][0] -= 1.0


def _recolor_reflected_object(scene):
    material = next(item for item in scene["materials"] if item["id"] == "reflected")
    material["base_color"], material["emission"] = [0.03, 1.0, 0.1, 1.0], [0.01, 0.2, 0.03]


def _unshadowed(graph):
    graph = deepcopy(graph)
    root = graph.nodes["film"]
    settings = json.loads(root.params["production"][0])
    settings["rendering"]["shadows"] = False
    root.params["production"] = [evidence.canonical_bytes(settings).decode("ascii")]
    return graph


def _probe_specs(graph):
    return (
        ("moving_occluder_shadow", 120000, _edit_scene_node(graph, _move_occluder)),
        ("off_camera_material_reflection", 148800, _edit_scene_node(graph, _recolor_reflected_object)),
    )


def _reference_pixels(graph, tick, dimensions):
    return nw13_film.reference_image(graph, graph.nodes["film"], tick, width=dimensions[0], height=dimensions[1])


def _response_channels(name, graph, variant, tick, dimensions):
    original = _reference_pixels(graph, tick, dimensions)
    altered = _reference_pixels(variant, tick, dimensions)
    expected = [b-a for a, b in zip(original, altered, strict=True)]
    # Isolate shadow-only pixels: the authored edit leaves these channels
    # invariant with shadow queries disabled. Material edits cannot change
    # occlusion and this object is behind the camera at the selected tick.
    control = None
    if name == "moving_occluder_shadow":
        control = [b-a for a, b in zip(_reference_pixels(_unshadowed(graph), tick, dimensions),
                                      _reference_pixels(_unshadowed(variant), tick, dimensions), strict=True)]
    channels = [i for i, difference in enumerate(expected)
                if i % 4 != 3 and abs(difference) > 0.025 and (control is None or abs(control[i]) < 0.001)]
    if len(channels) < 3:
        raise evidence.EvidenceError(f"CPU oracle does not show enough of the isolated {name} effect")
    return altered, expected, channels


def _native_controlled_proofs(document, graph, source, output, build, template, dxc, audio, adapter,
                              *, selection=None, revision=None, common_proofs=None):
    dimensions = tuple(document["rendering"][axis] for axis in ("width", "height"))
    tolerance = document["rendering"]["tolerances"]
    old_frames = [
        (row["tick"], row["action_tick"], row["frozen"])
        for row in json.loads((source / "native-frames.json").read_text(encoding="utf-8"))["frames"]
    ]
    frozen_frames = [row for row in old_frames if row[2]]
    if len(frozen_frames) < 2 or frozen_frames[0][1] != frozen_frames[1][1]:
        raise evidence.EvidenceError("native sequence did not cover two frames with the same frozen action")
    first, second = (row[0] for row in frozen_frames[:2])
    _, old_camera = _read_capture(source / f"tick-{first:06d}.rgba32f", dimensions)
    _, moved_camera = _read_capture(source / f"tick-{second:06d}.rgba32f", dimensions)
    camera_response = nw13_render.image_difference(old_camera, moved_camera)
    if camera_response["max_abs_channel_error"] < 0.025:
        raise evidence.EvidenceError("native camera move was not visible while action was frozen")

    results = {"moving_camera_frozen_action": {"ticks": [first, second], "action_tick": frozen_frames[0][1],
                                               "image_delta": camera_response}}
    for name, tick, variant in _probe_specs(graph):
        reference, expected, channels = _response_channels(name, graph, variant, tick, dimensions)
        artifact_dir = output / name
        authored = build / name / "source" / "probe.nwgraph"
        authored.parent.mkdir(parents=True, exist_ok=True)
        authored.write_text(nw08_editor.emit_graph_text(variant), encoding="utf-8")
        result = _export(authored, artifact_dir, build / name, template, dxc, audio,
                         selection=selection, revision=revision)
        sm = _smoke(result, artifact_dir, dimensions, tick=tick, adapter=adapter)
        if sm["status"] != "pass":
            raise evidence.EvidenceError(f"native {name} scene-response probe failed execution")
        _, before = _read_capture(source / f"tick-{tick:06d}.rgba32f", dimensions)
        _, after = _read_capture(artifact_dir / sm["capture"], dimensions)
        oracle = _compare_pixels(reference, after, tolerance)
        if not oracle["image_within_tolerance"]:
            raise evidence.EvidenceError(f"native {name} probe differs from its CPU oracle")
        observed = [after[i] - before[i] for i in channels]
        same_sign = sum(d * expected[i] > 0 for i, d in zip(channels, observed, strict=True))
        if same_sign < math.ceil(0.8 * len(channels)) or max(map(abs, observed)) < 0.02:
            raise evidence.EvidenceError(f"native {name} did not reproduce the analytic region response")
        matched = None
        if common_proofs is not None:
            _, common = _read_capture(common_proofs / name / sm["capture"], dimensions)
            matched = _compare_pixels(common, after, tolerance)
            if not matched["image_within_tolerance"]:
                raise evidence.EvidenceError(f"optional {name} probe differs from its matched common output")
        results[name] = {
            "tick": tick, "capture": f"{name}/{sm['capture']}", "capture_sha256": sm["capture_sha256"],
            "oracle": oracle, "matched_common": matched, "isolated_channels": len(channels),
            "matching_response_channels": same_sign, "observed_max_delta": max(map(abs, observed)),
        }
    return results


def _native_history_proof(document, graph, frames, output, build, template, dxc, audio, adapter,
                          *, selection=None, revision=None):
    spec = nw13_history.read_feedback(graph)
    if spec is None:
        return None
    width, height = dimensions = tuple(document["rendering"][axis] for axis in ("width", "height"))
    editor = nw08_editor.GraphEditorSession(deepcopy(graph))
    result = editor.set_parameter(spec.node_id, "weight", [0])
    if not result.valid:
        raise evidence.EvidenceError("selecting zero feedback weight failed", result.diagnostics)
    modified = build / "source" / "no-feedback.nwgraph"
    modified.parent.mkdir(parents=True, exist_ok=True)
    modified.write_text(nw08_editor.emit_graph_text(editor.document), encoding="utf-8")
    artifact = _export(modified, output, build, template, dxc, audio,
                       selection=selection, revision=revision)
    oracle = nw13_film.FrameOracle(graph, graph.nodes[spec.film_id], width=width, height=height)
    if not oracle.onsets or not oracle.scene.cuts:
        raise evidence.EvidenceError("the History validation fixture must contain a flash and a shot cut")
    flash_end = oracle.onsets[0] + oracle.settings["flash"]["duration_ticks"]
    cut = oracle.scene.cuts[0]
    verdicts = {}
    for role, tick in (("visible_previous_flash", flash_end), ("clean_shot_cut", cut)):
        observation = _smoke(artifact, output, dimensions, tick=tick, adapter=adapter)
        if observation["status"] != "pass":
            raise evidence.EvidenceError(f"zero-weight History control failed at tick {tick}")
        enabled_path = frames / f"tick-{tick:06d}.rgba32f"
        enabled_raw, enabled = _read_capture(enabled_path, dimensions)
        disabled_raw, disabled = _read_capture(output / observation["capture"], dimensions)
        delta = nw13_render.image_difference(enabled, disabled)
        if ((role == "visible_previous_flash" and delta["max_abs_channel_error"] <= 0.02)
                or (role == "clean_shot_cut" and enabled_raw != disabled_raw)):
            raise evidence.EvidenceError(f"native scene History did not satisfy {role} at tick {tick}")
        verdicts[role] = {"tick": tick, "with_feedback_sha256": _sha256(enabled_raw),
                          "zero_weight_sha256": _sha256(disabled_raw), **delta}
    return verdicts


def _query_capabilities(probe, output) -> dict:
    if probe is None:
        return {"query_status": "unavailable"}, None
    record = output / "device-query.json"
    try:
        completed = subprocess.run([str(probe), "--json", str(record)], capture_output=True, check=False, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        record.unlink(missing_ok=True)
        return {"query_status": "unavailable"}, None
    if completed.returncode:
        record.unlink(missing_ok=True)
        return {"query_status": "unavailable", "query_returncode": completed.returncode}, None
    # Keep adapter identifiers and host-specific display data out of the evidence.
    raw = json.loads(record.read_text(encoding="utf-8"))
    record.unlink()
    queried = raw["features"]
    return {
        "query_status": "queried",
        "d3d_feature_level": str(queried["d3d_feature_level"]).removeprefix("D3D_FEATURE_LEVEL_"),
        "highest_shader_model": str(queried["highest_shader_model"]).removeprefix("D3D12_SHADER_MODEL_"),
        "mesh_shader_tier": {"D3D12_MESH_SHADER_TIER_1": 1}.get(queried["mesh_shader_tier"]),
        "raytracing_tier": {10: 1.0, 11: 1.1}.get(queried["raytracing_tier"]),
    }, raw["adapter"]["adapter_luid"]


def _tracked_source(repo_root, selected, revision):
    root = repo_root.resolve()
    path = (root / selected).resolve()
    try:
        relative = path.relative_to(root).as_posix()
        committed = subprocess.run(["git", "show", f"{revision}:{relative}"], cwd=root,
                                   check=True, capture_output=True).stdout
        if path.read_bytes() != committed:
            raise evidence.EvidenceError(f"native source differs from the measured revision: {relative}")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, evidence.EvidenceError):
            raise
        raise evidence.EvidenceError("native evidence inputs must be tracked files in the measured repository") from exc
    return relative, committed


def write_evidence(
    *, repo_root: Path, production_path: Path, output_dir: Path,
    template: Path | None = None, dxc: Path | None = None,
    probe: Path | None = None, expected_revision: str | None = None,
) -> dict[str, Any]:
    document = production.load_production(repo_root, production_path)
    native = template is not None or dxc is not None
    if native and (os.name != "nt" or template is None or dxc is None):
        raise evidence.EvidenceError("native artifact measurement requires Windows, a player template and offline DXC")
    revision = production.measured_revision(repo_root, expected_revision, require_clean=native)
    document["revision"] = revision
    output_dir.mkdir(parents=True, exist_ok=True)
    common = fallback = rebuilds = edit_proof = common_frames = common_proofs = source_identity = None
    common_history = None
    optional_frames, optional_proofs, optional_history = {}, {}, {}
    unreachable = evidence.unreachable_incremental(False)
    capabilities, adapter = _query_capabilities(probe, output_dir) if native else ({}, None)
    requests = document.get("optional_requests", [])
    optional_failures = []
    optional_observations = {}
    optional_rebuilds = {}
    if native:
        with tempfile.TemporaryDirectory(prefix="nw13-clean-build-") as short_build_root:
            build = Path(short_build_root)
            production_name, production_bytes = _tracked_source(repo_root, production_path, revision)
            pinned = json.loads(production_bytes)
            for key in ("graph", "song"):
                if pinned[key] != document[key]:
                    raise evidence.EvidenceError("production source changed during measurement preparation")
            graph_name, graph_bytes = _tracked_source(repo_root, pinned["graph"], revision)
            song_name, song_bytes = _tracked_source(repo_root, pinned["song"], revision)
            # Later compilation and verification read these exact immutable
            # inputs even if a working file is edited while a native run lives.
            graph, song = build / Path(graph_name).name, build / Path(song_name).name
            graph.write_bytes(graph_bytes)
            song.write_bytes(song_bytes)
            document = {**pinned, "revision": revision}
            parsed_graph, _ = production.film_graph(build, {**document, "graph": graph.name})
            audio = production.playback_song(build, {**document, "song": song.name}, graph=parsed_graph)
            source_identity = {
                "production": {"path": production_name, "sha256": _sha256(production_bytes)},
                "graph": {"path": graph_name, "sha256": _sha256(graph_bytes)},
                "song": {"path": song_name, "sha256": _sha256(song_bytes), "nwas_sha256": _sha256(audio)},
            }
            dimensions = tuple(document["rendering"][axis] for axis in ("width", "height"))
            common, rebuilds = _independent_observations(
                graph, output_dir, build / "independent", template, dxc, audio, dimensions, adapter=adapter,
            )
            common_dir = output_dir / "common-fixed-ticks"
            common_frames = _native_frame_set(document, parsed_graph, output_dir / "independent-a",
                                              common_dir, adapter)
            proof_dir = output_dir / "common-scene-probes"
            common_proofs = _native_controlled_proofs(
                document, parsed_graph, common_dir, proof_dir, build / "common-scene-probes",
                template, dxc, audio, adapter,
            )
            common_history = _native_history_proof(
                document, parsed_graph, common_dir, output_dir / "common-history-control",
                build / "common-history-control", template, dxc, audio, adapter,
            )
            unreachable = _unreachable_observations(repo_root, graph, output_dir, build / "unreachable", template, dxc)
            edit_proof = _native_edit_proof(document, graph, output_dir, build / "edit-proof",
                                            template, dxc, audio, adapter)

            for feature in requests:
                try:
                    selection = evidence.select_lowering(feature, capabilities)
                    if selection["profile_id"] == nwp.COMMON_PROFILE_ID:
                        continue
                    if feature != "dxr_inline_ray_query":
                        raise evidence.EvidenceError(f"no admitted film executable for {feature}")
                    optional_dir = output_dir / "inline-ray"
                    observation, builds = _independent_observations(
                        graph, optional_dir, build / "inline-ray", template, dxc, audio, dimensions,
                        selection=selection, adapter=adapter, revision=revision,
                    )
                    optional_frames[feature] = _native_frame_set(
                        document, parsed_graph, optional_dir / "independent-a",
                        optional_dir / "fixed-ticks", adapter, common_frames=common_dir,
                    )
                    observation.update(_summary(optional_frames[feature]["frames"]))
                    optional_proofs[feature] = _native_controlled_proofs(
                        document, parsed_graph, optional_dir / "fixed-ticks", optional_dir / "scene-probes",
                        build / "optional-scene-probes", template, dxc, audio, adapter,
                        selection=selection, revision=revision, common_proofs=proof_dir,
                    )
                    optional_history[feature] = _native_history_proof(
                        document, parsed_graph, optional_dir / "fixed-ticks",
                        optional_dir / "history-control", build / "optional-history-control",
                        template, dxc, audio, adapter, selection=selection, revision=revision,
                    )
                    optional_observations[feature] = observation
                    optional_rebuilds[feature] = builds
                except (evidence.EvidenceError, nw05_export.ExportError, nw03_compiler.CompilerError) as error:
                    if getattr(error, "diagnostics", None):
                        optional_failures.extend(error.diagnostics)
                    else:
                        optional_failures.append({
                            "code": "nw13_optional_build_or_execution_failed", "feature": feature,
                            "severity": "error", "message": str(error),
                        })
            # The fallback is its own clean build and command execution.
            fallback_dir = output_dir / "fallback"
            fallback_result = _export(graph, fallback_dir, build / "fallback", template, dxc, audio)
            fallback = _description(fallback_result)
            fallback["smoke"] = _smoke(fallback_result, fallback_dir, dimensions, adapter=adapter)
            for metric in ("startup_ms", "committed_memory_bytes"):
                fallback[metric] = fallback["smoke"].get(metric)
    bundle = evidence.export_artifacts(
        document, capabilities=capabilities,
        common_observation=common, fallback_observation=fallback, independent_builds=rebuilds is not None,
        requested_features=list(optional_observations), optional_observations=optional_observations,
    )
    bundle = evidence.seal_bundle({
        **bundle, "independent_observations": rebuilds, "unreachable_weight": unreachable, "native_edit_proof": edit_proof,
        "diagnostics": optional_failures, "optional_requests": requests, "optional_rebuilds": optional_rebuilds,
        "measured_sources": source_identity,
        "native_fixed_ticks": {"common": common_frames, "optional": optional_frames},
        "native_scene_probes": {"common": common_proofs, "optional": optional_proofs},
        "native_history": {"common": common_history, "optional": optional_history},
    })
    diagnostics = evidence.validate_bundle(bundle)
    errors = [item for item in optional_failures
              if item.get("severity") == "error" and item.get("code") != "nw13_optional_unsupported"]
    ledger = evidence.matched_arms(
        revision=revision, common=bundle["common"], optional=bundle["optional"][0] if bundle["optional"] else None,
    )
    for name, data in (("artifact-bundle.v1.json", bundle), ("matched-arms.v1.json", ledger)):
        (output_dir / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if diagnostics or errors:
        raise evidence.EvidenceError("artifact evidence validation failed", diagnostics + errors)
    return {"bundle": bundle, "ledger": ledger}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--production", type=Path, default=Path("fixtures/nw13/hotel_corridor.production.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("build/nw13-evidence"))
    parser.add_argument("--template", type=Path)
    parser.add_argument("--dxc", type=Path)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    try:
        result = write_evidence(
            repo_root=root, production_path=root / args.production, output_dir=root / args.output_dir,
            template=root / args.template if args.template else None,
            dxc=root / args.dxc if args.dxc else None, probe=root / args.probe if args.probe else None,
            expected_revision=args.revision,
        )
    except (evidence.EvidenceError, nw05_export.ExportError) as error:
        parser.exit(1, f"{error}: {error.diagnostics}\n")
    print(json.dumps({key: result["bundle"][key] for key in ("revision", "independent_builds", "diagnostics")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
