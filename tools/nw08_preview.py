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
import subprocess
import sys
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
        }


@dataclass(frozen=True)
class PassTimings:
    timings: tuple[PassTiming, ...]

    @property
    def total_milliseconds(self) -> float:
        return sum(item.milliseconds for item in self.timings)

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
    backend: str = "reference"
    hardware_evidence: bool = False
    execution_kind: str = "contract_reference"
    diagnostics: tuple[dict[str, Any], ...] = ()

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
            "backend": self.backend,
            "hardware_evidence": self.hardware_evidence,
            "execution_kind": self.execution_kind,
            "diagnostics": list(self.diagnostics),
        }


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
    stats_path: Path | None = None
    export_report: Mapping[str, Any] = field(default_factory=dict)


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
        if self.allow_test_payloads:
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
        if self.mode == "package-host":
            package_path = work_directory / "edited-preview.nwp"
            package_path.write_bytes(artifact.package_bytes)
            return PreviewExportBundle(
                package_sha256=package_sha,
                package_path=package_path,
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
            export_report=result.to_dict(),
        )


class WindowsRuntimePreviewBackend:
    """Preview the *edited* graph through the existing package-player runtime.

    The backend first exports the document it was given (through the same
    ``nw05_export.export_graph`` path used by the CLI) and then runs either the
    native package host on that ``.nwp`` or the generated player built from it.
    A successful process exit without a readback file is a failure, never
    hardware evidence.
    """

    name = "windows-package-player"
    persistent_realizations = False
    reserves_gpu_memory = True

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
        if self.memory_budget_bytes:
            command.extend(["--memory-budget", str(int(self.memory_budget_bytes))])
        if stats_path is not None:
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
        import tempfile

        timings: list[PassTiming] = []
        with tempfile.TemporaryDirectory(prefix=".nw08-preview-") as raw:
            work_directory = Path(raw)
            capture_dir = work_directory / "capture"
            capture_dir.mkdir()
            stats_path = work_directory / "runtime-stats.json"
            export_start = time.perf_counter()
            try:
                bundle = self._exporter.export(
                    document, work_directory, width=width, height=height
                )
            except (nw05_export.ExportError, nw03_compiler.CompilerError) as exc:
                raise PreviewError(
                    str(exc), getattr(exc, "diagnostics", [])
                ) from exc
            timings.append(
                PassTiming(
                    "offline_compile_export",
                    (time.perf_counter() - export_start) * 1000.0,
                    kind="compiler",
                )
            )
            command = self.command(
                tick,
                capture_dir,
                bundle,
                width=width,
                height=height,
                stats_path=stats_path,
            )
            start = time.perf_counter()
            process = self._runner(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            timings.append(PassTiming("runtime_frame", elapsed_ms, kind="runtime"))
            if process.returncode != 0:
                diagnostics = [
                    {
                        "category": "preview",
                        "severity": "error",
                        "code": "preview_execution_failed",
                        "message": (
                            f"package player exited {process.returncode}: "
                            f"{(process.stderr or '').strip()}"
                        ),
                    }
                ]
                raise PreviewError(
                    f"package player exited {process.returncode}", diagnostics
                )
            capture = capture_dir / f"tick_{tick}.rgba32f"
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
            expected = width * height * 16
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
            timings.append(
                PassTiming("present_readback", elapsed_ms * 0.25, kind="runtime")
            )
            stats = self._read_stats(stats_path)
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
            resources = estimate_size_attribution(document, width=width, height=height)
            if "engine_resource_bytes" in stats:
                engine_bytes = int(stats["engine_resource_bytes"])
            else:
                engine_bytes = int(resources.get("resident_payload_bytes", 0))
            physical = bool(self._physical_capable and image_sha)
            return PreviewFrame(
                tick=tick,
                width=width,
                height=height,
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
                        "width": width,
                        "height": height,
                        "image_sha256": image_sha,
                        "execution_kind": "physical" if physical else "simulated",
                    }
                ),
                resident_bytes=max(1, engine_bytes),
                pass_timings=PassTimings(tuple(timings)),
                size_attribution=resources,
                runtime_stats=stats,
                backend=self.name,
                hardware_evidence=physical,
                execution_kind=(
                    "physical_runtime" if physical else "simulated_runtime"
                ),
            )




@dataclass
class _PendingPreviewJob:
    token: GenerationToken
    tick: int
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
        if adapter is None:
            observed = discover_host_device_identity()
            adapter = observed.adapter
            if not driver_version:
                driver_version = observed.driver_version
            if not os_build:
                os_build = observed.os_build
            self.host_identity = observed
        else:
            self.host_identity = discover_host_device_identity()
            self.host_identity = replace(
                self.host_identity,
                adapter=adapter,
                driver_version=driver_version or self.host_identity.driver_version,
                os_build=os_build or self.host_identity.os_build,
                available=adapter.available,
            )
        if toolchain is None:
            self._toolchain = ToolchainIdentity(os_build=os_build)
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
            decision = self.queue.complete(job.token)
            if not decision.accepted:
                self._pending.pop(job_id, None)
                self._record_rejection_locked(decision)
                return decision
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
        # Rendering deliberately happens outside the state lock so an explicit
        # device-loss/reinitialization can invalidate an in-flight completion.
        try:
            frame = job.backend.render(
                job.document,
                job.tick,
                width=self.width,
                height=self.height,
            )
        except PreviewError as exc:
            diagnostics = link_source_diagnostics(
                exc.diagnostics,
                source_text=job.source_text,
                source_name=str(self.session.source_path or "<editor>"),
            )
            with self._state_lock:
                self._pending.pop(job_id, None)
                if reservation is not None:
                    self._retire_reservation_locked(reservation)
                return self._reject_locked(
                    job.token, "preview_failed", diagnostics
                )
        except DeviceLostError as exc:
            with self._state_lock:
                self._pending.pop(job_id, None)
                if reservation is not None:
                    self._retire_reservation_locked(reservation)
                return self._reject_locked(
                    job.token,
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
        except Exception as exc:  # pragma: no cover - defensive boundary
            with self._state_lock:
                self._pending.pop(job_id, None)
                if reservation is not None:
                    self._retire_reservation_locked(reservation)
                return self._reject_locked(
                    job.token,
                    "preview_failed",
                    [
                        {
                            "category": "preview",
                            "severity": "error",
                            "code": "preview_failed",
                            "message": str(exc),
                            "source_linked": False,
                        }
                    ],
                )
        with self._state_lock:
            self._pending.pop(job_id, None)
            if reservation is not None:
                self._retire_reservation_locked(reservation)
            # Revalidate both the immutable snapshot generation and the device
            # epoch after rendering.  A completion from an older snapshot or
            # device instance can never install.
            current, reason = self._completion_current_locked(job)
            if not current:
                return self._reject_locked(job.token, reason)
            installed, install_reason, diagnostics = self._install_locked(
                job, frame
            )
            if not installed:
                return self._reject_locked(job.token, install_reason, diagnostics)
            return decision

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
            decision = self.queue.complete(job.token)
            if not decision.accepted:
                self._pending.pop(job_id, None)
                self._record_rejection_locked(decision)
                return decision
            current, reason = self._completion_current_locked(job)
            if not current:
                self._pending.pop(job_id, None)
                return self._reject_locked(job.token, reason)
            self._pending.pop(job_id, None)
            if error is not None:
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
                    width=self.width,
                    height=self.height,
                )
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
            old_epoch = self.device.epoch
            self._device_serial += 1
            generation = self.device.handle_device_loss()
            self.queue.handle_device_loss()
            self.cache.invalidate_device(old_epoch, force=True)
            self._pending.clear()
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
            self._pending.clear()
            self._last_valid = None
            self._last_diagnostics = []
            return generation

    # -- cache keys / memory ----------------------------------------------
    def cache_keys(self) -> tuple[str, str]:
        inputs = self.session.cache_key_inputs(
            toolchain=self._toolchain,
            compiler_settings=self.session.document.settings,
        )
        compiler = inputs.compiler_key()
        device = inputs.device_realization_key(self.device.generation)
        return compiler.key, device.key

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
    ) -> CacheKeyInputs:
        return CacheKeyInputs(
            semantic_digest=semantic_digest,
            capability_profile=CapabilityProfileIdentity(
                document.profile_id, document.requirements_digest
            ),
            target_profile_id=document.profile_id,
            target_requirements_digest=document.requirements_digest,
            toolchain=self._toolchain,
            compiler_settings=dict(document.settings),
            semantic_inputs={"document_profile": document.profile_id},
            compiler_inputs={
                "compiler_id": self._toolchain.compiler_id,
                "compiler_version": self._toolchain.compiler_version,
            },
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
            width=self.width,
            height=self.height,
            total_bytes=max(1, int(frame.resident_bytes)),
        )
        profile = CapabilityProfileIdentity(
            document.profile_id, document.requirements_digest
        )
        rows: list[_NodeRealization] = []
        for node_id in sorted(document.nodes):
            node = document.nodes[node_id]
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
                },
                compiler_inputs={
                    "compiler_id": self._toolchain.compiler_id,
                    "compiler_version": self._toolchain.compiler_version,
                    "entry_point": f"node:{node.operator_id}",
                },
                observed_hardware={},
            )
            key = node_inputs.device_realization_key(
                self.device.generation
            ).key
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
        if job.token.job_id < self._last_installed_job:
            return False, "superseded_by_newer_job", []
        persistent = bool(
            getattr(job.backend, "persistent_realizations", True)
        )
        inputs = self._cache_inputs_for(job.document, job.token.semantic_digest)
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
        )
        self._last_installed_job = job.token.job_id
        self._last_valid = record
        self._history.append(record)
        self._last_diagnostics = list(frame_with_digest.diagnostics)
        return True, "", []

    def _reserve_runtime_locked(
        self, job: _PendingPreviewJob
    ) -> _RuntimeReservation:
        resources = estimate_size_attribution(
            job.document, width=self.width, height=self.height
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
) -> PreviewBackend:
    """Select the real runtime backend when a player is supplied."""
    if player_executable is not None:
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
    "RuntimePackageExporter",
    "WindowsRuntimePreviewBackend",
    "create_preview_backend",
    "render_preview",
]
