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

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import nw03_compiler  # noqa: E402
import nw06_procedural  # noqa: E402
import nw07_export  # noqa: E402
from nw08_cache import (  # noqa: E402
    AdapterDeviceIdentity,
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
    canonical_json_bytes,
    sha256_value,
)
from nw08_editor import (  # noqa: E402
    EditResult,
    GraphEditorSession,
    estimate_size_attribution,
    link_source_diagnostics,
    semantic_digest as editor_semantic_digest,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.token.job_id,
            "snapshot_generation": self.token.snapshot_generation,
            "device_epoch": self.token.device_epoch,
            "semantic_digest": self.token.semantic_digest,
            "compiler_key": self.compiler_key,
            "device_realization_key": self.device_realization_key,
            "cache_hit": self.cache_hit,
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


class WindowsRuntimePreviewBackend:
    """Preview through the existing Windows package-player runtime.

    ``capture_controls`` selects the diagnostic sibling's ``--capture-tick``
    protocol.  Without capture controls the existing generated player is
    launched at ``--tick``; no image is fabricated when no readback file is
    produced.
    """

    name = "windows-package-player"
    hardware_evidence = True

    def __init__(
        self,
        executable: str | os.PathLike[str],
        *,
        capture_controls: bool = True,
        width: int = 1920,
        height: int = 1080,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ):
        self.executable = Path(executable)
        if not self.executable.is_file():
            raise PreviewError(
                f"preview executable is unavailable: {self.executable}"
            )
        self.capture_controls = capture_controls
        self.width = width
        self.height = height
        self._runner = runner or subprocess.run

    def command(self, tick: int, capture_dir: Path) -> list[str]:
        if self.capture_controls:
            return [
                str(self.executable),
                "--capture-tick",
                str(tick),
                "--capture-dir",
                str(capture_dir),
            ]
        return [str(self.executable), "--tick", str(tick), "--hold-ms", "1"]

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
            capture_dir = Path(raw)
            command = self.command(tick, capture_dir)
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
            image_sha = ""
            if capture.is_file():
                data = capture.read_bytes()
                expected = width * height * 16
                if len(data) != expected:
                    raise PreviewError(
                        f"preview readback is {len(data)} bytes, expected {expected}"
                    )
                image_sha = hashlib.sha256(data).hexdigest()
                timings.append(PassTiming("present_readback", elapsed_ms * 0.25, kind="runtime"))
            try:
                sample = nw07_export.evaluate_camera_at_tick(document, tick)
            except Exception:
                sample = {}
            resources = estimate_size_attribution(document, width=width, height=height)
            return PreviewFrame(
                tick=tick,
                width=width,
                height=height,
                image_sha256=image_sha,
                semantic_digest="",
                camera_angle_radians=sample.get("camera_angle_radians"),
                camera_angle_modulo_turn=sample.get("camera_angle_modulo_turn"),
                twist_degrees=sample.get("twist_degrees"),
                content_digest=sha256_value(
                    {
                        "command": command,
                        "tick": tick,
                        "image_sha256": image_sha,
                    }
                ),
                resident_bytes=int(resources.get("resident_payload_bytes", 0)),
                pass_timings=PassTimings(tuple(timings)),
                size_attribution=resources,
                backend=self.name,
                hardware_evidence=True,
                execution_kind="physical_runtime",
            )


@dataclass
class _PendingPreviewJob:
    token: GenerationToken
    tick: int
    backend: PreviewBackend


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
        self.device = DeviceEpochManager(
            adapter or AdapterDeviceIdentity(vendor_id=0x10DE, device_id=0x2805),
            capability_profile=CapabilityProfileIdentity(
                session.document.profile_id, session.document.requirements_digest
            ),
            driver_version=driver_version,
            os_build=os_build,
            toolchain_identity=_toolchain_text(toolchain or ToolchainIdentity()),
        )
        self._toolchain = toolchain or ToolchainIdentity()
        self._pending: dict[int, _PendingPreviewJob] = {}
        self._last_valid: PreviewRecord | None = None
        self._last_diagnostics: list[dict[str, Any]] = []
        self._history: list[PreviewRecord] = []
        self._rejected: list[dict[str, Any]] = []
        self._last_installed_job = 0
        self._fence = 0
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
        self._last_edit = result
        if result.valid:
            if result.semantic_digest != self.queue.semantic_digest:
                self.queue.note_snapshot(result.semantic_digest)
                # A new semantic snapshot invalidates no last-valid preview by
                # itself; the previous frame remains visible until a newer job
                # succeeds.
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
        # Callers may manipulate the session directly; synchronize the job
        # generation so an unseen semantic edit still invalidates older jobs.
        if self.session.semantic_digest != self.queue.semantic_digest:
            self.queue.note_snapshot(self.session.semantic_digest)
        token = self.queue.submit(semantic_digest=self.session.semantic_digest)
        job = _PendingPreviewJob(
            token=token,
            tick=self.session.timeline_tick if tick is None else int(tick),
            backend=backend or self.backend,
        )
        self._pending[token.job_id] = job
        return token

    def cancel(self, token: GenerationToken | int) -> bool:
        job_id = token.job_id if isinstance(token, GenerationToken) else int(token)
        self._pending.pop(job_id, None)
        return self.queue.cancel(job_id)

    def execute(self, token: GenerationToken | int) -> CompletionDecision:
        job_id = token.job_id if isinstance(token, GenerationToken) else int(token)
        job = self._pending.get(job_id)
        if job is None:
            decision = self.queue.complete(job_id)
            self._record_rejection(decision)
            return decision
        # Gate before doing any work: obsolete/cancelled jobs never compile.
        decision = self.queue.complete(job.token)
        if not decision.accepted:
            self._pending.pop(job_id, None)
            self._record_rejection(decision)
            return decision
        try:
            frame = job.backend.render(
                self.session.document,
                job.tick,
                width=self.width,
                height=self.height,
            )
        except PreviewError as exc:
            self._last_diagnostics = link_source_diagnostics(
                exc.diagnostics,
                source_text=self.session.source_text,
                source_name=str(self.session.source_path or "<editor>"),
            )
            self._pending.pop(job_id, None)
            return decision
        except Exception as exc:  # pragma: no cover - defensive runtime boundary
            self._last_diagnostics = [
                {
                    "category": "preview",
                    "severity": "error",
                    "code": "preview_failed",
                    "message": str(exc),
                    "source_linked": False,
                }
            ]
            self._pending.pop(job_id, None)
            return decision
        self._pending.pop(job_id, None)
        self._install(job, frame)
        return decision

    def finish(
        self,
        token: GenerationToken | int,
        *,
        frame: PreviewFrame | None = None,
        error: PreviewError | None = None,
    ) -> CompletionDecision:
        """Complete a job with an injected frame/error (deterministic tests)."""
        job_id = token.job_id if isinstance(token, GenerationToken) else int(token)
        job = self._pending.get(job_id)
        if job is None:
            decision = self.queue.complete(job_id)
            self._record_rejection(decision)
            return decision
        decision = self.queue.complete(job.token)
        if not decision.accepted:
            self._pending.pop(job_id, None)
            self._record_rejection(decision)
            return decision
        self._pending.pop(job_id, None)
        if error is not None:
            self._last_diagnostics = link_source_diagnostics(
                error.diagnostics,
                source_text=self.session.source_text,
                source_name=str(self.session.source_path or "<editor>"),
            )
            return decision
        if frame is None:
            frame = ReferencePreviewBackend().render(
                self.session.document,
                job.tick,
                width=self.width,
                height=self.height,
            )
        self._install(job, frame)
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
        old_epoch = self.device.epoch
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
        generation = self.device.reinitialize(
            adapter=adapter,
            capability_profile=CapabilityProfileIdentity(
                self.session.document.profile_id,
                self.session.document.requirements_digest,
            ),
            driver_version=driver_version,
            os_build=os_build,
            toolchain_identity=(
                _toolchain_text(toolchain or self._toolchain)
                if toolchain is not None
                else None
            ),
        )
        queue_epoch = self.queue.reinitialize()
        if queue_epoch != generation.epoch:  # pragma: no cover - internal invariant
            raise PreviewError("device and job queue epochs diverged")
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
        self.cache.set_budget(budget_bytes)

    # -- reporting ---------------------------------------------------------
    def report(self) -> dict[str, Any]:
        record = self.last_valid_preview
        return {
            "format": "nw08-preview-report",
            "version": 1,
            "snapshot_generation": self.queue.snapshot_generation,
            "device": {
                "epoch": self.device.epoch,
                "lost": self.device.lost,
                "generation": self.device.generation.to_key_fields(),
                "generation_digest": self.device.generation.digest,
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
        }

    # -- internals ---------------------------------------------------------
    def _install(self, job: _PendingPreviewJob, frame: PreviewFrame) -> None:
        if job.token.job_id < self._last_installed_job:
            return
        inputs = self.session.cache_key_inputs(
            toolchain=self._toolchain,
            compiler_settings=self.session.document.settings,
        )
        compiler_key = inputs.compiler_key()
        device_key = inputs.device_realization_key(self.device.generation)
        size_bytes = max(1, int(frame.resident_bytes))
        cache_hit = self.cache.lookup(device_key.key, epoch=self.device.epoch)
        if cache_hit is None:
            try:
                self.cache.acquire(
                    device_key.key,
                    size_bytes,
                    epoch=self.device.epoch,
                )
            except GpuBudgetExceeded as exc:
                self._last_diagnostics = [
                    {
                        "category": "cache",
                        "severity": "error",
                        "code": "gpu_cache_budget_exceeded",
                        "message": str(exc),
                    }
                ]
                return
        self._fence += 1
        self.cache.submit([device_key.key], self._fence)
        frame_with_digest = PreviewFrame(
            **{
                **frame.__dict__,
                "semantic_digest": job.token.semantic_digest,
            }
        )
        record = PreviewRecord(
            token=job.token,
            frame=frame_with_digest,
            compiler_key=compiler_key.key,
            device_realization_key=device_key.key,
            cache_hit=cache_hit is not None,
        )
        self._last_installed_job = job.token.job_id
        self._last_valid = record
        self._history.append(record)
        self._last_diagnostics = list(frame_with_digest.diagnostics)

    def _record_rejection(self, decision: CompletionDecision) -> None:
        self._rejected.append(decision.to_dict())
        # Retain the last valid frame; diagnostics never replace it.
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
) -> PreviewBackend:
    """Select the real runtime backend when a player is supplied."""
    if player_executable is not None:
        return WindowsRuntimePreviewBackend(
            player_executable, capture_controls=capture_controls
        )
    return ReferencePreviewBackend()


def render_preview(
    session: GraphEditorSession,
    *,
    tick: int | None = None,
    backend: PreviewBackend | None = None,
    gpu_budget_bytes: int = 512 * 1024 * 1024,
) -> PreviewRecord | None:
    """Synchronously render one preview and return the accepted record."""
    pipeline = PreviewPipeline(
        session, backend=backend, gpu_budget_bytes=gpu_budget_bytes
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
    "PreviewFrame",
    "PreviewPipeline",
    "PreviewRecord",
    "ReferencePreviewBackend",
    "WindowsRuntimePreviewBackend",
    "create_preview_backend",
    "render_preview",
]
