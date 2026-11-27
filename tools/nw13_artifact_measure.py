#!/usr/bin/env python3
"""Build and verify NW-13 artifacts from a clean measured revision."""
from __future__ import annotations

import argparse
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
import nw13_render
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
    distribution = artifact.size_report["distribution"]
    lowering = artifact.manifest.get("film_lowering", {})
    return {
        "distribution_bytes": int(distribution["total_distributed_bytes"]),
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


def _smoke(result, directory, *, tick=148800, adapter=None):
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    frame = directory / f"tick-{tick:06d}.rgba32f"
    environment = {**os.environ, "NODEWERK_ADAPTER_LUID": adapter} if adapter else None
    observation = evidence.smoke_observation(
        str(directory / artifact.executable_name),
        ["--tick", str(tick), "--headless", "--allow-software-adapter", "--film-capture", str(frame)],
        environment=environment,
    )
    if observation["status"] == "pass":
        if not frame.is_file() or not frame.stat().st_size or frame.stat().st_size % 16:
            observation.update(status="error", error="standalone ran without a complete RGBA32F readback")
        else:
            observation["capture"] = frame.name
            observation["capture_sha256"] = _sha256(frame.read_bytes())
    return observation


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
            byte_ceiling=evidence.CANONICAL_DISTRIBUTION_CEILING_BYTES,
            memory_budget=64 * 1024 * 1024, player_template=str(template),
            specialized_build_root=str(build / "native"),
            film_inline_ray_payload_dir=str(payload) if selection else "",
        ),
        dxc_executable=str(dxc), cache_dir=str(build / "dxc-cache"),
        audio_song_payload=audio_payload, **extra,
    )


def _independent_observations(graph, output, build, template, dxc, audio_payload, *,
                              selection=None, adapter=None, revision=None):
    descriptions = []
    for arm in ("a", "b"):
        directory = output / f"independent-{arm}"
        result = _export(graph, directory, build / arm, template, dxc, audio_payload,
                         selection=selection, revision=revision)
        description = _description(result)
        description["smoke"] = _smoke(result, directory, adapter=adapter)
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


def _compare_captures(common, optional, tolerance):
    first, second = common.read_bytes(), optional.read_bytes()
    if not first or len(first) != len(second):
        raise evidence.EvidenceError("matched captures must have identical nonzero dimensions")
    number = len(first) // 4
    pixels = struct.unpack(f"<{number}f", first), struct.unpack(f"<{number}f", second)
    if any(not math.isfinite(channel) for image in pixels for channel in image):
        raise evidence.EvidenceError("matched captures contain a nonfinite pixel channel")
    differences = [abs(a - b) for a, b in zip(*pixels, strict=True)]
    maximum = max(differences)
    rmse = math.sqrt(sum(value * value for value in differences) / len(differences))
    return {
        "image_max_abs_error": maximum, "image_rmse": rmse,
        "image_within_tolerance": maximum <= tolerance["max_abs_channel_error"] and rmse <= tolerance["rmse"],
    }


def _unreachable_observations(repo_root, film_graph, output, build, template, dxc):
    unrelated = repo_root / "fixtures" / "nw05" / "procedural_image.nwgraph"
    original = nw03_compiler.load_graph(unrelated)
    film = nw03_compiler.load_graph(film_graph)
    for identifier, node in film.nodes.items():
        if node.operator_id.startswith("nw.render."):
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
    exe = before_dir / (nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID) + ".exe")
    before_pixels = before_dir / "tick-000000.rgba32f"
    baseline = evidence.smoke_observation(
        str(exe), ["--headless", "--allow-software-adapter", "--tick", "0", "--film-capture", str(before_pixels)],
        environment={**os.environ, "NODEWERK_ADAPTER_LUID": adapter} if adapter else None,
    )
    after_dir = output / "edited"
    result = _export(authored, after_dir, build / "edited", template, dxc, audio)
    after = _smoke(result, after_dir, tick=0, adapter=adapter)
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


def _query_capabilities(probe, output) -> dict:
    if probe is None:
        return {}, None
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
    common = fallback = rebuilds = edit_proof = None
    unreachable = evidence.unreachable_incremental(False)
    capabilities, adapter = _query_capabilities(probe, output_dir) if native else ({}, None)
    requests = document.get("optional_requests", [])
    optional_failures = []
    optional_observations = {}
    optional_rebuilds = {}
    if native:
        with tempfile.TemporaryDirectory(prefix="nw13-clean-build-") as short_build_root:
            build = Path(short_build_root)
            graph = build / Path(document["graph"]).name
            production.import_graph(repo_root, document, graph)
            audio = production.playback_song(repo_root, document)
            common, rebuilds = _independent_observations(
                graph, output_dir, build / "independent", template, dxc, audio, adapter=adapter,
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
                        graph, optional_dir, build / "inline-ray", template, dxc, audio,
                        selection=selection, adapter=adapter, revision=revision,
                    )
                    comparison = _compare_captures(
                        output_dir / "independent-a" / common["smoke"]["capture"],
                        optional_dir / "independent-a" / observation["smoke"]["capture"],
                        document["rendering"]["tolerances"],
                    )
                    observation.update(comparison)
                    if not comparison["image_within_tolerance"]:
                        optional_failures.append({
                            "code": "nw13_optional_image_mismatch", "feature": feature,
                            "severity": "error", **comparison,
                        })
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
            fallback["smoke"] = _smoke(fallback_result, fallback_dir, adapter=adapter)
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
    except evidence.EvidenceError as error:
        parser.exit(1, f"{error}: {error.diagnostics}\n")
    print(json.dumps({key: result["bundle"][key] for key in ("revision", "independent_builds", "diagnostics")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
