#!/usr/bin/env python3
"""NW-08 authoring editor model for the NW-07 proof graph.

The editor is an authoring-side layer over the authoritative NW-02 document
model (``tools/nw03_compiler.py``).  It never introduces a second graph model:
every edit is re-serialized to ``.nwgraph`` v1 and parsed/validated by the same
compiler frontend used by the CLI export path.  Layout, selection, undo/redo,
timeline position, and cache-budget configuration are editor-only state and are
not part of canonical semantic identity.

The module is importable headlessly; no UI toolkit is imported here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import nw03_compiler  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer  # noqa: E402
import nw06_procedural  # noqa: E402
import nw06_timeline  # noqa: E402
import nw07_contract  # noqa: E402
import nwp_package  # noqa: E402
from nw08_cache import (  # noqa: E402
    CacheKeyInputs,
    CapabilityProfileIdentity,
    ToolchainIdentity,
    canonical_json_bytes,
    sha256_value,
)

NW07_PROOF_GRAPH = REPO_ROOT / "fixtures" / "nw07" / "production_loop.nwgraph"
COMMON_PROFILE_ID = nwp_package.COMMON_PROFILE_ID
COMMON_REQUIREMENTS_DIGEST = nwp_package.COMMON_REQUIREMENTS_DIGEST


class EditorError(RuntimeError):
    """Raised when an editor operation is malformed or unsafe."""

    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


@dataclass(frozen=True)
class CurvePoint:
    tick: int
    value: float

    def to_text(self) -> str:
        return f"{self.tick}:{format(self.value, '.17g')}"


@dataclass
class EditResult:
    """Deterministic outcome of one editor mutation."""

    valid: bool
    message: str = ""
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    linked_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    semantic_digest: str = ""
    previous_semantic_digest: str = ""
    snapshot_generation: int = 0
    changed_nodes: tuple[str, ...] = ()
    recomputed_nodes: tuple[str, ...] = ()
    cached_nodes: tuple[str, ...] = ()
    affected_passes: tuple[str, ...] = ()
    recomputed_resources: tuple[str, ...] = ()
    editor_metadata_changed: bool = False

    @property
    def ok(self) -> bool:
        return self.valid

    @property
    def invalidated_nodes(self) -> tuple[str, ...]:
        return self.recomputed_nodes

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "message": self.message,
            "diagnostics": self.diagnostics,
            "linked_diagnostics": self.linked_diagnostics,
            "semantic_digest": self.semantic_digest,
            "previous_semantic_digest": self.previous_semantic_digest,
            "snapshot_generation": self.snapshot_generation,
            "changed_nodes": list(self.changed_nodes),
            "recomputed_nodes": list(self.recomputed_nodes),
            "cached_nodes": list(self.cached_nodes),
            "affected_passes": list(self.affected_passes),
            "recomputed_resources": list(self.recomputed_resources),
            "editor_metadata_changed": self.editor_metadata_changed,
        }


@dataclass
class TimelineState:
    tick: int
    frame_index: int
    duration_ticks: int
    tick_rate: int
    camera_angle_radians: float | None = None
    camera_angle_modulo_turn: float | None = None
    twist_degrees: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "frame_index": self.frame_index,
            "duration_ticks": self.duration_ticks,
            "tick_rate": self.tick_rate,
            "camera_angle_radians": self.camera_angle_radians,
            "camera_angle_modulo_turn": self.camera_angle_modulo_turn,
            "twist_degrees": self.twist_degrees,
        }


# ---------------------------------------------------------------------------
# Deterministic textual emission
# ---------------------------------------------------------------------------


def _quote_token(value: str) -> str:
    if value == "":
        return '""'
    if any(
        character.isspace() or character in {'#', '"', "\\"}
        for character in value
    ):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return value


def _emit_number(value: float) -> str:
    if not math.isfinite(value):
        raise EditorError("editor layout coordinates must be finite")
    if value == 0.0:
        return "0"
    text = format(value, ".17g")
    return text


def emit_graph_text(document: nw03_compiler.GraphDocument) -> str:
    """Serialize an authoritative NW-02 document back to ``.nwgraph`` v1.

    The output is deterministic and excludes comments.  Editor layout is
    emitted in a separate ``layout`` directive set, exactly as the NW-02
    textual schema requires.
    """
    lines = [
        "nwgraph " + str(document.schema_version),
        f"profile {document.profile_id} {document.requirements_digest}",
    ]
    for key in sorted(document.settings):
        lines.append(f"setting {key} {document.settings[key]}")
    for node_id in sorted(document.nodes):
        node = document.nodes[node_id]
        attributes = f"frequency={node.frequency} residency={node.residency}"
        lines.append(
            f"node {node.node_id} {node.operator_id}@{node.version} {attributes}"
        )
        for name in sorted(node.params):
            if name == "__seed__":
                continue
            tokens = node.params[name]
            joined = " ".join(_quote_token(str(token)) for token in tokens)
            lines.append(f"  param {name} {joined}".rstrip())
        seed_tokens = node.params.get("__seed__")
        if seed_tokens:
            lines.append("  seed " + " ".join(str(token) for token in seed_tokens))
        for port in sorted(node.inputs):
            source_node, source_port = node.inputs[port]
            lines.append(f"  input {port} {source_node}.{source_port}")
    for node_id in sorted(document.layout):
        if node_id not in document.nodes:
            continue
        x, y = document.layout[node_id]
        lines.append(f"layout {node_id} {_emit_number(x)} {_emit_number(y)}")
    return "\n".join(lines) + "\n"


def parse_graph_text(
    text: str, *, source_name: str = "<editor>"
) -> tuple[nw03_compiler.GraphDocument | None, list[dict[str, Any]]]:
    return nw03_compiler.parse_graph_text(text, source_name=source_name)


def _node_source_line(text: str, node_id: str) -> int:
    prefix = f"node {node_id} "
    for index, line in enumerate(text.splitlines(), start=1):
        if line.startswith(prefix):
            return index
    return 0


def link_source_diagnostics(
    diagnostics: Iterable[Mapping[str, Any]],
    *,
    source_text: str,
    source_name: str = "<editor>",
) -> list[dict[str, Any]]:
    """Attach editor file/line/column information to compiler diagnostics."""
    linked: list[dict[str, Any]] = []
    for item in diagnostics:
        entry = dict(item)
        source = dict(entry.get("source") or {})
        node_id = str(source.get("node_id", ""))
        line = int(source.get("line", 0) or 0)
        if line <= 0 and node_id:
            line = _node_source_line(source_text, node_id)
        source.setdefault("file", source_name)
        source["line"] = line
        source.setdefault("column", int(source.get("column", 0) or 0))
        entry["source"] = source
        if node_id:
            entry["editor_selection"] = {"node_id": node_id}
        entry["source_linked"] = bool(line or node_id)
        linked.append(entry)
    return linked


# ---------------------------------------------------------------------------
# Structural identity and dependency invalidation
# ---------------------------------------------------------------------------


def _topological_order(document: nw03_compiler.GraphDocument) -> list[str]:
    remaining = set(document.nodes)
    order: list[str] = []
    while remaining:
        ready = sorted(
            node_id
            for node_id in remaining
            if all(
                source_node not in remaining
                for source_node, _ in document.nodes[node_id].inputs.values()
            )
        )
        if not ready:
            # Cycles are already rejected by validation.  Preserve determinism
            # for an invalid draft so the editor can still report diagnostics.
            order.extend(sorted(remaining))
            break
        order.extend(ready)
        remaining.difference_update(ready)
    return order


def structural_hashes(
    document: nw03_compiler.GraphDocument,
) -> dict[str, str]:
    """Python mirror of the NW-02 dependency-sensitive node identity.

    Equality is what the editor needs for incremental cache invalidation.  The
    authoritative canonical digest still comes from the NW-02 C++ helper or the
    compiler's package identity; this function intentionally excludes layout and
    selection.
    """
    order = _topological_order(document)
    hashes: dict[str, str] = {}
    graph_context = {
        "schema_version": document.schema_version,
        "profile_id": document.profile_id,
        "requirements_digest": document.requirements_digest,
        "settings": dict(sorted(document.settings.items())),
    }
    for node_id in order:
        node = document.nodes[node_id]
        inputs = []
        for port in sorted(node.inputs):
            source_node, source_port = node.inputs[port]
            inputs.append(
                {
                    "port": port,
                    "source_port": source_port,
                    "upstream": hashes.get(source_node, ""),
                }
            )
        payload = {
            "domain": "nw08.editor.node.v1",
            "graph": graph_context,
            "operator_id": node.operator_id,
            "operator_version": node.version,
            "frequency": node.frequency,
            "residency": node.residency,
            "params": {
                name: list(tokens) for name, tokens in sorted(node.params.items())
            },
            "selected_outputs": list(node.selected_outputs),
            "inputs": inputs,
        }
        hashes[node_id] = sha256_value(payload)
    return hashes


def _dependents_map(document: nw03_compiler.GraphDocument) -> dict[str, set[str]]:
    dependents: dict[str, set[str]] = {node_id: set() for node_id in document.nodes}
    for node in document.nodes.values():
        for source_node, _source_port in node.inputs.values():
            dependents.setdefault(source_node, set()).add(node.node_id)
    return dependents


def invalidated_closure(
    document: nw03_compiler.GraphDocument,
    changed: Iterable[str],
) -> set[str]:
    dependents = _dependents_map(document)
    result: set[str] = set()
    queue = [node_id for node_id in changed if node_id in document.nodes]
    while queue:
        node_id = queue.pop()
        if node_id in result:
            continue
        result.add(node_id)
        queue.extend(sorted(dependents.get(node_id, ()), reverse=True))
    return result


def semantic_digest(
    document: nw03_compiler.GraphDocument,
) -> tuple[str, str]:
    """Return ``(digest, provenance)`` for an editor snapshot.

    The authoritative NW-02 C++ helper is used whenever available.  On hosts
    without a C++ compiler the editor uses a deterministic, layout-independent
    Python fingerprint and labels it honestly; the CLI export path still refuses
    to substitute an incompatible package identity.
    """
    try:
        native = nw03_compiler._native_nw02_digest(document)  # type: ignore[attr-defined]
    except Exception:
        native = None
    if native:
        return native, "nw02"
    payload = {
        "domain": "nw08.editor.semantic.v1",
        "schema_version": document.schema_version,
        "profile_id": document.profile_id,
        "requirements_digest": document.requirements_digest,
        "settings": dict(sorted(document.settings.items())),
        "structural_hashes": structural_hashes(document),
    }
    return sha256_value(payload), "python-fallback"


# ---------------------------------------------------------------------------
# Resource/pass attribution
# ---------------------------------------------------------------------------


def _node_param_tokens(
    node: nw03_compiler.GraphNode, name: str
) -> list[str]:
    return nw03_compiler._param_tokens(node, name)  # type: ignore[attr-defined]


def _node_float(
    node: nw03_compiler.GraphNode, name: str, default: float
) -> float:
    tokens = _node_param_tokens(node, name)
    if not tokens:
        return default
    try:
        value = float(tokens[0])
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def _node_int(
    node: nw03_compiler.GraphNode, name: str, default: int
) -> int:
    value = _node_float(node, name, float(default))
    return int(value)


def estimate_size_attribution(
    document: nw03_compiler.GraphDocument,
    *,
    width: int = 1920,
    height: int = 1080,
) -> dict[str, Any]:
    """Deterministic CPU-side size attribution for the procedural proof."""
    nodes = document.nodes

    def find(operator_id: str) -> nw03_compiler.GraphNode | None:
        for node in nodes.values():
            if node.operator_id == operator_id:
                return node
        return None

    texture = find("nw.texture.noise2d") or find("nw.texture.procedural")
    torus = find("nw.mesh.torus")
    instances = find("nw.scene.instances")
    material = find("nw.material.standard")
    bloom = find("nw.post.bloom")
    tonemap = find("nw.post.tonemap")
    if not all((texture, torus, instances, material, bloom, tonemap)):
        return {
            "format": "nw08-size-attribution",
            "version": 1,
            "available": False,
            "reason": "graph is not the NW-07 procedural proof shape",
        }
    resolution_scale = _node_float(bloom, "resolution_scale", 0.5)
    bloom_width = max(1, int(round(width * resolution_scale)))
    bloom_height = max(1, int(round(height * resolution_scale)))
    noise = nw06_procedural.Noise2DParameters(
        width=_node_int(texture, "width", 256),
        height=_node_int(texture, "height", 256),
        seed=_node_int(texture, "seed", 0),
        octaves=_node_int(texture, "octaves", 4),
        scale=_node_float(texture, "scale", 8.0),
        lacunarity=_node_float(texture, "lacunarity", 2.0),
        gain=_node_float(texture, "gain", 0.5),
        format=(
            texture.params.get("format", ["r8g8b8a8_unorm"])[0]
            if texture.params.get("format")
            else "r8g8b8a8_unorm"
        ),
    )
    torus_params = nw06_procedural.TorusParameters(
        major_segments=_node_int(torus, "major_segments", 64),
        minor_segments=_node_int(torus, "minor_segments", 32),
        major_radius=_node_float(torus, "major_radius", 0.7),
        minor_radius=_node_float(torus, "minor_radius", 0.3),
    )
    instance_params = nw06_procedural.InstanceParameters(
        count=_node_int(instances, "count", 256),
        seed=_node_int(instances, "seed", 0),
        placement_radius=_node_float(instances, "placement_radius", 3.0),
        scale_min=_node_float(instances, "scale_min", 0.1),
        scale_max=_node_float(instances, "scale_max", 0.2),
        twist_radians=_node_float(instances, "twist_radians", math.pi),
    )
    material_params = nw06_procedural.MaterialParameters(
        base_color=(1.0, 1.0, 1.0, 1.0),
        roughness=_node_float(material, "roughness", 0.42),
        metallic=_node_float(material, "metallic", 0.0),
        specular=_node_float(material, "specular", 0.5),
        texture_index=_node_int(material, "texture_index", 0),
    )
    resources = nw06_procedural.estimate_resources(
        noise=noise,
        torus=torus_params,
        instances=instance_params,
        materials=material_params,
        width=width,
        height=height,
    )
    # The proof bloom is half resolution; replace the reference default when
    # the edited graph changes the scale.
    default_bloom_bytes = resources["bloom"]["bytes"]
    bloom_bytes = bloom_width * bloom_height * 16
    resources["bloom"].update(
        {
            "width": bloom_width,
            "height": bloom_height,
            "resolution_scale": resolution_scale,
            "bytes": bloom_bytes,
        }
    )
    resources["resident_payload_bytes"] = (
        resources["resident_payload_bytes"]
        - default_bloom_bytes
        + bloom_bytes
    )
    resources.update(
        {
            "format": "nw08-size-attribution",
            "version": 1,
            "available": True,
            "width": width,
            "height": height,
            "procedural_only": True,
            "baked_asset_bytes": 0,
            "pass_attribution": {
                "texture_generation": resources["texture"]["bytes"],
                "mesh_generation": resources["vertices"]["bytes"]
                + resources["indices"]["bytes"],
                "instance_generation": resources["instances"]["bytes"]
                + resources["indirect_args_bytes"],
                "material": resources["materials"]["bytes"],
                "scene_render": resources["scene_color_bytes"]
                + resources["depth_bytes"],
                "bloom": bloom_bytes,
                "tonemap": resources["final_color_bytes"],
                "timeline": resources["camera_curve_bytes"]
                + resources["light_bytes"],
            },
        }
    )
    return resources


# ---------------------------------------------------------------------------
# Export settings plumbing (single existing export path)
# ---------------------------------------------------------------------------


def export_settings_from_mapping(
    values: Mapping[str, Any] | None = None,
) -> nw05_export.ExportSettings:
    values = dict(values or {})
    allowed = set(nw05_export.ExportSettings.__dataclass_fields__)
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise EditorError(
            "unknown export setting(s): " + ", ".join(unknown)
        )
    for name in ("optional_profiles", "generation_overrides", "bake_overrides"):
        if name in values and values[name] is not None:
            values[name] = tuple(tuple(item) for item in values[name])
    try:
        return nw05_export.ExportSettings(**values)
    except (TypeError, ValueError) as exc:
        raise EditorError(f"invalid export settings: {exc}") from exc


def _safe_output_path(path: Path) -> Path:
    resolved = path.resolve()
    protected = (REPO_ROOT / "fixtures" / "nw07").resolve()
    if resolved == protected or protected in resolved.parents:
        raise EditorError(
            "refusing to write into the frozen fixtures/nw07 contract directory"
        )
    return resolved


@dataclass
class _EditorSnapshot:
    document: nw03_compiler.GraphDocument
    selection: tuple[str, ...]
    timeline_tick: int
    semantic_digest: str

    def clone(self) -> "_EditorSnapshot":
        return _EditorSnapshot(
            document=copy.deepcopy(self.document),
            selection=tuple(self.selection),
            timeline_tick=self.timeline_tick,
            semantic_digest=self.semantic_digest,
        )


class GraphEditorSession:
    """Interactive, undoable editor over an immutable NW-02 document model."""

    def __init__(
        self,
        document: nw03_compiler.GraphDocument,
        *,
        source_path: str | os.PathLike[str] | None = None,
        max_undo: int = 128,
    ):
        self._document = copy.deepcopy(document)
        self._source_path = Path(source_path) if source_path else None
        self._max_undo = max(1, int(max_undo))
        self._undo: list[_EditorSnapshot] = []
        self._redo: list[_EditorSnapshot] = []
        self._selection: tuple[str, ...] = ()
        self._timeline_tick = 0
        self._draft_text: str | None = None
        self._draft_diagnostics: list[dict[str, Any]] = []
        self._snapshot_generation = 0
        self._digest, self._digest_provenance = semantic_digest(self._document)
        self._hashes = structural_hashes(self._document)
        self._selection_history: list[tuple[str, ...]] = []

    # -- construction -----------------------------------------------------
    @classmethod
    def create_nw07_proof(
        cls, graph_path: str | os.PathLike[str] | None = None
    ) -> "GraphEditorSession":
        path = Path(graph_path) if graph_path else NW07_PROOF_GRAPH
        if not path.is_file():
            raise EditorError(f"NW-07 proof graph is unavailable: {path}")
        document = nw03_compiler.load_graph(path)
        return cls(document, source_path=path)

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        source_name: str = "<editor>",
        source_path: str | os.PathLike[str] | None = None,
    ) -> "GraphEditorSession":
        document, diagnostics = parse_graph_text(text, source_name=source_name)
        if document is None or any(
            item.get("severity") == "error" for item in diagnostics
        ):
            raise EditorError(
                f"graph '{source_name}' is invalid",
                [item for item in diagnostics if item.get("severity") == "error"],
            )
        return cls(document, source_path=source_path)

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "GraphEditorSession":
        return cls.create_nw07_proof(path)

    # -- read-only properties --------------------------------------------
    @property
    def document(self) -> nw03_compiler.GraphDocument:
        return self._document

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    @property
    def semantic_digest(self) -> str:
        return self._digest

    @property
    def semantic_digest_provenance(self) -> str:
        return self._digest_provenance

    @property
    def snapshot_generation(self) -> int:
        return self._snapshot_generation

    @property
    def selection(self) -> tuple[str, ...]:
        return self._selection

    @property
    def timeline_tick(self) -> int:
        return self._timeline_tick

    @property
    def draft_diagnostics(self) -> list[dict[str, Any]]:
        return list(self._draft_diagnostics)

    @property
    def source_text(self) -> str:
        return emit_graph_text(self._document)

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._document.nodes))

    @property
    def nodes(self) -> Mapping[str, nw03_compiler.GraphNode]:
        return self._document.nodes

    def node(self, node_id: str) -> nw03_compiler.GraphNode:
        try:
            return self._document.nodes[node_id]
        except KeyError as exc:
            raise EditorError(f"unknown node '{node_id}'") from exc

    def parameter_names(self, node_id: str) -> tuple[str, ...]:
        node = self.node(node_id)
        names = set(nw03_compiler.PARAM_RULES.get(node.operator_id, {}))
        names.update(name for name in node.params if name != "__seed__")
        return tuple(sorted(names))

    def parameter(self, node_id: str, name: str) -> tuple[str, ...]:
        node = self.node(node_id)
        return tuple(node.params.get(name, ()))

    def connections(self) -> tuple[dict[str, str], ...]:
        rows: list[dict[str, str]] = []
        for node_id in sorted(self._document.nodes):
            node = self._document.nodes[node_id]
            for port in sorted(node.inputs):
                source_node, source_port = node.inputs[port]
                rows.append(
                    {
                        "target_node": node_id,
                        "target_port": port,
                        "source_node": source_node,
                        "source_port": source_port,
                    }
                )
        return tuple(rows)

    def layout(self) -> dict[str, tuple[float, float]]:
        return {key: tuple(value) for key, value in self._document.layout.items()}

    # -- editor-only mutations -------------------------------------------
    def select(self, *node_ids: str) -> EditResult:
        for node_id in node_ids:
            if node_id and node_id not in self._document.nodes:
                raise EditorError(f"unknown node '{node_id}'")
        self._selection_history.append(self._selection)
        self._selection = tuple(node_id for node_id in node_ids if node_id)
        return EditResult(
            valid=True,
            semantic_digest=self._digest,
            previous_semantic_digest=self._digest,
            snapshot_generation=self._snapshot_generation,
            editor_metadata_changed=True,
            message="selection changed",
        )

    def set_layout(
        self, node_id: str, x: float, y: float
    ) -> EditResult:
        self.node(node_id)
        x = float(x)
        y = float(y)
        if not math.isfinite(x) or not math.isfinite(y):
            raise EditorError("layout coordinates must be finite")
        self._push_undo()
        self._document.layout[node_id] = (x, y)
        return EditResult(
            valid=True,
            semantic_digest=self._digest,
            previous_semantic_digest=self._digest,
            snapshot_generation=self._snapshot_generation,
            editor_metadata_changed=True,
            message="layout changed",
        )

    def set_timeline_tick(self, tick: int) -> EditResult:
        if isinstance(tick, bool) or not isinstance(tick, int):
            raise EditorError("timeline tick must be an integer")
        if tick < 0:
            raise EditorError("timeline tick must be non-negative")
        duration = self.timeline_duration()
        if duration is not None and tick > duration:
            tick = duration
        self._timeline_tick = tick
        return EditResult(
            valid=True,
            semantic_digest=self._digest,
            previous_semantic_digest=self._digest,
            snapshot_generation=self._snapshot_generation,
            editor_metadata_changed=True,
            message="timeline changed",
        )

    scrub = set_timeline_tick

    # -- semantic mutations ----------------------------------------------
    def set_parameter(
        self,
        node_id: str,
        name: str,
        value: str | float | int | Sequence[str | float | int],
    ) -> EditResult:
        candidate = self._clone_document()
        node = candidate.nodes.get(node_id)
        if node is None:
            raise EditorError(f"unknown node '{node_id}'")
        if name == "__seed__":
            raise EditorError("use set_seed() for seed behavior")
        tokens = _coerce_tokens(value)
        previous = list(node.params.get(name, ()))
        node.params[name] = tokens
        return self._commit(candidate, changed_hint={node_id})

    def set_curve_points(
        self,
        node_id: str,
        points: Sequence[CurvePoint | tuple[int, float] | tuple[int, int]],
    ) -> EditResult:
        node = self.node(node_id)
        parsed = [
            point
            if isinstance(point, CurvePoint)
            else CurvePoint(int(point[0]), float(point[1]))
            for point in points
        ]
        if not parsed:
            raise EditorError("curve must contain at least one key")
        value_min = None
        value_max = None
        if node.params.get("curve_value_min"):
            value_min = float(node.params["curve_value_min"][0])
        if node.params.get("curve_value_max"):
            value_max = float(node.params["curve_value_max"][0])
        try:
            nw06_timeline.Curve(
                tuple(
                    nw06_timeline.CurveKey(point.tick, point.value)
                    for point in parsed
                ),
                value_min=value_min,
                value_max=value_max,
            )
        except nw06_timeline.TimelineError as exc:
            raise EditorError(str(exc)) from exc
        text = ";".join(point.to_text() for point in parsed)
        return self.set_parameter(node_id, "keys", [text])

    def curve_points(self, node_id: str) -> tuple[CurvePoint, ...]:
        tokens = self._document.nodes[node_id].params.get("keys", [])
        text = " ".join(tokens)
        points: list[CurvePoint] = []
        for entry in text.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            tick_text, value_text = entry.split(":", 1)
            points.append(CurvePoint(int(tick_text), float(value_text)))
        return tuple(points)

    def connect(
        self,
        target_node: str,
        target_port: str,
        source_node: str,
        source_port: str,
    ) -> EditResult:
        candidate = self._clone_document()
        target = candidate.nodes.get(target_node)
        if target is None:
            raise EditorError(f"unknown node '{target_node}'")
        if source_node not in candidate.nodes:
            raise EditorError(f"unknown node '{source_node}'")
        target.inputs[target_port] = (source_node, source_port)
        return self._commit(candidate, changed_hint={target_node})

    def disconnect(self, target_node: str, target_port: str) -> EditResult:
        candidate = self._clone_document()
        target = candidate.nodes.get(target_node)
        if target is None:
            raise EditorError(f"unknown node '{target_node}'")
        if target_port not in target.inputs:
            raise EditorError(
                f"node '{target_node}' has no input connection '{target_port}'"
            )
        del target.inputs[target_port]
        return self._commit(candidate, changed_hint={target_node})

    def add_node(
        self,
        node_id: str,
        operator_id: str,
        *,
        version: str = "1.0.0",
        frequency: str = "frame",
        residency: str = "cpu",
        params: Mapping[str, str | Sequence[str | float | int]] | None = None,
        inputs: Mapping[str, tuple[str, str]] | None = None,
    ) -> EditResult:
        if node_id in self._document.nodes:
            raise EditorError(f"duplicate node id '{node_id}'")
        candidate = self._clone_document()
        candidate.nodes[node_id] = nw03_compiler.GraphNode(
            node_id=node_id,
            operator_id=operator_id,
            version=version,
            frequency=frequency,
            residency=residency,
            params={name: _coerce_tokens(value) for name, value in (params or {}).items()},
            inputs=dict(inputs or {}),
        )
        candidate.layout.setdefault(node_id, (0.0, 0.0))
        return self._commit(candidate, changed_hint={node_id})

    def remove_node(self, node_id: str) -> EditResult:
        if node_id not in self._document.nodes:
            raise EditorError(f"unknown node '{node_id}'")
        candidate = self._clone_document()
        del candidate.nodes[node_id]
        candidate.layout.pop(node_id, None)
        for node in candidate.nodes.values():
            for port, (source_node, _source_port) in list(node.inputs.items()):
                if source_node == node_id:
                    del node.inputs[port]
        affected = {
            node.node_id
            for node in candidate.nodes.values()
            if any(source == node_id for source, _ in node.inputs.values())
        }
        affected.add(node_id)
        return self._commit(candidate, changed_hint=affected)

    # -- undo / redo ------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def undo_available(self) -> bool:
        return self.can_undo

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def redo_available(self) -> bool:
        return self.can_redo

    def semantic_identity(self) -> str:
        return self._digest

    def undo(self) -> EditResult:
        if not self._undo:
            raise EditorError("nothing to undo")
        self._redo.append(self._current_snapshot())
        previous = self._undo.pop()
        return self._restore(previous)

    def redo(self) -> EditResult:
        if not self._redo:
            raise EditorError("nothing to redo")
        self._undo.append(self._current_snapshot())
        previous = self._redo.pop()
        return self._restore(previous)

    # -- timeline ---------------------------------------------------------
    def timeline_duration(self) -> int | None:
        for key in ("nw07_loop_ticks", "nw06_loop_ticks", "nw06_duration_ticks"):
            if key in self._document.settings:
                try:
                    return int(float(self._document.settings[key]))
                except ValueError:
                    continue
        return None

    def timeline_state(self) -> TimelineState:
        tick = self._timeline_tick
        duration = self.timeline_duration() or tick
        frame_step = nw06_timeline.RationalFrameStep(
            _setting_int(self._document, "nw06_frame_step_numerator", 800),
            _setting_int(self._document, "nw06_frame_step_denominator", 1),
        )
        camera_angle = None
        camera_modulo = None
        twist = None
        try:
            sample = nw07_export_sample(self._document, tick)
            camera_angle = sample["camera_angle_radians"]
            camera_modulo = sample["camera_angle_modulo_turn"]
            twist = sample["twist_degrees"]
        except Exception:
            pass
        return TimelineState(
            tick=tick,
            frame_index=(
                int(tick / frame_step.ticks_per_frame)
                if frame_step.ticks_per_frame
                else 0
            ),
            duration_ticks=duration,
            tick_rate=_setting_int(
                self._document, "nw06_ticks_per_second", 48000
            ),
            camera_angle_radians=camera_angle,
            camera_angle_modulo_turn=camera_modulo,
            twist_degrees=twist,
        )

    # -- persistence ------------------------------------------------------
    def save(self, path: str | os.PathLike[str]) -> Path:
        destination = _safe_output_path(Path(path))
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = self.source_text
        # Verify the exact bytes that will be written before replacing a file.
        document, diagnostics = parse_graph_text(text, source_name=str(destination))
        errors = [
            item for item in diagnostics if item.get("severity") == "error"
        ]
        if document is None or errors:
            raise EditorError("editor draft is not serializable", errors)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".nw08-save-", suffix=".nwgraph", dir=str(destination.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
        finally:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
        self._source_path = destination
        return destination

    # -- export via the existing compiler/export/package path -------------
    def export(
        self,
        output_directory: str | os.PathLike[str],
        *,
        settings: nw05_export.ExportSettings | Mapping[str, Any] | None = None,
        report_path: str | os.PathLike[str] | None = None,
        dxc_executable: str | None = None,
        optimization: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        allow_unpinned_dxc: bool = False,
        allow_test_payloads: bool = False,
        allocation_query: Mapping[Any, Any] | None = None,
        adapter_identity: str = "unavailable-native-adapter",
    ) -> nw05_export.ExportResult:
        output = Path(output_directory)
        output.parent.mkdir(parents=True, exist_ok=True)
        text = self.source_text
        document, diagnostics = parse_graph_text(
            text, source_name=str(self._source_path or "<editor>")
        )
        errors = [
            item for item in diagnostics if item.get("severity") == "error"
        ]
        if document is None or errors:
            raise EditorError(
                "cannot export an invalid editor draft",
                link_source_diagnostics(
                    errors,
                    source_text=text,
                    source_name=str(self._source_path or "<editor>"),
                ),
            )
        if settings is None:
            export_settings = nw05_export.ExportSettings()
        elif isinstance(settings, Mapping):
            export_settings = export_settings_from_mapping(settings)
        else:
            export_settings = settings
        effective_optimization = optimization or self._document.settings.get(
            "optimization", "O2"
        )
        with tempfile.TemporaryDirectory(
            prefix=".nw08-export-source-", dir=str(output.parent)
        ) as staging:
            graph_path = Path(staging) / "editor-graph.nwgraph"
            graph_path.write_text(text, encoding="utf-8", newline="\n")
            try:
                return nw05_export.export_graph(
                    graph_path,
                    output,
                    settings=export_settings,
                    report_path=report_path,
                    dxc_executable=dxc_executable,
                    optimization=effective_optimization,
                    cache_dir=cache_dir,
                    allow_unpinned_dxc=allow_unpinned_dxc,
                    allow_test_payloads=allow_test_payloads,
                    allocation_query=allocation_query,
                    adapter_identity=adapter_identity,
                )
            except (nw05_export.ExportError, nw03_compiler.CompilerError) as exc:
                linked = link_source_diagnostics(
                    getattr(exc, "diagnostics", []),
                    source_text=text,
                    source_name=str(self._source_path or "<editor>"),
                )
                raise EditorError(str(exc), linked) from exc

    def export_report(
        self,
        output_directory: str | os.PathLike[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = self.export(output_directory, **kwargs)
        common = result.artifacts[nwp_package.COMMON_PROFILE_ID]
        attribution = estimate_size_attribution(self._document)
        return {
            "format": "nw08-studio-export",
            "version": 1,
            "semantic_digest": self._digest,
            "semantic_digest_provenance": self._digest_provenance,
            "profile_id": self._document.profile_id,
            "requirements_digest": self._document.requirements_digest,
            "output_directory": result.output_directory,
            "common_artifact_key": result.common_artifact_key,
            "package_sha256": hashlib.sha256(common.package_bytes).hexdigest(),
            "package_bytes": len(common.package_bytes),
            "manifest_sha256": common.manifest["manifest_sha256"],
            "executable_sha256": (
                hashlib.sha256(common.executable_bytes).hexdigest()
                if common.executable_bytes is not None
                else ""
            ),
            "executable_bytes": (
                len(common.executable_bytes)
                if common.executable_bytes is not None
                else 0
            ),
            "size_attribution": attribution,
            "settings": export_settings_from_mapping(kwargs.get("settings")).to_dict()
            if isinstance(kwargs.get("settings"), Mapping)
            else None,
        }

    def cache_key_inputs(
        self,
        *,
        toolchain: ToolchainIdentity | None = None,
        compiler_settings: Mapping[str, Any] | None = None,
        semantic_inputs: Mapping[str, Any] | None = None,
        compiler_inputs: Mapping[str, Any] | None = None,
        observed_hardware: Mapping[str, Any] | None = None,
        shader_identity: str = "",
        entry_point: str = "",
    ) -> CacheKeyInputs:
        return CacheKeyInputs(
            semantic_digest=self._digest,
            capability_profile=CapabilityProfileIdentity(
                self._document.profile_id,
                self._document.requirements_digest,
            ),
            target_profile_id=self._document.profile_id,
            target_requirements_digest=self._document.requirements_digest,
            toolchain=toolchain or ToolchainIdentity(),
            compiler_settings=(
                dict(self._document.settings)
                if compiler_settings is None
                else dict(compiler_settings)
            ),
            semantic_inputs=dict(semantic_inputs or {}),
            compiler_inputs=dict(compiler_inputs or {}),
            observed_hardware=dict(observed_hardware or {}),
            shader_identity=shader_identity,
            entry_point=entry_point,
        )

    # -- internals --------------------------------------------------------
    def _clone_document(self) -> nw03_compiler.GraphDocument:
        return copy.deepcopy(self._document)

    def _current_snapshot(self) -> _EditorSnapshot:
        return _EditorSnapshot(
            document=copy.deepcopy(self._document),
            selection=tuple(self._selection),
            timeline_tick=self._timeline_tick,
            semantic_digest=self._digest,
        )

    def _push_undo(self) -> None:
        self._undo.append(self._current_snapshot())
        if len(self._undo) > self._max_undo:
            self._undo.pop(0)
        self._redo.clear()

    def _commit(
        self,
        candidate: nw03_compiler.GraphDocument,
        *,
        changed_hint: Iterable[str] = (),
    ) -> EditResult:
        text = emit_graph_text(candidate)
        parsed, diagnostics = parse_graph_text(
            text, source_name=str(self._source_path or "<editor>")
        )
        errors = [
            item for item in diagnostics if item.get("severity") == "error"
        ]
        if parsed is None or errors:
            self._draft_text = text
            self._draft_diagnostics = link_source_diagnostics(
                errors or diagnostics,
                source_text=text,
                source_name=str(self._source_path or "<editor>"),
            )
            return EditResult(
                valid=False,
                message="edit rejected; last valid snapshot retained",
                diagnostics=self._draft_diagnostics,
                linked_diagnostics=self._draft_diagnostics,
                semantic_digest=self._digest,
                previous_semantic_digest=self._digest,
                snapshot_generation=self._snapshot_generation,
            )
        new_hashes = structural_hashes(parsed)
        changed = sorted(
            node_id
            for node_id in set(self._hashes) | set(new_hashes)
            if self._hashes.get(node_id) != new_hashes.get(node_id)
        )
        if not changed and changed_hint:
            # A connection that is not semantically meaningful may not change
            # any structural hash; keep the hint for UI feedback but do not
            # invent invalidation.
            changed = sorted(set(changed_hint) & set(parsed.nodes))
        closure = invalidated_closure(parsed, changed)
        previous_digest = self._digest
        new_digest, provenance = semantic_digest(parsed)
        changed_semantics = new_digest != previous_digest
        self._push_undo()
        self._document = parsed
        self._hashes = new_hashes
        self._digest = new_digest
        self._digest_provenance = provenance
        self._draft_text = None
        self._draft_diagnostics = []
        if changed_semantics:
            self._snapshot_generation += 1
        recomputed = sorted(closure)
        cached = sorted(set(parsed.nodes) - closure)
        return EditResult(
            valid=True,
            message="edit applied",
            semantic_digest=new_digest,
            previous_semantic_digest=previous_digest,
            snapshot_generation=self._snapshot_generation,
            changed_nodes=tuple(changed),
            recomputed_nodes=tuple(recomputed),
            cached_nodes=tuple(cached),
            affected_passes=tuple(
                node_id
                for node_id in recomputed
                if parsed.nodes[node_id].frequency == "frame"
            ),
            recomputed_resources=tuple(
                node_id
                for node_id in recomputed
                if parsed.nodes[node_id].residency == "gpu"
                or parsed.nodes[node_id].operator_id.startswith("nw.residency.")
            ),
            editor_metadata_changed=False,
        )

    def _restore(self, snapshot: _EditorSnapshot) -> EditResult:
        self._document = copy.deepcopy(snapshot.document)
        self._selection = tuple(snapshot.selection)
        self._timeline_tick = snapshot.timeline_tick
        self._hashes = structural_hashes(self._document)
        previous_digest = self._digest
        self._digest, self._digest_provenance = semantic_digest(self._document)
        changed = self._digest != previous_digest
        if changed:
            self._snapshot_generation += 1
        self._draft_text = None
        self._draft_diagnostics = []
        return EditResult(
            valid=True,
            message="undo/redo applied",
            semantic_digest=self._digest,
            previous_semantic_digest=previous_digest,
            snapshot_generation=self._snapshot_generation,
            changed_nodes=tuple(sorted(self._hashes)),
            recomputed_nodes=tuple(sorted(self._hashes)),
            cached_nodes=(),
            editor_metadata_changed=not changed,
        )

    def to_report(self) -> dict[str, Any]:
        return {
            "format": "nw08-studio-session",
            "version": 1,
            "semantic_digest": self._digest,
            "semantic_digest_provenance": self._digest_provenance,
            "snapshot_generation": self._snapshot_generation,
            "profile_id": self._document.profile_id,
            "requirements_digest": self._document.requirements_digest,
            "source_path": str(self._source_path) if self._source_path else "",
            "nodes": list(self.node_ids),
            "connections": list(self.connections()),
            "layout": {
                node_id: {"x": x, "y": y}
                for node_id, (x, y) in sorted(self._document.layout.items())
            },
            "selection": list(self._selection),
            "timeline": self.timeline_state().to_dict(),
            "draft_diagnostics": self._draft_diagnostics,
            "can_undo": self.can_undo,
            "can_redo": self.can_redo,
        }


def _coerce_tokens(
    value: str | float | int | Sequence[str | float | int] | None,
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, Sequence):
        tokens: list[str] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                raise EditorError("parameter tokens must be strings or numbers")
            tokens.append(str(item))
        return tokens
    raise EditorError("unsupported parameter value type")


def _setting_int(
    document: nw03_compiler.GraphDocument, key: str, default: int
) -> int:
    try:
        return int(float(document.settings.get(key, default)))
    except (TypeError, ValueError):
        return default


def nw07_export_sample(
    document: nw03_compiler.GraphDocument, tick: int
) -> dict[str, Any]:
    # Imported lazily to keep the editor import light and to avoid a cycle with
    # verifier tooling that imports both modules.
    import nw07_export

    return nw07_export.evaluate_camera_at_tick(document, tick)


def load_proof_graph(
    path: str | os.PathLike[str] | None = None,
) -> GraphEditorSession:
    """Compatibility helper for callers that only need the proof session."""
    return GraphEditorSession.create_nw07_proof(path)


# Descriptive aliases used by GUI/automation code.
EditorSession = GraphEditorSession
StudioSession = GraphEditorSession
GraphEditor = GraphEditorSession


__all__ = [
    "COMMON_PROFILE_ID",
    "COMMON_REQUIREMENTS_DIGEST",
    "CurvePoint",
    "EditResult",
    "EditorError",
    "EditorSession",
    "GraphEditor",
    "GraphEditorSession",
    "NW07_PROOF_GRAPH",
    "TimelineState",
    "emit_graph_text",
    "estimate_size_attribution",
    "export_settings_from_mapping",
    "invalidated_closure",
    "link_source_diagnostics",
    "load_proof_graph",
    "parse_graph_text",
    "semantic_digest",
    "structural_hashes",
    "StudioSession",
]
