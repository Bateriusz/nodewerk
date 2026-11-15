#!/usr/bin/env python3
"""Branch-only export/validation helper for NW-07-test-demo.

This helper deliberately composes the existing NW-03/NW-05/NW-06/NW-07
pipeline.  It does not add a new runtime path, operator, package format, or
player semantics.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402

DEMO_GRAPH = REPO_ROOT / "fixtures" / "nw07-test-demo" / "orbital_vortex.nwgraph"
PROFILE_ID = "nw.common.d3d12.fl12_0.sm6_0.v1"
REQUIREMENTS_DIGEST = (
    "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a"
)
LOOP_TICKS = 1_152_000
LOOP_SECONDS = 24
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
INSTANCE_COUNT = 4096

ALLOWED_OPERATORS = frozenset(
    {
        "nw.texture.noise2d",
        "nw.residency.generate_texture",
        "nw.mesh.torus",
        "nw.residency.generate_mesh",
        "nw.scene.instances",
        "nw.residency.generate_instances",
        "nw.material.standard",
        "nw.camera.curve",
        "nw.light.directional",
        "nw.animation.twist",
        "nw.render.instanced",
        "nw.post.bloom",
        "nw.post.tonemap",
        "nw.render.present",
    }
)


class DemoContractError(RuntimeError):
    """Raised when the branch-only demo leaves the NW-07 capability envelope."""


def _scalar(node: nw03.GraphNode, name: str) -> str:
    values = node.params.get(name, [])
    if len(values) != 1:
        raise DemoContractError(
            f"node '{node.node_id}' parameter '{name}' must have one value"
        )
    return values[0]


def _float(node: nw03.GraphNode, name: str) -> float:
    try:
        value = float(_scalar(node, name))
    except ValueError as exc:
        raise DemoContractError(
            f"node '{node.node_id}' parameter '{name}' is not numeric"
        ) from exc
    if not math.isfinite(value):
        raise DemoContractError(
            f"node '{node.node_id}' parameter '{name}' is not finite"
        )
    return value


def _camera_keys(node: nw03.GraphNode) -> tuple[tuple[int, float], ...]:
    raw = _scalar(node, "keys").strip('"')
    rows: list[tuple[int, float]] = []
    for entry in raw.split(";"):
        try:
            tick_text, value_text = entry.split(":", 1)
            tick = int(tick_text)
            value = float(value_text)
        except (TypeError, ValueError) as exc:
            raise DemoContractError(f"malformed camera key '{entry}'") from exc
        rows.append((tick, value))
    return tuple(rows)


def validate_demo_graph(path: Path = DEMO_GRAPH) -> dict[str, Any]:
    document = nw03.load_graph(path)
    if document.profile_id != PROFILE_ID:
        raise DemoContractError("demo changed the common capability profile")
    if document.requirements_digest != REQUIREMENTS_DIGEST:
        raise DemoContractError("demo changed the common requirements digest")

    expected_settings = {
        "nw06_ticks_per_second": "48000",
        "nw06_output_width": str(OUTPUT_WIDTH),
        "nw06_output_height": str(OUTPUT_HEIGHT),
        "nw06_frame_step_numerator": "800",
        "nw06_frame_step_denominator": "1",
        "nw07_output_width": str(OUTPUT_WIDTH),
        "nw07_output_height": str(OUTPUT_HEIGHT),
        "nw07_loop_ticks": str(LOOP_TICKS),
        "nw07_loop_seconds": str(LOOP_SECONDS),
    }
    for name, expected in expected_settings.items():
        observed = str(document.settings.get(name, "")).strip()
        if observed != expected:
            raise DemoContractError(
                f"setting '{name}' is '{observed}', expected '{expected}'"
            )

    optimized = optimizer.optimize_graph(document)
    used = {node.operator_id for node in optimized.nodes}
    unexpected = sorted(used - ALLOWED_OPERATORS)
    missing = sorted(ALLOWED_OPERATORS - used)
    if unexpected:
        raise DemoContractError(f"demo uses non-NW-07 operators: {unexpected}")
    if missing:
        raise DemoContractError(f"demo is missing required showcase operators: {missing}")

    node_map = optimized.node_map()
    for node_id in (
        "texture",
        "texture_gpu",
        "torus",
        "mesh_gpu",
        "instances",
        "scene_gpu",
    ):
        if node_id not in optimized.partitions.get("initialization", ()):
            raise DemoContractError(f"'{node_id}' must remain initialization-only")

    for node_id in ("material", "camera", "sun", "twist", "render", "bloom", "tonemap", "screen"):
        if node_id not in optimized.partitions.get("frame", ()):
            raise DemoContractError(f"'{node_id}' must remain frame-frequency")

    if int(_float(node_map["instances"], "count")) != INSTANCE_COUNT:
        raise DemoContractError("demo must use 4096 ring instances")
    if _float(node_map["torus"], "minor_radius") >= _float(
        node_map["torus"], "major_radius"
    ):
        raise DemoContractError("demo torus must remain a valid thin ring")
    if _scalar(node_map["tonemap"], "operator") != "aces":
        raise DemoContractError("demo must retain ACES tonemapping")
    if abs(_float(node_map["bloom"], "resolution_scale") - 0.5) > 1e-9:
        raise DemoContractError("demo must use the existing half-resolution bloom path")
    if abs(_float(node_map["camera"], "radius_min") - 5.2) > 1e-9:
        raise DemoContractError("demo camera radius changed unexpectedly")
    if abs(_float(node_map["camera"], "radius_max") - 5.2) > 1e-9:
        raise DemoContractError("demo camera radius changed unexpectedly")

    keys = _camera_keys(node_map["camera"])
    if len(keys) != 4:
        raise DemoContractError("existing NW-07 camera path supports exactly four demo keys")
    if keys[0][0] != 0 or keys[-1][0] != LOOP_TICKS:
        raise DemoContractError("camera curve does not span the complete loop")
    if abs(keys[0][1]) > 1e-12 or abs(keys[-1][1] - math.tau) > 1e-12:
        raise DemoContractError("camera loop does not close at one full revolution")
    expected_key_ticks = (0, LOOP_TICKS // 3, (2 * LOOP_TICKS) // 3, LOOP_TICKS)
    if tuple(tick for tick, _ in keys) != expected_key_ticks:
        raise DemoContractError("camera keys are not evenly spaced")
    expected_values = (0.0, math.tau / 3.0, 2.0 * math.tau / 3.0, math.tau)
    for (_, value), expected in zip(keys, expected_values):
        if abs(value - expected) > 1e-12:
            raise DemoContractError("camera curve is not constant-speed around the loop")

    render_dimensions = nw05_export._graph_render_dimensions(document, optimized)
    if render_dimensions != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
        raise DemoContractError(
            f"demo render size is {render_dimensions}, expected 1920x1080"
        )

    return {
        "graph": str(path.relative_to(REPO_ROOT)),
        "profile_id": document.profile_id,
        "requirements_digest": document.requirements_digest,
        "operators": sorted(used),
        "instance_count": INSTANCE_COUNT,
        "loop_ticks": LOOP_TICKS,
        "loop_seconds": LOOP_SECONDS,
        "render_width": render_dimensions[0],
        "render_height": render_dimensions[1],
        "camera_keys": [[tick, value] for tick, value in keys],
        "branch_only": True,
    }


def export_demo(
    output: Path,
    *,
    player_template: Path | None,
    dxc: Path | None,
    build_root: Path | None,
    report_path: Path | None,
    contracts_only: bool,
) -> nw05_export.ExportResult:
    validate_demo_graph()

    if contracts_only:
        settings = nw05_export.ExportSettings(
            byte_ceiling=64 * 1024 * 1024,
            memory_budget=512 * 1024 * 1024,
            frames_in_flight=2,
            present_sync_interval=0,
            include_optional_artifacts=False,
            allow_optional_profiles=False,
        )
        return nw05_export.export_graph(
            DEMO_GRAPH,
            output,
            settings=settings,
            report_path=report_path,
            allow_test_payloads=True,
        )

    if player_template is None or dxc is None:
        raise DemoContractError(
            "release export requires both --player-template and --dxc"
        )

    settings = nw05_export.ExportSettings(
        byte_ceiling=64 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        frames_in_flight=2,
        player_template=str(player_template),
        specialized_build_root=str(build_root) if build_root is not None else "",
        present_sync_interval=0,
        frozen_embedded_specialization=True,
        include_optional_artifacts=False,
        allow_optional_profiles=False,
    )
    return nw05_export.export_graph(
        DEMO_GRAPH,
        output,
        settings=settings,
        report_path=report_path,
        dxc_executable=str(dxc),
        optimization="O2",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "build" / "nw07-test-demo" / "artifact",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--player-template", type=Path)
    parser.add_argument("--dxc", type=Path)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument(
        "--contracts-only",
        action="store_true",
        help="use structural test shader payloads; no Windows player/DXC required",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the graph contract without exporting a package",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.validate_only:
        print(json.dumps(validate_demo_graph(), indent=2, sort_keys=True))
        return 0

    result = export_demo(
        args.output,
        player_template=args.player_template,
        dxc=args.dxc,
        build_root=args.build_root,
        report_path=args.report,
        contracts_only=args.contracts_only,
    )
    common = result.artifacts[PROFILE_ID]
    print(
        json.dumps(
            {
                "success": True,
                "branch_only": True,
                "output_directory": result.output_directory,
                "package_size_bytes": len(common.package_bytes),
                "executable_name": common.executable_name,
                "executable_size_bytes": (
                    len(common.executable_bytes)
                    if common.executable_bytes is not None
                    else 0
                ),
                "peak_resource_bytes": common.physical_realization.peak_bytes,
                "loop_ticks": LOOP_TICKS,
                "loop_seconds": LOOP_SECONDS,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
