#!/usr/bin/env python3
"""NW-05 whole-graph typed lowering and optimisation.

This module extends the NW-02 typed graph and the NW-03 expression lowering
without introducing a second graph format or a runtime graph interpreter.  It
consumes the established ``nw03_compiler.GraphDocument`` produced from
``.nwgraph`` v1 text and produces a deterministic, textual *optimised IR*:

* whole-graph typed lowering from reachable semantic outputs;
* constant propagation for the typed CPU operators and ShaderExpr ASTs;
* dead-node and dead-output elimination;
* common-subexpression elimination for pure typed nodes;
* compile-time / initialization / frame partitioning;
* bounded, correctness-checked expression fusion;
* field-specific, tolerance-bounded, reversible parameter quantization.

The optimised IR is compiler-side data.  It is never linked into the player and
does not redefine the `.nwp` v1 package or the NW-02 graph semantics.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import nw03_compiler as nw03
import nw03_oracle as oracle
import nwp_package as nwp


IR_FORMAT = "nw05-optimized-ir"
IR_VERSION = 1
COMMON_PROFILE_ID = nwp.COMMON_PROFILE_ID
COMMON_REQUIREMENTS_DIGEST = nwp.COMMON_REQUIREMENTS_DIGEST
DEFAULT_FUSION_MAX_NODES = 64
DEFAULT_FUSION_MAX_EXPRESSION_BYTES = 65536
QUANTIZATION_TOLERANCE = 0.0

FREQUENCY_RANK = {"compile-time": 0, "initialization": 1, "frame": 2}

# Compiler/authoring-only implementations that are known but are deliberately
# excluded from a generated player link closure unless the graph actually uses
# them.  The list is reported in every link manifest so an absent optional
# implementation is explicit rather than accidental.
OPTIONAL_IMPLEMENTATIONS = (
    "nw.proc.hash_noise",
    "nw.proc.value_noise",
    "nw.proc.checker",
    "nw.proc.palette",
    "nw.curve.keyframes",
    "nw.curve.sample",
    "nw.scene.flat",
    "nw.camera.perspective",
    "nw.light.directional",
    "nw.material.describe",
    "nw.render.gbuffer",
    "nw.render.composite",
)

OPTIONAL_PROFILES = {
    nwp.OPTIONAL_PROFILE_WAVE_OPS: nwp.OPTIONAL_PROFILE_WAVE_OPS_DIGEST,
}

PURE_OPERATORS = frozenset(
    {
        "nw.math.scalar_const",
        "nw.math.vec2_const",
        "nw.math.vec3_const",
        "nw.math.vec4_const",
        "nw.math.mat4_const",
        "nw.math.add",
        "nw.math.mul",
        "nw.math.sin",
        "nw.math.clamp01",
        "nw.math.vec4_scale",
        "nw.math.vec4_split",
        "nw.math.dot",
        "nw.control.smoothstep",
        "nw.control.seed",
        "nw.control.image_cpu_passthrough",
        "nw.proc.hash_noise",
        "nw.proc.value_noise",
        "nw.proc.checker",
        "nw.proc.palette",
        "nw.curve.keyframes",
        "nw.curve.sample",
        "nw.scene.flat",
        "nw.camera.perspective",
        "nw.light.directional",
        "nw.material.describe",
        "nw.shader.vec4_const",
        "nw.shader.expr",
        "nw.shader.add",
        "nw.shader.mul",
        "nw.shader.mix",
    }
)

SEMANTIC_OUTPUT_OPERATORS = frozenset(
    {
        "nw.render.present",
        "nw.render.fullscreen",
        "nw.render.gbuffer",
        "nw.render.composite",
        "nw.readback.scalar",
    }
)


class OptimizationError(RuntimeError):
    """Raised when the typed graph cannot be lowered into a valid IR."""

    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


# ---------------------------------------------------------------------------
# Typed values and optimised IR records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypedValue:
    logical_type: str
    dimension: int = 1
    residency: str = "cpu"

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_type": self.logical_type,
            "dimension": self.dimension,
            "residency": self.residency,
        }


@dataclass(frozen=True)
class IRInput:
    port: str
    source_node: str
    source_port: str
    value: TypedValue | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "port": self.port,
            "source": f"{self.source_node}.{self.source_port}",
        }
        if self.value is not None:
            result["value"] = self.value.to_dict()
        return result


@dataclass
class IRNode:
    node_id: str
    operator_id: str
    operator_version: str
    frequency: str
    residency: str
    params: dict[str, tuple[str, ...]]
    inputs: tuple[IRInput, ...]
    outputs: dict[str, TypedValue]
    constant: dict[str, tuple[float, ...]] | None = None
    source_node_ids: tuple[str, ...] = ()
    cse_key: str = ""
    raw_params: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def operator_key(self) -> str:
        return f"{self.operator_id}@{self.operator_version}"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.node_id,
            "operator": self.operator_key,
            "frequency": self.frequency,
            "residency": self.residency,
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": {
                port: value.to_dict() for port, value in sorted(self.outputs.items())
            },
            "params": {name: list(values) for name, values in sorted(self.params.items())},
            "source_node_ids": list(self.source_node_ids),
        }
        if self.constant is not None:
            result["constant"] = {
                port: list(values) for port, values in sorted(self.constant.items())
            }
        if self.cse_key:
            result["cse_key"] = self.cse_key
        return result


@dataclass(frozen=True)
class GraphOutput:
    node_id: str
    port: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"node": self.node_id, "port": self.port, "kind": self.kind}


@dataclass(frozen=True)
class CseRecord:
    canonical_node: str
    merged_nodes: tuple[str, ...]
    cse_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_node": self.canonical_node,
            "merged_nodes": list(self.merged_nodes),
            "cse_key": self.cse_key,
        }


@dataclass(frozen=True)
class FusionRecord:
    fusion_id: str
    root_node: str
    member_nodes: tuple[str, ...]
    expression_bytes_before: int
    expression_bytes_after: int
    estimated_instructions_before: int
    estimated_instructions_after: int
    correctness_preserved: bool
    measured: bool
    accepted: bool
    reason: str
    fused_expression: str = ""
    unfused_fallback: str = "nw05-multi-dispatch-uav"
    measured_hlsl_bytes_before: int = 0
    measured_hlsl_bytes_after: int = 0
    measured_runtime_ns_before: int = 0
    measured_runtime_ns_after: int = 0
    measurement_source: str = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fusion_id": self.fusion_id,
            "root_node": self.root_node,
            "member_nodes": list(self.member_nodes),
            "node_count": len(self.member_nodes),
            "expression_bytes_before": self.expression_bytes_before,
            "expression_bytes_after": self.expression_bytes_after,
            "estimated_instructions_before": self.estimated_instructions_before,
            "estimated_instructions_after": self.estimated_instructions_after,
            "correctness_preserved": self.correctness_preserved,
            "measured": self.measured,
            "accepted": self.accepted,
            "reason": self.reason,
            "fused_expression": self.fused_expression,
            "unfused_fallback": self.unfused_fallback,
            "measured_hlsl_bytes_before": self.measured_hlsl_bytes_before,
            "measured_hlsl_bytes_after": self.measured_hlsl_bytes_after,
            "measured_runtime_ns_before": self.measured_runtime_ns_before,
            "measured_runtime_ns_after": self.measured_runtime_ns_after,
            "measurement_source": self.measurement_source,
        }


@dataclass(frozen=True)
class QuantizationDecision:
    node_id: str
    operator_id: str
    parameter: str
    policy: str
    bits: int
    step: float
    tolerance: float
    original: tuple[float, ...]
    quantized: tuple[float, ...]
    max_absolute_error: float
    within_tolerance: bool
    reversible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "operator_id": self.operator_id,
            "parameter": self.parameter,
            "policy": self.policy,
            "bits": self.bits,
            "step": self.step,
            "tolerance": self.tolerance,
            "original": list(self.original),
            "quantized": list(self.quantized),
            "max_absolute_error": self.max_absolute_error,
            "within_tolerance": self.within_tolerance,
            "reversible_to_full_precision": self.reversible,
        }


@dataclass
class QuantizedGraph:
    document: nw03.GraphDocument
    decisions: tuple[QuantizationDecision, ...] = ()
    errors: tuple[str, ...] = ()

    def recover_full_precision(self) -> dict[str, tuple[float, ...]]:
        return {
            f"{decision.node_id}.{decision.parameter}": decision.original
            for decision in self.decisions
        }


def _folded_constant_node(
    source_node: str,
    source_port: str,
    values: tuple[float, ...],
) -> nw03.GraphNode:
    width = len(values)
    operator_id = {
        1: "nw.math.scalar_const",
        2: "nw.math.vec2_const",
        3: "nw.math.vec3_const",
        4: "nw.math.vec4_const",
        16: "nw.math.mat4_const",
    }.get(width)
    if operator_id is None:
        raise OptimizationError(
            "folded constant has an unsupported width",
            [
                nw03._diagnostic(
                    "materialize_folded_width",
                    f"folded '{source_node}.{source_port}' has width {width}",
                    node_id=source_node,
                    port_id=source_port,
                )
            ],
        )
    safe = "".join(
        character if character.isalnum() else "_"
        for character in f"{source_node}_{source_port}"
    )
    return nw03.GraphNode(
        node_id=f"__nw05_folded_{safe}",
        operator_id=operator_id,
        version="1.0.0",
        frequency="compile-time",
        residency="cpu",
        params={"value": [_float_text(value) for value in values]},
        inputs={},
        selected_outputs=("out",),
    )


@dataclass
class OptimizedGraph:
    profile_id: str
    requirements_digest: str
    schema_version: int
    settings: dict[str, str]
    nodes: tuple[IRNode, ...]
    outputs: tuple[GraphOutput, ...]
    partitions: dict[str, tuple[str, ...]]
    dead_nodes: tuple[str, ...]
    dead_outputs: tuple[tuple[str, str], ...]
    cse: tuple[CseRecord, ...]
    constants_folded: tuple[str, ...]
    fusions: tuple[FusionRecord, ...]
    quantization: tuple[QuantizationDecision, ...]
    source_sha256: str
    source_name: str
    source_text: str = ""
    # Values of nodes eliminated by constant propagation.  Materialising the
    # optimised graph must not leave consumers pointing at those absent nodes,
    # so the values are carried here and emitted as literal constant nodes.
    folded_constants: dict[str, dict[str, tuple[float, ...]]] = field(
        default_factory=dict
    )

    @property
    def reachable_node_ids(self) -> tuple[str, ...]:
        return tuple(sorted(node.node_id for node in self.nodes))

    @property
    def shader_node_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                node.node_id
                for node in self.nodes
                if node.operator_id in nw03.SHADER_OPERATORS
            )
        )

    def node_map(self) -> dict[str, IRNode]:
        return {node.node_id: node for node in self.nodes}

    def materialize_document(
        self,
        *,
        generation_modes: dict[str, str] | None = None,
        apply_accepted_fusions: bool = True,
    ) -> nw03.GraphDocument:
        """Materialize the optimized IR back into the existing NW-03 document.

        This is the bridge that makes optimisation observable in the emitted
        ``.nwp`` bytes: dead nodes are absent, CSE inputs point at canonical
        nodes, accepted fusion roots are emitted as their folded expression,
        quantized parameters carry their quantized values, and explicit
        generation/bake decisions are part of the compiler settings identity.
        It is not a second graph model; the resulting document is consumed by
        the established NW-03 compiler.
        """
        accepted_fusions = (
            {
                record.root_node: record
                for record in self.fusions
                if record.accepted and record.fused_expression
            }
            if apply_accepted_fusions
            else {}
        )
        nodes: dict[str, nw03.GraphNode] = {}
        member_ids: set[str] = set()
        for ir_node in self.nodes:
            params = {name: list(values) for name, values in ir_node.params.items()}
            for name in ("expression", "keys", "interpolation", "entries", "format"):
                if name in ir_node.raw_params:
                    params[name] = list(ir_node.raw_params[name])
            inputs = {
                item.port: (item.source_node, item.source_port)
                for item in ir_node.inputs
            }
            node = nw03.GraphNode(
                node_id=ir_node.node_id,
                operator_id=ir_node.operator_id,
                version=ir_node.operator_version,
                frequency=ir_node.frequency,
                residency=ir_node.residency,
                params=params,
                inputs=inputs,
                selected_outputs=tuple(sorted(ir_node.outputs)),
            )
            fusion = accepted_fusions.get(ir_node.node_id)
            if fusion is not None and (
                ir_node.operator_id in nw03.SHADER_OPERATORS
                or ir_node.operator_id == "nw.shader.expr"
            ):
                # The established compiler accepts the folded expression as an
                # ordinary ShaderExpr.  Unsupported/disabled fusion leaves the
                # original unfused node untouched.
                node.operator_id = "nw.shader.expr"
                node.params = {"expression": [fusion.fused_expression]}
                node.inputs = {}
                member_ids.update(
                    member
                    for member in fusion.member_nodes
                    if member != ir_node.node_id
                )
            nodes[ir_node.node_id] = node

        # Constant propagation removes folded producer nodes from the
        # optimised IR.  Re-materialise every still-referenced folded value as
        # an explicit literal constant node so the NW-03 document never
        # contains a dangling input reference.
        folded_nodes: dict[str, nw03.GraphNode] = {}
        for node in list(nodes.values()):
            rewritten_inputs: dict[str, tuple[str, str]] = {}
            for port, (source_node, source_port) in sorted(node.inputs.items()):
                if source_node in nodes:
                    rewritten_inputs[port] = (source_node, source_port)
                    continue
                values = self.folded_constants.get(source_node, {}).get(
                    source_port
                )
                if values is None:
                    raise OptimizationError(
                        "materialised graph references an eliminated node",
                        [
                            nw03._diagnostic(
                                "materialize_dangling_input",
                                f"node '{node.node_id}' input '{port}' "
                                f"references eliminated '{source_node}.{source_port}'",
                                node_id=node.node_id,
                                port_id=port,
                            )
                        ],
                    )
                folded_node = _folded_constant_node(source_node, source_port, values)
                folded_nodes[folded_node.node_id] = folded_node
                rewritten_inputs[port] = (folded_node.node_id, "out")
            node.inputs = rewritten_inputs
        nodes.update(folded_nodes)

        # Remove fused member shader nodes only when nothing else still
        # consumes them (a conservative unfused fallback is preserved
        # otherwise).
        consumers: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for node in nodes.values():
            for source_node, _source_port in node.inputs.values():
                if source_node in consumers:
                    consumers[source_node].append(node.node_id)
        for member in sorted(member_ids):
            if member in nodes and not consumers.get(member):
                del nodes[member]

        settings = dict(self.settings)
        ir_by_id = {node.node_id: node for node in self.nodes}
        baked_nodes: set[str] = set()
        constant_operator = {
            1: "nw.math.scalar_const",
            2: "nw.math.vec2_const",
            3: "nw.math.vec3_const",
            4: "nw.math.vec4_const",
        }
        for node_id, mode in sorted((generation_modes or {}).items()):
            if mode != "bake" or node_id not in nodes:
                continue
            node = nodes[node_id]
            ir_node = ir_by_id.get(node_id)
            if node.operator_id in nw03.SHADER_OPERATORS or ir_node is None:
                continue
            values = None
            if ir_node.constant and len(ir_node.constant) == 1:
                values = next(iter(ir_node.constant.values()), None)
            if values is not None and 1 <= len(values) <= 4:
                # Replace the generating operator with the baked literal while
                # keeping the same node identity, so consumers remain wired.
                node.operator_id = constant_operator[len(values)]
                node.params = {
                    "value": [_float_text(value) for value in values]
                }
                node.inputs = {}
                continue
            if not consumers.get(node_id):
                baked_nodes.add(node_id)
        for node_id in sorted(baked_nodes):
            # A supported bake replaces generation for this node.  Its payload
            # is emitted as package parameter content by the exporter; keeping
            # the node here would still generate the code bake was meant to
            # replace.
            del nodes[node_id]
        for decision in self.quantization:
            settings[
                f"nw05.quantization.{decision.node_id}.{decision.parameter}"
            ] = " ".join(_float_text(value) for value in decision.quantized)
        for ir_node in self.nodes:
            full_ports = set(_output_specs(ir_node.operator_id))
            retained_ports = set(ir_node.outputs)
            if retained_ports != full_ports:
                settings[f"nw05.outputs.{ir_node.node_id}"] = ",".join(
                    sorted(retained_ports)
                )
        for node_id, mode in sorted((generation_modes or {}).items()):
            settings[f"nw05.generation.{node_id}.mode"] = mode
        return nw03.GraphDocument(
            schema_version=self.schema_version,
            profile_id=self.profile_id,
            requirements_digest=self.requirements_digest,
            settings=settings,
            nodes=nodes,
            source_name=self.source_name,
            source_text=self.source_text,
        )

    def ir_sha256(self) -> str:
        return "sha256:v1:" + _sha256(
            nwp.canonical_json_bytes(self._semantic_payload())
        )

    def materialized_semantic_digest(
        self, generation_modes: dict[str, str] | None = None
    ) -> str:
        """Identity of the *emitted* semantics, including lowering choices.

        The base IR digest intentionally excludes diagnostic fusion records and
        export-only generation choices.  The package semantic identity must not:
        accepted fusion changes emitted HLSL, and bake/generate overrides change
        emitted package content.  Both are folded into this digest.
        """
        payload = self._semantic_payload()
        payload["accepted_fusions"] = [
            {
                "root": record.root_node,
                "members": list(record.member_nodes),
                "expression": record.fused_expression,
            }
            for record in self.fusions
            if record.accepted
        ]
        payload["generation_modes"] = dict(
            sorted((generation_modes or {}).items())
        )
        return "sha256:v1:" + _sha256(
            nwp.canonical_json_bytes(payload)
        )

    def _semantic_payload(self) -> dict[str, Any]:
        """Retained semantic release content.

        Optimisation diagnostics (dead nodes, CSE bookkeeping, fusion and
        generation decisions, and the raw source hash) are deliberately
        excluded so adding an unreachable operator or unused output cannot
        change the generated player link closure.
        """
        return {
            "format": IR_FORMAT,
            "version": IR_VERSION,
            "profile_id": self.profile_id,
            "requirements_digest": self.requirements_digest,
            "schema_version": self.schema_version,
            "settings": dict(sorted(self.settings.items())),
            "nodes": [
                {
                    key: value
                    for key, value in node.to_dict().items()
                    if key not in {"source_node_ids", "cse_key"}
                }
                for node in sorted(self.nodes, key=lambda n: n.node_id)
            ],
            "outputs": [output.to_dict() for output in self.outputs],
            "partitions": {
                name: list(ids) for name, ids in sorted(self.partitions.items())
            },
            "quantization": [item.to_dict() for item in self.quantization],
        }

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "format": IR_FORMAT,
            "version": IR_VERSION,
            "profile_id": self.profile_id,
            "requirements_digest": self.requirements_digest,
            "schema_version": self.schema_version,
            "settings": dict(sorted(self.settings.items())),
            "nodes": [node.to_dict() for node in sorted(self.nodes, key=lambda n: n.node_id)],
            "outputs": [output.to_dict() for output in self.outputs],
            "partitions": {
                name: list(ids) for name, ids in sorted(self.partitions.items())
            },
            "dead_nodes": list(self.dead_nodes),
            "dead_outputs": [list(item) for item in self.dead_outputs],
            "cse": [item.to_dict() for item in self.cse],
            "constants_folded": list(self.constants_folded),
            "fusions": [item.to_dict() for item in self.fusions],
            "quantization": [item.to_dict() for item in self.quantization],
            "source_sha256": self.source_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._canonical_payload()
        payload["ir_sha256"] = self.ir_sha256()
        payload["source_name"] = self.source_name
        payload["reachable_node_ids"] = list(self.reachable_node_ids)
        payload["shader_node_ids"] = list(self.shader_node_ids)
        return payload

    def to_text(self) -> str:
        lines = [
            f"{IR_FORMAT} {IR_VERSION}",
            f"profile {self.profile_id} {self.requirements_digest}",
        ]
        for key in sorted(self.settings):
            lines.append(f"setting {key} {self.settings[key]}")
        for node in sorted(self.nodes, key=lambda item: item.node_id):
            attributes = [f"frequency={node.frequency}", f"residency={node.residency}"]
            lines.append(f"node {node.node_id} {node.operator_key} " + " ".join(attributes))
            for name, values in sorted(node.params.items()):
                if name in {"expression", "keys", "interpolation", "entries", "format"}:
                    quoted = " ".join(values).replace("\\", "\\\\").replace('"', '\\"')
                    lines.append(f'  param {name} "{quoted}"')
                else:
                    lines.append(f"  param {name} {' '.join(values)}")
            for value in node.inputs:
                lines.append(f"  input {value.port} {value.source_node}.{value.source_port}")
            for port, values in sorted((node.constant or {}).items()):
                lines.append(
                    f"  const {port} {' '.join(_float_text(item) for item in values)}"
                )
        for output in self.outputs:
            lines.append(f"output {output.node_id}.{output.port} {output.kind}")
        for partition in ("compile-time", "initialization", "frame"):
            values = self.partitions.get(partition, ())
            lines.append(f"partition {partition} {' '.join(values)}")
        lines.append(f"dead-nodes {' '.join(self.dead_nodes)}")
        lines.append(
            "dead-outputs " + " ".join(f"{node}.{port}" for node, port in self.dead_outputs)
        )
        for record in self.cse:
            lines.append(
                f"cse {record.canonical_node} {' '.join(record.merged_nodes)}"
            )
        lines.append(f"folded {' '.join(self.constants_folded)}")
        for fusion in self.fusions:
            lines.append(
                "fusion "
                f"{fusion.fusion_id} accepted={'true' if fusion.accepted else 'false'} "
                f"nodes={len(fusion.member_nodes)} "
                f"bytes={fusion.expression_bytes_before}->{fusion.expression_bytes_after}"
            )
        for decision in self.quantization:
            lines.append(
                "quantization "
                f"{decision.node_id}.{decision.parameter} "
                f"policy={decision.policy} bits={decision.bits} step={_float_text(decision.step)} "
                f"error={_float_text(decision.max_absolute_error)} "
                f"tolerance={_float_text(decision.tolerance)}"
            )
        lines.append(f"source-sha256 {self.source_sha256}")
        lines.append(f"ir-sha256 {self.ir_sha256()}")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _float_text(value: float) -> str:
    if value == 0.0:
        value = 0.0
    text = format(value, ".17g")
    if "." not in text and "e" not in text and "E" not in text:
        text += ".0"
    return text


def _token_values(tokens: Sequence[str]) -> tuple[float, ...] | None:
    values: list[float] = []
    for token in tokens:
        try:
            value = float(token)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        values.append(0.0 if value == 0.0 else value)
    return tuple(values)


def _normalised_params(params: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for name, values in params.items():
        parts: list[str] = []
        for token in values:
            parts.extend(part for part in re.split(r"[,\s]+", token) if part)
        normalised = []
        for part in parts:
            try:
                value = float(part)
            except ValueError:
                normalised.append(part)
                continue
            normalised.append(_float_text(0.0 if value == 0.0 else value))
        result[name] = tuple(normalised)
    return result


def _param_float(params: dict[str, tuple[str, ...]], name: str) -> float | None:
    values = params.get(name)
    if not values:
        return None
    try:
        return float(values[0])
    except ValueError:
        return None


def _param_floats(
    params: dict[str, tuple[str, ...]], name: str
) -> tuple[float, ...] | None:
    values = params.get(name)
    if values is None:
        return None
    return _token_values(values)


def _output_specs(operator_id: str) -> dict[str, TypedValue]:
    specs = nw03.PORT_SPECS.get(operator_id, {}).get("outputs", {})
    return {
        port: TypedValue(logical_type, dimension, residency)
        for port, (logical_type, dimension, residency) in specs.items()
    }


def _input_specs(operator_id: str) -> dict[str, TypedValue]:
    specs = nw03.PORT_SPECS.get(operator_id, {}).get("inputs", {})
    return {
        port: TypedValue(logical_type, dimension, residency)
        for port, (logical_type, dimension, residency) in specs.items()
    }


def _structural_key(
    operator_id: str,
    operator_version: str,
    frequency: str,
    params: dict[str, tuple[str, ...]],
    inputs: Sequence[IRInput],
) -> str:
    payload = {
        "operator": f"{operator_id}@{operator_version}",
        "frequency": frequency,
        "params": {name: list(values) for name, values in sorted(params.items())},
        "inputs": [
            {
                "port": item.port,
                "source_node": item.source_node,
                "source_port": item.source_port,
            }
            for item in sorted(inputs, key=lambda entry: entry.port)
        ],
    }
    return "sha256:v1:" + _sha256(nwp.canonical_json_bytes(payload))


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


def _node_consumers(document: nw03.GraphDocument) -> dict[str, list[str]]:
    consumers: dict[str, list[str]] = {node_id: [] for node_id in document.nodes}
    for node in document.nodes.values():
        for source_node, _source_port in node.inputs.values():
            if source_node in consumers:
                consumers[source_node].append(node.node_id)
    for values in consumers.values():
        values.sort()
    return consumers


def semantic_outputs(document: nw03.GraphDocument) -> list[GraphOutput]:
    """Return reachable external outputs using NW-02/NW-03 graph semantics."""
    consumers = _node_consumers(document)
    present = [
        node
        for node in sorted(document.nodes.values(), key=lambda item: item.node_id)
        if node.operator_id == "nw.render.present"
    ]
    if present:
        return [GraphOutput(node.node_id, "out", "present") for node in present]

    render_nodes: list[nw03.GraphNode] = []
    for node in sorted(document.nodes.values(), key=lambda item: item.node_id):
        if not node.operator_id.startswith("nw.render."):
            continue
        if consumers.get(node.node_id):
            continue
        if node.operator_id == "nw.render.fullscreen":
            render_nodes.append(node)
        elif node.operator_id == "nw.render.gbuffer":
            render_nodes.extend(
                [node, node]
            )
    if render_nodes:
        outputs: list[GraphOutput] = []
        for node in render_nodes:
            ports = list(_output_specs(node.operator_id))
            for port in ports:
                outputs.append(GraphOutput(node.node_id, port, "render"))
        return _dedupe_outputs(outputs)

    readbacks = [
        node
        for node in sorted(document.nodes.values(), key=lambda item: item.node_id)
        if node.operator_id == "nw.readback.scalar" and not consumers.get(node.node_id)
    ]
    if readbacks:
        return [GraphOutput(node.node_id, "value", "readback") for node in readbacks]

    roots = nw03._reachable_shader_roots(document)
    if roots:
        return [GraphOutput(node.node_id, "out", "shader") for node in roots]

    # Last resort: terminal typed outputs so a graph with no render path still
    # gets DCE rather than silently emitting nothing.
    outputs = []
    for node in sorted(document.nodes.values(), key=lambda item: item.node_id):
        if consumers.get(node.node_id):
            continue
        for port in _output_specs(node.operator_id):
            outputs.append(GraphOutput(node.node_id, port, "terminal"))
    return _dedupe_outputs(outputs)


def _dedupe_outputs(outputs: Iterable[GraphOutput]) -> list[GraphOutput]:
    seen: set[tuple[str, str]] = set()
    result: list[GraphOutput] = []
    for output in outputs:
        key = (output.node_id, output.port)
        if key in seen:
            continue
        seen.add(key)
        result.append(output)
    return result


def reachable_node_ports(
    document: nw03.GraphDocument, outputs: Sequence[GraphOutput]
) -> tuple[set[str], dict[str, set[str]]]:
    """Return reachable nodes and the output ports actually consumed."""
    needed_ports: dict[str, set[str]] = {}
    reachable: set[str] = set()

    def visit(node_id: str) -> None:
        node = document.nodes.get(node_id)
        if node is None:
            return
        for source_node, source_port in node.inputs.values():
            needed_ports.setdefault(source_node, set()).add(source_port)
        if node_id in reachable:
            return
        reachable.add(node_id)
        for source_node, _source_port in node.inputs.values():
            visit(source_node)

    for output in outputs:
        needed_ports.setdefault(output.node_id, set()).add(output.port)
        visit(output.node_id)
    return reachable, needed_ports


# ---------------------------------------------------------------------------
# Constant propagation
# ---------------------------------------------------------------------------


def _evaluate_cpu_node(
    node: nw03.GraphNode,
    value_for: Callable[[str, str], tuple[float, ...] | None],
) -> dict[str, tuple[float, ...]] | None:
    """Evaluate a typed CPU node when every required input is constant."""
    params = node.params

    def input_value(port: str) -> tuple[float, ...] | None:
        connection = node.inputs.get(port)
        if connection is None:
            return None
        source_node, source_port = connection
        return value_for(source_node, source_port)

    def scalar_input(port: str) -> float | None:
        value = input_value(port)
        return None if value is None or len(value) != 1 else value[0]

    def vector_input(port: str) -> tuple[float, ...] | None:
        return input_value(port)

    operator_id = node.operator_id
    try:
        if operator_id == "nw.math.scalar_const":
            values = _param_floats(_normalised_params(params), "value")
            return None if values is None or len(values) != 1 else {"out": values}
        if operator_id in {"nw.math.vec2_const", "nw.math.vec3_const", "nw.math.vec4_const"}:
            expected = {"nw.math.vec2_const": 2, "nw.math.vec3_const": 3, "nw.math.vec4_const": 4}[operator_id]
            values = _param_floats(_normalised_params(params), "value")
            return (
                None
                if values is None or len(values) != expected
                else {"out": values}
            )
        if operator_id == "nw.math.mat4_const":
            values = _param_floats(_normalised_params(params), "value")
            if values is None:
                tokens = params.get("value", [])
                if tokens and tokens[0] == "identity":
                    return {"out": tuple(float(value) for value in (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1))}
                return None
            return {"out": values} if len(values) == 16 else None
        if operator_id in {"nw.math.add", "nw.math.mul"}:
            a = scalar_input("a")
            b = scalar_input("b")
            if a is None or b is None:
                return None
            value = a + b if operator_id == "nw.math.add" else a * b
            return {"out": (value,)}
        if operator_id == "nw.math.sin":
            x = scalar_input("x")
            return None if x is None else {"out": (math.sin(x),)}
        if operator_id == "nw.math.clamp01":
            x = scalar_input("x")
            return None if x is None else {"out": (min(max(x, 0.0), 1.0),)}
        if operator_id == "nw.math.vec4_scale":
            vector = vector_input("v")
            scale = scalar_input("s")
            if vector is None or scale is None or len(vector) != 4:
                return None
            return {"out": tuple(component * scale for component in vector)}
        if operator_id == "nw.math.vec4_split":
            vector = vector_input("v")
            if vector is None or len(vector) != 4:
                return None
            return {port: (vector[index],) for index, port in enumerate("xyzw")}
        if operator_id == "nw.math.dot":
            a = vector_input("a")
            b = vector_input("b")
            if a is None or b is None or len(a) != len(b):
                return None
            return {"out": (sum(x * y for x, y in zip(a, b)),)}
        if operator_id == "nw.control.smoothstep":
            edge0 = scalar_input("edge0")
            edge1 = scalar_input("edge1")
            x = scalar_input("x")
            if edge0 is None or edge1 is None or x is None:
                return None
            if edge1 == edge0:
                value = 0.0
            else:
                t = min(max((x - edge0) / (edge1 - edge0), 0.0), 1.0)
                value = t * t * (3.0 - 2.0 * t)
            return {"out": (value,)}
        if operator_id == "nw.control.seed":
            seed = node.params.get("__seed__", [])
            if seed and seed[0] == "explicit" and len(seed) > 1:
                try:
                    return {"out": (float(int(seed[1], 0)),)}
                except ValueError:
                    return None
            seed_param = _param_float(_normalised_params(params), "value")
            return None if seed_param is None else {"out": (seed_param,)}
        if operator_id == "nw.proc.hash_noise":
            seed = scalar_input("seed")
            if seed is None:
                return None
            value = math.sin(seed * 12.9898 + 78.233) * 43758.5453
            return {"out": (value - math.floor(value),)}
        if operator_id == "nw.proc.value_noise":
            position = vector_input("position")
            if position is None:
                return None
            value = sum(
                math.sin(component * (index + 1) * 12.9898)
                for index, component in enumerate(position)
            )
            return {"out": (0.5 + 0.5 * value / max(1, len(position)),)}
        if operator_id == "nw.proc.checker":
            uv = vector_input("uv")
            scale = scalar_input("scale")
            if uv is None or scale is None or len(uv) != 2:
                return None
            value = int(math.floor(uv[0] * scale)) + int(math.floor(uv[1] * scale))
            return {"out": (1.0 if value % 2 == 0 else 0.0,)}
        if operator_id == "nw.proc.palette":
            t = scalar_input("t")
            if t is None:
                return None
            return {"out": (0.5 + 0.5 * math.sin(t * 6.283185307179586),
                            0.5 + 0.5 * math.sin(t * 6.283185307179586 + 2.0943951023931953),
                            0.5 + 0.5 * math.sin(t * 6.283185307179586 + 4.1887902047863905))}
        if operator_id == "nw.shader.vec4_const":
            values = _param_floats(_normalised_params(params), "value")
            if values is None:
                return None
            values = values[:4]
            if len(values) < 4:
                values = values + (0.0,) * (4 - len(values))
            return {"out": values}
    except (ValueError, OverflowError, ZeroDivisionError):
        return None
    return None


def constant_propagation(
    document: nw03.GraphDocument,
    reachable: set[str],
) -> tuple[dict[str, dict[str, tuple[float, ...]]], set[str]]:
    """Fold reachable typed nodes to constants and return folded identities."""
    constants: dict[str, dict[str, tuple[float, ...]]] = {}
    folded: set[str] = set()
    order = _topological_ids(document, reachable)

    def value_for(source_node: str, source_port: str) -> tuple[float, ...] | None:
        values = constants.get(source_node)
        return None if values is None else values.get(source_port)

    for node_id in order:
        node = document.nodes[node_id]
        result = _evaluate_cpu_node(node, value_for)
        if result is None:
            continue
        constants[node_id] = result
        if node.operator_id not in nw03.SHADER_OPERATORS:
            folded.add(node_id)
    return constants, folded


def _topological_ids(
    document: nw03.GraphDocument, subset: set[str] | None = None
) -> list[str]:
    subset = set(document.nodes) if subset is None else subset
    result: list[str] = []
    state: dict[str, int] = {}

    def visit(node_id: str) -> None:
        if node_id not in subset:
            return
        current = state.get(node_id, 0)
        if current == 2:
            return
        if current == 1:
            raise OptimizationError(
                "ordinary cycle detected during typed lowering",
                [
                    nw03._diagnostic(
                        "ordinary_cycle",
                        f"cycle detected at node '{node_id}'",
                        node_id=node_id,
                    )
                ],
            )
        state[node_id] = 1
        node = document.nodes.get(node_id)
        if node is not None:
            for source_node, _source_port in sorted(node.inputs.values()):
                visit(source_node)
        state[node_id] = 2
        result.append(node_id)

    for node_id in sorted(subset):
        visit(node_id)
    return result


# ---------------------------------------------------------------------------
# ShaderExpr AST constant folding and CSE analysis
# ---------------------------------------------------------------------------


def _expression_has_variable(expression: oracle.Expression) -> bool:
    if isinstance(expression, oracle.Variable):
        return True
    if isinstance(expression, oracle.Member):
        return _expression_has_variable(expression.base)
    if isinstance(expression, oracle.Unary):
        return _expression_has_variable(expression.operand)
    if isinstance(expression, oracle.Binary):
        return _expression_has_variable(expression.left) or _expression_has_variable(
            expression.right
        )
    if isinstance(expression, oracle.Call):
        return any(_expression_has_variable(item) for item in expression.arguments)
    return False


def _literal_from_value(value: float | tuple[float, ...]) -> oracle.Expression:
    if isinstance(value, tuple):
        if len(value) not in (2, 3, 4):
            raise oracle.ExpressionError("constant vector has unsupported width")
        return oracle.Call(
            f"vec{len(value)}",
            tuple(oracle.Literal(float(component)) for component in value),
        )
    return oracle.Literal(float(value))


def fold_expression(expression: oracle.Expression) -> tuple[oracle.Expression, int]:
    """Fold constant subtrees of a ShaderExpr AST."""
    if isinstance(expression, (oracle.Literal, oracle.Variable)):
        return expression, 0
    if isinstance(expression, oracle.Member):
        base, count = fold_expression(expression.base)
        folded = oracle.Member(base, expression.selector)
        if _expression_has_variable(folded):
            return folded, count
        try:
            return _literal_from_value(oracle.evaluate(folded)), count + 1
        except oracle.ExpressionError:
            return folded, count
    if isinstance(expression, oracle.Unary):
        operand, count = fold_expression(expression.operand)
        folded = oracle.Unary(expression.operator, operand)
        if _expression_has_variable(folded):
            return folded, count
        try:
            return _literal_from_value(oracle.evaluate(folded)), count + 1
        except oracle.ExpressionError:
            return folded, count
    if isinstance(expression, oracle.Binary):
        left, left_count = fold_expression(expression.left)
        right, right_count = fold_expression(expression.right)
        folded = oracle.Binary(expression.operator, left, right)
        count = left_count + right_count
        if _expression_has_variable(folded):
            return folded, count
        try:
            return _literal_from_value(oracle.evaluate(folded)), count + 1
        except oracle.ExpressionError:
            return folded, count
    assert isinstance(expression, oracle.Call)
    arguments = []
    count = 0
    for argument in expression.arguments:
        folded, folded_count = fold_expression(argument)
        arguments.append(folded)
        count += folded_count
    folded = oracle.Call(expression.name, tuple(arguments))
    if _expression_has_variable(folded) or expression.name in {
        "texture2d",
        "sample",
    }:
        return folded, count
    try:
        return _literal_from_value(oracle.evaluate(folded)), count + 1
    except oracle.ExpressionError:
        return folded, count


_BINARY_PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2}


def expression_to_text(expression: oracle.Expression, _parent_precedence: int = 0) -> str:
    """Render a parsed ShaderExpr AST back to compact canonical syntax."""
    if isinstance(expression, oracle.Literal):
        return _float_text(expression.value)
    if isinstance(expression, oracle.Variable):
        return expression.name
    if isinstance(expression, oracle.Member):
        base = expression_to_text(expression.base, 3)
        return f"{base}.{expression.selector}"
    if isinstance(expression, oracle.Unary):
        operand = expression_to_text(expression.operand, 3)
        text = f"{expression.operator}{operand}"
        return f"({text})" if _parent_precedence >= 3 else text
    if isinstance(expression, oracle.Binary):
        precedence = _BINARY_PRECEDENCE[expression.operator]
        left = expression_to_text(
            expression.left,
            precedence if expression.operator in {"*", "/"} else precedence,
        )
        right_precedence = precedence + (
            1 if expression.operator in {"-", "/"} else 0
        )
        right = expression_to_text(expression.right, right_precedence)
        text = f"{left} {expression.operator} {right}"
        return f"({text})" if _parent_precedence > precedence else text
    assert isinstance(expression, oracle.Call)
    return (
        f"{expression.name}("
        + ", ".join(expression_to_text(argument) for argument in expression.arguments)
        + ")"
    )


def _expression_cse_count(expression: oracle.Expression) -> int:
    seen: set[oracle.Expression] = set()
    duplicates = 0

    def visit(node: oracle.Expression) -> None:
        nonlocal duplicates
        if node in seen:
            duplicates += 1
            return
        seen.add(node)
        if isinstance(node, oracle.Member):
            visit(node.base)
        elif isinstance(node, oracle.Unary):
            visit(node.operand)
        elif isinstance(node, oracle.Binary):
            visit(node.left)
            visit(node.right)
        elif isinstance(node, oracle.Call):
            for argument in node.arguments:
                visit(argument)

    visit(expression)
    return duplicates


def _expression_samples() -> tuple[tuple[tuple[float, float], float, float], ...]:
    return tuple(
        ((x / 7.0, y / 5.0), index / 11.0, 0.25)
        for index, (x, y) in enumerate(
            (x, y) for y in range(5) for x in range(7)
        )
    )


def _expressions_equivalent(
    left: oracle.Expression,
    right: oracle.Expression,
    *,
    tolerance_absolute: float = oracle.NUMERIC_TOLERANCE_ABSOLUTE,
    tolerance_relative: float = oracle.NUMERIC_TOLERANCE_RELATIVE,
) -> bool:
    def texture_sample(u: float, v: float) -> tuple[float, float, float, float]:
        # The fusion oracle compares two expressions, not texture contents.
        # A deterministic function of both coordinates is sufficient to catch
        # changes to sampling structure while keeping real texture data outside
        # the CPU optimizer.
        return (u, v, u * 0.5 + v * 0.25, 1.0)

    for uv, time_value, seed in _expression_samples():
        variables = {"uv": uv, "time": time_value, "seed": seed}
        try:
            left_value = oracle.evaluate(
                left,
                variables=variables,
                float32=True,
                texture_function=texture_sample,
            )
            right_value = oracle.evaluate(
                right,
                variables=variables,
                float32=True,
                texture_function=texture_sample,
            )
        except oracle.ExpressionError:
            return False
        left_values = left_value if isinstance(left_value, tuple) else (left_value,)
        right_values = right_value if isinstance(right_value, tuple) else (right_value,)
        failure = oracle.tolerance_failure(left_values, right_values)
        if failure is not None:
            # Fall back to the documented absolute/relative tolerance.
            if len(left_values) != len(right_values):
                return False
            for expected, actual in zip(left_values, right_values):
                difference = abs(expected - actual)
                scale = max(abs(expected), abs(actual), 1.0)
                if (
                    difference > tolerance_absolute
                    and difference / scale > tolerance_relative
                ):
                    return False
    return True


# ---------------------------------------------------------------------------
# Whole-graph lowering
# ---------------------------------------------------------------------------


def _shader_root_expression_text(
    document: nw03.GraphDocument, node: nw03.GraphNode
) -> str:
    return nw03._shader_expression_text(document, node)


def _shader_members(
    document: nw03.GraphDocument, root: nw03.GraphNode
) -> tuple[str, ...]:
    return tuple(
        sorted(
            node_id
            for node_id in nw03._shader_dependencies(document, root)
            if document.nodes[node_id].operator_id in nw03.SHADER_OPERATORS
        )
    )


def _build_ir_nodes(
    document: nw03.GraphDocument,
    reachable: set[str],
    constants: dict[str, dict[str, tuple[float, ...]]],
    *,
    cse_aliases: dict[str, str] | None = None,
    retained_output_ports: dict[str, set[str]] | None = None,
) -> tuple[list[IRNode], tuple[CseRecord, ...]]:
    cse_aliases = dict(cse_aliases or {})
    retained_output_ports = retained_output_ports or {}
    order = _topological_ids(document, reachable)
    records: list[IRNode] = []
    cse_records: list[CseRecord] = []
    key_to_node: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for node_id in order:
        node = document.nodes[node_id]
        params = _normalised_params(node.params)
        inputs: list[IRInput] = []
        for port, (source_node, source_port) in sorted(node.inputs.items()):
            resolved_source = cse_aliases.get(source_node, source_node)
            # Follow aliases introduced by this pass as well.
            while resolved_source in aliases:
                resolved_source = aliases[resolved_source]
            inputs.append(
                IRInput(
                    port=port,
                    source_node=resolved_source,
                    source_port=source_port,
                    value=_output_specs(document.nodes[source_node].operator_id).get(
                        source_port
                    )
                    if source_node in document.nodes
                    else None,
                )
            )
        constant = constants.get(node_id)
        specs = _output_specs(node.operator_id)
        retained = retained_output_ports.get(node_id)
        outputs = (
            {port: value for port, value in specs.items() if port in retained}
            if retained is not None
            else specs
        )
        ir_node = IRNode(
            node_id=node_id,
            operator_id=node.operator_id,
            operator_version=node.version,
            frequency=node.frequency,
            residency=node.residency,
            params=params,
            inputs=tuple(inputs),
            outputs=outputs,
            constant=dict(constant) if constant is not None else None,
            source_node_ids=(node_id,),
            raw_params={
                name: tuple(values) for name, values in node.params.items()
            },
        )
        if node.operator_id in PURE_OPERATORS and node_id not in constants:
            cse_key = _structural_key(
                node.operator_id,
                node.version,
                node.frequency,
                params,
                ir_node.inputs,
            )
            ir_node.cse_key = cse_key
            if cse_key in key_to_node:
                canonical = key_to_node[cse_key]
                aliases[node_id] = canonical
                alias_ports = retained_output_ports.get(node_id, set())
                alias_outputs = {
                    port: value
                    for port, value in _output_specs(node.operator_id).items()
                    if port in alias_ports
                }
                for record in records:
                    if record.node_id == canonical:
                        record.source_node_ids = record.source_node_ids + (node_id,)
                        record.outputs.update(alias_outputs)
                continue
            key_to_node[cse_key] = node_id
        records.append(ir_node)

    # Collapse alias chains and re-point inputs.
    def resolve(node_id: str) -> str:
        seen: set[str] = set()
        while node_id in aliases and node_id not in seen:
            seen.add(node_id)
            node_id = aliases[node_id]
        return node_id

    merged: dict[str, list[str]] = {}
    for alias, canonical in aliases.items():
        merged.setdefault(resolve(canonical), []).append(alias)
    for record in records:
        record.inputs = tuple(
            IRInput(
                port=item.port,
                source_node=resolve(item.source_node),
                source_port=item.source_port,
                value=item.value,
            )
            for item in record.inputs
        )
    for canonical, values in sorted(merged.items()):
        cse_key = next(
            record.cse_key for record in records if record.node_id == canonical
        )
        cse_records.append(
            CseRecord(
                canonical_node=canonical,
                merged_nodes=tuple(sorted(values)),
                cse_key=cse_key,
            )
        )
    return records, tuple(cse_records)


def _prune_reachable_after_folding(
    document: nw03.GraphDocument,
    outputs: Sequence[GraphOutput],
    constants: dict[str, dict[str, tuple[float, ...]]],
) -> set[str]:
    reachable: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in reachable or node_id not in document.nodes:
            return
        reachable.add(node_id)
        if node_id in constants:
            return
        node = document.nodes[node_id]
        for source_node, _source_port in node.inputs.values():
            visit(source_node)

    for output in outputs:
        visit(output.node_id)
    return reachable


def _partition_nodes(
    nodes: Sequence[IRNode],
) -> tuple[dict[str, tuple[str, ...]], list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    partitions: dict[str, list[str]] = {
        "compile-time": [],
        "initialization": [],
        "frame": [],
    }
    node_by_id = {node.node_id: node for node in nodes}
    order = _topological_order_nodes(nodes)
    position = {node_id: index for index, node_id in enumerate(order)}
    for node_id in order:
        node = node_by_id[node_id]
        if node.constant is not None:
            partitions["compile-time"].append(node_id)
        elif node.frequency == "compile-time":
            partitions["compile-time"].append(node_id)
        elif node.frequency == "initialization":
            partitions["initialization"].append(node_id)
        else:
            partitions["frame"].append(node_id)
        for item in node.inputs:
            source = node_by_id.get(item.source_node)
            if source is None:
                continue
            if FREQUENCY_RANK.get(source.frequency, 99) > FREQUENCY_RANK.get(node.frequency, 99):
                diagnostics.append(
                    nw03._diagnostic(
                        "frequency_transition",
                        f"node '{node.node_id}' runs earlier than upstream '{source.node_id}'",
                        node_id=node.node_id,
                        operator_id=node.operator_id,
                        port_id=item.port,
                    )
                )
    # Ensure deterministic order inside each partition while preserving
    # dependency order globally.
    for name, values in partitions.items():
        partitions[name] = sorted(values, key=lambda node_id: position[node_id])
    return {name: tuple(values) for name, values in partitions.items()}, diagnostics


def _topological_order_nodes(nodes: Sequence[IRNode]) -> list[str]:
    node_by_id = {node.node_id: node for node in nodes}
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(node_id: str) -> None:
        if state.get(node_id) == 2:
            return
        if state.get(node_id) == 1:
            raise OptimizationError(f"cycle detected at '{node_id}'")
        state[node_id] = 1
        node = node_by_id.get(node_id)
        if node is not None and node.constant is None:
            for item in sorted(node.inputs, key=lambda entry: entry.port):
                visit(item.source_node)
        state[node_id] = 2
        order.append(node_id)

    for node_id in sorted(node_by_id):
        visit(node_id)
    return order


def _fusion_records(
    document: nw03.GraphDocument,
    nodes: Sequence[IRNode],
    *,
    max_nodes: int,
    max_expression_bytes: int,
    enable: bool,
    measurements: Mapping[str, Any] | None = None,
) -> tuple[FusionRecord, ...]:
    node_ids = {node.node_id for node in nodes}
    consumers: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for node in nodes:
        for item in node.inputs:
            if item.source_node in consumers:
                consumers[item.source_node].append(node.node_id)
    roots = [
        node
        for node in document.nodes.values()
        if node.operator_id in nw03.SHADER_OPERATORS
        and node.node_id in node_ids
        and not any(
            document.nodes[consumer].operator_id in nw03.SHADER_OPERATORS
            for consumer in consumers.get(node.node_id, [])
            if consumer in node_ids
        )
    ]
    records: list[FusionRecord] = []
    for root in sorted(roots, key=lambda item: item.node_id):
        members = _shader_members(document, root)
        try:
            source_text = _shader_root_expression_text(document, root)
            source_expression = oracle.parse_expression(source_text)
            folded, _ = fold_expression(source_expression)
            folded_text = expression_to_text(folded)
            equivalent = _expressions_equivalent(source_expression, folded)
        except (nw03.CompilerError, oracle.ExpressionError) as exc:
            records.append(
                FusionRecord(
                    fusion_id=root.node_id,
                    root_node=root.node_id,
                    member_nodes=members,
                    expression_bytes_before=0,
                    expression_bytes_after=0,
                    estimated_instructions_before=0,
                    estimated_instructions_after=0,
                    correctness_preserved=False,
                    measured=False,
                    accepted=False,
                    reason=f"unsupported expression: {exc}",
                )
            )
            continue
        before_bytes = 0
        for node_id in members:
            try:
                before_bytes += len(
                    expression_to_text(
                        oracle.parse_expression(
                            _shader_root_expression_text(document, document.nodes[node_id])
                        )
                    ).encode("utf-8")
                )
            except (nw03.CompilerError, oracle.ExpressionError):
                before_bytes += len(
                    _shader_root_expression_text(document, document.nodes[node_id]).encode(
                        "utf-8"
                    )
                )
        instruction_count = sum(
            max(1, len(oracle.expression_nodes(oracle.parse_expression(
                _shader_root_expression_text(document, document.nodes[node_id])
            ))))
            for node_id in members
        )
        after_instructions = len(oracle.expression_nodes(folded))
        fusion_cse = _expression_cse_count(folded)
        # Fusion is accepted only with externally recorded measurements of the
        # actual exported implementations.  HLSL source length and Python
        # oracle timing are not exported size/performance measurements.
        measured_hlsl_before = 0
        measured_hlsl_after = 0
        measured_runtime_before = 0
        measured_runtime_after = 0
        measurement_source = "unavailable"
        measurement = (measurements or {}).get(root.node_id)
        measurement_ok = False
        if isinstance(measurement, Mapping):
            try:
                measured_hlsl_before = int(measurement["compiled_bytes_before"])
                measured_hlsl_after = int(measurement["compiled_bytes_after"])
                measured_runtime_before = int(measurement["runtime_ns_before"])
                measured_runtime_after = int(measurement["runtime_ns_after"])
                measurement_ok = bool(
                    measurement.get("correctness_preserved", False)
                    and measured_hlsl_before > 0
                    and measured_runtime_before > 0
                )
                measurement_source = str(
                    measurement.get("source", "exported-implementation-measurement")
                )
            except (KeyError, TypeError, ValueError):
                measurement_ok = False
        accepted = (
            enable
            and measurement_ok
            and len(members) <= max_nodes
            and len(folded_text.encode("utf-8")) <= max_expression_bytes
            and equivalent
            and len(folded_text.encode("utf-8")) <= before_bytes
            and measured_hlsl_after <= measured_hlsl_before
            and measured_runtime_after <= measured_runtime_before
        )
        if not enable:
            reason = "fusion disabled by explicit setting"
        elif len(members) > max_nodes:
            reason = f"fusion bound exceeded ({len(members)} > {max_nodes})"
        elif len(folded_text.encode("utf-8")) > max_expression_bytes:
            reason = "expression byte bound exceeded"
        elif not equivalent:
            reason = "correctness oracle rejected fused expression"
        elif len(folded_text.encode("utf-8")) > before_bytes:
            reason = "fused expression is not smaller than the unfused path"
        elif not measurement_ok:
            reason = (
                "exported fused/unfused implementation measurements unavailable; "
                "retained the correct NW-05 multi-dispatch UAV fallback"
            )
        elif measured_hlsl_after > measured_hlsl_before:
            reason = "measured emitted HLSL is larger than the unfused path"
        else:
            reason = (
                "correctness preserved; measured emitted size and recorded "
                "sample-evaluation tradeoff accepted"
            )
        records.append(
            FusionRecord(
                fusion_id=root.node_id,
                root_node=root.node_id,
                member_nodes=members,
                expression_bytes_before=before_bytes,
                expression_bytes_after=len(folded_text.encode("utf-8")),
                estimated_instructions_before=instruction_count,
                estimated_instructions_after=after_instructions,
                correctness_preserved=equivalent,
                measured=(
                    measurement_ok
                ),
                accepted=accepted,
                reason=reason + (f"; {fusion_cse} duplicate AST nodes analysed" if fusion_cse else ""),
                fused_expression=folded_text,
                measured_hlsl_bytes_before=measured_hlsl_before,
                measured_hlsl_bytes_after=measured_hlsl_after,
                measured_runtime_ns_before=measured_runtime_before,
                measured_runtime_ns_after=measured_runtime_after,
                measurement_source=measurement_source,
            )
        )
    return tuple(records)


def lower_typed_graph(
    document: nw03.GraphDocument,
    *,
    settings: dict[str, str] | None = None,
    quantized: QuantizedGraph | None = None,
    fusion_enabled: bool = True,
    fusion_max_nodes: int = DEFAULT_FUSION_MAX_NODES,
    fusion_max_expression_bytes: int = DEFAULT_FUSION_MAX_EXPRESSION_BYTES,
    fusion_measurements: Mapping[str, Any] | None = None,
) -> OptimizedGraph:
    """Lower a validated NW-02 document into the deterministic NW-05 IR."""
    active = dict(settings if settings is not None else document.settings)
    if quantized is not None:
        document = quantized.document
        decisions = quantized.decisions
        if quantized.errors:
            raise OptimizationError(
                "quantization exceeds the declared tolerance",
                [
                    nw03._diagnostic(
                        "quantization_tolerance_exceeded",
                        message,
                    )
                    for message in quantized.errors
                ],
            )
    else:
        decisions = ()

    outputs = semantic_outputs(document)
    if not outputs:
        raise OptimizationError(
            "graph has no reachable semantic output",
            [
                nw03._diagnostic(
                    "missing_implementation",
                    "graph has no reachable semantic output",
                )
            ],
        )
    reachable, needed_ports = reachable_node_ports(document, outputs)
    unsupported = sorted(
        node_id
        for node_id in reachable
        if node_id not in document.nodes
        or document.nodes[node_id].operator_id not in nw03.KNOWN_OPERATORS
    )
    if unsupported:
        raise OptimizationError(
            "reachable graph uses an unsupported operator",
            [
                nw03._diagnostic(
                    "missing_implementation",
                    "reachable graph uses an unsupported operator",
                    node_id=node_id,
                )
                for node_id in unsupported
            ],
        )
    quantized_reachable = set(reachable)
    constants, folded = constant_propagation(document, reachable)
    reachable = _prune_reachable_after_folding(document, outputs, constants)
    if decisions:
        decisions = tuple(
            decision
            for decision in decisions
            if decision.node_id in quantized_reachable
        )
    retained_ports = {node_id: set(ports) for node_id, ports in needed_ports.items()}
    for output in outputs:
        retained_ports.setdefault(output.node_id, set()).add(output.port)
    nodes, cse_records = _build_ir_nodes(
        document,
        reachable,
        constants,
        retained_output_ports=retained_ports,
    )
    partitions, partition_diagnostics = _partition_nodes(nodes)
    if partition_diagnostics:
        raise OptimizationError("frequency partition invalid", partition_diagnostics)

    reachable_nodes = {node.node_id for node in nodes}
    cse_merged = {
        merged
        for record in cse_records
        for merged in record.merged_nodes
    }
    dead_nodes = tuple(
        sorted(set(document.nodes) - reachable_nodes - cse_merged)
    )
    dead_outputs: list[tuple[str, str]] = []
    for node in nodes:
        used = needed_ports.get(node.node_id, set())
        for port in sorted(_output_specs(node.operator_id)):
            if port not in used and not any(
                output.node_id == node.node_id and output.port == port
                for output in outputs
            ):
                dead_outputs.append((node.node_id, port))

    fusions = _fusion_records(
        document,
        nodes,
        max_nodes=fusion_max_nodes,
        max_expression_bytes=fusion_max_expression_bytes,
        enable=fusion_enabled,
        measurements=fusion_measurements,
    )
    canonical_outputs = tuple(
        GraphOutput(
            node_id=_canonical_node_id(output.node_id, cse_records),
            port=output.port,
            kind=output.kind,
        )
        for output in outputs
    )
    return OptimizedGraph(
        profile_id=document.profile_id,
        requirements_digest=document.requirements_digest,
        schema_version=document.schema_version,
        settings=active,
        nodes=tuple(nodes),
        outputs=canonical_outputs,
        partitions=partitions,
        dead_nodes=dead_nodes,
        dead_outputs=tuple(sorted(dead_outputs)),
        cse=cse_records,
        constants_folded=tuple(sorted(folded)),
        fusions=fusions,
        quantization=decisions,
        # Authoring text may be checked out with CRLF on Windows.  Semantic
        # source identity is platform-independent, so hash the canonical LF
        # spelling rather than the checkout's newline convention.
        source_sha256=hashlib.sha256(
            document.source_text.replace("\r\n", "\n").replace("\r", "\n").encode(
                "utf-8"
            )
        ).hexdigest(),
        source_name=document.source_name,
        source_text=document.source_text,
        folded_constants={
            node_id: dict(constants[node_id])
            for node_id in sorted(folded)
            if node_id in constants
        },
    )


def _canonical_node_id(node_id: str, cse_records: Sequence[CseRecord]) -> str:
    for record in cse_records:
        if node_id == record.canonical_node or node_id in record.merged_nodes:
            return record.canonical_node
    return node_id


def optimize_graph(
    document: nw03.GraphDocument,
    *,
    settings: dict[str, str] | None = None,
    quantization_tolerance: float | None = None,
    fusion_enabled: bool = True,
    fusion_max_nodes: int = DEFAULT_FUSION_MAX_NODES,
    fusion_max_expression_bytes: int = DEFAULT_FUSION_MAX_EXPRESSION_BYTES,
    fusion_measurements: Mapping[str, Any] | None = None,
) -> OptimizedGraph:
    """Convenience entry point: quantize (if configured) then lower the graph."""
    active = dict(settings if settings is not None else document.settings)
    quantized = quantize_document(
        document,
        active,
        tolerance_override=quantization_tolerance,
    )
    if quantized.errors:
        raise OptimizationError(
            "quantization exceeds the declared tolerance",
            [
                nw03._diagnostic("quantization_tolerance_exceeded", message)
                for message in quantized.errors
            ],
        )
    return lower_typed_graph(
        document,
        settings=active,
        quantized=quantized,
        fusion_enabled=fusion_enabled,
        fusion_max_nodes=fusion_max_nodes,
        fusion_max_expression_bytes=fusion_max_expression_bytes,
        fusion_measurements=fusion_measurements,
    )


# ---------------------------------------------------------------------------
# Field-specific, reversible quantization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuantizationPolicy:
    mode: str = "none"
    bits: int = 0
    step: float = 0.0
    tolerance: float = QUANTIZATION_TOLERANCE
    fields: tuple[str, ...] = ()
    overrides: tuple[tuple[str, str, int, float, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "bits": self.bits,
            "step": self.step,
            "tolerance": self.tolerance,
            "fields": list(self.fields),
            "overrides": [
                {
                    "selector": selector,
                    "mode": mode,
                    "bits": bits,
                    "step": step,
                    "tolerance": tolerance,
                }
                for selector, mode, bits, step, tolerance in self.overrides
            ],
        }


def _parse_float_setting(settings: dict[str, str], names: Sequence[str], default: float) -> float:
    for name in names:
        if name in settings:
            try:
                return float(settings[name])
            except ValueError:
                return default
    return default


def parse_quantization_policy(
    settings: dict[str, str],
    *,
    tolerance_override: float | None = None,
) -> QuantizationPolicy:
    mode = settings.get("quantization_policy", "none").strip().lower()
    if mode in {"full", "off", "disabled"}:
        mode = "none"
    elif mode in {"fixed", "fixed-point", "field"}:
        mode = "bits"
    try:
        bits = int(settings.get("quantization_bits", "0") or "0", 0)
    except ValueError:
        bits = 0
    try:
        step = float(settings.get("quantization_step", "0") or "0")
    except ValueError:
        step = 0.0
    tolerance_keys = ("quantization_tolerance", "quantization_error_tolerance")
    tolerance_declared = any(key in settings for key in tolerance_keys)
    tolerance = _parse_float_setting(
        settings,
        tolerance_keys,
        QUANTIZATION_TOLERANCE,
    )
    if not tolerance_declared and tolerance_override is None:
        if step > 0.0:
            tolerance = step / 2.0
        elif bits > 0:
            tolerance = (2.0 ** (-bits)) / 2.0
    if tolerance_override is not None:
        tolerance = float(tolerance_override)
    fields_text = settings.get("quantization_fields", "")
    fields = tuple(
        sorted(
            item.strip()
            for item in re.split(r"[;,\s]+", fields_text)
            if item.strip()
        )
    )
    overrides: list[tuple[str, str, int, float, float]] = []
    for key, value in sorted(settings.items()):
        if not key.startswith("quantization_field."):
            continue
        selector = key[len("quantization_field.") :]
        parts = dict(
            item.split("=", 1)
            for item in re.split(r"[;,\s]+", value)
            if "=" in item
        )
        override_mode = parts.get("mode", parts.get("policy", mode))
        if override_mode in {"full", "off", "disabled"}:
            override_mode = "none"
        if "bits" in parts:
            try:
                override_bits = int(parts["bits"], 0)
            except ValueError:
                override_bits = bits
        else:
            override_bits = bits
        try:
            override_step = float(parts.get("step", step))
        except ValueError:
            override_step = step
        try:
            override_tolerance = float(parts.get("tolerance", tolerance))
        except ValueError:
            override_tolerance = tolerance
        overrides.append(
            (
                selector,
                override_mode,
                override_bits,
                override_step,
                override_tolerance,
            )
        )
    return QuantizationPolicy(
        mode=mode,
        bits=bits,
        step=step,
        tolerance=tolerance,
        fields=fields,
        overrides=tuple(overrides),
    )


def _field_selected(
    policy: QuantizationPolicy,
    node: nw03.GraphNode,
    parameter: str,
) -> tuple[str, str, int, float, float] | None:
    selectors = {
        parameter,
        f"{node.node_id}.{parameter}",
        f"{node.operator_id}.{parameter}",
    }
    for override in policy.overrides:
        if override[0] in selectors or override[0] == "*":
            return override
    if policy.fields:
        if "*" in policy.fields or "all" in policy.fields:
            return (parameter, policy.mode, policy.bits, policy.step, policy.tolerance)
        if not (selectors & set(policy.fields)):
            return None
    return (parameter, policy.mode, policy.bits, policy.step, policy.tolerance)


def _quantize_value(
    value: float,
    mode: str,
    bits: int,
    step: float,
) -> float:
    if mode == "none":
        return value
    if step <= 0.0 and bits > 0:
        step = 2.0 ** (-bits)
    if step <= 0.0:
        return value
    return round(value / step) * step


def quantize_document(
    document: nw03.GraphDocument,
    settings: dict[str, str] | None = None,
    *,
    tolerance_override: float | None = None,
) -> QuantizedGraph:
    """Quantize selected parameter fields while retaining full-precision values."""
    active = dict(settings if settings is not None else document.settings)
    policy = parse_quantization_policy(
        active, tolerance_override=tolerance_override
    )
    if policy.mode == "none" and not policy.overrides:
        return QuantizedGraph(document=copy.deepcopy(document))
    quantized_document = copy.deepcopy(document)
    decisions: list[QuantizationDecision] = []
    errors: list[str] = []
    non_numeric = {
        "expression",
        "keys",
        "interpolation",
        "format",
        "entries",
    }
    integer_fields = {
        "width",
        "height",
        "depth",
        "bytes",
        "vertices",
        "indices",
        "seed",
        "sample_count",
    }
    for node in sorted(quantized_document.nodes.values(), key=lambda item: item.node_id):
        original_node = document.nodes[node.node_id]
        for name in sorted(node.params):
            if name.startswith("__") or name in non_numeric:
                continue
            raw_tokens: list[str] = []
            for token in node.params[name]:
                raw_tokens.extend(
                    part for part in re.split(r"[,\s]+", token) if part
                )
            try:
                original = tuple(float(token) for token in raw_tokens)
            except ValueError:
                continue
            if not original or not all(math.isfinite(item) for item in original):
                continue
            selection = _field_selected(policy, original_node, name)
            if selection is None:
                continue
            _selector, mode, bits, step, tolerance = selection
            if name in integer_fields and _selector == name and not policy.fields:
                # Global numeric quantization never silently converts an
                # integer dimension/seed field; select it explicitly to do so.
                continue
            if mode == "none":
                continue
            quantized = tuple(
                _quantize_value(item, mode, bits, step) for item in original
            )
            errors_for_field = [
                abs(after - before) for before, after in zip(original, quantized)
            ]
            maximum = max(errors_for_field, default=0.0)
            within = maximum <= tolerance + 1e-15
            if not within:
                errors.append(
                    f"{node.node_id}.{name}: error {maximum} exceeds tolerance {tolerance}"
                )
            decisions.append(
                QuantizationDecision(
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                    parameter=name,
                    policy=mode,
                    bits=bits,
                    step=step,
                    tolerance=tolerance,
                    original=original,
                    quantized=quantized,
                    max_absolute_error=maximum,
                    within_tolerance=within,
                )
            )
            node.params[name] = [" ".join(_float_text(value) for value in quantized)]
    return QuantizedGraph(
        document=quantized_document,
        decisions=tuple(decisions),
        errors=tuple(errors),
    )


def recover_full_precision(
    decisions: Sequence[QuantizationDecision] | QuantizedGraph | OptimizedGraph,
) -> dict[str, tuple[float, ...]]:
    """Return the exact pre-quantization values keyed by ``node.parameter``."""
    if isinstance(decisions, QuantizedGraph):
        decisions = decisions.decisions
    elif isinstance(decisions, OptimizedGraph):
        decisions = decisions.quantization
    return {
        f"{decision.node_id}.{decision.parameter}": decision.original
        for decision in decisions
    }


# ---------------------------------------------------------------------------
# Reachable link manifest
# ---------------------------------------------------------------------------


def _reachable_feature_queries(
    optimized: OptimizedGraph,
    *,
    formats: Sequence[str] = (),
    requires_aliasing: bool = True,
    wave_ops: bool = False,
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = [
        {"name": "D3D12_FEATURE_LEVELS", "reason": "declared feature level 12_0"},
        {"name": "D3D12_FEATURE_D3D12_OPTIONS", "reason": "resource binding and heap tiers"},
    ]
    if optimized.shader_node_ids:
        queries.append(
            {"name": "D3D12_FEATURE_SHADER_MODEL", "reason": "emitted cs_6_0 shaders"}
        )
    if formats:
        queries.append(
            {"name": "D3D12_FEATURE_FORMAT_SUPPORT", "reason": "emitted DXGI format support"}
        )
    used_operators = {node.operator_id for node in optimized.nodes}
    if wave_ops:
        queries.append(
            {
                "name": "D3D12_FEATURE_D3D12_OPTIONS1",
                "reason": "declared wave-operations profile requirement",
            }
        )
    if requires_aliasing:
        queries.append(
            {
                "name": "D3D12_FEATURE_GPU_VIRTUAL_ADDRESS_SUPPORT",
                "reason": "resource realization limits for alias proof",
            }
        )
    queries.append(
        {
            "name": "ID3D12Device::GetResourceAllocationInfo",
            "reason": "adapter-specific resource sizing and alias proof",
        }
    )
    # Keep the used-operator variable meaningful for future query derivation.
    _ = used_operators
    return sorted(queries, key=lambda item: item["name"])


def build_link_manifest(
    optimized: OptimizedGraph,
    *,
    package: nwp.NwpPackage | None = None,
    shader_blobs: Sequence[tuple[str, str, int]] = (),
    excluded_optional_profiles: Sequence[str] = (),
    profile_id: str | None = None,
    requirements_digest: str | None = None,
    optional_profile_id: str = "",
    requires_aliasing: bool = True,
    runtime_dependencies: Sequence[str] | None = None,
    memory_budget_bytes: int = 0,
) -> dict[str, Any]:
    """Build the generated player link manifest for one profile artifact."""
    used_operators = sorted({node.operator_id for node in optimized.nodes})
    excluded = sorted(set(OPTIONAL_IMPLEMENTATIONS) - set(used_operators))
    shaders = [
        {"specialization_id": specialization, "target": target, "blob_bytes": size}
        for specialization, target, size in sorted(shader_blobs)
    ]
    if package is not None:
        formats = sorted({resource.format for resource in package.resources})
        limits = {
            "max_cbv_srv_uav_descriptors_per_frame": package.capability.max_cbv_srv_uav_descriptors_per_frame,
            "max_sampler_descriptors_per_frame": package.capability.max_sampler_descriptors_per_frame,
            "max_root_signature_dword_cost": package.capability.max_root_signature_dword_cost,
            "max_texture_2d_dimension": package.capability.limit_max_texture_2d_dimension,
            "max_compute_threads_per_group": package.capability.limit_max_compute_threads_per_group,
            "max_buffer_bytes": package.capability.limit_max_buffer_bytes,
            "swap_chain_buffer_count": package.capability.limit_swap_chain_buffer_count,
        }
        shader_targets = sorted(package.capability.emitted_shader_targets)
        required_init = list(package.capability.required_init_instructions)
        required_frame = list(package.capability.required_frame_instructions)
    else:
        formats = []
        limits = {}
        shader_targets = []
        required_init = []
        required_frame = []
    wave_ops = (
        optional_profile_id == nwp.OPTIONAL_PROFILE_WAVE_OPS
        or any("wave" in operator for operator in used_operators)
    )
    feature_queries = _reachable_feature_queries(
        optimized,
        formats=formats,
        requires_aliasing=requires_aliasing,
        wave_ops=wave_ops,
    )
    dependencies = (
        sorted(set(runtime_dependencies))
        if runtime_dependencies is not None
        else []
    )
    runtime_requirements = {
        "feature_level": "12_0",
        "shader_model": "6_0",
        "resource_binding_tier": 2,
        "resource_heap_tier": 2,
        "direct_queue_count": 1,
        "runtime_dxc_dependency": False,
        "runtime_slang_dependency": False,
        "agility_sdk_required": False,
        "min_windows_build": nwp.MIN_WINDOWS_BUILD,
    }
    payload: dict[str, Any] = {
        "format": "nw05-player-link-manifest",
        "version": 1,
        "profile_id": profile_id or optimized.profile_id,
        "requirements_digest": requirements_digest or optimized.requirements_digest,
        "optional_profile_id": optional_profile_id,
        "ir_sha256": optimized.ir_sha256(),
        "reachable_content": {
            "node_ids": list(optimized.reachable_node_ids),
            "operators": used_operators,
            "outputs": [output.to_dict() for output in optimized.outputs],
            "partitions": {
                name: list(values) for name, values in optimized.partitions.items()
            },
        },
        "feature_queries": feature_queries,
        "used_features": [query["name"] for query in feature_queries],
        "feature_requirements": {
            "feature_level": "12_0",
            "shader_model": "6_0",
            "resource_binding_tier": 2,
            "resource_heap_tier": 2,
            "direct_queue_count": 1,
            "wave_ops": wave_ops,
            "fp16_shader_ops": False,
            "int64_shader_ops": False,
            "sm_6_6_direct_heap_indexing": False,
            "mesh_shaders": False,
            "ray_tracing": False,
            "enhanced_barriers": False,
        },
        "formats": formats,
        "limits": limits,
        "shader_targets": shader_targets,
        "shaders": shaders,
        "runtime_requirements": runtime_requirements,
        "link_metadata": {
            "wave_ops": wave_ops,
            "fp16_shader_ops": False,
            "int64_shader_ops": False,
            "memory_budget_bytes": int(memory_budget_bytes),
        },
        "required_init_instructions": required_init,
        "required_frame_instructions": required_frame,
        "retained": {
            "code": used_operators,
            "shaders": shaders,
            "data": ["package-resources", "parameters", "initialization", "frame"],
            "dependencies": dependencies,
        },
        "excluded": {
            "optional_implementations": excluded,
            "optional_profiles": sorted(excluded_optional_profiles),
            "authoring_infrastructure": [
                "nw02-graph-parser",
                "nw03-source-maps",
                "nw03-reflection-tables",
                "nodewerk-studio",
            ],
            "gpu_name_predicates": [],
        },
    }
    payload["manifest_sha256"] = "sha256:v1:" + _sha256(
        nwp.canonical_json_bytes(payload)
    )
    return payload


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


# Backwards-friendly aliases used by verification and downstream tooling.
dead_code_elimination = lower_typed_graph
common_subexpression_elimination = lower_typed_graph
partition_initialization_and_frame = _partition_nodes
quantize_parameters = quantize_document
shader_reachability = semantic_outputs
lower_graph = lower_typed_graph
lower_to_optimized_ir = lower_typed_graph
optimize = optimize_graph

