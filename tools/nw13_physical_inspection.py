"""Build the native NW-13 inspection profile without hosted render or oracle claims."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

import nw03_compiler
import nw13_artifact_measure as measurement
import nw13_evidence as film
import nw13_film
import nw13_physical_film as handoff
import nw13_production as production


def build(repo: Path, production_path: Path, output: Path, template: Path, dxc: Path, revision: str, *,
          windows: bool = os.name == "nt", export=measurement._export, describe=measurement._description) -> None:
    if not windows:
        raise handoff.HandoffError("the inspection build requires the exact native Windows toolchain")
    repo, output, template, dxc = (path.resolve() for path in (repo, output, template, dxc))
    if not template.is_file() or not dxc.is_file():
        raise handoff.HandoffError("the inspection build lacks its configured Windows template or DXC")
    production.measured_revision(repo, revision, require_clean=True)
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    source_name, source_bytes = measurement._tracked_source(repo, production_path, revision)
    document = handoff.json_object(source_bytes)
    rendering = document["rendering"]
    if rendering.get("work_profile") != handoff.INSPECTION_PROFILE:
        raise handoff.HandoffError("inspection must name the distinct admitted graph profile")
    graph_name, graph_bytes = measurement._tracked_source(repo, document["graph"], revision)
    song_name, song_bytes = measurement._tracked_source(repo, document["song"], revision)
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="nw13-inspect-build-") as directory:
        root = Path(directory)
        graph, song = root / "inspection.nwgraph", root / "song.json"
        graph.write_bytes(graph_bytes)
        song.write_bytes(song_bytes)
        graph_document, _ = production.film_graph(root, {**document, "graph": graph.name})
        audio = production.playback_song(root, {**document, "song": song.name}, graph=graph_document)
        shader = next(node for node in nw03_compiler._reachable_shader_roots(graph_document)
                      if node.operator_id == nw13_film.OPERATOR)
        _, _, _, scene, settings, _ = nw13_film.read_film(graph_document, shader)
        timeline, onsets = nw13_film.film_timing(scene, settings)
        ticks = sorted({*document["fixed_ticks"], *(event["presentation_tick"] for event in timeline.events
                       if event["kind"] in {"cut", "freeze", "reverse", "resume"}), *onsets})
        # These are compiler/runtime target requirements, not a hosted adapter
        # observation. The offline build does not open D3D12. The physical
        # launcher independently probes the selected device before dispatch.
        required_dxr = film.select_lowering("dxr_inline_ray_query",
                    {"d3d_feature_level": "12_0", "highest_shader_model": "6_5", "raytracing_tier": 1.1})
        dxr = {**required_dxr, "capability_provenance": "compile_target_only_not_observed"}
        arms = {}
        for name, path in handoff.ARMS.items():
            selection = dxr if name == "dxr" else None
            result = export(graph, output / path, root / name, template, dxc, audio, selection=selection, revision=revision)
            arms[name] = {**describe(result), "measurement_status": "built_only", "revision": revision,
                          "budgets": {"max_memory_bytes": 64 * 1024 * 1024, **(selection or {}).get("budgets", {})}}
    sources = {name: {"path": path, "sha256": handoff.digest(data)}
               for name, path, data in (("production", source_name, source_bytes),
                        ("graph", graph_name, graph_bytes), ("song", song_name, song_bytes))}
    sources["song"]["nwas_sha256"] = handoff.digest(audio)
    handoff.write_json(output / handoff.INSPECTION_INPUT, film.seal_bundle({
        "format": "nw13.inspection-build", "revision": revision, "tree": tree,
        "hosted_execution": "not_run", "image_reference": handoff.PEER, "measured_sources": sources,
        "render_size": [rendering["width"], rendering["height"]], "ticks": ticks, "arms": arms,
    }))
