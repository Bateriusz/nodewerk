#!/usr/bin/env python3
"""Lower fixtures/nw07/production_loop.nwgraph into NW-11 renderer inputs.

The NW-11 renderer must consume the graph-derived production scene rather
than hard-coding torus radii or segment counts.  This tool reads the frozen
graph, verifies its canonical digest, and emits a generated C++ header plus a
JSON descriptor used by the offline shader compilation and validation
bundles.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import nw11_contract as contract  # noqa: E402

HEADER_FORMAT = "nw11-graph-scene"


class GraphSceneError(ValueError):
    """Raised when the frozen graph cannot be lowered unambiguously."""


def _tokens(line: str) -> list[str]:
    return re.findall(r'"[^"]*"|\S+', line)


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] == '"':
        return token[1:-1]
    return token


class Node:
    def __init__(self, name: str, operator: str) -> None:
        self.name = name
        self.operator = operator
        self.params: dict[str, list[str]] = {}

    def param(self, key: str) -> list[str]:
        try:
            return self.params[key]
        except KeyError as exc:
            raise GraphSceneError(
                f"node '{self.name}' ({self.operator}) is missing param '{key}'"
            ) from exc

    def integer(self, key: str) -> int:
        return int(self.param(key)[0])

    def number(self, key: str) -> float:
        return float(self.param(key)[0])

    def floats(self, key: str) -> list[float]:
        return [float(value) for value in self.param(key)]


def parse_graph(text: str) -> tuple[dict[str, str], dict[str, Node]]:
    settings: dict[str, str] = {}
    nodes: dict[str, Node] = {}
    current: Node | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = _tokens(line)
        head = tokens[0]
        if head == "nwgraph":
            continue
        if head == "profile":
            settings["profile"] = tokens[1]
            settings["requirements_digest"] = tokens[2]
            continue
        if head == "setting":
            settings[tokens[1]] = tokens[2]
            continue
        if head == "node":
            name = tokens[1]
            operator = tokens[2]
            current = Node(name, operator)
            nodes[name] = current
            continue
        if head == "param":
            if current is None:
                raise GraphSceneError("param outside a node")
            current.params[tokens[1]] = [_unquote(value) for value in tokens[2:]]
            continue
        if head == "input" or head == "layout":
            continue
        raise GraphSceneError(f"unrecognised graph statement: {line}")
    return settings, nodes


def _node_by_operator(nodes: dict[str, Node], operator: str) -> Node:
    matches = [node for node in nodes.values() if node.operator == operator]
    if len(matches) != 1:
        raise GraphSceneError(
            f"graph must contain exactly one '{operator}' node, found {len(matches)}"
        )
    return matches[0]


def lower(graph_path: Path, *, repo_root: Path) -> dict[str, Any]:
    data = graph_path.read_bytes()
    digest = contract.sha256_bytes(data.replace(b"\r\n", b"\n"))
    if digest != contract.BASE_GRAPH_SHA256:
        raise GraphSceneError("the graph bytes do not match the frozen NW-07 digest")
    settings, nodes = parse_graph(data.decode("utf-8"))
    if settings.get("profile") != contract.COMMON_PROFILE_ID:
        raise GraphSceneError("the graph does not target the common profile")
    if settings.get("requirements_digest") != contract.COMMON_REQUIREMENTS_DIGEST:
        raise GraphSceneError("the graph requirements digest is not the frozen common digest")

    texture = _node_by_operator(nodes, "nw.texture.noise2d@1.0.0")
    torus = _node_by_operator(nodes, "nw.mesh.torus@1.0.0")
    instances = _node_by_operator(nodes, "nw.scene.instances@1.0.0")
    material = _node_by_operator(nodes, "nw.material.standard@1.0.0")
    camera = _node_by_operator(nodes, "nw.camera.curve@1.0.0")
    sun = _node_by_operator(nodes, "nw.light.directional@1.0.0")
    twist = _node_by_operator(nodes, "nw.animation.twist@1.0.0")

    keys = []
    for key in camera.param("keys")[0].split(";"):
        tick, value = key.split(":")
        keys.append((int(tick), float(value)))
    if len(keys) != 4:
        raise GraphSceneError("the NW-07 camera curve must have four absolute-tick keys")

    base_color = material.floats("base_color")
    direction = sun.floats("direction")
    color = sun.floats("color")
    return {
        "format": HEADER_FORMAT,
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "graph_path": contract.BASE_GRAPH,
        "graph_sha256": digest,
        "profile_id": contract.COMMON_PROFILE_ID,
        "requirements_digest": contract.COMMON_REQUIREMENTS_DIGEST,
        "output_width": int(settings["nw06_output_width"]),
        "output_height": int(settings["nw06_output_height"]),
        "ticks_per_second": int(settings["nw06_ticks_per_second"]),
        "loop_ticks": int(settings["nw07_loop_ticks"]),
        "texture": {
            "width": texture.integer("width"),
            "height": texture.integer("height"),
            "seed": texture.integer("seed"),
            "octaves": texture.integer("octaves"),
            "scale": texture.number("scale"),
            "lacunarity": texture.number("lacunarity"),
            "gain": texture.number("gain"),
        },
        "torus": {
            "major_segments": torus.integer("major_segments"),
            "minor_segments": torus.integer("minor_segments"),
            "major_radius": torus.number("major_radius"),
            "minor_radius": torus.number("minor_radius"),
        },
        "instances": {
            "count": instances.integer("count"),
            "seed": instances.integer("seed"),
            "placement_radius": instances.number("placement_radius"),
            "scale_min": instances.number("scale_min"),
            "scale_max": instances.number("scale_max"),
        },
        "material": {
            "base_color": base_color,
            "roughness": material.number("roughness"),
            "metallic": material.number("metallic"),
            "specular": material.number("specular"),
            "texture_index": material.integer("texture_index"),
        },
        "camera": {
            "keys": keys,
            "interpolation": camera.param("interpolation")[0],
            "fov_degrees": camera.number("fov_degrees"),
            "aspect": camera.number("aspect"),
            "near": camera.number("near"),
            "far": camera.number("far"),
            "radius_min": camera.number("radius_min"),
            "radius_max": camera.number("radius_max"),
            "height": camera.number("height"),
        },
        "light": {
            "direction": direction,
            "color": color,
        },
        "twist": {
            "max_twist_degrees": twist.number("max_twist_degrees"),
            "twist_radians": twist.number("max_twist_degrees")
            * 3.14159265358979323846
            / 180.0,
        },
    }


def emit_header(document: dict[str, Any]) -> str:
    torus = document["torus"]
    texture = document["texture"]
    instances = document["instances"]
    material = document["material"]
    camera = document["camera"]
    light = document["light"]
    keys = camera["keys"]
    while len(keys) < 4:
        keys.append(keys[-1])
    base = material["base_color"] + [0.0] * (4 - len(material["base_color"]))
    direction = light["direction"] + [0.0] * (3 - len(light["direction"]))
    color = light["color"] + [0.0] * (3 - len(light["color"]))
    lines = [
        "// Generated by tools/nw11_graph_scene.py from the frozen NW-07 graph.",
        "// Do not edit by hand.",
        "#pragma once",
        "#include <cstdint>",
        "namespace nw11::scene {",
        f'inline constexpr char kGraphPath[] = "{document["graph_path"]}";',
        f'inline constexpr char kGraphSha256[] = "{document["graph_sha256"]}";',
        f"inline constexpr std::uint32_t kOutputWidth = {document['output_width']}u;",
        f"inline constexpr std::uint32_t kOutputHeight = {document['output_height']}u;",
        f"inline constexpr std::uint32_t kTicksPerSecond = {document['ticks_per_second']}u;",
        f"inline constexpr std::uint32_t kLoopTicks = {document['loop_ticks']}u;",
        f"inline constexpr std::uint32_t kTextureWidth = {texture['width']}u;",
        f"inline constexpr std::uint32_t kTextureHeight = {texture['height']}u;",
        f"inline constexpr std::uint32_t kTextureSeed = {texture['seed']}u;",
        f"inline constexpr std::uint32_t kTextureOctaves = {texture['octaves']}u;",
        f"inline constexpr float kTextureScale = {texture['scale']:.9f}f;",
        f"inline constexpr float kTextureLacunarity = {texture['lacunarity']:.9f}f;",
        f"inline constexpr float kTextureGain = {texture['gain']:.9f}f;",
        f"inline constexpr std::uint32_t kMajorSegments = {torus['major_segments']}u;",
        f"inline constexpr std::uint32_t kMinorSegments = {torus['minor_segments']}u;",
        f"inline constexpr float kMajorRadius = {torus['major_radius']:.9f}f;",
        f"inline constexpr float kMinorRadius = {torus['minor_radius']:.9f}f;",
        f"inline constexpr std::uint32_t kVertexCount = {torus['major_segments'] * torus['minor_segments']}u;",
        f"inline constexpr std::uint32_t kIndexCount = {torus['major_segments'] * torus['minor_segments'] * 6}u;",
        f"inline constexpr std::uint32_t kInstanceCount = {instances['count']}u;",
        f"inline constexpr std::uint32_t kInstanceSeed = {instances['seed']}u;",
        f"inline constexpr float kPlacementRadius = {instances['placement_radius']:.9f}f;",
        f"inline constexpr float kScaleMin = {instances['scale_min']:.9f}f;",
        f"inline constexpr float kScaleMax = {instances['scale_max']:.9f}f;",
        f"inline constexpr float kMaterialBaseColor[4] = {{{base[0]:.9f}f, {base[1]:.9f}f, {base[2]:.9f}f, {base[3]:.9f}f}};",
        f"inline constexpr float kMaterialRoughness = {material['roughness']:.9f}f;",
        f"inline constexpr float kMaterialMetallic = {material['metallic']:.9f}f;",
        f"inline constexpr float kMaterialSpecular = {material['specular']:.9f}f;",
        f"inline constexpr std::uint32_t kMaterialTextureIndex = {material['texture_index']}u;",
        f"inline constexpr std::uint32_t kCameraTick0 = {keys[0][0]}u;",
        f"inline constexpr std::uint32_t kCameraTick1 = {keys[1][0]}u;",
        f"inline constexpr std::uint32_t kCameraTick2 = {keys[2][0]}u;",
        f"inline constexpr std::uint32_t kCameraTick3 = {keys[3][0]}u;",
        f"inline constexpr float kCameraValue0 = {keys[0][1]:.9f}f;",
        f"inline constexpr float kCameraValue1 = {keys[1][1]:.9f}f;",
        f"inline constexpr float kCameraValue2 = {keys[2][1]:.9f}f;",
        f"inline constexpr float kCameraValue3 = {keys[3][1]:.9f}f;",
        f"inline constexpr float kCameraFovDegrees = {camera['fov_degrees']:.9f}f;",
        f"inline constexpr float kCameraAspect = {camera['aspect']:.9f}f;",
        f"inline constexpr float kCameraNear = {camera['near']:.9f}f;",
        f"inline constexpr float kCameraFar = {camera['far']:.9f}f;",
        f"inline constexpr float kCameraRadiusMin = {camera['radius_min']:.9f}f;",
        f"inline constexpr float kCameraRadiusMax = {camera['radius_max']:.9f}f;",
        f"inline constexpr float kCameraHeight = {camera['height']:.9f}f;",
        f"inline constexpr std::uint32_t kCameraInterpolation = {1 if camera['interpolation'] == 'linear' else 0}u;",
        f"inline constexpr float kLightDirection[3] = {{{direction[0]:.9f}f, {direction[1]:.9f}f, {direction[2]:.9f}f}};",
        f"inline constexpr float kLightColor[3] = {{{color[0]:.9f}f, {color[1]:.9f}f, {color[2]:.9f}f}};",
        f"inline constexpr float kTwistRadians = {document['twist']['twist_radians']:.9f}f;",
        "}  // namespace nw11::scene",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--graph", default=None)
    parser.add_argument("--header", required=True)
    parser.add_argument("--json", required=True)
    args = parser.parse_args(argv)
    root = Path(args.repo_root)
    graph = Path(args.graph) if args.graph else root / contract.BASE_GRAPH
    try:
        document = lower(graph, repo_root=root)
    except GraphSceneError as exc:
        print(f"[NW11-GRAPH-ERR] {exc}", file=sys.stderr)
        return 2
    header = Path(args.header)
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_text(emit_header(document), encoding="utf-8", newline="\n")
    payload = Path(args.json)
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
