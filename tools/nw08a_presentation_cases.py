#!/usr/bin/env python3
"""Generate and validate the NW-08A presentation-geometry cases.

The tool uses only the existing graph/package/export path.  It writes one
`.nwgraph` per presentation shape, exports or builds the package, decodes the
bytes it produced, and reports the exact `nw05.present.width` /
`nw05.present.height` initialization parameters.

Without `--dxc` and `--player-template` the package is built from the shared
test-payload path so the geometry contract can be checked on a GPU-less host.
With both supplied it runs the real release export and additionally retains
the self-contained player for that case.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import nw03_compiler as nw03  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw06_export  # noqa: E402
import nw08_preview as preview  # noqa: E402
import nwp_package as nwp  # noqa: E402

DEFAULT_GRAPH = REPO_ROOT / "fixtures" / "nw07" / "production_loop.nwgraph"
CASES: tuple[tuple[str, int, int], ...] = (
    ("landscape", 256, 144),
    ("portrait", 144, 256),
    ("canonical_hd", 1920, 1080),
    ("square", 256, 256),
)


def _present_parameters(package: nwp.NwpPackage) -> dict[str, int]:
    values: dict[str, int] = {}
    for parameter in package.parameters:
        if parameter.name not in {"nw05.present.width", "nw05.present.height"}:
            continue
        if (
            parameter.type != "uint"
            or parameter.phase != "initialization"
            or parameter.component_count != 1
            or len(parameter.value) != 4
            or parameter.name in values
        ):
            raise ValueError(
                f"invalid or duplicate presentation parameter {parameter.name!r}"
            )
        values[parameter.name] = struct.unpack("<I", parameter.value)[0]
    if set(values) != {"nw05.present.width", "nw05.present.height"}:
        raise ValueError("package does not carry both presentation parameters")
    return values


def _write_case_graph(
    source: Path, destination: Path, width: int, height: int
) -> nw03.GraphDocument:
    document = nw03.load_graph(source)
    document.settings["nw07_output_width"] = str(width)
    document.settings["nw07_output_height"] = str(height)
    destination.write_text(
        preview.emit_graph_text(document), encoding="utf-8", newline="\n"
    )
    return document


def _check_case(
    name: str,
    width: int,
    height: int,
    *,
    graph: Path,
    work: Path,
    dxc: str | None,
    player_template: str | None,
) -> dict[str, Any]:
    case_dir = work / name
    case_dir.mkdir(parents=True, exist_ok=True)
    case_graph = case_dir / f"{name}.nwgraph"
    document = _write_case_graph(graph, case_graph, width, height)
    package_path = case_dir / f"{name}.nwp"
    executable_path: Path | None = None
    if dxc and player_template:
        result = nw05_export.export_graph(
            case_graph,
            case_dir / "export",
            settings=nw05_export.ExportSettings(
                player_template=player_template,
                present_sync_interval=0,
            ),
            dxc_executable=dxc,
        )
        artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
        package_bytes = artifact.package_bytes
        if artifact.executable_bytes is not None:
            executable_path = case_dir / f"{name}.exe"
            executable_path.write_bytes(artifact.executable_bytes)
        export_mode = "release"
    else:
        optimized = optimizer.optimize_graph(document)
        package_bytes, _package, _report = nw06_export.compile_profile_package(
            optimized,
            document,
            package_path,
            dxc_executable=None,
            optimization="O2",
            allow_unpinned_dxc=False,
            allow_test_payloads=True,
            width=width,
            height=height,
        )
        export_mode = "test_payload"
    package_path.write_bytes(package_bytes)
    package = nwp.decode_package(package_bytes)
    present = _present_parameters(package)
    expected = {
        "nw05.present.width": width,
        "nw05.present.height": height,
    }
    if present != expected:
        raise ValueError(
            f"{name}: presentation parameters {present} != {expected}"
        )
    preview_dimensions = preview._package_capture_dimensions(
        package, fallback_width=width, fallback_height=height
    )
    if preview_dimensions != (width, height):
        raise ValueError(
            f"{name}: preview geometry {preview_dimensions} != {(width, height)}"
        )
    present_resource_id = next(
        instruction.operands[0].value
        for instruction in package.frame
        if instruction.opcode == "frame.present"
    )
    present_resource = next(
        resource
        for resource in package.resources
        if resource.resource_id == present_resource_id
    )
    if present_resource.size_bytes < width * height * 16:
        raise ValueError(
            f"{name}: presentation buffer is too small for {width}x{height}"
        )
    return {
        "name": name,
        "width": width,
        "height": height,
        "export_mode": export_mode,
        "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "package_bytes": len(package_bytes),
        "present_width": present["nw05.present.width"],
        "present_height": present["nw05.present.height"],
        "preview_dimensions": list(preview_dimensions),
        "present_resource_bytes": present_resource.size_bytes,
        "executable": str(executable_path) if executable_path else None,
        "status": "passed",
    }


def run(
    *,
    graph: Path = DEFAULT_GRAPH,
    work: Path,
    dxc: str | None = None,
    player_template: str | None = None,
) -> dict[str, Any]:
    if bool(dxc) != bool(player_template):
        raise ValueError("--dxc and --player-template must be supplied together")
    work.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    for name, width, height in CASES:
        cases.append(
            _check_case(
                name,
                width,
                height,
                graph=graph,
                work=work,
                dxc=dxc,
                player_template=player_template,
            )
        )
    return {
        "format": "nw08a-presentation-geometry-validation",
        "version": 1,
        "graph": str(graph),
        "work_directory": str(work),
        "export_mode": "release" if dxc else "test_payload",
        "hardware_evidence": False,
        "real_player_validation": "unexecuted",
        "cases": cases,
        "success": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", default=str(DEFAULT_GRAPH))
    parser.add_argument("--work", default=str(REPO_ROOT / "build" / "nw08a-cases"))
    parser.add_argument("--dxc", default=None)
    parser.add_argument("--player-template", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)
    report = run(
        graph=Path(args.graph),
        work=Path(args.work),
        dxc=args.dxc,
        player_template=args.player_template,
    )
    report_path = Path(args.report) if args.report else Path(args.work) / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
