#!/usr/bin/env python3
"""NW-08 preview pipeline: asynchronous generation tokens and last-valid preview.

Preview execution reuses the existing runtime pass implementation.  On Windows,
``WindowsRuntimePreviewBackend`` launches the existing package player (the same
``PackagePlayer`` / generated-player path used for release) and captures its
presented tick.  On non-Windows hosts the deterministic reference backend is
used only for contract tests and is explicitly *not* hardware evidence.

All asynchronous completions are gated by both the immutable graph snapshot
generation and the D3D12 device-instance recreation epoch.  A failed, cancelled,
or obsolete job can never replace the last valid preview.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import nw03_compiler  # noqa: E402
import nw05_export  # noqa: E402
import nw06_procedural  # noqa: E402
import nw07_export  # noqa: E402
import nwp_package  # noqa: E402
from nw08_cache import (  # noqa: E402
    AdapterDeviceIdentity,
    CacheIdentityError,
    CacheKeyInputs,
    CapabilityProfileIdentity,
    CompletionDecision,
    DeviceEpochManager,
    DeviceGeneration,
    DeviceLostError,
    GenerationToken,
    GpuBudgetExceeded,
    GpuRealizationCache,
    AsyncJobQueue,
    HostDeviceIdentity,
    ToolchainIdentity,
    discover_host_device_identity,
    sha256_value,
)
from nw08_editor import (  # noqa: E402
    EditResult,
    GraphEditorSession,
    emit_graph_text,
    estimate_size_attribution,
    link_source_diagnostics,
    node_size_attribution,
    semantic_digest as editor_semantic_digest,
    structural_hashes,
)

NO_SHADER_IDENTITY = sha256_value(
    {"domain": "nw08.preview.no-shader.v1"}
)

# Mirrors src/player/exit_codes.h ExitCode::DeviceRemoved.  Device removal is a
# lost-device condition, never a recoverable layout mismatch.
DEVICE_REMOVED_EXIT_CODE = 51

# Mirrors nw::package::kTimelineTicksPerSimulationTick.  The authoring session
# only needs it to decide whether a scrub target precedes the last committed
# simulation tick and therefore requires the explicit History rewind/replay
# boundary.
HISTORY_TIMELINE_TICKS_PER_SIMULATION_TICK = 400


class PreviewError(RuntimeError):
    """Raised when preview execution cannot produce a valid frame."""

    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


@dataclass(frozen=True)
class PassTiming:
    name: str
    milliseconds: float
    kind: str = "reference"
    measured: bool = True
    available: bool = True
    # Timings nested inside another measured interval (for example GPU work
    # inside the process wall time) must not be added again to the total.
    counts_toward_total: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise PreviewError("pass timing requires a name")
        if self.milliseconds < 0.0:
            raise PreviewError("pass timing must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "milliseconds": self.milliseconds,
            "kind": self.kind,
            "measured": self.measured,
            "available": self.available,
            "counts_toward_total": self.counts_toward_total,
        }


@dataclass(frozen=True)
class PassTimings:
    timings: tuple[PassTiming, ...]

    @property
    def total_milliseconds(self) -> float:
        return sum(
            item.milliseconds
            for item in self.timings
            if item.counts_toward_total and item.available
        )

    @property
    def complete(self) -> bool:
        return all(item.available for item in self.timings)

    def by_name(self) -> dict[str, float]:
        return {item.name: item.milliseconds for item in self.timings}

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_milliseconds": self.total_milliseconds,
            "passes": [item.to_dict() for item in self.timings],
        }


@dataclass(frozen=True)
class PreviewFrame:
    tick: int
    width: int
    height: int
    image_sha256: str
    semantic_digest: str
    camera_angle_radians: float | None = None
    camera_angle_modulo_turn: float | None = None
    twist_degrees: float | None = None
    content_digest: str = ""
    resident_bytes: int = 0
    pass_timings: PassTimings = field(default_factory=lambda: PassTimings(()))
    size_attribution: Mapping[str, Any] = field(default_factory=dict)
    runtime_stats: Mapping[str, Any] = field(default_factory=dict)
    shader_identity: str = ""
    binding_identity: str = ""
    node_shader_identities: Mapping[str, str] = field(default_factory=dict)
    backend: str = "reference"
    hardware_evidence: bool = False
    execution_kind: str = "contract_reference"
    diagnostics: tuple[dict[str, Any], ...] = ()
    # The captured presented pixels are retained in memory so the GUI can
    # display the last accepted frame after failed edits.  They are never
    # serialized into reports.
    image_data: bytes = field(default=b"", repr=False, compare=False)
    image_format: str = "rgba32f"
    device_epoch: int | None = None
    device_identity: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "width": self.width,
            "height": self.height,
            "image_sha256": self.image_sha256,
            "semantic_digest": self.semantic_digest,
            "camera_angle_radians": self.camera_angle_radians,
            "camera_angle_modulo_turn": self.camera_angle_modulo_turn,
            "twist_degrees": self.twist_degrees,
            "content_digest": self.content_digest,
            "resident_bytes": self.resident_bytes,
            "pass_timings": self.pass_timings.to_dict(),
            "size_attribution": dict(self.size_attribution),
            "runtime_stats": dict(self.runtime_stats),
            "shader_identity": self.shader_identity,
            "binding_identity": self.binding_identity,
            "node_shader_identities": dict(self.node_shader_identities),
            "backend": self.backend,
            "hardware_evidence": self.hardware_evidence,
            "execution_kind": self.execution_kind,
            "diagnostics": list(self.diagnostics),
            "image_bytes": len(self.image_data),
            "image_format": self.image_format,
            "has_image": bool(self.image_data),
            "device_epoch": self.device_epoch,
            "device_identity": dict(self.device_identity),
        }

    @property
    def has_image(self) -> bool:
        return bool(self.image_data)


@dataclass(frozen=True)
class PreviewRecord:
    token: GenerationToken
    frame: PreviewFrame
    compiler_key: str
    device_realization_key: str
    cache_hit: bool
    node_realization_keys: tuple[tuple[str, str], ...] = ()
    cached_nodes: tuple[str, ...] = ()
    recomputed_nodes: tuple[str, ...] = ()
    realization_persistent: bool = True
    document_identity: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.token.job_id,
            "snapshot_generation": self.token.snapshot_generation,
            "device_epoch": self.token.device_epoch,
            "semantic_digest": self.token.semantic_digest,
            "compiler_key": self.compiler_key,
            "device_realization_key": self.device_realization_key,
            "cache_hit": self.cache_hit,
            "node_realization_keys": {
                node_id: key for node_id, key in self.node_realization_keys
            },
            "cached_nodes": list(self.cached_nodes),
            "recomputed_nodes": list(self.recomputed_nodes),
            "realization_persistent": self.realization_persistent,
            "document_identity": dict(self.document_identity),
            "frame": self.frame.to_dict(),
        }

    @property
    def snapshot_generation(self) -> int:
        return self.token.snapshot_generation

    @property
    def device_epoch(self) -> int:
        return self.token.device_epoch

    @property
    def semantic_digest(self) -> str:
        return self.token.semantic_digest


class PreviewBackend(Protocol):
    name: str
    hardware_evidence: bool

    def render(
        self,
        document: nw03_compiler.GraphDocument,
        tick: int,
        *,
        width: int,
        height: int,
    ) -> PreviewFrame: ...


def _node(document: nw03_compiler.GraphDocument, operator_id: str) -> nw03_compiler.GraphNode | None:
    for node in document.nodes.values():
        if node.operator_id == operator_id:
            return node
    return None


def _float_param(node: nw03_compiler.GraphNode | None, name: str, default: float) -> float:
    if node is None:
        return default
    tokens = nw03_compiler._param_tokens(node, name)  # type: ignore[attr-defined]
    if not tokens:
        return default
    try:
        value = float(tokens[0])
    except ValueError:
        return default
    return value if value == value and abs(value) != float("inf") else default


def _int_param(node: nw03_compiler.GraphNode | None, name: str, default: int) -> int:
    return int(_float_param(node, name, float(default)))


def _toolchain_text(toolchain: ToolchainIdentity) -> str:
    return sha256_value(
        {"domain": "nw08.toolchain-identity.v1", **toolchain.to_key_fields()}
    )


def toolchain_identity_for(
    dxc_executable: str | os.PathLike[str] | None,
    *,
    optimization: str = "O2",
    os_build: str = "",
    compiler_id: str = "nodewerkc",
    compiler_version: str = "1",
    target: str = "d3d12.fl12_0.sm6_0",
) -> ToolchainIdentity:
    """Build the toolchain identity actually used for compilation.

    When a DXC executable is configured its measured version is part of every
    compiler and device-resident key.  A missing/unqueryable DXC is represented
    explicitly as ``unavailable`` rather than silently retaining the default
    pinned version string.
    """
    dxc_version = "unavailable"
    if dxc_executable:
        try:
            measured = nw03_compiler.query_dxc_version(str(dxc_executable))
        except Exception:
            measured = ""
        if measured:
            dxc_version = measured
    return ToolchainIdentity(
        compiler_id=compiler_id,
        compiler_version=compiler_version,
        dxc_version=dxc_version,
        target=target,
        optimization=optimization,
        os_build=os_build,
    )


def _procedural_inputs(
    document: nw03_compiler.GraphDocument,
) -> tuple[
    nw06_procedural.Noise2DParameters,
    nw06_procedural.TorusParameters,
    nw06_procedural.InstanceParameters,
    nw06_procedural.MaterialParameters,
]:
    texture = _node(document, "nw.texture.noise2d") or _node(
        document, "nw.texture.procedural"
    )
    torus_node = _node(document, "nw.mesh.torus")
    instances_node = _node(document, "nw.scene.instances")
    material_node = _node(document, "nw.material.standard")
    noise = nw06_procedural.Noise2DParameters(
        width=_int_param(texture, "width", 256),
        height=_int_param(texture, "height", 256),
        seed=_int_param(texture, "seed", 0),
        octaves=_int_param(texture, "octaves", 4),
        scale=_float_param(texture, "scale", 8.0),
        lacunarity=_float_param(texture, "lacunarity", 2.0),
        gain=_float_param(texture, "gain", 0.5),
        format=(
            texture.params.get("format", ["r8g8b8a8_unorm"])[0]
            if texture and texture.params.get("format")
            else "r8g8b8a8_unorm"
        ),
    )
    torus = nw06_procedural.TorusParameters(
        major_segments=_int_param(torus_node, "major_segments", 64),
        minor_segments=_int_param(torus_node, "minor_segments", 32),
        major_radius=_float_param(torus_node, "major_radius", 0.7),
        minor_radius=_float_param(torus_node, "minor_radius", 0.3),
    )
    instances = nw06_procedural.InstanceParameters(
        count=_int_param(instances_node, "count", 256),
        seed=_int_param(instances_node, "seed", 0),
        placement_radius=_float_param(instances_node, "placement_radius", 3.0),
        scale_min=_float_param(instances_node, "scale_min", 0.1),
        scale_max=_float_param(instances_node, "scale_max", 0.2),
        twist_radians=_float_param(instances_node, "twist_radians", 3.141592653589793),
    )
    materials = nw06_procedural.MaterialParameters(
        base_color=(1.0, 1.0, 1.0, 1.0),
        roughness=_float_param(material_node, "roughness", 0.42),
        metallic=_float_param(material_node, "metallic", 0.0),
        specular=_float_param(material_node, "specular", 0.5),
        texture_index=_int_param(material_node, "texture_index", 0),
    )
    return noise, torus, instances, materials


class ReferencePreviewBackend:
    """Deterministic contract/reference preview (never hardware evidence)."""

    name = "contract-reference"
    hardware_evidence = False
    persistent_realizations = True
    reserves_gpu_memory = False

    def render(
        self,
        document: nw03_compiler.GraphDocument,
        tick: int,
        *,
        width: int,
        height: int,
    ) -> PreviewFrame:
        timings: list[PassTiming] = []
        noise, torus, instances, materials = _procedural_inputs(document)

        start = time.perf_counter()
        nw06_procedural.procedural_content_digest(
            noise=noise, torus=torus, instances=instances, materials=materials
        )
        timings.append(
            PassTiming("initialization_reference", (time.perf_counter() - start) * 1000.0)
        )

        start = time.perf_counter()
        sample: Mapping[str, Any]
        try:
            sample = nw07_export.evaluate_camera_at_tick(document, tick)
        except Exception:
            sample = {
                "camera_angle_radians": None,
                "camera_angle_modulo_turn": None,
                "twist_degrees": None,
            }
        timings.append(
            PassTiming("timeline_sample", (time.perf_counter() - start) * 1000.0)
        )

        start = time.perf_counter()
        resources = estimate_size_attribution(document, width=width, height=height)
        timings.append(
            PassTiming("size_attribution", (time.perf_counter() - start) * 1000.0)
        )

        content_digest = nw06_procedural.procedural_content_digest(
            noise=noise, torus=torus, instances=instances, materials=materials
        )
        identity = {
            "content_digest": content_digest,
            "tick": tick,
            "width": width,
            "height": height,
            "camera": sample,
            "semantic_digest": editor_semantic_digest(document)[0],
        }
        image_sha256 = sha256_value(identity)
        resident_bytes = int(resources.get("resident_payload_bytes", 0))
        return PreviewFrame(
            tick=tick,
            width=width,
            height=height,
            image_sha256=image_sha256,
            semantic_digest="",
            camera_angle_radians=sample.get("camera_angle_radians"),
            camera_angle_modulo_turn=sample.get("camera_angle_modulo_turn"),
            twist_degrees=sample.get("twist_degrees"),
            content_digest=content_digest,
            resident_bytes=resident_bytes,
            pass_timings=PassTimings(tuple(timings)),
            size_attribution=resources,
            backend=self.name,
            hardware_evidence=False,
            execution_kind="contract_reference",
        )


@dataclass(frozen=True)
class PreviewExportBundle:
    """Artifacts exported from the exact edited document for one preview."""

    package_sha256: str
    package_path: Path | None = None
    executable_path: Path | None = None
    capture_width: int = 0
    capture_height: int = 0
    stats_path: Path | None = None
    shader_identity: str = ""
    binding_identity: str = ""
    node_shader_identities: Mapping[str, str] = field(default_factory=dict)
    export_report: Mapping[str, Any] = field(default_factory=dict)


def _export_preview_identities(
    artifact: Any,
) -> tuple[str, str, dict[str, str]]:
    """Derive the actual generated shader/binding identities of an export.

    These identities come from the compiled package, not from a second graph
    model: shader rows are the export manifest's compiled specializations and
    bindings are the emitted package resource/root-signature descriptors.
    """
    manifest = getattr(artifact, "manifest", {}) or {}
    shader_rows = manifest.get("shaders") or []
    canonical_shaders: list[dict[str, Any]] = []
    node_entries: dict[str, list[str]] = {}
    for row in shader_rows:
        normalized = {
            "specialization_id": str(row.get("specialization_id", "")),
            "target": str(row.get("target", "")),
            "blob_sha256": str(row.get("blob_sha256", "")),
            "hlsl_sha256": str(row.get("hlsl_sha256", "")),
            "execution_root": str(row.get("execution_root", "")),
            "execution_order": int(row.get("execution_order", 0) or 0),
            "output_region": int(row.get("output_region", 0) or 0),
            "region_count": int(row.get("region_count", 1) or 1),
            "unfused": bool(row.get("unfused", False)),
            "root_node_ids": sorted(
                str(node_id) for node_id in (row.get("root_node_ids") or [])
            ),
        }
        canonical_shaders.append(normalized)
        signature = sha256_value(
            {
                "domain": "nw08.preview.generated-shader.v1",
                "shader": normalized,
            }
        )
        for node_id in normalized["root_node_ids"]:
            node_entries.setdefault(node_id, []).append(signature)
    shader_identity = sha256_value(
        {
            "domain": "nw08.preview.shader-set.v1",
            "shaders": sorted(
                canonical_shaders,
                key=lambda item: (
                    item["specialization_id"],
                    item["target"],
                    item["blob_sha256"],
                ),
            ),
        }
    )
    package = getattr(artifact, "package", None)
    resources = (
        [resource.to_dict() for resource in package.resources]
        if package is not None
        else []
    )
    root_signatures = (
        [record.to_dict() for record in package.root_signatures]
        if package is not None
        else []
    )
    package_shaders = (
        [
            {
                "entry_point": shader.entry_point,
                "target": shader.target,
                "blob_sha256": nwp_package.sha256_hex(shader.blob),
                "root_signature_id": shader.root_signature_id,
                "family": shader.family,
                "thread_group_x": shader.thread_group_x,
                "thread_group_y": shader.thread_group_y,
                "thread_group_z": shader.thread_group_z,
            }
            for shader in package.shaders
        ]
        if package is not None
        else []
    )
    binding_identity = sha256_value(
        {
            "domain": "nw08.preview.binding.v1",
            "resources": sorted(
                resources, key=lambda item: int(item["resource_id"])
            ),
            "root_signatures": sorted(
                root_signatures,
                key=lambda item: int(item["root_signature_id"]),
            ),
            "shaders": sorted(
                package_shaders,
                key=lambda item: (
                    item["entry_point"],
                    item["target"],
                    item["blob_sha256"],
                ),
            ),
        }
    )
    node_shader_identities = {
        node_id: sha256_value(
            {
                "domain": "nw08.preview.node-shader.v1",
                "node_id": node_id,
                "shaders": sorted(signatures),
            }
        )
        for node_id, signatures in node_entries.items()
    }
    return shader_identity, binding_identity, node_shader_identities


def _package_capture_dimensions(
    package: Any, *, fallback_width: int, fallback_height: int
) -> tuple[int, int]:
    """Return the presentation dimensions the package player will read back."""
    present_resource_id = 0
    for instruction in getattr(package, "frame", ()) or ():
        if getattr(instruction, "opcode", "") != "frame.present":
            continue
        operands = getattr(instruction, "operands", ()) or ()
        if operands:
            operand = operands[0]
            if getattr(operand, "type", "") == "resource_ref":
                present_resource_id = int(getattr(operand, "value", 0) or 0)
            break

    present_resource = None
    for resource in getattr(package, "resources", ()) or ():
        if int(getattr(resource, "resource_id", 0) or 0) == present_resource_id:
            present_resource = resource
            break

    # PackagePlayer resolves buffer-backed presentation geometry from these
    # explicit package parameters before it falls back to shape inference.
    # Mirror that contract here so the Python side validates the same byte
    # count the native runtime actually writes.
    explicit: dict[str, int] = {}
    for parameter in getattr(package, "parameters", ()) or ():
        name = str(getattr(parameter, "name", "") or "")
        if name not in {"nw05.present.width", "nw05.present.height"}:
            continue
        if str(getattr(parameter, "type", "") or "") != "uint":
            continue
        if int(getattr(parameter, "component_count", 0) or 0) != 1:
            continue
        value = bytes(getattr(parameter, "value", b"") or b"")
        if len(value) < 4:
            continue
        explicit[name] = int.from_bytes(value[:4], "little", signed=False)

    if present_resource is not None:
        size_bytes = int(getattr(present_resource, "size_bytes", 0) or 0)
        pixel_count = size_bytes // 16 if size_bytes >= 16 else 0
        explicit_width = explicit.get("nw05.present.width", 0)
        explicit_height = explicit.get("nw05.present.height", 0)
        if (
            explicit_width > 0
            and explicit_height > 0
            and pixel_count > 0
            and explicit_width * explicit_height <= pixel_count
        ):
            return explicit_width, explicit_height

        width = int(getattr(present_resource, "width", 0) or 0)
        height = int(getattr(present_resource, "height", 0) or 0)
        if width > 0 and height > 0:
            return width, height

    return max(1, int(fallback_width)), max(1, int(fallback_height))


class PreviewExporter(Protocol):
    def export(
        self,
        document: nw03_compiler.GraphDocument,
        work_directory: Path,
        *,
        width: int,
        height: int,
    ) -> PreviewExportBundle: ...


class RuntimePackageExporter:
    """Compile the edited document through the established export path.

    In ``package-host`` mode the exported ``.nwp`` is handed to the native
    authoring preview host.  In ``embedded-player`` mode the generated player
    produced by the same export path is launched with the diagnostic capture
    controls.  Either way the renderer consumes bytes compiled from the edited
    graph, never a frozen embedded package from an unrelated player.
    """

    def __init__(
        self,
        *,
        mode: str = "package-host",
        dxc_executable: str | None = None,
        player_template: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        settings: nw05_export.ExportSettings | Mapping[str, Any] | None = None,
        allow_test_payloads: bool = False,
        optimization: str = "O2",
        export_function: Callable[..., Any] | None = None,
    ):
        if mode not in {"package-host", "embedded-player"}:
            raise PreviewError(f"unsupported preview export mode '{mode}'")
        self.mode = mode
        self.dxc_executable = dxc_executable
        self.player_template = player_template or ""
        self.cache_dir = cache_dir
        if isinstance(settings, Mapping):
            from nw08_editor import export_settings_from_mapping

            self.settings: nw05_export.ExportSettings = export_settings_from_mapping(
                settings
            )
        else:
            self.settings = settings or nw05_export.ExportSettings()
        self.allow_test_payloads = allow_test_payloads
        self.optimization = optimization
        self._export_function = export_function or nw05_export.export_graph

    def export(
        self,
        document: nw03_compiler.GraphDocument,
        work_directory: Path,
        *,
        width: int,
        height: int,
    ) -> PreviewExportBundle:
        work_directory.mkdir(parents=True, exist_ok=True)
        graph_path = work_directory / "edited-preview.nwgraph"
        graph_path.write_text(
            emit_graph_text(document), encoding="utf-8", newline="\n"
        )
        output_directory = work_directory / "export"
        settings = self.settings
        if self.player_template and not settings.player_template:
            settings = replace(settings, player_template=self.player_template)
        if self.allow_test_payloads and self.player_template:
            settings = replace(settings, player_template=str(self.player_template))
        result = self._export_function(
            graph_path,
            output_directory,
            settings=settings,
            dxc_executable=self.dxc_executable,
            optimization=self.optimization,
            cache_dir=str(self.cache_dir) if self.cache_dir is not None else None,
            allow_test_payloads=self.allow_test_payloads,
        )
        artifact = result.artifacts[nwp_package.COMMON_PROFILE_ID]
        package_sha = hashlib.sha256(artifact.package_bytes).hexdigest()
        shader_identity, binding_identity, node_shader_identities = (
            _export_preview_identities(artifact)
        )
        capture_width, capture_height = _package_capture_dimensions(
            artifact.package,
            fallback_width=width,
            fallback_height=height,
        )
        if self.mode == "package-host":
            package_path = work_directory / "edited-preview.nwp"
            package_path.write_bytes(artifact.package_bytes)
            return PreviewExportBundle(
                package_sha256=package_sha,
                package_path=package_path,
                capture_width=capture_width,
                capture_height=capture_height,
                shader_identity=shader_identity,
                binding_identity=binding_identity,
                node_shader_identities=node_shader_identities,
                export_report=result.to_dict(),
            )
        if artifact.executable_bytes is None:
            raise PreviewError(
                "preview export produced no generated player executable"
            )
        executable_path = work_directory / "edited-preview-player.exe"
        executable_path.write_bytes(artifact.executable_bytes)
        return PreviewExportBundle(
            package_sha256=package_sha,
            executable_path=executable_path,
            capture_width=capture_width,
            capture_height=capture_height,
            shader_identity=shader_identity,
            binding_identity=binding_identity,
            node_shader_identities=node_shader_identities,
            export_report=result.to_dict(),
        )


class RuntimeSessionError(PreviewError):
    """Raised when the authoring runtime session protocol fails."""


class AuthoringRuntimeSession:
    """Persistent authoring-side D3D12 device/process session.

    The native host keeps one D3D12 device (and therefore one device-instance
    epoch) alive across timeline scrubs *and compatible graph edits*.  A package
    hot reload waits for submitted work, retains unchanged committed GPU
    resources/root signatures/PSOs, and rebuilds only invalidated realizations.

    Only a resource-layout incompatibility, explicit reinitialization, device
    loss, or dead runtime process recreates the D3D12 device and advances the
    epoch.  Stale completions from an older instance therefore never install.
    """

    def __init__(
        self,
        executable: str | os.PathLike[str] | None,
        *,
        mode: str = "package-host",
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        execution_kind: str = "simulated",
        allow_software_adapter: bool = False,
    ):
        self.executable = Path(executable) if executable is not None else None
        self.mode = mode
        self._runner = runner
        # A stub runner is a deterministic one-shot contract harness; it is
        # never reported as a persistent device session.
        self._persistent = (
            runner is None and mode == "package-host" and self.executable is not None
        )
        self.execution_kind = execution_kind
        self.allow_software_adapter = bool(allow_software_adapter)
        self._process: subprocess.Popen[str] | None = None
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._epoch = -1
        self._package_sha = ""
        self._width = 0
        self._height = 0
        self._identity = HostDeviceIdentity.unavailable(
            "runtime session has not started"
        )
        self._lost = False
        self._fence = 0
        self._reservations: dict[str, tuple[int, int]] = {}
        self._retired_bytes = 0
        self._renders = 0
        self._hot_reloads = 0
        self._last_reload: dict[str, Any] = {}
        self._capture_dir: Path | None = None
        self._stats_path: Path | None = None
        self._device_running = False
        self._just_reinitialized = False
        self._last_render_tick: int | None = None
        self._lock = threading.RLock()

    # -- state ------------------------------------------------------------
    @property
    def device_epoch(self) -> int:
        return max(0, self._epoch)

    @property
    def persistent(self) -> bool:
        return self._persistent and self._process is not None and not self._lost

    @property
    def lost(self) -> bool:
        return self._lost

    @property
    def identity(self) -> HostDeviceIdentity:
        return self._identity

    def observe_identity(self, stats: Mapping[str, Any]) -> None:
        """Refine the device identity from the selected D3D12 adapter."""
        identity = HostDeviceIdentity.from_runtime_stats(stats)
        if identity.available:
            self._identity = identity

    @property
    def renders(self) -> int:
        return self._renders

    @property
    def reserved_bytes(self) -> int:
        return sum(size for _epoch, size in self._reservations.values())

    @property
    def retired_bytes(self) -> int:
        return self._retired_bytes

    @property
    def session_package_sha256(self) -> str:
        return self._package_sha

    def report(self) -> dict[str, Any]:
        return {
            "persistent": self._persistent,
            "process_alive": self._process is not None
            and self._process.poll() is None,
            "device_epoch": self.device_epoch,
            "lost": self._lost,
            "renders": self._renders,
            "hot_reloads": self._hot_reloads,
            "last_reload": dict(self._last_reload),
            "reserved_bytes": self.reserved_bytes,
            "retired_bytes": self._retired_bytes,
            "identity": self._identity.to_key_fields(),
            "session_package_sha256": self._package_sha,
            "width": self._width,
            "height": self._height,
        }

    # -- device lifecycle -------------------------------------------------
    def prepare(
        self,
        bundle: PreviewExportBundle,
        *,
        width: int,
        height: int,
        memory_budget_bytes: int | None = None,
        capture_dir: Path | None = None,
        stats_path: Path | None = None,
    ) -> int:
        """Ensure a device exists for ``bundle`` and return its epoch."""
        with self._lock:
            if self._lost:
                raise DeviceLostError(
                    "runtime device is lost; reinitialize before preparing a "
                    "preview"
                )
            if self._persistent:
                alive = (
                    self._device_running
                    and self._process is not None
                    and self._process.poll() is None
                    and not self._lost
                )
                resolution_known = self._width > 0 and self._height > 0
                if alive and resolution_known and (
                    self._width != width or self._height != height
                ):
                    # The native host freezes --width/--height at process
                    # creation.  A resolution change is therefore a real
                    # device-instance recreation, not a package reload.
                    self._stop_process()
                    self._last_render_tick = None
                    self._epoch = max(0, self._epoch + 1)
                    self._package_sha = ""
                    self._device_running = False
                    alive = False
                if alive and self._package_sha == bundle.package_sha256:
                    self._capture_dir = capture_dir
                    self._stats_path = stats_path
                    return self.device_epoch
                if alive:
                    if bundle.package_path is None:
                        raise RuntimeSessionError(
                            "persistent package-host reload requires a package path"
                        )
                    self._write(f"load {bundle.package_path}")
                    response = self._read_response(timeout=300.0)
                    if response.get("event") != "reload":
                        raise RuntimeSessionError(
                            "authoring runtime emitted an unexpected reload response: "
                            + json.dumps(response, sort_keys=True)
                        )
                    self._last_reload = dict(response)
                    exit_code = int(response.get("exit_code", 0) or 0)
                    device_removed = (
                        bool(response.get("device_removed"))
                        or exit_code == DEVICE_REMOVED_EXIT_CODE
                    )
                    if device_removed:
                        # DeviceRemoved can surface from fence waits or
                        # initialization inside the reload path.  It is not a
                        # recoverable layout mismatch: enter the lost-device
                        # state and require explicit reinitialization instead
                        # of silently restarting and replaying the package.
                        self._lost = True
                        self._reservations.clear()
                        self._stop_process()
                        error = DeviceLostError(
                            "authoring runtime device was removed during "
                            f"package reload (exit {exit_code})"
                        )
                        error.device_epoch = self.device_epoch
                        raise error
                    if response.get("ok", False) and not response.get(
                        "requires_device_recreation", False
                    ):
                        # A compatible edit was installed inside the same
                        # PackagePlayer/device.  Device epoch deliberately does
                        # not change: unchanged physical realizations remain live.
                        self._package_sha = bundle.package_sha256
                        self._hot_reloads += 1
                        # A hot reload reruns initialization (including the
                        # initial History upload and simulation-counter
                        # rewind), so the backward-scrub detector starts clean.
                        self._last_render_tick = None
                        self._capture_dir = capture_dir
                        self._stats_path = stats_path
                        return self.device_epoch
                    if not response.get("requires_device_recreation", False):
                        raise RuntimeSessionError(
                            "authoring runtime package hot reload failed: "
                            + json.dumps(response, sort_keys=True)
                        )
                    # The native runtime proved that descriptor/resource layout
                    # is incompatible with in-place reload.  This is an actual
                    # device recreation, so advance the epoch exactly once.
                    self._stop_process()
                    self._epoch = max(0, self._epoch + 1)
                else:
                    had_device = self._epoch >= 0 and bool(self._package_sha)
                    self._stop_process()
                    if self._epoch < 0:
                        self._epoch = 0
                    elif had_device and not self._just_reinitialized:
                        # A dead host/device followed by a new process is also a
                        # real device-instance recreation.
                        self._epoch += 1
                self._lost = False
                self._capture_dir = capture_dir
                self._stats_path = stats_path
                self._last_render_tick = None
                self._start_process(
                    bundle,
                    width=width,
                    height=height,
                    memory_budget_bytes=memory_budget_bytes,
                )
                self._device_running = True
                self._just_reinitialized = False
                return self.device_epoch
            # One-shot/injected execution creates a fresh process and D3D12
            # device for every preview invocation.  Its identity therefore
            # advances for every prepare after the first, regardless of whether
            # package bytes changed.
            if self._epoch < 0:
                self._epoch = 0
            elif self._just_reinitialized:
                pass
            else:
                self._epoch += 1
            self._just_reinitialized = False
            self._package_sha = bundle.package_sha256
            self._width = width
            self._height = height
            self._lost = False
            return self.device_epoch

    def reinitialize(self, minimum_epoch: int | None = None) -> int:
        """Explicitly recreate the runtime device and increment the epoch."""
        with self._lock:
            self._stop_process()
            self._epoch = max(
                self._epoch + 1,
                0 if minimum_epoch is None else int(minimum_epoch),
            )
            self._lost = False
            self._device_running = False
            self._just_reinitialized = True
            self._last_render_tick = None
            self._package_sha = ""
            self._identity = HostDeviceIdentity.unavailable(
                "runtime device reinitialized; awaiting first render"
            )
            return self.device_epoch

    def handle_device_loss(self) -> int:
        with self._lock:
            self._lost = True
            self._reservations.clear()
            self._device_running = False
            self._last_render_tick = None
            if self._process is not None and self._process.poll() is None:
                try:
                    self._write("lose-device")
                    self._read_response(timeout=5.0)
                except (OSError, RuntimeSessionError):
                    pass
            self._stop_process()
            return self.device_epoch

    def close(self) -> None:
        with self._lock:
            self._stop_process()
            self._reservations.clear()
            self._device_running = False
            self._lost = True

    # -- fences -----------------------------------------------------------
    def reserve(self, key: str, size_bytes: int) -> tuple[str, int]:
        with self._lock:
            if self._lost:
                raise DeviceLostError("cannot reserve on a lost runtime device")
            self._fence += 1
            self._reservations[key] = (self._fence, max(1, int(size_bytes)))
            return key, self._fence

    def release(self, key: str, *, retired: bool = True) -> None:
        with self._lock:
            entry = self._reservations.pop(key, None)
            if entry is not None and retired:
                self._retired_bytes += entry[1]

    def audio_command(self, command: str) -> dict[str, Any]:
        with self._lock:
            if not self._persistent or self._lost or not self._device_running:
                raise RuntimeSessionError("audio requires a prepared native package-host session")
            try:
                self._write(command)
                response = self._read_response(timeout=30.0)
                if response.get("event") != command.split()[0]:
                    raise RuntimeSessionError("unexpected audio transport response")
                if not response.get("ok", False):
                    raise RuntimeSessionError("audio transport failed: " + json.dumps(response))
                if command.startswith("audio-start "):
                    self._last_render_tick = int(response["tick"])
                return response
            except (OSError, RuntimeSessionError):
                # A failed protocol must not leave a live, unowned audio stream.
                self._stop_process()
                raise

    # -- rendering --------------------------------------------------------
    def render(
        self,
        *,
        tick: int,
        capture_dir: Path,
        stats_path: Path,
        command: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._lost:
                raise DeviceLostError(
                    "runtime device is lost; reinitialize before previewing"
                )
            if self._persistent:
                return self._render_session(
                    tick=tick, capture_dir=capture_dir, stats_path=stats_path
                )
            if command is None:
                raise RuntimeSessionError(
                    "one-shot runtime execution requires a command"
                )
            return self._render_one_shot(
                command=command, tick=tick, stats_path=stats_path
            )

    def _render_one_shot(
        self,
        *,
        command: Sequence[str],
        tick: int,
        stats_path: Path,
    ) -> dict[str, Any]:
        if self._runner is None:
            raise RuntimeSessionError("one-shot runtime session has no runner")
        process = self._runner(
            list(command), capture_output=True, text=True, check=False
        )
        self._renders += 1
        result = {
            "tick": tick,
            "returncode": process.returncode,
            "stderr": process.stderr or "",
            "device_removed": process.returncode == DEVICE_REMOVED_EXIT_CODE,
        }
        if process.returncode != 0:
            if process.returncode == DEVICE_REMOVED_EXIT_CODE:
                self._lost = True
                error = DeviceLostError(
                    f"package player reported device removal: "
                    f"{(process.stderr or '').strip()}"
                )
                error.device_epoch = self.device_epoch
                raise error
            raise RuntimeSessionError(
                f"package player exited {process.returncode}: "
                f"{(process.stderr or '').strip()}"
            )
        return result

    def _render_session(
        self,
        *,
        tick: int,
        capture_dir: Path,
        stats_path: Path,
    ) -> dict[str, Any]:
        if (
            self._last_render_tick is not None
            and tick // HISTORY_TIMELINE_TICKS_PER_SIMULATION_TICK
            < self._last_render_tick
            // HISTORY_TIMELINE_TICKS_PER_SIMULATION_TICK
        ):
            # A backward authoring scrub must reset temporal resources and the
            # simulation counter together, then replay through the ordinary
            # render request.  This is the existing persistent runtime, not a
            # second timeline or playback engine.
            self._write("rewind")
            rewind_response = self._read_response(timeout=300.0)
            if not rewind_response.get("ok", False):
                exit_code = int(rewind_response.get("exit_code", 1) or 1)
                if (
                    bool(rewind_response.get("device_removed"))
                    or exit_code == DEVICE_REMOVED_EXIT_CODE
                ):
                    self._lost = True
                    error = DeviceLostError(
                        "authoring runtime device was removed during History "
                        f"rewind (exit {exit_code})"
                    )
                    error.device_epoch = self.device_epoch
                    self._stop_process()
                    raise error
                raise RuntimeSessionError(
                    "authoring runtime History rewind failed "
                    f"(exit {exit_code}): "
                    + json.dumps(rewind_response, sort_keys=True)
                )
        self._last_render_tick = tick
        self._write(f"render {tick}")
        response = self._read_response(timeout=300.0)
        self._renders += 1
        response["tick"] = tick
        if not response.get("ok", False):
            exit_code = int(response.get("exit_code", 1) or 1)
            device_removed = (
                bool(response.get("device_removed"))
                or exit_code == DEVICE_REMOVED_EXIT_CODE
            )
            if device_removed:
                self._lost = True
                error = DeviceLostError(
                    f"authoring runtime device was removed (exit {exit_code})"
                )
                error.device_epoch = self.device_epoch
                self._stop_process()
                raise error
            runtime_diagnostics = response.get("diagnostics")
            diagnostics: list[dict[str, Any]] = []
            if isinstance(runtime_diagnostics, list):
                diagnostics.extend(
                    dict(item)
                    for item in runtime_diagnostics
                    if isinstance(item, Mapping)
                )
            if not diagnostics:
                diagnostics.append(
                    {
                        "category": "preview",
                        "severity": "error",
                        "code": "authoring_runtime_render_failed",
                        "message": (
                            f"authoring runtime render failed (exit {exit_code}): "
                            + json.dumps(response, sort_keys=True)
                        ),
                    }
                )
            raise RuntimeSessionError(
                f"authoring runtime render failed (exit {exit_code})",
                diagnostics,
            )
        return response

    # -- process plumbing -------------------------------------------------
    def _session_command(
        self,
        bundle: PreviewExportBundle,
        *,
        width: int,
        height: int,
        memory_budget_bytes: int | None,
    ) -> list[str]:
        if bundle.package_path is None:
            raise RuntimeSessionError("session mode requires a package-host bundle")
        command = [
            str(self.executable),
            "--package",
            str(bundle.package_path),
            "--width",
            str(width),
            "--height",
            str(height),
            "--session",
        ]
        if self._capture_dir is not None:
            command.extend(["--capture-dir", str(self._capture_dir)])
        if self._stats_path is not None:
            command.extend(["--stats-json", str(self._stats_path)])
        if memory_budget_bytes:
            command.extend(["--memory-budget", str(int(memory_budget_bytes))])
        if self.allow_software_adapter:
            command.append("--allow-software-adapter")
        return command

    def _start_process(
        self,
        bundle: PreviewExportBundle,
        *,
        width: int,
        height: int,
        memory_budget_bytes: int | None,
    ) -> None:
        command = self._session_command(
            bundle,
            width=width,
            height=height,
            memory_budget_bytes=memory_budget_bytes,
        )
        self._package_sha = bundle.package_sha256
        self._width = int(width)
        self._height = int(height)
        self._stdout = queue.Queue()
        self._stderr_lines = []
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdout_queue = self._stdout
        stderr_lines = self._stderr_lines
        process = self._process
        threading.Thread(
            target=self._pump_stdout,
            args=(process, stdout_queue),
            name="nw08-runtime-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._pump_stderr,
            args=(process, stderr_lines),
            name="nw08-runtime-stderr",
            daemon=True,
        ).start()
        ready = self._read_response(timeout=60.0)
        if ready.get("event") != "ready":
            self._stop_process()
            raise RuntimeSessionError(
                "authoring runtime session did not become ready: "
                + json.dumps(ready, sort_keys=True)
            )
        self._identity = HostDeviceIdentity.from_runtime_stats(ready)

    def _pump_stdout(
        self, process: subprocess.Popen[str], out_queue: queue.Queue[str | None]
    ) -> None:
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                out_queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            out_queue.put(None)

    def _pump_stderr(
        self, process: subprocess.Popen[str], stderr_lines: list[str]
    ) -> None:
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                stderr_lines.append(line.rstrip("\r\n"))
                if len(stderr_lines) > 200:
                    del stderr_lines[:100]
        except (OSError, ValueError):
            pass

    def _read_response(self, *, timeout: float) -> dict[str, Any]:
        try:
            line = self._stdout.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeSessionError(
                "authoring runtime session did not respond within "
                f"{timeout:.1f}s"
            ) from exc
        if line is None:
            stderr = "\n".join(self._stderr_lines).strip()
            raise RuntimeSessionError(
                "authoring runtime session closed unexpectedly"
                + (f": {stderr}" if stderr else "")
            )
        text = line.strip()
        if not text:
            return self._read_response(timeout=timeout)
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise RuntimeSessionError(
                f"authoring runtime session emitted invalid JSON: {text!r}"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeSessionError(
                f"authoring runtime session emitted non-object: {text!r}"
            )
        return value

    def _write(self, text: str) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeSessionError("authoring runtime session is not running")
        try:
            process.stdin.write(text + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeSessionError(
                "authoring runtime session input closed"
            ) from exc

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        self._device_running = False
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write("quit\n")
                process.stdin.flush()
                process.wait(timeout=30.0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except (OSError, ValueError):
                    pass
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass


class WindowsRuntimePreviewBackend:
    """Preview the *edited* graph through the existing package-player runtime.

    The backend first exports the document it was given (through the same
    ``nw05_export.export_graph`` path used by the CLI) and then runs either the
    native package host on that ``.nwp`` or the generated player built from it.
    In package-host mode an :class:`AuthoringRuntimeSession` keeps the host
    process (and therefore its D3D12 device instance) alive while the exported
    package is compatible with the live authoring layout.  Timeline scrubs and
    ordinary graph edits therefore reuse actual unchanged GPU realizations and
    submitted-work retirement fences.  The device epoch changes only when the
    native runtime reports that a real device recreation is required.

    A successful process exit without a readback file is a failure, never
    hardware evidence.  Captured pixels are retained on the frame so the GUI
    can display the last accepted image after a failed edit.
    """

    name = "windows-package-player"
    reserves_gpu_memory = True
    reports_device_identity = True

    def __init__(
        self,
        executable: str | os.PathLike[str] | None = None,
        *,
        capture_controls: bool = True,
        width: int = 1920,
        height: int = 1080,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        exporter: PreviewExporter | None = None,
        mode: str = "package-host",
        dxc_executable: str | None = None,
        player_template: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        export_settings: nw05_export.ExportSettings | Mapping[str, Any] | None = None,
        allow_test_payloads: bool = False,
        optimization: str = "O2",
        execution_kind: str = "simulated",
        memory_budget_bytes: int | None = None,
        allow_software_adapter: bool = False,
    ):
        if mode not in {"package-host", "embedded-player"}:
            raise PreviewError(f"unsupported preview mode '{mode}'")
        if capture_controls is False:
            raise PreviewError(
                "image preview requires the diagnostic capture-control protocol"
            )
        self.mode = mode
        self.executable = Path(executable) if executable is not None else None
        if self.executable is not None and not self.executable.is_file():
            raise PreviewError(
                f"preview executable is unavailable: {self.executable}"
            )
        if self.executable is None and mode == "package-host":
            raise PreviewError("package-host preview requires a preview executable")
        self.capture_controls = capture_controls
        self.width = width
        self.height = height
        self._runner = runner or subprocess.run
        self._injected_runner = runner is not None
        self.memory_budget_bytes = memory_budget_bytes
        self.execution_kind = execution_kind
        self.allow_software_adapter = bool(allow_software_adapter)
        # Physical evidence is possible only for an un-injected Windows process
        # invoked with an explicit physical/passthrough execution declaration.
        self._physical_capable = (
            os.name == "nt"
            and not self._injected_runner
            and mode == "package-host"
            and execution_kind in {"physical", "documented_gpu_passthrough"}
        )
        self.hardware_evidence = self._physical_capable
        self._exporter = exporter or RuntimePackageExporter(
            mode=mode,
            dxc_executable=dxc_executable,
            player_template=player_template,
            cache_dir=cache_dir,
            settings=export_settings,
            allow_test_payloads=allow_test_payloads,
            optimization=optimization,
        )
        self._session = AuthoringRuntimeSession(
            self.executable,
            mode=mode,
            # A real package-host session keeps the device alive; the
            # generated-player path is intentionally one process per render
            # and uses the configured runner (subprocess.run by default).
            runner=(
                runner
                if runner is not None
                else (self._runner if mode != "package-host" else None)
            ),
            execution_kind=execution_kind,
            allow_software_adapter=self.allow_software_adapter,
        )
        self._work_root: Path | None = Path(
            tempfile.mkdtemp(prefix=".nw08-preview-session-")
        )
        self._bundles: dict[str, PreviewExportBundle] = {}
        self._prepared_key: str = ""
        self._prepared_export_duration_ms: float | None = None
        self._lock = threading.RLock()

    # -- device/session state --------------------------------------------
    @property
    def persistent_realizations(self) -> bool:
        return self._session.persistent

    @property
    def device_epoch(self) -> int:
        return self._session.device_epoch

    @property
    def device_identity(self) -> HostDeviceIdentity:
        return self._session.identity

    def report(self) -> dict[str, Any]:
        return self._session.report()

    def reinitialize_device(self, minimum_epoch: int | None = None) -> int:
        with self._lock:
            self._prepared_key = ""
            return self._session.reinitialize(minimum_epoch)

    def handle_device_loss(self) -> int:
        with self._lock:
            self._prepared_key = ""
            return self._session.handle_device_loss()

    def close(self) -> None:
        with self._lock:
            self._session.close()
            root = self._work_root
            self._work_root = None
            self._bundles.clear()
            self._prepared_export_duration_ms = None
            self._prepared_key = ""
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    def _document_key(
        self,
        document: nw03_compiler.GraphDocument,
        *,
        width: int,
        height: int,
    ) -> str:
        return sha256_value(
            {
                "domain": "nw08.preview.session-document.v1",
                "semantic_digest": editor_semantic_digest(document)[0],
                "settings": dict(document.settings),
                "width": width,
                "height": height,
                "mode": self.mode,
                "exporter": repr(getattr(self._exporter, "settings", {})),
            }
        )

    def _ensure_bundle(
        self,
        document: nw03_compiler.GraphDocument,
        *,
        width: int,
        height: int,
    ) -> tuple[str, PreviewExportBundle, float]:
        key = self._document_key(document, width=width, height=height)
        existing = self._bundles.get(key)
        if existing is not None:
            return key, existing, 0.0
        if self._work_root is None:
            self._work_root = Path(
                tempfile.mkdtemp(prefix=".nw08-preview-session-")
            )
        work_directory = self._work_root / f"export-{len(self._bundles):04d}"
        work_directory.mkdir(parents=True, exist_ok=True)
        export_start = time.perf_counter()
        try:
            bundle = self._exporter.export(
                document, work_directory, width=width, height=height
            )
        except (nw05_export.ExportError, nw03_compiler.CompilerError) as exc:
            raise PreviewError(str(exc), getattr(exc, "diagnostics", [])) from exc
        export_duration_ms = (time.perf_counter() - export_start) * 1000.0
        self._bundles[key] = bundle
        return key, bundle, export_duration_ms

    def prepare(
        self,
        document: nw03_compiler.GraphDocument,
        *,
        width: int,
        height: int,
    ) -> int:
        """Export the document and create/reuse its runtime device instance."""
        with self._lock:
            key, bundle, export_duration = self._ensure_bundle(
                document, width=width, height=height
            )
            self._prepared_export_duration_ms = export_duration
            if self._work_root is None:
                raise PreviewError("preview session was closed")
            capture_dir = self._work_root / "capture"
            capture_dir.mkdir(parents=True, exist_ok=True)
            stats_path = self._work_root / "runtime-stats.json"
            if self._session.persistent:
                epoch = self._session.prepare(
                    bundle,
                    width=width,
                    height=height,
                    memory_budget_bytes=self.memory_budget_bytes,
                    capture_dir=capture_dir,
                    stats_path=stats_path,
                )
            else:
                # One-shot devices do not exist until render launches the
                # generated/runtime process.  Report the current epoch here;
                # the actual invocation epoch is synchronized from the frame.
                epoch = self._session.device_epoch
            self._prepared_key = key
            return epoch

    def configure_exporter(
        self,
        *,
        mode: str | None = None,
        dxc_executable: str | None = None,
        player_template: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        export_settings: nw05_export.ExportSettings | Mapping[str, Any] | None = None,
        allow_test_payloads: bool | None = None,
        optimization: str | None = None,
        minimum_epoch: int | None = None,
    ) -> None:
        """Replace export/runtime state without leaking the previous device."""
        selected_mode = self.mode if mode is None else mode
        if selected_mode not in {"package-host", "embedded-player"}:
            raise PreviewError(f"unsupported preview mode '{selected_mode}'")
        if selected_mode == "package-host" and self.executable is None:
            raise PreviewError(
                "package-host preview requires a preview executable"
            )
        with self._lock:
            self.mode = selected_mode
            self._physical_capable = (
                os.name == "nt"
                and not self._injected_runner
                and selected_mode == "package-host"
                and self.execution_kind
                in {"physical", "documented_gpu_passthrough"}
            )
            self.hardware_evidence = self._physical_capable
            self._exporter = RuntimePackageExporter(
                mode=selected_mode,
                dxc_executable=dxc_executable,
                player_template=player_template,
                cache_dir=cache_dir,
                settings=export_settings,
                allow_test_payloads=bool(allow_test_payloads),
                optimization=optimization or "O2",
            )
            self._session.close()
            self._session = AuthoringRuntimeSession(
                self.executable,
                mode=selected_mode,
                runner=(
                    self._runner
                    if (self._injected_runner or selected_mode != "package-host")
                    else None
                ),
                execution_kind=self.execution_kind,
                allow_software_adapter=self.allow_software_adapter,
            )
            if minimum_epoch is not None:
                aligned = self._session.reinitialize(int(minimum_epoch))
                if aligned != int(minimum_epoch):
                    raise PreviewError(
                        "replacement runtime could not align its device epoch"
                    )
            self._prepared_key = ""
            self._bundles.clear()
            self._prepared_export_duration_ms = None

    def command(
        self,
        tick: int,
        capture_dir: Path,
        bundle: PreviewExportBundle,
        *,
        width: int,
        height: int,
        stats_path: Path | None = None,
    ) -> list[str]:
        if self.mode == "package-host":
            if bundle.package_path is None:
                raise PreviewError("preview export produced no package")
            command = [
                str(self.executable),
                "--package",
                str(bundle.package_path),
                "--tick",
                str(tick),
                "--width",
                str(width),
                "--height",
                str(height),
                "--capture-dir",
                str(capture_dir),
            ]
        else:
            if bundle.executable_path is None:
                raise PreviewError("preview export produced no generated player")
            command = [
                str(bundle.executable_path),
                "--capture-tick",
                str(tick),
                "--capture-dir",
                str(capture_dir),
            ]
        if self.mode == "package-host" and self.memory_budget_bytes:
            command.extend(["--memory-budget", str(int(self.memory_budget_bytes))])
        if self.mode == "package-host" and stats_path is not None:
            command.extend(["--stats-json", str(stats_path)])
        return command

    def _read_stats(self, stats_path: Path) -> dict[str, Any]:
        if not stats_path.is_file():
            return {}
        try:
            data = json.loads(stats_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def render(
        self,
        document: nw03_compiler.GraphDocument,
        tick: int,
        *,
        width: int,
        height: int,
    ) -> PreviewFrame:
        with self._lock:
            key, bundle, export_duration = self._ensure_bundle(
                document, width=width, height=height
            )
            if key != self._prepared_key and self._session.persistent:
                self._session.prepare(
                    bundle,
                    width=width,
                    height=height,
                    memory_budget_bytes=self.memory_budget_bytes,
                    capture_dir=(
                        (self._work_root / "capture")
                        if self._work_root is not None
                        else None
                    ),
                    stats_path=(
                        (self._work_root / "runtime-stats.json")
                        if self._work_root is not None
                        else None
                    ),
                )
                self._prepared_key = key
            if self._prepared_export_duration_ms is not None:
                export_duration = self._prepared_export_duration_ms
                self._prepared_export_duration_ms = None
            if self._work_root is None:
                raise PreviewError("preview session was closed")
            work_directory = self._work_root / "capture"
            work_directory.mkdir(parents=True, exist_ok=True)
            capture_dir = work_directory
            stats_path = self._work_root / "runtime-stats.json"
            capture_width = bundle.capture_width or width
            capture_height = bundle.capture_height or height
            capture = capture_dir / f"tick_{tick}.rgba32f"
            # Never accept artifacts left by an earlier same-tick invocation.
            for artifact_path in (capture, stats_path):
                try:
                    artifact_path.unlink()
                except FileNotFoundError:
                    pass
            command = None
            if not self._session.persistent:
                self._session.prepare(
                    bundle,
                    width=width,
                    height=height,
                    memory_budget_bytes=self.memory_budget_bytes,
                    capture_dir=capture_dir,
                    stats_path=stats_path,
                )
                command = self.command(
                    tick,
                    capture_dir,
                    bundle,
                    width=width,
                    height=height,
                    stats_path=stats_path,
                )
            timings: list[PassTiming] = [
                PassTiming(
                    "offline_compile_export",
                    max(0.0, export_duration),
                    kind="compiler",
                )
            ]
            start = time.perf_counter()
            response = self._session.render(
                tick=tick,
                capture_dir=capture_dir,
                stats_path=stats_path,
                command=command,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            timings.append(
                PassTiming("runtime_process_wall", elapsed_ms, kind="runtime")
            )
            if not capture.is_file():
                raise PreviewError(
                    "package player produced no presented-output readback; "
                    "a zero exit status is not hardware evidence",
                    [
                        {
                            "category": "preview",
                            "severity": "error",
                            "code": "preview_readback_missing",
                            "message": (
                                f"expected readback '{capture.name}' was not "
                                "produced"
                            ),
                        }
                    ],
                )
            data = capture.read_bytes()
            expected = capture_width * capture_height * 16
            if len(data) != expected:
                raise PreviewError(
                    f"preview readback is {len(data)} bytes, expected {expected}",
                    [
                        {
                            "category": "preview",
                            "severity": "error",
                            "code": "preview_readback_size_mismatch",
                            "message": (
                                f"readback is {len(data)} bytes, expected "
                                f"{expected}"
                            ),
                        }
                    ],
                )
            image_sha = hashlib.sha256(data).hexdigest()
            stats = self._read_stats(stats_path)
            if not stats and isinstance(response, dict):
                stats = {
                    key: value
                    for key, value in response.items()
                    if key not in {"event", "ok"}
                }
            self._session.observe_identity(stats)
            self._append_measured_timings(timings, stats)
            if self._physical_capable and not stats.get("fence_retired"):
                raise PreviewError(
                    "physical preview did not confirm fence retirement",
                    [
                        {
                            "category": "preview",
                            "severity": "error",
                            "code": "preview_fence_not_retired",
                            "message": (
                                "the runtime did not report fence_retired=true "
                                "after waiting for submitted GPU work"
                            ),
                        }
                    ],
                )
            try:
                sample = nw07_export.evaluate_camera_at_tick(document, tick)
            except Exception:
                sample = {}
            resources = estimate_size_attribution(
                document, width=capture_width, height=capture_height
            )
            if "peak_engine_resource_bytes" in stats:
                engine_bytes = int(stats["peak_engine_resource_bytes"])
            elif "engine_resource_bytes" in stats:
                engine_bytes = int(stats["engine_resource_bytes"])
            else:
                engine_bytes = int(resources.get("resident_payload_bytes", 0))
            identity = self._session.identity
            # A WARP/software adapter is useful for native CI coverage but can
            # never be promoted to physical hardware evidence.
            physical = bool(
                self._physical_capable
                and image_sha
                and identity.available
                and not identity.software
            )
            stats_with_session = dict(stats)
            stats_with_session["session"] = self._session.report()
            return PreviewFrame(
                tick=tick,
                width=capture_width,
                height=capture_height,
                image_sha256=image_sha,
                semantic_digest="",
                camera_angle_radians=sample.get("camera_angle_radians"),
                camera_angle_modulo_turn=sample.get(
                    "camera_angle_modulo_turn"
                ),
                twist_degrees=sample.get("twist_degrees"),
                content_digest=sha256_value(
                    {
                        "domain": "nw08.runtime-preview.v2",
                        "package_sha256": bundle.package_sha256,
                        "tick": tick,
                        "width": capture_width,
                        "height": capture_height,
                        "image_sha256": image_sha,
                        "execution_kind": "physical" if physical else "simulated",
                    }
                ),
                resident_bytes=max(1, engine_bytes),
                pass_timings=PassTimings(tuple(timings)),
                size_attribution=resources,
                runtime_stats=stats_with_session,
                shader_identity=bundle.shader_identity,
                binding_identity=bundle.binding_identity,
                node_shader_identities=bundle.node_shader_identities,
                backend=self.name,
                hardware_evidence=physical,
                execution_kind=(
                    "physical_runtime" if physical else "simulated_runtime"
                ),
                image_data=data,
                image_format="rgba32f",
                device_epoch=self._session.device_epoch,
                device_identity=identity.to_key_fields(),
            )

    def audio_command(self, command: str) -> dict[str, Any]:
        with self._lock:
            try:
                return self._session.audio_command(command)
            except RuntimeError:
                self._prepared_key = ""
                raise

    def _append_measured_timings(
        self, timings: list[PassTiming], stats: Mapping[str, Any]
    ) -> None:
        cpu_available = bool(stats.get("runtime_cpu_available", False))
        gpu_available = bool(stats.get("runtime_gpu_available", False))
        if cpu_available:
            timings.append(
                PassTiming(
                    "runtime_cpu_submit",
                    max(0.0, float(stats.get("runtime_cpu_submit_ms", 0.0))),
                    kind="runtime_internal",
                    measured=True,
                    counts_toward_total=False,
                )
            )
        else:
            timings.append(
                PassTiming(
                    "runtime_cpu_submit",
                    0.0,
                    kind="runtime_internal",
                    measured=False,
                    available=False,
                    counts_toward_total=False,
                )
            )
        if gpu_available:
            timings.append(
                PassTiming(
                    "runtime_gpu",
                    max(0.0, float(stats.get("runtime_gpu_ms", 0.0))),
                    kind="runtime_internal",
                    measured=True,
                    counts_toward_total=False,
                )
            )
        else:
            timings.append(
                PassTiming(
                    "runtime_gpu",
                    0.0,
                    kind="runtime_internal",
                    measured=False,
                    available=False,
                    counts_toward_total=False,
                )
            )
        measured_passes = stats.get("runtime_pass_timings") or []
        appended_measured = False
        if isinstance(measured_passes, list):
            for index, row in enumerate(measured_passes):
                if not isinstance(row, Mapping):
                    continue
                try:
                    milliseconds = max(0.0, float(row.get("milliseconds", 0.0)))
                except (TypeError, ValueError):
                    continue
                name = str(row.get("name") or f"pass_{index}")
                timings.append(
                    PassTiming(
                        f"runtime_pass_{index}:{name}",
                        milliseconds,
                        kind="runtime_pass_cpu",
                        measured=True,
                        available=True,
                        counts_toward_total=False,
                    )
                )
                appended_measured = True
        if not appended_measured:
            instructions = stats.get("frame_instructions") or []
            if isinstance(instructions, list):
                for index, name in enumerate(instructions):
                    timings.append(
                        PassTiming(
                            f"runtime_pass_{index}:{name}",
                            0.0,
                            kind="runtime_pass",
                            measured=False,
                            available=False,
                            counts_toward_total=False,
                        )
                    )




@dataclass
class _PendingPreviewJob:
    token: GenerationToken
    tick: int
    width: int
    height: int
    backend: PreviewBackend
    document: nw03_compiler.GraphDocument
    source_text: str
    semantic_digest: str


@dataclass(frozen=True)
class _RuntimeReservation:
    key: str
    fence: int
    size_bytes: int


@dataclass(frozen=True)
class _NodeRealization:
    node_id: str
    key: str
    size_bytes: int


class PreviewPipeline:
    """Asynchronous preview controller with last-valid and device semantics."""

    def __init__(
        self,
        session: GraphEditorSession,
        *,
        backend: PreviewBackend | None = None,
        gpu_budget_bytes: int = 512 * 1024 * 1024,
        adapter: AdapterDeviceIdentity | None = None,
        driver_version: str = "",
        os_build: str = "",
        toolchain: ToolchainIdentity | None = None,
        width: int = 1920,
        height: int = 1080,
    ):
        self.session = session
        self.backend = backend or ReferencePreviewBackend()
        self.width = width
        self.height = height
        self.cache = GpuRealizationCache(gpu_budget_bytes)
        self.queue = AsyncJobQueue(session.semantic_digest)
        runtime_reports_identity = bool(
            getattr(self.backend, "reports_device_identity", False)
        )
        if adapter is None:
            observed = (
                HostDeviceIdentity.unavailable(
                    "runtime reports the selected D3D12 adapter identity"
                )
                if runtime_reports_identity
                else discover_host_device_identity()
            )
            adapter = observed.adapter
            if not driver_version:
                driver_version = observed.driver_version
            if not os_build:
                os_build = observed.os_build
            self.host_identity = observed
        else:
            observed = (
                HostDeviceIdentity.unavailable(
                    "runtime reports the selected D3D12 adapter identity"
                )
                if runtime_reports_identity
                else discover_host_device_identity()
            )
            if not driver_version:
                driver_version = observed.driver_version
            if not os_build:
                os_build = observed.os_build
            self.host_identity = replace(
                observed,
                adapter=adapter,
                driver_version=driver_version,
                os_build=os_build,
                available=adapter.available,
            )
        if toolchain is None:
            self._toolchain = toolchain_identity_for(None, os_build=os_build)
        elif not toolchain.os_build and os_build:
            self._toolchain = replace(toolchain, os_build=os_build)
        else:
            self._toolchain = toolchain
        self.device = DeviceEpochManager(
            adapter,
            capability_profile=CapabilityProfileIdentity(
                session.document.profile_id, session.document.requirements_digest
            ),
            driver_version=driver_version,
            os_build=os_build,
            toolchain_identity=_toolchain_text(self._toolchain),
        )
        self._state_lock = threading.RLock()
        # Preview execution is serialized so fence values are signalled in
        # submission order even when a caller drives the pipeline from several
        # threads.  Cancellation, device-loss, and snapshot edits remain
        # concurrent and are revalidated after rendering.
        self._execute_lock = threading.Lock()
        self._pending: dict[int, _PendingPreviewJob] = {}
        self._last_valid: PreviewRecord | None = None
        self._last_diagnostics: list[dict[str, Any]] = []
        self._history: list[PreviewRecord] = []
        self._rejected: list[dict[str, Any]] = []
        self._last_installed_job = 0
        self._fence = 0
        self._device_serial = 0
        self._last_edit: EditResult | None = None

    # -- read-only state ---------------------------------------------------
    @property
    def last_valid_preview(self) -> PreviewRecord | None:
        if self.device.lost:
            return None
        return self._last_valid

    @property
    def last_valid_frame(self) -> PreviewFrame | None:
        record = self.last_valid_preview
        return record.frame if record else None

    @property
    def last_preview(self) -> PreviewRecord | None:
        return self.last_valid_preview

    @property
    def last_diagnostics(self) -> list[dict[str, Any]]:
        return list(self._last_diagnostics)

    @property
    def history(self) -> tuple[PreviewRecord, ...]:
        return tuple(self._history)

    @property
    def rejected_completions(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._rejected)

    @property
    def device_generation(self) -> DeviceGeneration:
        return self.device.generation

    @property
    def snapshot_generation(self) -> int:
        return self.queue.snapshot_generation

    @property
    def last_edit(self) -> EditResult | None:
        return self._last_edit

    @property
    def toolchain(self) -> ToolchainIdentity:
        return self._toolchain

    # -- editing -----------------------------------------------------------
    def apply_edit(self, result: EditResult) -> EditResult:
        with self._state_lock:
            self._last_edit = result
            if result.valid:
                semantic_changed = (
                    result.semantic_digest != self.queue.semantic_digest
                )
                if semantic_changed:
                    self.queue.note_snapshot(result.semantic_digest)
                    # A new semantic snapshot invalidates no last-valid preview
                    # by itself; the previous frame remains visible until a
                    # newer job succeeds.
                if semantic_changed:
                    self._last_diagnostics = list(result.linked_diagnostics)
            else:
                self._last_diagnostics = list(
                    result.linked_diagnostics or result.diagnostics
                )
        return result

    def apply_parameter_edit(
        self, node_id: str, name: str, value: Any
    ) -> EditResult:
        return self.apply_edit(
            self.session.set_parameter(node_id, name, value)
        )

    # -- jobs --------------------------------------------------------------
    def submit_preview(
        self,
        *,
        tick: int | None = None,
        backend: PreviewBackend | None = None,
    ) -> GenerationToken:
        with self._state_lock:
            # Callers may manipulate the session directly; synchronize the job
            # generation so an unseen semantic edit still invalidates older
            # jobs.
            if self.session.semantic_digest != self.queue.semantic_digest:
                self.queue.note_snapshot(self.session.semantic_digest)
            document = copy.deepcopy(self.session.document)
            token = self.queue.submit(semantic_digest=self.session.semantic_digest)
            job = _PendingPreviewJob(
                token=token,
                tick=self.session.timeline_tick if tick is None else int(tick),
                width=self.width,
                height=self.height,
                backend=backend or self.backend,
                document=document,
                source_text=emit_graph_text(document),
                semantic_digest=self.session.semantic_digest,
            )
            self._pending[token.job_id] = job
            return token

    def cancel(self, token: GenerationToken | int) -> bool:
        with self._state_lock:
            job_id = (
                token.job_id if isinstance(token, GenerationToken) else int(token)
            )
            self._pending.pop(job_id, None)
            return self.queue.cancel(job_id)

    def execute(self, token: GenerationToken | int) -> CompletionDecision:
        with self._execute_lock:
            return self._execute_inner(token)

    def _execute_inner(
        self, token: GenerationToken | int
    ) -> CompletionDecision:
        job_id = (
            token.job_id if isinstance(token, GenerationToken) else int(token)
        )
        reservation: _RuntimeReservation | None = None
        with self._state_lock:
            job = self._pending.get(job_id)
            if job is None:
                decision = self.queue.complete(job_id)
                self._record_rejection_locked(decision)
                return decision
            # Gate before doing any work: obsolete/cancelled jobs never compile.
            # The job stays pending (and cancellable) through compilation and
            # rendering; it is finalized only at installation below.
            current, reason = self._completion_current_locked(job)
            if not current:
                self._pending.pop(job_id, None)
                return self._reject_locked(job.token, reason)
            if getattr(job.backend, "reserves_gpu_memory", False):
                try:
                    reservation = self._reserve_runtime_locked(job)
                except (GpuBudgetExceeded, DeviceLostError) as exc:
                    self._pending.pop(job_id, None)
                    diagnostics = [
                        {
                            "category": "cache",
                            "severity": "error",
                            "code": "gpu_cache_budget_exceeded",
                            "message": str(exc),
                        }
                    ]
                    return self._reject_locked(
                        job.token, "preview_gpu_budget_exceeded", diagnostics
                    )
        # Compilation/export and device preparation run outside the state lock
        # so edits, timeline scrubs, and cancellation remain responsive.
        planned_epoch: int | None = None
        prepare = getattr(job.backend, "prepare", None)
        if callable(prepare):
            try:
                planned_epoch = int(
                    prepare(job.document, width=job.width, height=job.height)
                )
            except DeviceLostError as exc:
                return self._device_lost_completion(job, reservation, exc)
            except PreviewError as exc:
                return self._preview_failure_completion(job, reservation, exc)
            except Exception as exc:  # pragma: no cover - defensive boundary
                return self._preview_failure_completion(
                    job, reservation, PreviewError(str(exc))
                )
        with self._state_lock:
            current_before_sync, reason_before_sync = (
                self._completion_current_locked(job)
            )
            if planned_epoch is not None and planned_epoch > self.device.epoch:
                try:
                    self._synchronize_device_epoch_locked(
                        job,
                        planned_epoch,
                        bind_job=current_before_sync,
                    )
                except PreviewError as exc:
                    self._pending.pop(job_id, None)
                    if reservation is not None:
                        self._retire_reservation_locked(reservation)
                    return self._reject_locked(
                        job.token,
                        "preview_device_epoch_diverged",
                        [
                            {
                                "category": "preview",
                                "severity": "error",
                                "code": "preview_device_epoch_diverged",
                                "message": str(exc),
                            }
                        ],
                    )
            elif (
                planned_epoch is not None
                and planned_epoch < self.device.epoch
                and current_before_sync
            ):
                self._pending.pop(job_id, None)
                if reservation is not None:
                    self._retire_reservation_locked(reservation)
                return self._reject_locked(
                    job.token,
                    "preview_device_epoch_diverged",
                    [
                        {
                            "category": "preview",
                            "severity": "error",
                            "code": "preview_device_epoch_diverged",
                            "message": (
                                "runtime prepared an older device instance than "
                                "the pipeline currently owns"
                            ),
                        }
                    ],
                )
            if not current_before_sync:
                self._pending.pop(job_id, None)
                if reservation is not None:
                    self._retire_reservation_locked(reservation)
                return self._reject_locked(job.token, reason_before_sync)
            current, reason = self._completion_current_locked(job)
            if not current:
                self._pending.pop(job_id, None)
                if reservation is not None:
                    self._retire_reservation_locked(reservation)
                return self._reject_locked(job.token, reason)
        # Rendering deliberately happens outside the state lock so an explicit
        # device-loss/reinitialization can invalidate an in-flight completion.
        try:
            frame = job.backend.render(
                job.document,
                job.tick,
                width=job.width,
                height=job.height,
            )
        except DeviceLostError as exc:
            return self._device_lost_completion(job, reservation, exc)
        except PreviewError as exc:
            return self._preview_failure_completion(job, reservation, exc)
        except Exception as exc:  # pragma: no cover - defensive boundary
            return self._preview_failure_completion(
                job,
                reservation,
                PreviewError(
                    str(exc),
                    [
                        {
                            "category": "preview",
                            "severity": "error",
                            "code": "preview_failed",
                            "message": str(exc),
                            "source_linked": False,
                        }
                    ],
                ),
            )
        with self._state_lock:
            current_before_frame_sync, reason_before_frame_sync = (
                self._completion_current_locked(job)
            )
            if (
                frame.device_epoch is not None
                and frame.device_epoch > self.device.epoch
            ):
                try:
                    self._synchronize_device_epoch_locked(
                        job,
                        frame.device_epoch,
                        bind_job=current_before_frame_sync,
                    )
                except PreviewError as exc:
                    self._pending.pop(job_id, None)
                    if reservation is not None:
                        self._retire_reservation_locked(reservation)
                    return self._reject_locked(
                        job.token,
                        "preview_device_epoch_diverged",
                        [
                            {
                                "category": "preview",
                                "severity": "error",
                                "code": "preview_device_epoch_diverged",
                                "message": str(exc),
                            }
                        ],
                    )
            if reservation is not None:
                self._retire_reservation_locked(reservation)
            if not current_before_frame_sync:
                self._pending.pop(job_id, None)
                return self._reject_locked(
                    job.token, reason_before_frame_sync
                )
            # Revalidate both the immutable snapshot generation and the device
            # epoch after rendering.  A completion from an older snapshot or
            # device instance can never install.
            current, reason = self._completion_current_locked(job)
            if not current:
                self._pending.pop(job_id, None)
                return self._reject_locked(job.token, reason)
            decision = self.queue.complete(job.token)
            if not decision.accepted:
                self._pending.pop(job_id, None)
                self._record_rejection_locked(decision)
                return decision
            self._pending.pop(job_id, None)
            installed, install_reason, diagnostics = self._install_locked(
                job, frame
            )
            if not installed:
                return self._reject_locked(job.token, install_reason, diagnostics)
            return decision

    def _device_lost_completion(
        self,
        job: _PendingPreviewJob,
        reservation: _RuntimeReservation | None,
        error: DeviceLostError,
    ) -> CompletionDecision:
        with self._state_lock:
            if reservation is not None:
                self._retire_reservation_locked(reservation)
            self._pending.pop(job.token.job_id, None)
            failure_epoch = int(
                getattr(error, "device_epoch", job.token.device_epoch)
            )
            if failure_epoch > self.device.epoch:
                current_before_sync, _ = self._completion_current_locked(job)
                self._synchronize_device_epoch_locked(
                    job,
                    failure_epoch,
                    bind_job=current_before_sync,
                )
            # Only loss reported by the device instance that is current now may
            # invalidate the pipeline.  Delayed errors from an older instance
            # are rejected without poisoning a reinitialized device.
            if failure_epoch != self.device.epoch:
                return self._reject_locked(
                    job.token,
                    "obsolete_device_epoch",
                    [
                        {
                            "category": "preview",
                            "severity": "warning",
                            "code": "stale_device_loss_ignored",
                            "message": str(error),
                        }
                    ],
                )
            self._enter_device_lost_locked()
            return self._reject_locked(
                job.token,
                "preview_device_lost",
                [
                    {
                        "category": "preview",
                        "severity": "error",
                        "code": "preview_device_lost",
                        "message": str(error),
                    }
                ],
            )

    def _preview_failure_completion(
        self,
        job: _PendingPreviewJob,
        reservation: _RuntimeReservation | None,
        error: PreviewError,
    ) -> CompletionDecision:
        raw_diagnostics: Sequence[Mapping[str, Any]] = error.diagnostics or (
            {
                "category": "preview",
                "severity": "error",
                "code": "preview_backend_failed",
                "message": str(error),
            },
        )
        diagnostics = link_source_diagnostics(
            raw_diagnostics,
            source_text=job.source_text,
            source_name=str(self.session.source_path or "<editor>"),
        )
        with self._state_lock:
            self._pending.pop(job.token.job_id, None)
            # Finalize the queue entry so it does not remain "pending" after a
            # failed compilation/render.  The rejection below still governs
            # installation.
            self.queue.complete(job.token)
            if reservation is not None:
                self._retire_reservation_locked(reservation)
            current, current_reason = self._completion_current_locked(job)
            if not current:
                return self._reject_locked(job.token, current_reason)
            return self._reject_locked(job.token, "preview_failed", diagnostics)

    def _synchronize_device_epoch_locked(
        self,
        job: _PendingPreviewJob,
        planned_epoch: int,
        *,
        bind_job: bool,
    ) -> None:
        """Synchronize global runtime recreation before publication gates."""
        if planned_epoch < self.device.epoch:
            raise PreviewError("runtime device epoch moved backwards")
        identity = getattr(job.backend, "device_identity", None)
        adapter = None
        driver_version = None
        os_build = None
        if identity is not None and getattr(identity, "available", False):
            adapter = identity.adapter
            driver_version = identity.driver_version
            os_build = identity.os_build
            self.host_identity = identity
        while self.device.epoch < planned_epoch:
            old_epoch = self.device.epoch
            generation = self.device.reinitialize(
                adapter=adapter,
                capability_profile=CapabilityProfileIdentity(
                    job.document.profile_id,
                    job.document.requirements_digest,
                ),
                driver_version=driver_version,
                os_build=os_build,
            )
            queue_epoch = self.queue.reinitialize()
            if queue_epoch != generation.epoch:  # pragma: no cover
                raise PreviewError("device and job queue epochs diverged")
            self.cache.invalidate_device(old_epoch)
        if bind_job:
            job.token = self.queue.bind_device_epoch(
                job.token.job_id, self.device.epoch
            )

    def _enter_device_lost_locked(self) -> None:
        if self.device.lost:
            return
        old_epoch = self.device.epoch
        self._device_serial += 1
        self.device.handle_device_loss()
        self.queue.handle_device_loss()
        self.cache.invalidate_device(old_epoch, force=True)
        self._pending.clear()

    def finish(
        self,
        token: GenerationToken | int,
        *,
        frame: PreviewFrame | None = None,
        error: PreviewError | None = None,
    ) -> CompletionDecision:
        """Complete a job with an injected frame/error (deterministic tests)."""
        with self._execute_lock:
            return self._finish_inner(token, frame=frame, error=error)

    def _finish_inner(
        self,
        token: GenerationToken | int,
        *,
        frame: PreviewFrame | None,
        error: PreviewError | None,
    ) -> CompletionDecision:
        with self._state_lock:
            job_id = (
                token.job_id if isinstance(token, GenerationToken) else int(token)
            )
            job = self._pending.get(job_id)
            if job is None:
                decision = self.queue.complete(job_id)
                self._record_rejection_locked(decision)
                return decision
            current, reason = self._completion_current_locked(job)
            if not current:
                self._pending.pop(job_id, None)
                return self._reject_locked(job.token, reason)
            if error is not None:
                self._pending.pop(job_id, None)
                self.queue.complete(job.token)
                diagnostics = link_source_diagnostics(
                    error.diagnostics,
                    source_text=job.source_text,
                    source_name=str(self.session.source_path or "<editor>"),
                )
                return self._reject_locked(
                    job.token, "preview_failed", diagnostics
                )
            if frame is None:
                frame = ReferencePreviewBackend().render(
                    job.document,
                    job.tick,
                    width=job.width,
                    height=job.height,
                )
            decision = self.queue.complete(job.token)
            if not decision.accepted:
                self._pending.pop(job_id, None)
                self._record_rejection_locked(decision)
                return decision
            self._pending.pop(job_id, None)
            installed, install_reason, diagnostics = self._install_locked(
                job, frame
            )
            if not installed:
                return self._reject_locked(job.token, install_reason, diagnostics)
            return decision

    def preview_now(
        self,
        *,
        tick: int | None = None,
        backend: PreviewBackend | None = None,
    ) -> CompletionDecision:
        token = self.submit_preview(tick=tick, backend=backend)
        return self.execute(token)

    def execute_pending_in_order(
        self, job_ids: Sequence[int] | None = None
    ) -> list[CompletionDecision]:
        ids = list(job_ids) if job_ids is not None else sorted(self._pending)
        return [self.execute(job_id) for job_id in ids]

    # -- device lifecycle --------------------------------------------------
    def handle_device_loss(self, *, clear_history: bool = False) -> DeviceGeneration:
        with self._state_lock:
            notify = getattr(self.backend, "handle_device_loss", None)
            if callable(notify):
                try:
                    notify()
                except Exception:  # pragma: no cover - defensive boundary
                    pass
            self._enter_device_lost_locked()
            generation = self.device.generation
            if clear_history:
                self._history.clear()
                self._last_valid = None
            return generation

    def reinitialize_device(
        self,
        *,
        adapter: AdapterDeviceIdentity | None = None,
        driver_version: str | None = None,
        os_build: str | None = None,
        toolchain: ToolchainIdentity | None = None,
    ) -> DeviceGeneration:
        with self._state_lock:
            self._device_serial += 1
            if toolchain is not None:
                self._toolchain = toolchain
            desired_epoch = self.device.epoch + 1
            old_epoch = self.device.epoch
            recreate = getattr(self.backend, "reinitialize_device", None)
            backend_epoch: int | None = None
            if callable(recreate):
                try:
                    backend_epoch = int(recreate(desired_epoch))
                except TypeError:
                    backend_epoch = int(recreate())
                except Exception:  # pragma: no cover - defensive boundary
                    backend_epoch = None
            generation = self.device.reinitialize(
                adapter=adapter,
                capability_profile=CapabilityProfileIdentity(
                    self.session.document.profile_id,
                    self.session.document.requirements_digest,
                ),
                driver_version=driver_version,
                os_build=os_build,
                toolchain_identity=(
                    _toolchain_text(self._toolchain) if toolchain is not None else None
                ),
            )
            queue_epoch = self.queue.reinitialize()
            if queue_epoch != generation.epoch:  # pragma: no cover
                raise PreviewError("device and job queue epochs diverged")
            if backend_epoch is not None and backend_epoch != generation.epoch:
                # Keep the pipeline honest about a runtime that recreated at a
                # different actual epoch.
                raise PreviewError(
                    "runtime device epoch diverged from the pipeline epoch"
                )
            self._pending.clear()
            self.cache.invalidate_device(old_epoch)
            self._last_valid = None
            self._last_diagnostics = []
            return generation

    def _advance_backend_epoch_locked(self, target_epoch: int) -> None:
        """Advance pipeline/queue identity for a newly created backend device."""
        if target_epoch <= self.device.epoch:
            raise PreviewError("backend replacement must advance the device epoch")
        while self.device.epoch < target_epoch:
            old_epoch = self.device.epoch
            generation = self.device.reinitialize(
                capability_profile=CapabilityProfileIdentity(
                    self.session.document.profile_id,
                    self.session.document.requirements_digest,
                )
            )
            queue_epoch = self.queue.reinitialize()
            if queue_epoch != generation.epoch:  # pragma: no cover
                raise PreviewError("device and job queue epochs diverged")
            self.cache.invalidate_device(old_epoch)
        self._device_serial += 1

    def replace_backend(self, backend: PreviewBackend) -> None:
        """Retire an old backend as a real device-lifecycle transition."""
        if backend is self.backend:
            return
        with self._execute_lock:
            with self._state_lock:
                target_epoch = self.device.epoch + 1
                align = getattr(backend, "reinitialize_device", None)
                if callable(align):
                    try:
                        aligned = int(align(target_epoch))
                    except TypeError:
                        aligned = int(align())
                    if aligned != target_epoch:
                        close_new = getattr(backend, "close", None)
                        if callable(close_new):
                            close_new()
                        raise PreviewError(
                            "replacement backend device epoch diverged from pipeline"
                        )
                self._cancel_pending_locked(advance_generation=True)
                old_backend = self.backend
                self._advance_backend_epoch_locked(target_epoch)
                self.backend = backend
            close_old = getattr(old_backend, "close", None)
            if callable(close_old):
                close_old()

    def reconfigure_backend(
        self, configure: Callable[[int], None]
    ) -> None:
        """Reconfigure a runtime as a new device instance, never the old epoch."""
        with self._execute_lock:
            with self._state_lock:
                target_epoch = self.device.epoch + 1
                configure(target_epoch)
                self._cancel_pending_locked(advance_generation=True)
                self._advance_backend_epoch_locked(target_epoch)

    # -- cache keys / memory ----------------------------------------------
    def set_toolchain(self, toolchain: ToolchainIdentity) -> None:
        """Update the compiler/toolchain identity used by every key.

        This does not increment the D3D12 device epoch, but it immediately
        changes compiler and device-resident realization keys.
        """
        if not isinstance(toolchain, ToolchainIdentity):
            raise PreviewError("set_toolchain requires a ToolchainIdentity")
        with self._state_lock:
            self._toolchain = toolchain
            self.device.update_toolchain_identity(_toolchain_text(toolchain))
            # A job submitted under the old toolchain/export identity must not
            # install after that identity changes.
            self._cancel_pending_locked(advance_generation=True)

    def _cancel_pending_locked(self, *, advance_generation: bool = False) -> None:
        for job_id in list(self._pending):
            self.queue.cancel(job_id)
        self._pending.clear()
        if advance_generation:
            # Evaluation inputs changed even though the semantic graph did
            # not.  Advance the immutable snapshot generation so an already
            # running render from the previous inputs cannot install.
            self.queue.note_snapshot(self.queue.semantic_digest)

    def invalidate_pending_jobs(self) -> None:
        with self._state_lock:
            self._cancel_pending_locked(advance_generation=True)

    def cache_keys(
        self, frame: PreviewFrame | None = None
    ) -> tuple[str, str]:
        """Return the same compiler/device keys used by realization install.

        ``frame`` supplies the generated shader/binding identities.  When it is
        omitted, the last valid frame is used if available; otherwise the key
        explicitly carries empty shader/binding identities plus the current
        measured toolchain, preview resolution, capability profile, and device
        generation.
        """
        with self._state_lock:
            selected = frame or self.last_valid_frame
            if selected is None:
                selected = PreviewFrame(
                    tick=self.session.timeline_tick,
                    width=self.width,
                    height=self.height,
                    image_sha256="",
                    semantic_digest=self.session.semantic_digest,
                )
                identity = self._current_document_identity()
            else:
                record = self._record_for_frame(selected)
                if record is not None and record.document_identity:
                    identity = dict(record.document_identity)
                else:
                    identity = self._current_document_identity()
                    if selected.semantic_digest:
                        identity["semantic_digest"] = selected.semantic_digest
            inputs = self._cache_inputs_from_identity(
                identity,
                selected,
                width=selected.width,
                height=selected.height,
            )
            compiler = inputs.compiler_key()
            device = inputs.device_realization_key(self.device.generation)
            return compiler.key, device.key

    def _record_for_frame(self, frame: PreviewFrame) -> PreviewRecord | None:
        for record in reversed(self._history):
            if record.frame is frame:
                return record
        for record in reversed(self._history):
            if (
                record.frame.semantic_digest == frame.semantic_digest
                and record.frame.tick == frame.tick
                and record.frame.width == frame.width
                and record.frame.height == frame.height
            ):
                return record
        return None

    def _current_document_identity(self) -> dict[str, Any]:
        document = self.session.document
        return {
            "semantic_digest": self.session.semantic_digest,
            "profile_id": document.profile_id,
            "requirements_digest": document.requirements_digest,
            "settings": dict(document.settings),
        }

    def set_gpu_budget(self, budget_bytes: int) -> None:
        with self._state_lock:
            self.cache.set_budget(budget_bytes)

    # -- reporting ---------------------------------------------------------
    def report(self) -> dict[str, Any]:
        with self._state_lock:
            record = self.last_valid_preview
            generation = self.device.generation
            return {
                "format": "nw08-preview-report",
                "version": 1,
                "snapshot_generation": self.queue.snapshot_generation,
                "device": {
                    "epoch": self.device.epoch,
                    "lost": self.device.lost,
                    "generation": generation.to_key_fields(),
                    "generation_digest": generation.digest,
                    "adapter_available": generation.adapter.available,
                    "host_identity": self.host_identity.to_key_fields(),
                },
                "last_valid": record.to_dict() if record else None,
                "last_valid_invalidated_on_device_loss": (
                    self.device.lost and self._last_valid is not None
                ),
                "diagnostics": self._last_diagnostics,
                "rejected_completions": list(self._rejected),
                "gpu_cache": {
                    "budget_bytes": self.cache.budget_bytes,
                    "live_bytes": self.cache.live_bytes,
                    "pending_retirement_bytes": self.cache.pending_retirement_bytes,
                    "total_bytes": self.cache.total_bytes,
                    "within_budget": self.cache.within_budget,
                    "stats": self.cache.stats,
                },
                "queue": {
                    "pending_jobs": list(self.queue.pending_jobs),
                    "stats": self.queue.stats,
                },
                "realization_persistence": bool(
                    getattr(self.backend, "persistent_realizations", True)
                ),
                "reserves_gpu_memory": bool(
                    getattr(self.backend, "reserves_gpu_memory", False)
                ),
                "runtime_session": (
                    self.backend.report()
                    if callable(getattr(self.backend, "report", None))
                    else {}
                ),
                "node_cache": (
                    {
                        "cached_nodes": list(record.cached_nodes),
                        "recomputed_nodes": list(record.recomputed_nodes),
                        "realization_keys": {
                            node_id: key
                            for node_id, key in record.node_realization_keys
                        },
                    }
                    if record
                    else {
                        "cached_nodes": [],
                        "recomputed_nodes": [],
                        "realization_keys": {},
                    }
                ),
        }

    # -- internals ---------------------------------------------------------
    def _completion_current_locked(
        self, job: _PendingPreviewJob
    ) -> tuple[bool, str]:
        current, reason = self.queue.is_current(job.token)
        if not current:
            return current, reason
        if self.device.lost:
            return False, "device_lost"
        if job.token.device_epoch != self.device.epoch:
            return False, "obsolete_device_epoch"
        if job.semantic_digest != job.token.semantic_digest:
            return False, "snapshot_document_mismatch"
        return True, "current"

    def _cache_inputs_for(
        self,
        document: nw03_compiler.GraphDocument,
        semantic_digest: str,
        frame: PreviewFrame,
        *,
        width: int,
        height: int,
    ) -> CacheKeyInputs:
        return self._cache_inputs_from_identity(
            {
                "semantic_digest": semantic_digest,
                "profile_id": document.profile_id,
                "requirements_digest": document.requirements_digest,
                "settings": dict(document.settings),
            },
            frame,
            width=width,
            height=height,
        )

    def _cache_inputs_from_identity(
        self,
        identity: Mapping[str, Any],
        frame: PreviewFrame,
        *,
        width: int,
        height: int,
    ) -> CacheKeyInputs:
        profile_id = str(identity["profile_id"])
        requirements_digest = str(identity["requirements_digest"])
        return CacheKeyInputs(
            semantic_digest=str(identity["semantic_digest"]),
            capability_profile=CapabilityProfileIdentity(
                profile_id, requirements_digest
            ),
            target_profile_id=profile_id,
            target_requirements_digest=requirements_digest,
            toolchain=self._toolchain,
            compiler_settings=dict(identity.get("settings") or {}),
            semantic_inputs={
                "document_profile": profile_id,
                "preview_width": width,
                "preview_height": height,
                "binding_identity": frame.binding_identity,
            },
            compiler_inputs={
                "compiler_id": self._toolchain.compiler_id,
                "compiler_version": self._toolchain.compiler_version,
                "binding_identity": frame.binding_identity,
            },
            shader_identity=frame.shader_identity,
            entry_point="preview",
        )

    def _node_realizations(
        self,
        job: _PendingPreviewJob,
        frame: PreviewFrame,
    ) -> list[_NodeRealization]:
        document = job.document
        hashes = structural_hashes(document)
        sizes = node_size_attribution(
            document,
            width=frame.width,
            height=frame.height,
            total_bytes=max(1, int(frame.resident_bytes)),
        )
        profile = CapabilityProfileIdentity(
            document.profile_id, document.requirements_digest
        )
        node_shader_ids = {
            node_id: frame.node_shader_identities.get(
                node_id, NO_SHADER_IDENTITY
            )
            for node_id in document.nodes
        }
        shader_closure_cache: dict[str, frozenset[str]] = {}

        def shader_closure(node_id: str) -> frozenset[str]:
            cached = shader_closure_cache.get(node_id)
            if cached is not None:
                return cached
            values: set[str] = set()
            own = node_shader_ids.get(node_id, NO_SHADER_IDENTITY)
            if own != NO_SHADER_IDENTITY:
                values.add(own)
            for source_node, _source_port in document.nodes[
                node_id
            ].inputs.values():
                if source_node in document.nodes:
                    values.update(shader_closure(source_node))
            result = frozenset(values)
            shader_closure_cache[node_id] = result
            return result

        rows: list[_NodeRealization] = []
        for node_id in sorted(document.nodes):
            node = document.nodes[node_id]
            shader_values = shader_closure(node_id)
            shader_identity = (
                sha256_value(
                    {
                        "domain": "nw08.preview.node-shader-closure.v1",
                        "node_id": node_id,
                        "shaders": sorted(shader_values),
                    }
                )
                if shader_values
                else NO_SHADER_IDENTITY
            )
            node_inputs = CacheKeyInputs(
                semantic_digest=hashes.get(node_id, ""),
                capability_profile=profile,
                target_profile_id=document.profile_id,
                target_requirements_digest=document.requirements_digest,
                toolchain=self._toolchain,
                compiler_settings=dict(document.settings),
                semantic_inputs={
                    "node_id": node_id,
                    "operator_id": node.operator_id,
                    "operator_version": node.version,
                    "preview_width": frame.width,
                    "preview_height": frame.height,
                },
                compiler_inputs={
                    "compiler_id": self._toolchain.compiler_id,
                    "compiler_version": self._toolchain.compiler_version,
                    "entry_point": f"node:{node.operator_id}",
                },
                observed_hardware={},
                shader_identity=shader_identity,
                entry_point=f"node:{node.operator_id}",
            )
            key = node_inputs.device_realization_key(
                self.device.generation
            ).key
            if node.frequency == "history" or node.history is not None:
                # Temporal state is an external-input recurrence, never a pure
                # frame-independent cached output.  Bind the key to the exact
                # simulation tick and committed state digest so a history
                # realization cannot be reused as a stateless cache entry.
                key = sha256_value(
                    {
                        "domain": "nw09.preview.temporal-realization.v1",
                        "base_key": key,
                        "node_id": node_id,
                        "tick": int(job.tick),
                        "state_layout_hash": (
                            node.history.state_layout_hash()
                            if node.history is not None
                            else ""
                        ),
                        "state_digest": frame.content_digest,
                    }
                )
            rows.append(
                _NodeRealization(
                    node_id=node_id,
                    key=key,
                    size_bytes=max(1, int(sizes.get(node_id, 1))),
                )
            )
        return rows

    def _install_locked(
        self,
        job: _PendingPreviewJob,
        frame: PreviewFrame,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        current, reason = self._completion_current_locked(job)
        if not current:
            return False, reason, []
        if (
            frame.device_epoch is not None
            and frame.device_epoch != self.device.epoch
        ):
            return False, "obsolete_device_epoch", []
        self._adopt_frame_device_identity_locked(frame)
        if job.token.job_id < self._last_installed_job:
            return False, "superseded_by_newer_job", []
        persistent = bool(
            getattr(job.backend, "persistent_realizations", True)
        )
        inputs = self._cache_inputs_for(
            job.document,
            job.token.semantic_digest,
            frame,
            width=frame.width,
            height=frame.height,
        )
        compiler_key = inputs.compiler_key()
        device_key = inputs.device_realization_key(self.device.generation)
        rows = self._node_realizations(job, frame)
        cached_nodes: list[str] = []
        recomputed_nodes: list[str] = []
        admitted: list[str] = []
        self._fence += 1
        fence = self._fence
        for row in rows:
            hit = self.cache.lookup(row.key, epoch=self.device.epoch)
            if hit is not None:
                # Pin the hit against eviction by later admissions in this
                # same installation.
                self.cache.submit([row.key], fence)
                cached_nodes.append(row.node_id)
                continue
            try:
                self.cache.acquire(
                    row.key,
                    row.size_bytes,
                    epoch=self.device.epoch,
                )
            except GpuBudgetExceeded as exc:
                self.cache.signal_fence(fence)
                for key in admitted:
                    self.cache.release(key)
                return (
                    False,
                    "gpu_cache_budget_exceeded",
                    [
                        {
                            "category": "cache",
                            "severity": "error",
                            "code": "gpu_cache_budget_exceeded",
                            "message": str(exc),
                        }
                    ],
                )
            except DeviceLostError as exc:
                self.cache.signal_fence(fence)
                for key in admitted:
                    self.cache.release(key)
                return (
                    False,
                    "preview_device_lost",
                    [
                        {
                            "category": "preview",
                            "severity": "error",
                            "code": "preview_device_lost",
                            "message": str(exc),
                        }
                    ],
                )
            admitted.append(row.key)
            recomputed_nodes.append(row.node_id)
            self.cache.submit([row.key], fence)
        self.cache.submit([row.key for row in rows], fence)
        # The frame has been produced/read back, so the work that referenced
        # these realizations has retired.  Live entries remain reusable cache
        # hits but are now evictable; only the non-persistent runtime path
        # destroys them immediately.
        self.cache.signal_fence(fence)
        if not persistent:
            # The runtime process has exited, so its device work is complete.
            # The realization is accounted for during execution but is not
            # retained as a reusable GPU object.
            for row in rows:
                self.cache.release(row.key)
        frame_with_digest = replace(
            frame, semantic_digest=job.token.semantic_digest
        )
        record = PreviewRecord(
            token=job.token,
            frame=frame_with_digest,
            compiler_key=compiler_key.key,
            device_realization_key=device_key.key,
            cache_hit=not recomputed_nodes,
            node_realization_keys=tuple(
                (row.node_id, row.key) for row in rows
            ),
            cached_nodes=tuple(sorted(cached_nodes)),
            recomputed_nodes=tuple(sorted(recomputed_nodes)),
            realization_persistent=persistent,
            document_identity={
                "semantic_digest": job.token.semantic_digest,
                "profile_id": job.document.profile_id,
                "requirements_digest": job.document.requirements_digest,
                "settings": dict(job.document.settings),
            },
        )
        self._last_installed_job = job.token.job_id
        if self._history and self._history[-1].frame.image_data:
            previous = self._history[-1]
            self._history[-1] = replace(
                previous, frame=replace(previous.frame, image_data=b"")
            )
        self._last_valid = record
        self._history.append(record)
        self._last_diagnostics = list(frame_with_digest.diagnostics)
        return True, "", []

    def _adopt_frame_device_identity_locked(self, frame: PreviewFrame) -> None:
        """Record the actual adapter of the device that produced ``frame``.

        This refines the identity of the *current* device instance (first
        render) and never changes the epoch.  Marketing names are not part of
        the identity mapping.
        """
        identity = frame.device_identity
        if not identity or not identity.get("available"):
            return
        adapter_fields = identity.get("adapter") or {}
        try:
            adapter = AdapterDeviceIdentity(
                vendor_id=int(adapter_fields.get("vendor_id", 0) or 0),
                device_id=int(adapter_fields.get("device_id", 0) or 0),
                subsystem_id=int(adapter_fields.get("subsystem_id", 0) or 0),
                adapter_luid=str(adapter_fields.get("adapter_luid", "") or ""),
            )
        except (CacheIdentityError, TypeError, ValueError):
            return
        if not adapter.available:
            return
        resolved_driver = str(identity.get("driver_version", "unavailable"))
        resolved_os = str(identity.get("os_build", "") or "")
        self.host_identity = HostDeviceIdentity(
            adapter=adapter,
            driver_version=resolved_driver,
            os_build=resolved_os or self.host_identity.os_build,
            available=True,
            software=bool(identity.get("software", False)),
            detail="identity reported by the selected runtime D3D12 device",
        )
        self.device.adopt_device_identity(
            adapter,
            driver_version=resolved_driver,
            os_build=resolved_os or None,
        )

    def _reserve_runtime_locked(
        self, job: _PendingPreviewJob
    ) -> _RuntimeReservation:
        resources = estimate_size_attribution(
            job.document, width=job.width, height=job.height
        )
        size_bytes = max(1, int(resources.get("resident_payload_bytes", 1)))
        key = sha256_value(
            {
                "domain": "nw08.runtime-preview-reservation.v1",
                "job_id": job.token.job_id,
                "semantic_digest": job.token.semantic_digest,
                "device_generation": self.device.generation.digest,
                "resident_bytes": size_bytes,
            }
        )
        self.cache.acquire(key, size_bytes, epoch=self.device.epoch)
        self._fence += 1
        fence = self._fence
        self.cache.submit([key], fence)
        return _RuntimeReservation(key=key, fence=fence, size_bytes=size_bytes)

    def _retire_reservation_locked(
        self, reservation: _RuntimeReservation
    ) -> None:
        self.cache.signal_fence(reservation.fence)
        self.cache.release(reservation.key)

    def _reject_locked(
        self,
        token: GenerationToken,
        reason: str,
        diagnostics: Sequence[Mapping[str, Any]] | None = None,
    ) -> CompletionDecision:
        decision = CompletionDecision(False, reason, token)
        self._record_rejection_locked(decision, diagnostics)
        return decision

    def _record_rejection(
        self,
        decision: CompletionDecision,
        diagnostics: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        with self._state_lock:
            self._record_rejection_locked(decision, diagnostics)

    def _record_rejection_locked(
        self,
        decision: CompletionDecision,
        diagnostics: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self._rejected.append(decision.to_dict())
        # Retain the last valid frame; diagnostics never replace it.
        if diagnostics:
            self._last_diagnostics = [dict(item) for item in diagnostics]
        else:
            self._last_diagnostics = [
                {
                    "category": "preview",
                    "severity": "warning",
                    "code": "preview_completion_rejected",
                    "message": f"completion rejected: {decision.reason}",
                    "job_id": decision.token.job_id,
                }
            ]


def create_preview_backend(
    player_executable: str | os.PathLike[str] | None = None,
    *,
    capture_controls: bool = True,
    mode: str = "package-host",
    dxc_executable: str | None = None,
    player_template: str | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    export_settings: nw05_export.ExportSettings | Mapping[str, Any] | None = None,
    allow_test_payloads: bool = False,
    optimization: str = "O2",
    execution_kind: str = "simulated",
    memory_budget_bytes: int | None = None,
    allow_software_adapter: bool = False,
) -> PreviewBackend:
    """Select the real runtime backend when a player is supplied."""
    if player_executable is not None or mode == "embedded-player":
        return WindowsRuntimePreviewBackend(
            player_executable,
            capture_controls=capture_controls,
            mode=mode,
            dxc_executable=dxc_executable,
            player_template=player_template,
            cache_dir=cache_dir,
            export_settings=export_settings,
            allow_test_payloads=allow_test_payloads,
            optimization=optimization,
            execution_kind=execution_kind,
            memory_budget_bytes=memory_budget_bytes,
            allow_software_adapter=allow_software_adapter,
        )
    return ReferencePreviewBackend()


def render_preview(
    session: GraphEditorSession,
    *,
    tick: int | None = None,
    backend: PreviewBackend | None = None,
    gpu_budget_bytes: int = 512 * 1024 * 1024,
    adapter: AdapterDeviceIdentity | None = None,
    driver_version: str = "",
    os_build: str = "",
    toolchain: ToolchainIdentity | None = None,
) -> PreviewRecord | None:
    """Synchronously render one preview and return the accepted record."""
    pipeline = PreviewPipeline(
        session,
        backend=backend,
        gpu_budget_bytes=gpu_budget_bytes,
        adapter=adapter,
        driver_version=driver_version,
        os_build=os_build,
        toolchain=toolchain,
    )
    pipeline.preview_now(tick=tick)
    return pipeline.last_valid_preview


# Compatibility aliases.
PreviewController = PreviewPipeline


__all__ = [
    "NO_SHADER_IDENTITY",
    "AuthoringRuntimeSession",
    "PassTiming",
    "PassTimings",
    "PreviewBackend",
    "PreviewController",
    "PreviewError",
    "PreviewExportBundle",
    "PreviewExporter",
    "PreviewFrame",
    "PreviewPipeline",
    "PreviewRecord",
    "ReferencePreviewBackend",
    "RuntimeSessionError",
    "RuntimePackageExporter",
    "WindowsRuntimePreviewBackend",
    "create_preview_backend",
    "render_preview",
    "toolchain_identity_for",
]
