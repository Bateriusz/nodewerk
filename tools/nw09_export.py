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
                "// Generated NW-09 particle History compute entry point.",
                "cbuffer NwParams : register(b0) { float4 g_params[4]; };",
                "ByteAddressBuffer nw_previous : register(t0);",
                "RWByteAddressBuffer nw_current : register(u0);",
                f"[numthreads({HISTORY_THREAD_GROUP[0]}, 1, 1)]",
                f"void {HISTORY_ENTRY_POINT}(uint3 dispatch_id : SV_DispatchThreadID) {{",
                f"    if (dispatch_id.x >= {count}u) return;",
                "    uint base = dispatch_id.x * 20u;",
                "    float x = asfloat(nw_previous.Load(base + 0u));",
                "    float y = asfloat(nw_previous.Load(base + 4u));",
                "    float vx = asfloat(nw_previous.Load(base + 8u));",
                "    float vy = asfloat(nw_previous.Load(base + 12u));",
                "    float age = asfloat(nw_previous.Load(base + 16u));",
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
                "// Generated NW-09 feedback-image History compute entry point.",
                "cbuffer NwParams : register(b0) { float4 g_params[4]; };",
                "ByteAddressBuffer nw_previous : register(t0);",
                "RWByteAddressBuffer nw_current : register(u0);",
                f"[numthreads({HISTORY_THREAD_GROUP[0]}, 1, 1)]",
                f"void {HISTORY_ENTRY_POINT}(uint3 dispatch_id : SV_DispatchThreadID) {{",
                f"    uint index = dispatch_id.x;",
                f"    if (index >= {width * height}u) return;",
                "    uint base = index * 16u;",
                "    float4 previous = float4(",
                "        asfloat(nw_previous.Load(base + 0u)),",
                "        asfloat(nw_previous.Load(base + 4u)),",
                "        asfloat(nw_previous.Load(base + 8u)),",
                "        asfloat(nw_previous.Load(base + 12u)));",
                "    uint tick = uint(max(0.0f, floor(g_params[0].x / 400.0f)));",
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


def _history_shaders(
    document: nw03.GraphDocument, specs: Sequence[temporal.HistorySpec]
) -> list[nw03.GeneratedShader]:
    shaders: list[nw03.GeneratedShader] = []
    for spec in specs:
        hlsl = _history_hlsl(document, spec)
        shaders.append(
            nw03.GeneratedShader(
                specialization_id=f"nw09.history.{spec.node_id}",
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
    dxc = dxc_executable
    if dxc is None:
        # Contract tests use the explicit synthetic DXIL container on every
        # host.  Falling back to an installed Windows DXC here would make the
        # same test compile real HLSL on CI and synthetic payloads locally.
        dxc = (
            _test_dxc(destination_path.parent / ".nw09-test-toolchain")
            if allow_test_payloads
            else nw03.resolve_dxc()
        )
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
        generated_shaders_override=_history_shaders(document, specs),
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
    if len(package.shaders) != len(specs):
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
    ordered_shaders = sorted(package.shaders, key=lambda item: item.shader_id)
    shader_id_by_state_id = {
        index: shader.shader_id
        for index, shader in enumerate(ordered_shaders, start=1)
    }
    next_resource = max(
        (resource.resource_id for resource in package.resources), default=0
    ) + 1
    resources = [parameters_resource]
    initialization: list[nwp.Instruction] = []
    parameters = list(package.parameters)
    next_parameter_id = max(
        (parameter.parameter_id for parameter in parameters), default=0
    ) + 1
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
                            "shader_ref", shader_id_by_state_id[index]
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
    present_source_node = ""
    for node in document.nodes.values():
        if node.operator_id != "nw.render.present":
            continue
        connection = node.inputs.get("image")
        if connection is not None:
            present_source_node = connection[0]
            break
    present_state_resource_id = resource_by_node.get(
        present_source_node,
        next(iter(resource_by_node.values())),
    )
    present_width, present_height = geometry or (1, 1)
    present_surface_id = next_resource
    next_resource += 1
    present_surface_bytes = present_width * present_height * 16
    resources.append(
        nwp.ResourceDescriptor(
            resource_id=present_surface_id,
            name="output",
            kind="buffer",
            format="DXGI_FORMAT_R32G32B32A32_FLOAT",
            usage="uav_srv",
            pass_family=nw03.PASS_FAMILY,
            binding_slot=0,
            binding_space=0,
            size_bytes=present_surface_bytes,
        )
    )
    initialization.append(
        nwp.Instruction(
            "init.create_resource",
            (nwp.InstructionOperand("resource_ref", present_surface_id),),
        )
    )
    present_state_record = next(
        record
        for record in history_records
        if record.current_resource_id == present_state_resource_id
    )
    frame.append(
        nwp.Instruction(
            "frame.copy_buffer",
            (
                nwp.InstructionOperand(
                    "resource_ref", present_state_resource_id
                ),
                nwp.InstructionOperand("resource_ref", present_surface_id),
                nwp.InstructionOperand("u64", present_state_record.state_bytes),
            ),
        )
    )
    present_resource_id = present_surface_id
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
            "minor": nwp.VERSION_MINOR_STATEFUL,
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
        "present_state_node": present_source_node,
        "present_state_resource_id": present_state_resource_id,
        "present_resource_id": present_resource_id,
        "present_surface": {
            "resource_id": present_surface_id,
            "width": present_width,
            "height": present_height,
            "format": "DXGI_FORMAT_R32G32B32A32_FLOAT",
            "size_bytes": present_surface_bytes,
        },
        "frames_in_flight": frames_in_flight,
        "pending_retirement_bytes": sum(
            report["state_bytes"] * frames_in_flight for report in reports
        ),
        "packaged_checkpoints": checkpoint_reports,
        "packaged_checkpoint_byte_cost": sum(
            item["byte_cost"] for item in checkpoint_reports
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

