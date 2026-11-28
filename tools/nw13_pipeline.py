#!/usr/bin/env python3
"""NW-13 shared optimizer/scheduler bridge for the procedural film production.

The module reuses the established NW-02 graph reader and NW-05 whole-graph
optimizer instead of defining a second graph format.  It adds only the
production-facing concerns required by NW-13:

* bounded admission before execution;
* canonical semantic serialization/hashes;
* dependency-local cache invalidation;
* initialization vs frame/history schedule partitioning;
* explicit migrations for changed contracts.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import nw03_compiler as nw03
import nw05_optimizer as nw05
import nwp_package as nwp


FORMAT = "nw13.pipeline"
VERSION = 1
TICKS_PER_SECOND = 48_000
SIMULATION_HZ = 120
TICKS_PER_STEP = TICKS_PER_SECOND // SIMULATION_HZ


class PipelineError(ValueError):
    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _diagnostic(code: str, message: str, **fields: Any) -> dict[str, Any]:
    result = {
        "category": "pipeline_validation",
        "severity": "error",
        "code": code,
        "message": message,
    }
    result.update(fields)
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256_text(value: str) -> str:
    return "sha256:v1:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def node_semantic_record(node: Any) -> dict[str, Any]:
    """Canonical node identity, excluding editor layout."""
    return {
        "id": node.node_id,
        "operator_id": node.operator_id,
        "operator_version": node.version,
        "frequency": node.frequency,
        "residency": node.residency,
        "params": {name: list(values) for name, values in sorted(node.params.items())},
        "inputs": {port: list(value) for port, value in sorted(node.inputs.items())},
        "selected_outputs": list(node.selected_outputs),
        "history": (
            {
                "state_type": node.history.state_type,
                "initial_value": node.history.initial_value,
                "reset_policy": node.history.reset_policy,
                "step_rate_hz": node.history.step_rate_hz,
            }
            if node.history
            else None
        ),
    }


def document_semantic_record(document: Any) -> dict[str, Any]:
    return {
        "schema_version": document.schema_version,
        "profile_id": document.profile_id,
        "requirements_digest": document.requirements_digest,
        "settings": dict(sorted(document.settings.items())),
        "nodes": [node_semantic_record(document.nodes[node_id]) for node_id in sorted(document.nodes)],
    }


def semantic_hash(document: Any) -> str:
    return sha256_text(canonical_json(document_semantic_record(document)))


def load_graph(path: str | Path) -> Any:
    return nw03.load_graph(path)


@dataclass
class OptimizedProduction:
    optimized: Any
    reachable: tuple[str, ...]
    dead_nodes: tuple[str, ...]
    partitions: dict[str, tuple[str, ...]]
    semantic_hash: str
    node_keys: dict[str, str]
    schedule: dict[str, Any] | None = None


def optimize_document(document: Any, *, settings: Mapping[str, str] | None = None) -> OptimizedProduction:
    admission_check(document)
    optimized = nw05.optimize_graph(document, settings=dict(settings) if settings else None)
    reachable = optimized.reachable_node_ids
    node_keys = {}
    for node in optimized.nodes:
        record = {
            "id": node.node_id,
            "operator_id": node.operator_id,
            "operator_version": node.operator_version,
            "frequency": node.frequency,
            "residency": node.residency,
            "params": {name: list(values) for name, values in sorted(node.params.items())},
            "inputs": [item.to_dict() for item in node.inputs],
            "outputs": sorted(node.outputs),
        }
        node_keys[node.node_id] = sha256_text(canonical_json(record))
    return OptimizedProduction(
        optimized=optimized,
        reachable=reachable,
        dead_nodes=tuple(sorted(optimized.dead_nodes)),
        partitions={name: tuple(values) for name, values in optimized.partitions.items()},
        semantic_hash=sha256_text(
            canonical_json(
                {
                    "profile_id": optimized.profile_id,
                    "requirements_digest": optimized.requirements_digest,
                    "settings": dict(sorted(optimized.settings.items())),
                    "partitions": {name: list(values) for name, values in sorted(optimized.partitions.items())},
                    "node_keys": dict(sorted(node_keys.items())),
                }
            )
        ),
        node_keys=node_keys,
    )


def downstream_consumers(document: Any) -> dict[str, set[str]]:
    consumers: dict[str, set[str]] = {node_id: set() for node_id in document.nodes}
    for node_id, node in document.nodes.items():
        for source_node, _source_port in node.inputs.values():
            consumers.setdefault(source_node, set()).add(node_id)
    return consumers


def dependency_local_invalidation(document: Any, changed: Iterable[str]) -> set[str]:
    """Return only changed nodes and their transitive consumers."""
    consumers = downstream_consumers(document)
    invalid: set[str] = set()
    stack = list(changed)
    while stack:
        node_id = stack.pop()
        if node_id in invalid:
            continue
        if node_id not in document.nodes:
            raise PipelineError(f"changed node '{node_id}' does not exist")
        invalid.add(node_id)
        stack.extend(consumers.get(node_id, ()))
    return invalid


def _param_int(document: Any, node_id: str, name: str, default: int) -> int:
    node = document.nodes.get(node_id)
    if node is None or name not in node.params or not node.params[name]:
        return default
    try:
        return int(float(node.params[name][0]))
    except (TypeError, ValueError):
        return default


def admission_check(document: Any, *, budget: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Reject excessive work before the optimizer or any GPU execution."""
    active = dict(
        {
            "max_nodes": 2048,
            "max_evaluation_steps": 4_194_304,
            "max_instances": 4096,
            "max_generated_geometry": 131072,
            "max_repetition": 65536,
            "max_recursion": 64,
            "max_memory_bytes": 256 * 1024 * 1024,
        }
    )
    if budget:
        active.update(budget)
    nodes = len(document.nodes)
    if nodes > active["max_nodes"]:
        raise PipelineError(
            "graph node admission bound exceeded",
            [_diagnostic("pipeline_node_budget_exceeded", "graph node admission bound exceeded", nodes=nodes)],
        )
    instances = 0
    for node in document.nodes.values():
        if node.operator_id == "nw.scene.instances":
            instances += _param_int(document, node.node_id, "count", 256)
        if node.operator_id in {"nw.particles.system"}:
            instances += _param_int(document, node.node_id, "count", 1)
    geometry_nodes = {node_id for node_id, node in document.nodes.items()
                      if node.operator_id.startswith("nw.geom.")}
    consumed = {source for node_id in geometry_nodes for source, _ in document.nodes[node_id].inputs.values()}

    visiting = set()

    @lru_cache(maxsize=None)
    def geometry_cost(node_id, depth):
        if depth > active["max_recursion"] or node_id in visiting:
            raise PipelineError("geometry recursion limit exceeded", [
                _diagnostic("pipeline_recursion_budget_exceeded", "geometry recursion limit exceeded", node_id=node_id),
            ])
        node = document.nodes[node_id]
        factor = 1
        if node.operator_id == "nw.geom.repeat":
            counts = node.params.get("counts", ())
            try:
                values = [float(value) for value in counts]
            except (TypeError, ValueError):
                values = []
            if len(values) != 3 or any(not value.is_integer() or not 1 <= value <= 64 for value in values):
                raise PipelineError("geometry repetition requires three integers in 1..64", [
                    _diagnostic("pipeline_invalid_repetition", "geometry repetition requires three integers in 1..64", node_id=node_id),
                ])
            factor = int(values[0] * values[1] * values[2])
        visiting.add(node_id)
        costs = [geometry_cost(source, depth + 1) for source, _ in node.inputs.values()
                 if source in geometry_nodes]
        visiting.remove(node_id)
        return (factor * max((cost[0] for cost in costs), default=1),
                factor * (sum(cost[1] for cost in costs) if costs else 1))

    # The typed schema rejects cycles. Bound valid DAGs here before compilation.
    if geometry_nodes and not geometry_nodes - consumed:
        raise PipelineError("geometry graph has no acyclic root", [
            _diagnostic("pipeline_recursion_budget_exceeded", "geometry graph has no acyclic root"),
        ])
    costs = [geometry_cost(node_id, 0) for node_id in sorted(geometry_nodes - consumed)]
    repetition = max((cost[0] for cost in costs), default=1)
    generated = sum(cost[1] for cost in costs)
    if instances > active["max_instances"]:
        raise PipelineError(
            "instance admission bound exceeded",
            [_diagnostic("pipeline_instance_budget_exceeded", "instance admission bound exceeded", instances=instances)],
        )
    if repetition > active["max_repetition"]:
        raise PipelineError(
            "repetition admission bound exceeded",
            [_diagnostic("pipeline_repetition_budget_exceeded", "repetition admission bound exceeded", repetition=repetition)],
        )
    if generated > active["max_generated_geometry"]:
        raise PipelineError(
            "generated geometry admission bound exceeded",
            [_diagnostic("pipeline_generated_geometry_budget_exceeded", "generated geometry admission bound exceeded", generated=generated)],
        )
    evaluation_steps = nodes * 64 + generated * 8 + instances * 4
    if evaluation_steps > active["max_evaluation_steps"]:
        raise PipelineError(
            "evaluation-step admission bound exceeded",
            [_diagnostic("pipeline_evaluation_budget_exceeded", "evaluation-step admission bound exceeded", steps=evaluation_steps)],
        )
    memory = nodes * 1024 + instances * 160 + generated * 64
    if memory > active["max_memory_bytes"]:
        raise PipelineError(
            "memory admission bound exceeded",
            [_diagnostic("pipeline_memory_budget_exceeded", "memory admission bound exceeded", bytes=memory)],
        )
    return {
        "nodes": nodes,
        "instances": instances,
        "repetition": repetition,
        "generated_geometry": generated,
        "evaluation_steps": evaluation_steps,
        "memory_bytes": memory,
        "budget": active,
    }


def build_schedule(
    production: OptimizedProduction,
    *,
    start_tick: int = 0,
    end_tick: int | None = None,
    work_cap_steps: int | None = None,
) -> dict[str, Any]:
    """Partition static initialization work from frame-dependent work."""
    if start_tick < 0 or start_tick % TICKS_PER_STEP != 0:
        raise PipelineError("schedule start must be a non-negative fixed tick")
    if end_tick is None:
        end_tick = start_tick + TICKS_PER_STEP * 120
    if end_tick < start_tick or end_tick % TICKS_PER_STEP != 0:
        raise PipelineError("schedule end must be a fixed tick")
    steps = (end_tick - start_tick) // TICKS_PER_STEP
    if steps > 3600:
        raise PipelineError(
            "schedule work bound exceeded",
            [_diagnostic("pipeline_schedule_budget_exceeded", "schedule work bound exceeded", steps=steps)],
        )
    partitions = production.partitions
    initialization = list(partitions.get("initialization", ())) + list(partitions.get("compile-time", ()))
    history = list(partitions.get("history", ()))
    frame = list(partitions.get("frame", ()))
    timeline = []
    pending = steps
    for step in range(steps):
        if work_cap_steps is not None and step >= work_cap_steps:
            break
        tick = start_tick + step * TICKS_PER_STEP
        timeline.append(
            {
                "tick": tick,
                "initialization": initialization if step == 0 else [],
                "history": history,
                "frame": frame,
                "frame_dependent": bool(history or frame),
            }
        )
        pending -= 1
    return {
        "tick_rate_hz": TICKS_PER_SECOND,
        "simulation_hz": SIMULATION_HZ,
        "ticks_per_step": TICKS_PER_STEP,
        "start_tick": start_tick,
        "end_tick": end_tick,
        "steps": steps,
        "pending_steps": pending,
        "initialization": initialization,
        "history": history,
        "frame": frame,
        "timeline": timeline,
        "semantic_hash": production.semantic_hash,
    }


def migrate_v0(document: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit migration for the short-lived NW-13 v0 production envelope."""
    if not isinstance(document, Mapping) or document.get("format") != FORMAT:
        raise PipelineError("not an nw13.pipeline document")
    version = document.get("version")
    if version == VERSION:
        return dict(document)
    if version != 0:
        raise PipelineError(
            "unsupported pipeline document version",
            [_diagnostic("pipeline_unknown_version", "unsupported pipeline document version", version=version)],
        )
    migrated = json.loads(json.dumps(document))
    migrated["version"] = VERSION
    migrated["migration"] = {"from": "nw13.pipeline.v0", "to": "nw13.pipeline.v1", "rule": "add-profile-and-budget"}
    migrated.setdefault("profile", {"id": nwp.COMMON_PROFILE_ID, "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST})
    migrated.setdefault("budget", {})
    return migrated

