#!/usr/bin/env python3
"""NW-09 stateful export extension over the established `.nwp` package path.

Stateful content uses an explicitly versioned additive extension of the
existing container: package minor version 1 plus optional ``HIST`` and
``CKPT`` sections.  History-free exports never enter this module and retain the
exact NW-08A baseline bytes and linked closure.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import nw03_compiler as nw03
import nw04_fixture
import nw05_optimizer as optimizer
import nwp_package as nwp
import nw09_temporal as temporal


NW09_EXPORT_FORMAT = "nw09-stateful-export-report"
NW09_EXPORT_VERSION = 1


class StatefulExportError(RuntimeError):
    def __init__(
        self,
        message: str,
        diagnostics: Sequence[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


HISTORY_ENTRY_POINT = "nw_history_main"
HISTORY_PRESENT_ENTRY_POINT = "nw_history_present"
HISTORY_THREAD_GROUP = (64, 1, 1)


def _hlsl_float(value: float) -> str:
    """Return a valid HLSL float literal.

    Python's ``g`` formatting emits ``16`` for an integral float; HLSL
    rejects the resulting ``16f`` literal.  Always include a decimal point and
    normalize exponent forms so the generated shader is accepted by DXC on
    Windows as well as by the test container path.
    """

    text = format(float(value), ".9g").lower()
    if "e" in text:
        mantissa, exponent = text.split("e", 1)
        if "." not in mantissa:
            mantissa += ".0"
        return f"{mantissa}e{int(exponent)}f"
    if "." not in text:
        text += ".0"
    return text + "f"


def present_geometry(document: nw03.GraphDocument) -> tuple[int, int] | None:
    """Return the explicit NW-05 rectangular presentation geometry."""

    width = document.settings.get("nw05.present.width")
    height = document.settings.get("nw05.present.height")
    if width is None or height is None:
        return None
    try:
        parsed = (int(float(width)), int(float(height)))
    except (TypeError, ValueError):
        return None
    if parsed[0] <= 0 or parsed[1] <= 0:
        return None
    return parsed


def _history_hlsl(
    document: nw03.GraphDocument,
    spec: temporal.HistorySpec,
) -> str:
    """Return one deterministic compute entry point for a History node.

    The shader consumes the previous-tick state as a read-only external input
    (t0) and writes the current-tick state to the current UAV (u0).  The state
    is packed as little-endian float32 values in a raw buffer, matching the
    declared ``values_per_element * element_count * 4`` byte layout.
    """

    node = document.nodes[spec.node_id]
    count = spec.element_count
    parsed = temporal._parse_canonical_initial(spec.initial_value)
    fields = parsed if isinstance(parsed, Mapping) else {}
    if spec.state_type == "nw.particles.v1":
        gravity = temporal._declared_float(
            fields, "gravity", temporal._param_float(node, "gravity", -0.5)
        )
        width = max(
            1, temporal._declared_int(fields, "width", temporal._param_int(node, "width", 16))
        )
        height = max(
            1, temporal._declared_int(fields, "height", temporal._param_int(node, "height", 8))
        )
        return "\n".join(
            [
                f"// Generated NW-09 particle History compute entry point: {spec.node_id}.",
                "cbuffer NwParams : register(b0) { float4 g_params[4]; };",
                "// Typed R32_UINT SRV access (no raw SRV is created for state).",
                "Buffer<uint> nw_previous : register(t0);",
                "RWByteAddressBuffer nw_current : register(u0);",
                f"[numthreads({HISTORY_THREAD_GROUP[0]}, 1, 1)]",
                f"void {HISTORY_ENTRY_POINT}(uint3 dispatch_id : SV_DispatchThreadID) {{",
                f"    if (dispatch_id.x >= {count}u) return;",
                "    uint base = dispatch_id.x * 20u;",
                "    uint word = dispatch_id.x * 5u;",
                "    float x = asfloat(nw_previous[word + 0u]);",
                "    float y = asfloat(nw_previous[word + 1u]);",
                "    float vx = asfloat(nw_previous[word + 2u]);",
                "    float vy = asfloat(nw_previous[word + 3u]);",
                "    float age = asfloat(nw_previous[word + 4u]);",
                f"    float dt = 1.0f / {_hlsl_float(temporal.FIXED_STEP_HZ)};",
                f"    vy += {_hlsl_float(gravity)} * dt;",
                "    x += vx * dt;",
                "    y += vy * dt;",
                "    age += 1.0f;",
                f"    if (x < 0.0f || x >= {_hlsl_float(width)}) {{",
                "        vx = -vx;",
                f"        x = clamp(x, 0.0f, {_hlsl_float(width - 1)});",
                "    }",
                f"    if (y < 0.0f || y >= {_hlsl_float(height)}) {{",
                "        vy = -vy;",
                f"        y = clamp(y, 0.0f, {_hlsl_float(height - 1)});",
                "    }",
                "    nw_current.Store(base + 0u, asuint(x));",
                "    nw_current.Store(base + 4u, asuint(y));",
                "    nw_current.Store(base + 8u, asuint(vx));",
                "    nw_current.Store(base + 12u, asuint(vy));",
                "    nw_current.Store(base + 16u, asuint(age));",
                "}",
                "",
            ]
        )
    if spec.state_type == "nw.feedback.image.v1":
        decay = temporal._declared_float(
            fields, "decay", temporal._param_float(node, "decay", 0.95)
        )
        gain = temporal._declared_float(
            fields, "gain", temporal._param_float(node, "gain", 0.5)
        )
        seed = temporal._declared_int(
            fields, "seed", temporal._param_int(node, "seed", 0)
        )
        width = max(
            1, temporal._declared_int(fields, "width", temporal._param_int(node, "width", 16))
        )
        height = max(
            1, temporal._declared_int(fields, "height", temporal._param_int(node, "height", 8))
        )
        return "\n".join(
            [
                f"// Generated NW-09 feedback-image History compute entry point: {spec.node_id}.",
                "cbuffer NwParams : register(b0) { float4 g_params[4]; };",
                "// Typed R32_UINT SRV access (no raw SRV is created for state).",
                "Buffer<uint> nw_previous : register(t0);",
                "RWByteAddressBuffer nw_current : register(u0);",
                f"[numthreads({HISTORY_THREAD_GROUP[0]}, 1, 1)]",
                f"void {HISTORY_ENTRY_POINT}(uint3 dispatch_id : SV_DispatchThreadID) {{",
                f"    uint index = dispatch_id.x;",
                f"    if (index >= {width * height}u) return;",
                "    uint base = index * 16u;",
                "    uint word = index * 4u;",
                "    float4 previous = float4(",
                "        asfloat(nw_previous[word + 0u]),",
                "        asfloat(nw_previous[word + 1u]),",
                "        asfloat(nw_previous[word + 2u]),",
                "        asfloat(nw_previous[word + 3u]));",
                "    // The player publishes the exact 32-bit simulation tick in",
                "    // g_params[0].y; never rebuild it from float seconds.",
                "    uint tick = asuint(g_params[0].y);",
                "    uint x = index % "
                f"{width}u; uint y = index / {width}u;",
                "    uint h = 0x9e3779b9u;",
                f"    h ^= {seed & 0xFFFFFFFF}u + 0x9e3779b9u + (h << 6u) + (h >> 2u);",
                "    h ^= tick + 0x9e3779b9u + (h << 6u) + (h >> 2u);",
                "    h ^= x + 0x9e3779b9u + (h << 6u) + (h >> 2u);",
                "    h ^= y + 0x9e3779b9u + (h << 6u) + (h >> 2u);",
                "    h ^= 11u + 0x9e3779b9u + (h << 6u) + (h >> 2u);",
                "    h ^= h >> 16u; h *= 0x7feb352du; h ^= h >> 15u; "
                "h *= 0x846ca68bu; h ^= h >> 16u;",
                "    float injection = float(h >> 8u) * (1.0f / 16777216.0f);",
                f"    float4 current = float4({_hlsl_float(decay)} * previous.x + "
                f"{_hlsl_float(gain)} * injection,",
                f"        {_hlsl_float(decay)} * previous.y + "
                f"{_hlsl_float(gain)} * (1.0f - injection),",
                f"        {_hlsl_float(decay)} * previous.z + "
                f"{_hlsl_float(gain)} * (0.5f + 0.5f * injection), 1.0f);",
                "    nw_current.Store(base + 0u, asuint(current.x));",
                "    nw_current.Store(base + 4u, asuint(current.y));",
                "    nw_current.Store(base + 8u, asuint(current.z));",
                "    nw_current.Store(base + 12u, asuint(current.w));",
                "}",
                "",
            ]
        )
    raise StatefulExportError(f"unknown History state type '{spec.state_type}'")


def _state_dimensions(
    document: nw03.GraphDocument, spec: temporal.HistorySpec
) -> tuple[int, int]:
    node = document.nodes[spec.node_id]
    if spec.state_type == "nw.particles.v1":
        return max(1, spec.element_count), 1
    parsed = temporal._parse_canonical_initial(spec.initial_value)
    fields = parsed if isinstance(parsed, Mapping) else {}
    width = max(
        1, temporal._declared_int(fields, "width", temporal._param_int(node, "width", 16))
    )
    height = max(
        1, temporal._declared_int(fields, "height", temporal._param_int(node, "height", 8))
    )
    return width, height


def _history_present_hlsl(
    document: nw03.GraphDocument,
    spec: temporal.HistorySpec,
    present_width: int,
    present_height: int,
) -> str:
    """Return a compute display pass that renders state to RGBA32F pixels."""

    width = max(1, int(present_width))
    height = max(1, int(present_height))
    state_width, state_height = _state_dimensions(document, spec)
    lines = [
        "// Generated NW-09 History display pass; state -> RGBA32F pixels.",
        "cbuffer NwParams : register(b0) { float4 g_params[4]; };",
        "// Typed R32_UINT SRV access (no raw SRV is created for state).",
        "Buffer<uint> nw_state : register(t0);",
        "RWByteAddressBuffer nw_present : register(u0);",
        f"[numthreads({HISTORY_THREAD_GROUP[0]}, 1, 1)]",
        f"void {HISTORY_PRESENT_ENTRY_POINT}(uint3 dispatch_id : SV_DispatchThreadID) {{",
        f"    uint pixel = dispatch_id.x;",
        f"    if (pixel >= {width * height}u) return;",
        f"    uint x = pixel % {width}u;",
        f"    uint y = pixel / {width}u;",
    ]
    if spec.state_type == "nw.particles.v1":
        count = max(1, spec.element_count)
        lines.extend(
            [
                f"    uint particle = pixel % {count}u;",
                "    uint source = particle * 5u;",
                "    float4 value = float4(",
                "        asfloat(nw_state[source + 0u]),",
                "        asfloat(nw_state[source + 1u]),",
                "        asfloat(nw_state[source + 2u]),",
                "        asfloat(nw_state[source + 3u]));",
            ]
        )
    else:
        lines.extend(
            [
                f"    uint sx = min({state_width - 1}u, x * {state_width}u / {width}u);",
                f"    uint sy = min({state_height - 1}u, y * {state_height}u / {height}u);",
                f"    uint source = (sy * {state_width}u + sx) * 4u;",
                "    float4 value = float4(",
                "        asfloat(nw_state[source + 0u]),",
                "        asfloat(nw_state[source + 1u]),",
                "        asfloat(nw_state[source + 2u]),",
                "        asfloat(nw_state[source + 3u]));",
            ]
        )
    lines.extend(
        [
            "    uint destination = pixel * 16u;",
            "    nw_present.Store(destination + 0u, asuint(value.x));",
            "    nw_present.Store(destination + 4u, asuint(value.y));",
            "    nw_present.Store(destination + 8u, asuint(value.z));",
            "    nw_present.Store(destination + 12u, asuint(value.w));",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def _present_history_node(
    document: nw03.GraphDocument, specs: Sequence[temporal.HistorySpec]
) -> str:
    """Return the History node that actually feeds presentation.

    The authored graph may pass the History output through ordinary post
    operators (for example ``feedback -> nw.post.tonemap -> present``).  The
    previous direct-input check silently fell back to the first History node,
    which allowed a stateful export to discard the authored tonemap stage.
    Walk the real presentation ancestry and require exactly one History
    producer; an indirect or ambiguous presentation path is an explicit
    export error rather than a silent substitution.
    """

    spec_ids = {spec.node_id for spec in specs}
    present_nodes = sorted(
        (
            node
            for node in document.nodes.values()
            if node.operator_id == "nw.render.present"
        ),
        key=lambda item: item.node_id,
    )
    if not present_nodes:
        raise StatefulExportError(
            "stateful graph has no nw.render.present node"
        )
    present = present_nodes[0]
    connection = present.inputs.get("image")
    if connection is None:
        raise StatefulExportError(
            f"present node '{present.node_id}' has no image input"
        )

    visiting: set[str] = set()

    def history_ancestors(node_id: str) -> set[str]:
        if node_id in spec_ids:
            return {node_id}
        if node_id in visiting:
            return set()
        visiting.add(node_id)
        node = document.nodes.get(node_id)
        if node is None:
            return set()
        found: set[str] = set()
        for source, _source_port in node.inputs.values():
            found.update(history_ancestors(source))
        visiting.remove(node_id)
        return found

    ancestors = history_ancestors(connection[0])
    if not ancestors:
        raise StatefulExportError(
            f"presentation path from '{connection[0]}' reaches no History node"
        )
    if len(ancestors) != 1:
        raise StatefulExportError(
            "stateful presentation path reaches multiple History nodes: "
            + ", ".join(sorted(ancestors))
        )
    return next(iter(ancestors))


def presentation_chain(
    document: nw03.GraphDocument, history_node: str
) -> tuple[nw03.GraphNode, ...]:
    """Return the authored ordinary image nodes between History and present."""

    present_nodes = sorted(
        (
            node
            for node in document.nodes.values()
            if node.operator_id == "nw.render.present"
        ),
        key=lambda item: item.node_id,
    )
    if not present_nodes:
        return ()
    present = present_nodes[0]
    connection = present.inputs.get("image")
    if connection is None:
        return ()
    chain: list[nw03.GraphNode] = []
    seen: set[str] = set()
    node_id = connection[0]
    while node_id != history_node:
        if node_id in seen:
            raise StatefulExportError(
                "stateful presentation path contains a cycle"
            )
        seen.add(node_id)
        node = document.nodes.get(node_id)
        if node is None:
            raise StatefulExportError(
                f"presentation path references unknown node '{node_id}'"
            )
        image_inputs = [
            source
            for port, (source, _source_port) in node.inputs.items()
            if port == "image"
        ]
        if len(image_inputs) != 1:
            raise StatefulExportError(
                f"presentation node '{node_id}' ({node.operator_id}) must have "
                "exactly one image input to be lowered into the stateful schedule"
            )
        chain.insert(0, node)
        node_id = image_inputs[0]

    # NW-06 defines bloom as a half-resolution extraction/blur whose output is
    # combined with the original scene by the immediately following tonemap
    # pass.  The stateful lowering realizes the same final computation in a
    # buffer-only post chain by folding that scene+bloom composite into the
    # bloom pass.  That representation is equivalent only when bloom is
    # consumed directly by nw.post.tonemap.  Fail closed for bloom->present
    # (or bloom->another operator) instead of silently changing the authored
    # operator semantics.
    for index, node in enumerate(chain):
        if node.operator_id != "nw.post.bloom":
            continue
        if (
            index + 1 >= len(chain)
            or chain[index + 1].operator_id != "nw.post.tonemap"
        ):
            raise StatefulExportError(
                f"stateful bloom node '{node.node_id}' must be consumed "
                "directly by nw.post.tonemap to preserve the established "
                "NW-06 bloom semantics"
            )
    return tuple(chain)


def _post_pass_hlsl(
    document: nw03.GraphDocument,
    node: nw03.GraphNode,
    present_width: int,
    present_height: int,
) -> str:
    """Return a deterministic compute pass for an ordinary post operator."""

    width = max(1, int(present_width))
    height = max(1, int(present_height))
    count = width * height
    header = [
        f"// Generated NW-09 ordinary post pass: {node.operator_id} ({node.node_id}).",
        "cbuffer NwParams : register(b0) { float4 g_params[4]; };",
        # The player realizes buffer SRVs as typed views (RGBA32F has 16-byte
        # elements), so the matching shader access is a typed Buffer<float4>.
        # A ByteAddressBuffer here would require a raw SRV that the established
        # descriptor creation does not produce.
        "Buffer<float4> nw_input : register(t0);",
        "RWByteAddressBuffer nw_output : register(u0);",
        f"[numthreads({HISTORY_THREAD_GROUP[0]}, 1, 1)]",
        "void nw09_post_main(uint3 dispatch_id : SV_DispatchThreadID) {",
        "    uint pixel = dispatch_id.x;",
        f"    if (pixel >= {count}u) return;",
        "    uint base = pixel * 16u;",
        "    float3 scene = nw_input.Load(int(pixel)).rgb;",
    ]
    if node.operator_id == "nw.post.tonemap":
        exposure = temporal._param_float(node, "exposure", 1.0)
        gamma = temporal._param_float(node, "gamma", 2.2)
        operator = node.params.get("operator", ["aces"])[0]
        operator_value = {"linear": 0, "reinhard": 1, "aces": 2}.get(
            operator, 2
        )
        body = [
            f"    float3 color = scene * {_hlsl_float(exposure)};",
            f"    uint tonemap_operator = {operator_value}u;",
            "    float3 mapped;",
            "    if (tonemap_operator == 1u) {",
            "        mapped = color / (1.0f + color);",
            "    } else if (tonemap_operator == 2u) {",
            "        const float a = 2.51f;",
            "        const float b = 0.03f;",
            "        const float c = 2.43f;",
            "        const float d = 0.59f;",
            "        const float e = 0.14f;",
            "        mapped = saturate((color * (a * color + b)) /",
            "            (color * (c * color + d) + e));",
            "    } else {",
            "        mapped = saturate(color);",
            "    }",
            "    mapped = pow(max(mapped, 0.0f), "
            f"1.0f / {_hlsl_float(gamma)});",
            "    float4 out_value = float4(mapped, 1.0f);",
        ]
    elif node.operator_id == "nw.post.bloom":
        # Reuse the established NW-06 bloom semantics exactly: a 3x3
        # luminance-thresholded extraction at half resolution with a
        # floating-point radius, averaged over nine taps and scaled by
        # intensity.  The established tonemap pass adds the bloom buffer to
        # the scene, so this pass emits that same `scene + bloom` composite
        # for the following tonemap pass.
        threshold = temporal._param_float(node, "threshold", 0.7)
        intensity = temporal._param_float(node, "intensity", 0.35)
        radius = temporal._param_float(node, "radius", 1.5)
        resolution_scale = temporal._param_float(
            node, "resolution_scale", 0.5
        )
        if abs(resolution_scale - 0.5) > 1e-6:
            raise StatefulExportError(
                f"stateful bloom node '{node.node_id}' must resolve at "
                "exactly half resolution to match the established "
                "nw.post.bloom operator"
            )
        half_width = max(1, width // 2)
        half_height = max(1, height // 2)
        body = [
            "    uint x = pixel % "
            f"{width}u; uint y = pixel / {width}u;",
            "    float2 uv = (float2(float(x), float(y)) + 0.5f) / "
            f"float2({_hlsl_float(width)}, {_hlsl_float(height)});",
            "    uint half_x = min(uint(uv.x * "
            f"{_hlsl_float(half_width)}), {half_width - 1}u);",
            "    uint half_y = min(uint(uv.y * "
            f"{_hlsl_float(half_height)}), {half_height - 1}u);",
            "    float2 half_uv = (float2(float(half_x), float(half_y)) + "
            "0.5f) / "
            f"float2({_hlsl_float(half_width)}, {_hlsl_float(half_height)});",
            "    float2 texel = 1.0f / "
            f"float2({_hlsl_float(width)}, {_hlsl_float(height)});",
            "    float3 bloom = float3(0.0f, 0.0f, 0.0f);",
            "    [unroll]",
            "    for (int dy = -1; dy <= 1; ++dy) {",
            "        [unroll]",
            "        for (int dx = -1; dx <= 1; ++dx) {",
            "            int2 coordinate = int2(clamp(",
            "                half_uv + float2(float(dx), float(dy)) * "
            f"texel * {_hlsl_float(radius)}, 0.0f, 1.0f) * "
            f"float2({_hlsl_float(width)}, {_hlsl_float(height)}));",
            "            coordinate = min(coordinate, "
            f"int2({width - 1}, {height - 1}));",
            "            uint source = uint(coordinate.y) * "
            f"{width}u + uint(coordinate.x);",
            "            float3 sample_color = nw_input.Load(int(source)).rgb;",
            "            float luma = dot(sample_color,",
            "                float3(0.2126f, 0.7152f, 0.0722f));",
            f"            bloom += luma > {_hlsl_float(threshold)} "
            "? sample_color : float3(0.0f, 0.0f, 0.0f);",
            "        }",
            "    }",
            "    float3 out_color = scene + bloom / 9.0f * "
            f"{_hlsl_float(intensity)};",
            "    float4 out_value = float4(out_color, 1.0f);",
        ]
    else:
        raise StatefulExportError(
            f"stateful presentation node '{node.node_id}' "
            f"({node.operator_id}) has no supported ordinary lowering"
        )
    return "\n".join(
        header
        + body
        + [
            "    nw_output.Store(base + 0u, asuint(out_value.x));",
            "    nw_output.Store(base + 4u, asuint(out_value.y));",
            "    nw_output.Store(base + 8u, asuint(out_value.z));",
            "    nw_output.Store(base + 12u, asuint(out_value.w));",
            "}",
            "",
        ]
    )


def _history_shaders(
    document: nw03.GraphDocument,
    specs: Sequence[temporal.HistorySpec],
    *,
    present_width: int | None = None,
    present_height: int | None = None,
) -> list[nw03.GeneratedShader]:
    present_node = _present_history_node(document, specs)
    post_chain = presentation_chain(document, present_node)
    shaders: list[nw03.GeneratedShader] = []
    for spec in specs:
        hlsl = _history_hlsl(document, spec)
        shaders.append(
            nw03.GeneratedShader(
                specialization_id=f"nw09.history.step.{spec.node_id}",
                entry_point=HISTORY_ENTRY_POINT,
                target=nw03.SHADER_TARGET,
                family=nw03.PASS_FAMILY,
                hlsl=hlsl,
                source_map=(),
                root_expression=f"history:{spec.state_type}",
                root_node_ids=(spec.node_id,),
                thread_group=HISTORY_THREAD_GROUP,
                execution_root=spec.node_id,
            )
        )
        if spec.node_id == present_node:
            shader_width = (
                present_width
                if present_width is not None
                else max(1, spec.state_bytes // 16)
            )
            shader_height = 1 if present_height is None else present_height
            present_hlsl = _history_present_hlsl(
                document, spec, shader_width, shader_height
            )
            shaders.append(
                nw03.GeneratedShader(
                    specialization_id=f"nw09.history.present.{spec.node_id}",
                    entry_point=HISTORY_PRESENT_ENTRY_POINT,
                    target=nw03.SHADER_TARGET,
                    family=nw03.PASS_FAMILY,
                    hlsl=present_hlsl,
                    source_map=(),
                    root_expression=f"history-present:{spec.state_type}",
                    root_node_ids=(spec.node_id,),
                    thread_group=HISTORY_THREAD_GROUP,
                    execution_root=spec.node_id,
                )
            )
            if post_chain:
                for post_node in post_chain:
                    post_hlsl = _post_pass_hlsl(
                        document, post_node, shader_width, shader_height
                    )
                    shaders.append(
                        nw03.GeneratedShader(
                            specialization_id=(
                                f"nw09.post.{post_node.operator_id}."
                                f"{post_node.node_id}"
                            ),
                            entry_point="nw09_post_main",
                            target=nw03.SHADER_TARGET,
                            family=nw03.PASS_FAMILY,
                            hlsl=post_hlsl,
                            source_map=(),
                            root_expression=(
                                f"post:{post_node.operator_id}"
                            ),
                            root_node_ids=(post_node.node_id,),
                            thread_group=HISTORY_THREAD_GROUP,
                            execution_root=post_node.node_id,
                        )
                    )
    return shaders


def _test_dxc(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    dxc = directory / "nw09_mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
        "fo.parent.mkdir(parents=True, exist_ok=True)\n"
        f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\n",
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    return str(dxc)


def resolve_history_dxc(destination: Path, explicit: str | None, *, allow_test_payloads: bool) -> str | None:
    """Select the same offline compiler for legacy and sampled History."""
    if explicit is not None:
        return explicit
    candidate = nw03.resolve_dxc()
    if not allow_test_payloads:
        return candidate
    if candidate is not None:
        try:
            if nw03.PINNED_DXC_VERSION in nw03.query_dxc_version(candidate):
                return candidate
        except (OSError, RuntimeError):
            pass
    return _test_dxc(destination.parent / ".nw09-test-toolchain")


def compile_history_base_package(
    document: nw03.GraphDocument,
    destination: str | os.PathLike[str],
    *,
    dxc_executable: str | None = None,
    optimization: str = "O2",
    allow_unpinned_dxc: bool = False,
    allow_test_payloads: bool = False,
    cache_dir: str | None = None,
) -> tuple[nwp.NwpPackage, dict[str, Any], tuple[temporal.HistorySpec, ...]]:
    """Compile the supplied document through the established NW-03 pipeline.

    History nodes are lowered to ordinary compute shaders and compiled by the
    same offline DXC/cache/report path as every other package.  The result is
    then extended with the versioned ``HIST`` state section by
    :func:`build_stateful_package`.
    """

    specs = temporal.history_specs(document)
    if not specs:
        raise StatefulExportError("document has no History declarations")
    try:
        optimized = optimizer.optimize_graph(document)
    except optimizer.OptimizationError as exc:
        raise StatefulExportError(
            "stateful graph optimisation failed", exc.diagnostics
        ) from exc
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    dxc = resolve_history_dxc(destination_path, dxc_executable, allow_test_payloads=allow_test_payloads)
    if dxc is None:
        raise StatefulExportError(
            "offline DXC compiler is required for stateful export",
            [
                nwp.package_diagnostic(
                    "toolchain_unavailable",
                    "offline DXC compiler is unavailable",
                )
            ],
        )
    runtime = temporal.TemporalRuntime(document)
    geometry = present_geometry(document)
    report = nw03.compile_package(
        document.source_name,
        destination_path,
        document=document,
        semantic_digest_override=runtime.semantic_digest,
        semantic_digest_test_identity=allow_test_payloads,
        cache_dir=cache_dir,
        dxc_executable=dxc,
        optimization=optimization,
        allow_unpinned_dxc=allow_unpinned_dxc,
        allow_test_payloads=allow_test_payloads,
        generated_shaders_override=_history_shaders(
            document,
            specs,
            present_width=geometry[0] if geometry else None,
            present_height=geometry[1] if geometry else None,
        ),
    )
    package = nwp.decode_package(destination_path.read_bytes())
    report["optimized_partitions"] = {
        name: list(nodes) for name, nodes in optimized.partitions.items()
    }
    return package, report, specs


def build_stateful_package(
    document: nw03.GraphDocument,
    *,
    allow_test_payloads: bool,
    packaged_checkpoint_ticks: Sequence[int] = (),
    frames_in_flight: int = 2,
    compiled_package: nwp.NwpPackage | None = None,
    compile_report: Mapping[str, Any] | None = None,
    specs: Sequence[temporal.HistorySpec] | None = None,
    dxc_executable: str | None = None,
    optimization: str = "O2",
    allow_unpinned_dxc: bool = False,
    cache_dir: str | None = None,
) -> tuple[nwp.NwpPackage, dict[str, Any]]:
    """Return the existing package model with the explicit NW-09 extension."""

    if frames_in_flight < 2:
        raise StatefulExportError(
            "stateful packages require at least two frames in flight"
        )
    specs = tuple(specs) if specs is not None else temporal.history_specs(document)
    specs = tuple(sorted(specs, key=lambda item: item.node_id))
    if not specs:
        raise StatefulExportError("document has no History declarations")
    if compiled_package is None:
        with tempfile.TemporaryDirectory(prefix=".nw09-base-") as temporary:
            compiled_package, base_report, compiled_specs = (
                compile_history_base_package(
                    document,
                    Path(temporary) / "base.nwp",
                    dxc_executable=dxc_executable,
                    optimization=optimization,
                    allow_unpinned_dxc=allow_unpinned_dxc,
                    allow_test_payloads=allow_test_payloads,
                    cache_dir=cache_dir,
                )
            )
            compile_report = base_report
            specs = compiled_specs
    package = compiled_package
    geometry_for_shaders = present_geometry(document)
    generated_order = sorted(
        _history_shaders(
            document,
            specs,
            present_width=(
                geometry_for_shaders[0] if geometry_for_shaders else None
            ),
            present_height=(
                geometry_for_shaders[1] if geometry_for_shaders else None
            ),
        ),
        key=lambda item: item.specialization_id,
    )
    ordered_shaders = sorted(package.shaders, key=lambda item: item.shader_id)
    if len(ordered_shaders) != len(generated_order):
        raise StatefulExportError(
            "compiled History shader count does not match the declarations"
        )
    if not package.parameters:
        raise StatefulExportError(
            "compiled History base package has no timeline parameters"
        )
    if not package.root_signatures:
        raise StatefulExportError(
            "compiled History base package has no root signature"
        )
    runtime = temporal.TemporalRuntime(document)
    parameters_resource = next(
        (resource for resource in package.resources if resource.usage == "cbv"),
        None,
    )
    if parameters_resource is None:
        raise StatefulExportError(
            "compiled History base package has no root-constant resource"
        )

    # Never infer shader identity from positional ordering.  NW-03 assigns
    # package shader ids while retaining an explicit specialization-id report;
    # use that report as the authoritative binding between generated History
    # specializations and package records.  Positional zips can silently swap
    # the step and display shaders when the two orderings differ.
    if compile_report is None:
        raise StatefulExportError(
            "compiled History shader identity report is required"
        )
    reported_shaders = compile_report.get("fixture", {}).get("shaders", [])
    if not reported_shaders:
        raise StatefulExportError(
            "compiled History report contains no shader identity rows"
        )
    shader_id_by_specialization: dict[str, int] = {}
    for row in reported_shaders:
        specialization_id = str(row.get("specialization_id", ""))
        shader_id = int(row.get("shader_id", 0))
        if not specialization_id or shader_id <= 0:
            raise StatefulExportError(
                "compiled History report contains an invalid shader identity"
            )
        if specialization_id in shader_id_by_specialization:
            raise StatefulExportError(
                f"duplicate compiled History specialization {specialization_id!r}"
            )
        shader_id_by_specialization[specialization_id] = shader_id
    expected_specializations = {
        generated.specialization_id for generated in generated_order
    }
    if set(shader_id_by_specialization) != expected_specializations:
        missing = sorted(expected_specializations - set(shader_id_by_specialization))
        unexpected = sorted(set(shader_id_by_specialization) - expected_specializations)
        raise StatefulExportError(
            "compiled History shader identity set does not match generated "
            f"specializations (missing={missing}, unexpected={unexpected})"
        )
    package_shader_ids = {shader.shader_id for shader in package.shaders}
    if not set(shader_id_by_specialization.values()) <= package_shader_ids:
        raise StatefulExportError(
            "compiled History report references a shader id absent from the package"
        )
    step_shader_id_by_state_id = {
        index: shader_id_by_specialization[
            f"nw09.history.step.{spec.node_id}"
        ]
        for index, spec in enumerate(specs, start=1)
    }
    present_shader_node = _present_history_node(document, specs)
    post_chain = presentation_chain(document, present_shader_node)
    history_image_shader_id = shader_id_by_specialization[
        f"nw09.history.present.{present_shader_node}"
    ]
    post_shader_rows: list[tuple[nw03.GraphNode, int]] = [
        (
            node,
            shader_id_by_specialization[
                f"nw09.post.{node.operator_id}.{node.node_id}"
            ],
        )
        for node in post_chain
    ]
    present_shader_id_by_node = {
        present_shader_node: (
            post_shader_rows[-1][1]
            if post_shader_rows
            else history_image_shader_id
        )
    }
    next_resource = max(
        (resource.resource_id for resource in package.resources), default=0
    ) + 1
    resources = [parameters_resource]
    initialization: list[nwp.Instruction] = []
    parameters = list(package.parameters)
    if (
        not parameters
        or parameters[0].name != "time"
        or len(parameters[0].value) != 4
    ):
        raise StatefulExportError(
            "compiled History base package must expose the canonical 'time' "
            "parameter first so the exact simulation tick can be packed "
            "immediately after it"
        )
    next_parameter_id = max(
        (parameter.parameter_id for parameter in parameters), default=0
    ) + 1
    # The fixed-step simulation tick is an exact integer scheduler input.  It
    # is packed immediately after the float ``time`` parameter so the player
    # can publish its 32-bit value in the root-constant block without ever
    # reconstructing it from a float32 timeline value (which loses precision
    # above 2^24 timeline ticks).
    parameters.append(
        nwp.ParameterRecord(
            parameter_id=next_parameter_id,
            name="nw09.simulation_tick",
            type="uint",
            phase="initialization",
            component_count=1,
            value=struct.pack("<I", 0),
            timeline_start_frame=0,
            timeline_end_frame=0,
            timeline_step=0,
            timeline_interpolation="none",
        )
    )
    next_parameter_id += 1
    geometry = present_geometry(document)
    if geometry is not None:
        for name, value in (
            ("nw05.present.width", geometry[0]),
            ("nw05.present.height", geometry[1]),
        ):
            parameters.append(
                nwp.ParameterRecord(
                    parameter_id=next_parameter_id,
                    name=name,
                    type="uint",
                    phase="initialization",
                    component_count=1,
                    value=struct.pack("<I", int(value) & 0xFFFFFFFF),
                    timeline_start_frame=0,
                    timeline_end_frame=0,
                    timeline_step=0,
                    timeline_interpolation="none",
                )
            )
            next_parameter_id += 1
    if runtime.loop_ticks is not None:
        parameters.append(
            nwp.ParameterRecord(
                parameter_id=next_parameter_id,
                name="nw09.loop_ticks",
                type="uint",
                phase="initialization",
                component_count=1,
                value=struct.pack("<I", int(runtime.loop_ticks) & 0xFFFFFFFF),
                timeline_start_frame=0,
                timeline_end_frame=0,
                timeline_step=0,
                timeline_interpolation="none",
            )
        )
        next_parameter_id += 1
    root_signature = package.root_signatures[0]
    bindings = (
        nwp.RootBinding("params", "cbv", 0, 0, 0),
        nwp.RootBinding("output", "uav", 0, 0, 1),
        nwp.RootBinding("input", "srv", 0, 0, 2),
    )
    root_signature = replace(
        root_signature,
        bindings=bindings,
        blob=nwp.make_root_signature_blob(root_signature.family, bindings),
        dword_cost=6,
    )
    frame: list[nwp.Instruction] = [
        nwp.Instruction(
            "frame.set_timeline",
            (
                nwp.InstructionOperand("u32", parameters[0].parameter_id),
                nwp.InstructionOperand("u64", 0),
            ),
        ),
        nwp.Instruction(
            "frame.set_root_signature",
            (
                nwp.InstructionOperand(
                    "root_signature_ref", root_signature.root_signature_id
                ),
            ),
        ),
        nwp.Instruction(
            "frame.set_root_constants",
            (
                nwp.InstructionOperand("u32", 0),
                nwp.InstructionOperand(
                    "resource_ref", parameters_resource.resource_id
                ),
                nwp.InstructionOperand("u32", 4),
            ),
        ),
    ]
    history_records: list[nwp.HistoryRecord] = []
    reports: list[dict[str, Any]] = []
    state_resource_ids: list[int] = []
    resource_by_node: dict[str, int] = {}
    for index, spec in enumerate(specs, start=1):
        current_id = next_resource
        next_resource += 1
        previous_id = next_resource
        next_resource += 1
        state_bytes = spec.state_bytes
        initial_values = temporal.initial_state_values(document, spec)
        initial_state = struct.pack(f"<{len(initial_values)}f", *initial_values)
        for resource_id, name, usage in (
            (current_id, "output", "uav"),
            (previous_id, "input", "srv"),
        ):
            resources.append(
                nwp.ResourceDescriptor(
                    resource_id=resource_id,
                    name=name,
                    kind="buffer",
                    format="DXGI_FORMAT_R32_UINT",
                    usage=usage,
                    pass_family=nw03.PASS_FAMILY,
                    binding_slot=0,
                    binding_space=0,
                    size_bytes=state_bytes,
                )
            )
            state_resource_ids.append(resource_id)
            initialization.append(
                nwp.Instruction(
                    "init.create_resource",
                    (nwp.InstructionOperand("resource_ref", resource_id),),
                )
            )
        initialization.extend(
            [
                nwp.Instruction(
                    "init.upload_history_state",
                    (
                        nwp.InstructionOperand("resource_ref", current_id),
                        nwp.InstructionOperand("u32", index),
                    ),
                ),
                nwp.Instruction(
                    "init.upload_history_state",
                    (
                        nwp.InstructionOperand("resource_ref", previous_id),
                        nwp.InstructionOperand("u32", index),
                    ),
                ),
            ]
        )
        # Explicit ping-pong boundaries: previous is sampled as t0, current is
        # written as u0, and the only writer for each state element is the
        # single History dispatch.  The resources are distinct and remain
        # live until the submitted work retires.
        dispatch_groups = (
            (spec.element_count + HISTORY_THREAD_GROUP[0] - 1)
            // HISTORY_THREAD_GROUP[0],
            1,
            1,
        )
        frame.extend(
            [
                nwp.Instruction(
                    "frame.barrier",
                    (
                        nwp.InstructionOperand("u32", 4),
                        nwp.InstructionOperand("resource_ref", current_id),
                    ),
                ),
                nwp.Instruction(
                    "frame.set_descriptor_table",
                    (
                        nwp.InstructionOperand("u32", 2),
                        nwp.InstructionOperand("string", "cbv_srv_uav"),
                        nwp.InstructionOperand("resource_ref", previous_id),
                    ),
                ),
                nwp.Instruction(
                    "frame.set_descriptor_table",
                    (
                        nwp.InstructionOperand("u32", 1),
                        nwp.InstructionOperand("string", "cbv_srv_uav"),
                        nwp.InstructionOperand("resource_ref", current_id),
                    ),
                ),
                nwp.Instruction(
                    "frame.simulation_step",
                    (
                        nwp.InstructionOperand("u32", dispatch_groups[0]),
                        nwp.InstructionOperand("u32", dispatch_groups[1]),
                        nwp.InstructionOperand("u32", dispatch_groups[2]),
                        nwp.InstructionOperand(
                            "shader_ref", step_shader_id_by_state_id[index]
                        ),
                        nwp.InstructionOperand("u32", index),
                    ),
                ),
                nwp.Instruction(
                    "frame.barrier",
                    (
                        nwp.InstructionOperand("u32", 4),
                        nwp.InstructionOperand("resource_ref", current_id),
                    ),
                ),
                nwp.Instruction(
                    "frame.copy_buffer",
                    (
                        nwp.InstructionOperand("resource_ref", current_id),
                        nwp.InstructionOperand("resource_ref", previous_id),
                        nwp.InstructionOperand("u64", state_bytes),
                    ),
                ),
            ]
        )
        resource_by_node[spec.node_id] = current_id
        history_records.append(
            nwp.HistoryRecord(
                state_id=index,
                node_id=spec.node_id,
                operator_id=spec.operator_id,
                state_type=spec.state_type,
                initial_value=spec.initial_value,
                reset_policy=spec.reset_policy,
                step_rate_hz=spec.step_rate_hz,
                state_layout_hash=spec.state_layout_hash,
                values_per_element=spec.values_per_element,
                element_count=spec.element_count,
                current_resource_id=current_id,
                previous_resource_id=previous_id,
                frames_in_flight=frames_in_flight,
                initial_state=initial_state,
            )
        )
        reports.append(
            {
                **spec.to_dict(),
                "current_resource_id": current_id,
                "previous_resource_id": previous_id,
                "ping_pong_distinct": current_id != previous_id,
                "state_bytes": state_bytes,
                "step_shader_id": step_shader_id_by_state_id[index],
                "frames_in_flight": frames_in_flight,
            }
        )
    initialization = [
        nwp.Instruction(
            "init.create_resource",
            (nwp.InstructionOperand("resource_ref", parameters_resource.resource_id),),
        ),
        *initialization,
        nwp.Instruction(
            "init.create_root_signature",
            (
                nwp.InstructionOperand(
                    "root_signature_ref", root_signature.root_signature_id
                ),
            ),
        ),
        nwp.Instruction(
            "init.upload_resource",
            (
                nwp.InstructionOperand(
                    "resource_ref", parameters_resource.resource_id
                ),
            ),
        ),
    ]
    for shader in package.shaders:
        initialization.append(
            nwp.Instruction(
                "init.create_pipeline",
                (
                    nwp.InstructionOperand("shader_ref", shader.shader_id),
                    nwp.InstructionOperand(
                        "root_signature_ref", root_signature.root_signature_id
                    ),
                ),
            )
        )
    present_state_resource_id = resource_by_node[present_shader_node]
    present_state_record = next(
        record
        for record in history_records
        if record.current_resource_id == present_state_resource_id
    )
    if geometry is None:
        present_surface_bytes = max(
            16, ((present_state_record.state_bytes + 15) // 16) * 16
        )
        present_width = present_surface_bytes // 16
        present_height = 1
    else:
        present_width, present_height = geometry
        present_surface_bytes = max(
            present_width * present_height * 16,
            ((present_state_record.state_bytes + 15) // 16) * 16,
        )
    history_image_surface_id = next_resource
    next_resource += 1
    resources.append(
        nwp.ResourceDescriptor(
            resource_id=history_image_surface_id,
            name="output",
            kind="buffer",
            format="DXGI_FORMAT_R32G32B32A32_FLOAT",
            usage="uav",
            pass_family=nw03.PASS_FAMILY,
            binding_slot=0,
            binding_space=0,
            size_bytes=present_surface_bytes,
        )
    )
    initialization.append(
        nwp.Instruction(
            "init.create_resource",
            (
                nwp.InstructionOperand(
                    "resource_ref", history_image_surface_id
                ),
            ),
        )
    )
    present_node_id = present_shader_node
    present_groups = (
        (present_width * present_height + HISTORY_THREAD_GROUP[0] - 1)
        // HISTORY_THREAD_GROUP[0],
        1,
        1,
    )
    frame.extend(
        [
            nwp.Instruction(
                "frame.barrier",
                (
                    nwp.InstructionOperand("u32", 4),
                    nwp.InstructionOperand(
                        "resource_ref", history_image_surface_id
                    ),
                ),
            ),
            nwp.Instruction(
                "frame.set_descriptor_table",
                (
                    nwp.InstructionOperand("u32", 2),
                    nwp.InstructionOperand("string", "cbv_srv_uav"),
                    nwp.InstructionOperand(
                        "resource_ref",
                        present_state_record.previous_resource_id,
                    ),
                ),
            ),
            nwp.Instruction(
                "frame.set_descriptor_table",
                (
                    nwp.InstructionOperand("u32", 1),
                    nwp.InstructionOperand("string", "cbv_srv_uav"),
                    nwp.InstructionOperand(
                        "resource_ref", history_image_surface_id
                    ),
                ),
            ),
            nwp.Instruction(
                "frame.dispatch",
                (
                    nwp.InstructionOperand("u32", present_groups[0]),
                    nwp.InstructionOperand("u32", present_groups[1]),
                    nwp.InstructionOperand("u32", present_groups[2]),
                    nwp.InstructionOperand(
                        "shader_ref", history_image_shader_id
                    ),
                ),
            ),
            nwp.Instruction(
                "frame.barrier",
                (
                    nwp.InstructionOperand("u32", 4),
                    nwp.InstructionOperand(
                        "resource_ref", history_image_surface_id
                    ),
                ),
            ),
        ]
    )
    present_resource_id = history_image_surface_id
    post_reports: list[dict[str, Any]] = []
    for post_node, post_shader_id in post_shader_rows:
        # The established root-signature/validation contract binds the
        # generated SRV root parameter to a resource literally named "input"
        # and the UAV root parameter to a resource named "output".  Publish the
        # ordinary pass input through an explicit copy into a distinct,
        # fence-safe "input" resource rather than aliasing the UAV.
        post_input_id = next_resource
        next_resource += 1
        resources.append(
            nwp.ResourceDescriptor(
                resource_id=post_input_id,
                name="input",
                kind="buffer",
                format="DXGI_FORMAT_R32G32B32A32_FLOAT",
                usage="srv",
                pass_family=nw03.PASS_FAMILY,
                binding_slot=0,
                binding_space=0,
                size_bytes=present_surface_bytes,
            )
        )
        initialization.append(
            nwp.Instruction(
                "init.create_resource",
                (
                    nwp.InstructionOperand(
                        "resource_ref", post_input_id
                    ),
                ),
            )
        )
        post_surface_id = next_resource
        next_resource += 1
        resources.append(
            nwp.ResourceDescriptor(
                resource_id=post_surface_id,
                name="output",
                kind="buffer",
                format="DXGI_FORMAT_R32G32B32A32_FLOAT",
                usage="uav",
                pass_family=nw03.PASS_FAMILY,
                binding_slot=0,
                binding_space=0,
                size_bytes=present_surface_bytes,
            )
        )
        initialization.append(
            nwp.Instruction(
                "init.create_resource",
                (
                    nwp.InstructionOperand(
                        "resource_ref", post_surface_id
                    ),
                ),
            )
        )
        frame.extend(
            [
                nwp.Instruction(
                    "frame.copy_buffer",
                    (
                        nwp.InstructionOperand(
                            "resource_ref", present_resource_id
                        ),
                        nwp.InstructionOperand(
                            "resource_ref", post_input_id
                        ),
                        nwp.InstructionOperand(
                            "u64", present_surface_bytes
                        ),
                    ),
                ),
                nwp.Instruction(
                    "frame.barrier",
                    (
                        nwp.InstructionOperand("u32", 4),
                        nwp.InstructionOperand(
                            "resource_ref", post_surface_id
                        ),
                    ),
                ),
                nwp.Instruction(
                    "frame.set_descriptor_table",
                    (
                        nwp.InstructionOperand("u32", 2),
                        nwp.InstructionOperand("string", "cbv_srv_uav"),
                        nwp.InstructionOperand(
                            "resource_ref", post_input_id
                        ),
                    ),
                ),
                nwp.Instruction(
                    "frame.set_descriptor_table",
                    (
                        nwp.InstructionOperand("u32", 1),
                        nwp.InstructionOperand("string", "cbv_srv_uav"),
                        nwp.InstructionOperand(
                            "resource_ref", post_surface_id
                        ),
                    ),
                ),
                nwp.Instruction(
                    "frame.dispatch",
                    (
                        nwp.InstructionOperand("u32", present_groups[0]),
                        nwp.InstructionOperand("u32", present_groups[1]),
                        nwp.InstructionOperand("u32", present_groups[2]),
                        nwp.InstructionOperand(
                            "shader_ref", post_shader_id
                        ),
                    ),
                ),
                nwp.Instruction(
                    "frame.barrier",
                    (
                        nwp.InstructionOperand("u32", 4),
                        nwp.InstructionOperand(
                            "resource_ref", post_surface_id
                        ),
                    ),
                ),
            ]
        )
        post_reports.append(
            {
                "node_id": post_node.node_id,
                "operator_id": post_node.operator_id,
                "shader_id": post_shader_id,
                "input_resource_id": present_resource_id,
                "copied_input_resource_id": post_input_id,
                "output_resource_id": post_surface_id,
            }
        )
        present_resource_id = post_surface_id
    frame.extend(
        [
            nwp.Instruction(
                "frame.barrier",
                (
                    nwp.InstructionOperand("u32", 4),
                    nwp.InstructionOperand("resource_ref", present_resource_id),
                ),
            ),
            nwp.Instruction(
                "frame.present",
                (nwp.InstructionOperand("resource_ref", present_resource_id),),
            ),
        ]
    )
    checkpoints: list[nwp.ReleaseCheckpointRecord] = []
    checkpoint_reports: list[dict[str, Any]] = []
    for checkpoint_id, tick in enumerate(packaged_checkpoint_ticks, start=1):
        if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
            raise StatefulExportError("packaged checkpoint tick must be a non-negative integer")
        if tick < runtime.timeline_tick:
            runtime.reset()
        runtime.advance_to(tick)
        checkpoint = runtime.checkpoint()
        simulation_tick = checkpoint.simulation_tick
        # Packaged/disk checkpoints are explicitly host-resident.  They make no
        # portable GPU-state replay claim.
        checkpoint_payload = checkpoint.to_dict()
        payload = nwp.canonical_json_bytes(checkpoint_payload)
        record = nwp.ReleaseCheckpointRecord(
            checkpoint_id=checkpoint_id,
            label=f"release-tick-{tick}",
            tick=int(checkpoint.tick),
            simulation_tick=int(simulation_tick),
            semantic_digest=checkpoint.semantic_digest,
            state_layout_hash=checkpoint.state_layout_hash,
            capability_profile_id=document.profile_id,
            capability_requirements_digest=document.requirements_digest,
            payload=payload,
            state_count=len(specs),
        )
        checkpoints.append(record)
        checkpoint_reports.append(record.to_dict())

    derived = nwp.derive_capability_requirements(
        resources,
        initialization,
        frame,
        package.shaders,
        [root_signature],
    )
    capability = replace(
        package.capability,
        **derived,
        required_init_instructions=tuple(
            sorted({instruction.opcode for instruction in initialization})
        ),
        required_frame_instructions=tuple(
            sorted({instruction.opcode for instruction in frame})
        ),
    )
    capability = replace(
        capability, profile_fingerprint=capability.computed_profile_fingerprint()
    )
    stateful = replace(
        package,
        capability=capability,
        resources=resources,
        initialization=initialization,
        frame=frame,
        parameters=parameters,
        shaders=tuple(
            replace(
                shader,
                thread_group_x=HISTORY_THREAD_GROUP[0],
                thread_group_y=1,
                thread_group_z=1,
            )
            for shader in package.shaders
        ),
        root_signatures=(root_signature,),
        history=tuple(history_records),
        release_checkpoints=tuple(checkpoints),
        flags=1,
    )
    diagnostics = nwp.validate_package_model(stateful)
    if diagnostics:
        raise StatefulExportError(
            "stateful package extension is invalid", diagnostics
        )
    report = {
        "format": NW09_EXPORT_FORMAT,
        "version": NW09_EXPORT_VERSION,
        "stateful": True,
        "package_version": {
            "major": nwp.VERSION_MAJOR,
            "minor": stateful.version_minor,
        },
        "capability_profile_id": document.profile_id,
        "capability_requirements_digest": document.requirements_digest,
        "fixed_step": {
            "simulation_hz": temporal.FIXED_STEP_HZ,
            "timeline_ticks_per_second": temporal.TIMELINE_TICKS_PER_SECOND,
            "timeline_ticks_per_simulation_tick": (
                temporal.TIMELINE_TICKS_PER_SIMULATION_TICK
            ),
        },
        "history": reports,
        "state_resources": state_resource_ids,
        "present_state_node": present_node_id,
        "present_state_resource_id": present_state_resource_id,
        "present_resource_id": present_resource_id,
        "present_shader_id": present_shader_id_by_node[present_node_id],
        "shader_ids_by_specialization": dict(
            sorted(shader_id_by_specialization.items())
        ),
        "present_surface": {
            "resource_id": present_resource_id,
            "kind": "buffer",
            "usage": "uav",
            "width": present_width,
            "height": present_height,
            "format": "DXGI_FORMAT_R32G32B32A32_FLOAT",
            "size_bytes": present_surface_bytes,
        },
        "history_image_surface": {
            "resource_id": history_image_surface_id,
            "shader_id": history_image_shader_id,
            "width": present_width,
            "height": present_height,
        },
        "post_passes": post_reports,
        "presentation_nodes": [
            {
                "node_id": node.node_id,
                "operator_id": node.operator_id,
                "shader_id": shader_id,
            }
            for node, shader_id in post_shader_rows
        ],
        "presentation_path": {
            "package_source": "rgba32f-uav-buffer",
            "player_display_stage": "buffer-to-player-owned-texture",
            "swapchain_stage": "player-owned-texture-to-backbuffer",
        },
        "frames_in_flight": frames_in_flight,
        "pending_retirement_bytes": sum(
            report["state_bytes"] * frames_in_flight for report in reports
        ),
        "packaged_checkpoints": checkpoint_reports,
        "packaged_checkpoint_byte_cost": (
            nwp.release_checkpoint_section_byte_cost(checkpoints)
        ),
        "present_geometry": (
            {"width": present_geometry(document)[0], "height": present_geometry(document)[1]}
            if present_geometry(document)
            else None
        ),
        "portable_gpu_state_replay": False,
    }
    return stateful, report


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".nw09-export-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            Path(name).unlink()
        except FileNotFoundError:
            pass


def export_stateful_graph(
    graph_path: str | os.PathLike[str],
    output_directory: str | os.PathLike[str],
    *,
    report_path: str | os.PathLike[str] | None = None,
    allow_test_payloads: bool = False,
    packaged_checkpoint_ticks: Sequence[int] = (),
    frames_in_flight: int = 2,
    dxc_executable: str | None = None,
    optimization: str = "O2",
    allow_unpinned_dxc: bool = False,
    cache_dir: str | None = None,
) -> dict[str, Any]:
    graph_path = Path(graph_path)
    output_directory = Path(output_directory)
    document = nw03.load_graph(graph_path)
    package, report = build_stateful_package(
        document,
        allow_test_payloads=allow_test_payloads,
        packaged_checkpoint_ticks=packaged_checkpoint_ticks,
        frames_in_flight=frames_in_flight,
        dxc_executable=dxc_executable,
        optimization=optimization,
        allow_unpinned_dxc=allow_unpinned_dxc,
        cache_dir=cache_dir,
    )
    package_bytes = nwp.encode_package(package)
    valid, diagnostics = nwp.validate_package_bytes(package_bytes)
    if not valid:
        raise StatefulExportError(
            "stateful package bytes did not round-trip", diagnostics
        )
    output_directory.mkdir(parents=True, exist_ok=True)
    package_path = output_directory / "stateful.nwp"
    _write_atomic(package_path, package_bytes)
    report.update(
        {
            "success": True,
            "source_graph": {
                "path": str(graph_path),
                "sha256": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
            },
            "package_path": str(package_path),
            "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
            "package_bytes": len(package_bytes),
            "checkpoint_byte_cost": report["packaged_checkpoint_byte_cost"],
        }
    )
    if report_path is not None:
        _write_atomic(
            Path(report_path),
            json.dumps(report, indent=2, sort_keys=True).encode("utf-8"),
        )
    return report

