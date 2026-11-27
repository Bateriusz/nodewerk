#!/usr/bin/env python3
"""NW-08 ``nodewerk-studio`` authoring application facade and CLI.

The interactive GUI lives in :mod:`nw08_gui`; this module provides the
headless application model, deterministic report generation, and a small CLI so
the same editor/session can be driven by tests, automation, and the GUI.

The editor exports through the unchanged ``nw05_export.export_graph`` path,
which is also what ``tools/nodewerkc.py export`` calls.  It does not define a
second graph model, package format, or release player.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import nw05_export  # noqa: E402
import nwp_package  # noqa: E402
from nw08_cache import (  # noqa: E402
    AdapterDeviceIdentity,
    DeviceGeneration,
    DeviceLostError,
    GenerationToken,
    ToolchainIdentity,
)
from nw08_editor import (  # noqa: E402
    CurvePoint,
    EditResult,
    EditorError,
    GraphEditorSession,
    NW07_PROOF_GRAPH,
    estimate_size_attribution,
)
from nw08_preview import (  # noqa: E402
    PreviewBackend,
    PreviewPipeline,
    WindowsRuntimePreviewBackend,
    create_preview_backend,
    toolchain_identity_for,
)

DEFAULT_GPU_CACHE_BUDGET = 512 * 1024 * 1024


@dataclass
class StudioApplication:
    """Headless editor application state shared by the GUI and test harness."""

    session: GraphEditorSession
    pipeline: PreviewPipeline
    export_settings: dict[str, Any] = field(default_factory=dict)
    gpu_cache_budget_bytes: int = DEFAULT_GPU_CACHE_BUDGET
    dxc_executable: str = ""
    player_template: str = ""
    cache_dir: str = ""
    optimization: str = "O2"
    allow_test_payloads: bool = False
    player_executable: str = ""
    capture_controls: bool = True
    preview_mode: str = "package-host"
    execution_kind: str = "simulated"
    allow_software_adapter: bool = False
    width: int = 1920
    height: int = 1080
    explicit_backend: bool = False

    @classmethod
    def create(
        cls,
        graph: str | os.PathLike[str] | None = None,
        *,
        backend: PreviewBackend | None = None,
        gpu_cache_budget_bytes: int = DEFAULT_GPU_CACHE_BUDGET,
        player_executable: str | os.PathLike[str] | None = None,
        capture_controls: bool = True,
        width: int = 1920,
        height: int = 1080,
        export_settings: Mapping[str, Any] | None = None,
        adapter: AdapterDeviceIdentity | None = None,
        driver_version: str = "",
        os_build: str = "",
        toolchain: ToolchainIdentity | None = None,
        preview_mode: str = "package-host",
        dxc_executable: str | None = None,
        player_template: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        optimization: str = "O2",
        allow_test_payloads: bool = False,
        execution_kind: str = "simulated",
        allow_software_adapter: bool = False,
    ) -> "StudioApplication":
        session = (
            GraphEditorSession.create_nw07_proof(graph)
            if graph is not None
            else GraphEditorSession.create_nw07_proof()
        )
        if toolchain is None:
            toolchain = toolchain_identity_for(
                dxc_executable,
                optimization=optimization,
                os_build=os_build,
            )
        selected_backend = backend or create_preview_backend(
            player_executable,
            capture_controls=capture_controls,
            mode=preview_mode,
            dxc_executable=dxc_executable,
            player_template=player_template,
            cache_dir=cache_dir,
            export_settings=export_settings,
            allow_test_payloads=allow_test_payloads,
            optimization=optimization,
            execution_kind=execution_kind,
            memory_budget_bytes=gpu_cache_budget_bytes,
            allow_software_adapter=allow_software_adapter,
        )
        pipeline = PreviewPipeline(
            session,
            backend=selected_backend,
            gpu_budget_bytes=gpu_cache_budget_bytes,
            adapter=adapter,
            driver_version=driver_version,
            os_build=os_build,
            toolchain=toolchain,
            width=width,
            height=height,
        )
        return cls(
            session=session,
            pipeline=pipeline,
            export_settings=dict(export_settings or {}),
            gpu_cache_budget_bytes=gpu_cache_budget_bytes,
            dxc_executable=str(dxc_executable or ""),
            player_template=str(player_template or ""),
            cache_dir=str(cache_dir or ""),
            optimization=optimization,
            allow_test_payloads=allow_test_payloads,
            player_executable=str(player_executable or ""),
            capture_controls=capture_controls,
            preview_mode=preview_mode,
            execution_kind=execution_kind,
            allow_software_adapter=allow_software_adapter,
            width=width,
            height=height,
            explicit_backend=backend is not None,
        )

    def _build_backend(self) -> PreviewBackend:
        return create_preview_backend(
            self.player_executable or None,
            capture_controls=self.capture_controls,
            mode=self.preview_mode,
            dxc_executable=self.dxc_executable or None,
            player_template=self.player_template or None,
            cache_dir=self.cache_dir or None,
            export_settings=self.export_settings,
            allow_test_payloads=self.allow_test_payloads,
            optimization=self.optimization,
            execution_kind=self.execution_kind,
            memory_budget_bytes=self.gpu_cache_budget_bytes,
            allow_software_adapter=self.allow_software_adapter,
        )

    def reopen(
        self, graph: str | os.PathLike[str] | None = None
    ) -> "StudioApplication":
        """Rebuild the session while preserving launch/export configuration."""
        generation = self.pipeline.device.generation
        return StudioApplication.create(
            graph,
            backend=self.pipeline.backend if self.explicit_backend else None,
            gpu_cache_budget_bytes=self.gpu_cache_budget_bytes,
            player_executable=self.player_executable or None,
            capture_controls=self.capture_controls,
            width=self.width,
            height=self.height,
            export_settings=self.export_settings,
            adapter=generation.adapter,
            driver_version=generation.driver_version,
            os_build=generation.os_build,
            toolchain=self.pipeline.toolchain,
            preview_mode=self.preview_mode,
            dxc_executable=self.dxc_executable or None,
            player_template=self.player_template or None,
            cache_dir=self.cache_dir or None,
            optimization=self.optimization,
            allow_test_payloads=self.allow_test_payloads,
            execution_kind=self.execution_kind,
            allow_software_adapter=self.allow_software_adapter,
        )

    # -- editing ----------------------------------------------------------
    def set_parameter(self, node_id: str, name: str, value: Any) -> EditResult:
        return self.pipeline.apply_edit(
            self.session.set_parameter(node_id, name, value)
        )

    def connect(
        self, target_node: str, target_port: str, source_node: str, source_port: str
    ) -> EditResult:
        return self.pipeline.apply_edit(
            self.session.connect(target_node, target_port, source_node, source_port)
        )

    def disconnect(self, target_node: str, target_port: str) -> EditResult:
        return self.pipeline.apply_edit(
            self.session.disconnect(target_node, target_port)
        )

    def set_curve_points(
        self, node_id: str, points: Sequence[CurvePoint | tuple[int, float]]
    ) -> EditResult:
        return self.pipeline.apply_edit(
            self.session.set_curve_points(node_id, points)
        )

    def add_curve_key(
        self, node_id: str, tick: int, value: float
    ) -> EditResult:
        return self.pipeline.apply_edit(
            self.session.add_curve_key(node_id, tick, value)
        )

    def remove_curve_key(self, node_id: str, tick: int) -> EditResult:
        return self.pipeline.apply_edit(
            self.session.remove_curve_key(node_id, tick)
        )

    def move_curve_key(
        self,
        node_id: str,
        old_tick: int,
        new_tick: int,
        value: float | None = None,
    ) -> EditResult:
        return self.pipeline.apply_edit(
            self.session.move_curve_key(node_id, old_tick, new_tick, value)
        )

    def scrub(self, tick: int) -> EditResult:
        return self.session.set_timeline_tick(tick)

    def audio_command(self, command: str) -> dict[str, Any]:
        backend = self.pipeline.backend
        if not isinstance(backend, WindowsRuntimePreviewBackend):
            raise EditorError("audio playback requires the native Windows preview host")
        return backend.audio_command(command)

    def undo(self) -> EditResult:
        return self.pipeline.apply_edit(self.session.undo())

    def redo(self) -> EditResult:
        return self.pipeline.apply_edit(self.session.redo())

    def select(self, *node_ids: str) -> EditResult:
        return self.session.select(*node_ids)

    def set_layout(self, node_id: str, x: float, y: float) -> EditResult:
        return self.session.set_layout(node_id, x, y)

    # -- preview / async --------------------------------------------------
    def preview(self, tick: int | None = None) -> bool:
        try:
            token = self.pipeline.submit_preview(tick=tick)
        except DeviceLostError:
            return False
        decision = self.pipeline.execute(token)
        record = self.pipeline.last_valid_preview
        return bool(
            decision.accepted
            and record is not None
            and record.token.job_id == token.job_id
        )

    def submit_preview(self, tick: int | None = None) -> GenerationToken:
        return self.pipeline.submit_preview(tick=tick)

    def complete_preview(self, token: GenerationToken | int) -> bool:
        return self.pipeline.execute(token).accepted

    def cancel_preview(self, token: GenerationToken | int) -> bool:
        return self.pipeline.cancel(token)

    def close(self) -> None:
        """Release the authoring runtime session and its exported artifacts."""
        close = getattr(self.pipeline.backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - defensive shutdown boundary
                pass

    def device_lost(self) -> DeviceGeneration:
        return self.pipeline.handle_device_loss()

    def reinitialize_device(self) -> DeviceGeneration:
        return self.pipeline.reinitialize_device()

    def configure_gpu_cache_budget(self, budget_bytes: int) -> None:
        self.pipeline.set_gpu_budget(budget_bytes)
        self.gpu_cache_budget_bytes = budget_bytes
        if hasattr(self.pipeline.backend, "memory_budget_bytes"):
            self.pipeline.backend.memory_budget_bytes = budget_bytes

    def configure_preview_resolution(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise EditorError("preview resolution must be positive")
        self.width = int(width)
        self.height = int(height)
        self.pipeline.width = self.width
        self.pipeline.height = self.height
        self.pipeline.invalidate_pending_jobs()

    @property
    def budget_bytes(self) -> int:
        return self.gpu_cache_budget_bytes

    # -- export -----------------------------------------------------------
    def export(
        self,
        output_directory: str | os.PathLike[str],
        **kwargs: Any,
    ) -> nw05_export.ExportResult:
        supplied = kwargs.pop("settings", None)
        if isinstance(supplied, Mapping):
            merged: Any = dict(self.export_settings)
            merged.update(supplied)
        elif supplied is not None:
            merged = supplied
        else:
            merged = dict(self.export_settings)
        if (
            isinstance(merged, Mapping)
            and not merged.get("player_template")
            and self.player_template
        ):
            merged = dict(merged)
            merged["player_template"] = self.player_template
        elif (
            isinstance(merged, nw05_export.ExportSettings)
            and not merged.player_template
            and self.player_template
        ):
            merged = replace(merged, player_template=self.player_template)
        if "dxc_executable" not in kwargs and self.dxc_executable:
            kwargs["dxc_executable"] = self.dxc_executable
        if "cache_dir" not in kwargs and self.cache_dir:
            kwargs["cache_dir"] = self.cache_dir
        if "optimization" not in kwargs and self.optimization:
            kwargs["optimization"] = self.optimization
        if "allow_test_payloads" not in kwargs:
            kwargs["allow_test_payloads"] = self.allow_test_payloads
        return self.session.export(output_directory, settings=merged, **kwargs)

    def export_report(
        self,
        output_directory: str | os.PathLike[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = self.export(output_directory, **kwargs)
        common = result.artifacts[nwp_package.COMMON_PROFILE_ID]
        return {
            "format": "nw08-studio-export",
            "version": 1,
            "task": "NW-08",
            "semantic_digest": self.session.semantic_digest,
            "semantic_digest_provenance": self.session.semantic_digest_provenance,
            "semantic_digest_canonical": self.session.semantic_digest_canonical,
            "semantic_digest_fallback_reason": (
                self.session.semantic_digest_status.reason
            ),
            "profile_id": self.session.document.profile_id,
            "requirements_digest": self.session.document.requirements_digest,
            "package_bytes": len(common.package_bytes),
            "package_sha256": hashlib.sha256(common.package_bytes).hexdigest(),
            "manifest_sha256": common.manifest["manifest_sha256"],
            "executable_bytes": len(common.executable_bytes or b""),
            "executable_sha256": (
                hashlib.sha256(common.executable_bytes).hexdigest()
                if common.executable_bytes
                else ""
            ),
            "size_attribution": estimate_size_attribution(self.session.document),
            "preview": self.pipeline.report(),
        }

    def configure_export(
        self,
        *,
        dxc_executable: str | os.PathLike[str] | None = None,
        player_template: str | os.PathLike[str] | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        optimization: str | None = None,
        allow_test_payloads: bool | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        if dxc_executable is not None:
            self.dxc_executable = str(dxc_executable)
        if player_template is not None:
            self.player_template = str(player_template)
        if cache_dir is not None:
            self.cache_dir = str(cache_dir)
        if optimization is not None:
            if optimization not in {"O0", "O1", "O2", "O3"}:
                raise EditorError(f"unsupported optimization level '{optimization}'")
            self.optimization = optimization
        if allow_test_payloads is not None:
            self.allow_test_payloads = bool(allow_test_payloads)
        if settings is not None:
            self.export_settings = dict(settings)
        current_toolchain = self.pipeline.toolchain
        self.pipeline.set_toolchain(
            toolchain_identity_for(
                self.dxc_executable or None,
                optimization=self.optimization,
                os_build=self.pipeline.device.generation.os_build,
                compiler_id=current_toolchain.compiler_id,
                compiler_version=current_toolchain.compiler_version,
                target=current_toolchain.target,
            )
        )
        if not self.explicit_backend:
            self.pipeline.replace_backend(self._build_backend())
        elif isinstance(self.pipeline.backend, WindowsRuntimePreviewBackend):
            backend = self.pipeline.backend

            def reconfigure(epoch: int) -> None:
                backend.configure_exporter(
                    mode=self.preview_mode,
                    dxc_executable=self.dxc_executable or None,
                    player_template=self.player_template or None,
                    cache_dir=self.cache_dir or None,
                    export_settings=self.export_settings,
                    allow_test_payloads=self.allow_test_payloads,
                    optimization=self.optimization,
                    minimum_epoch=epoch,
                )
                backend.memory_budget_bytes = self.gpu_cache_budget_bytes

            self.pipeline.reconfigure_backend(reconfigure)

    # -- reporting --------------------------------------------------------
    def report(self) -> dict[str, Any]:
        return {
            "format": "nw08-studio-application",
            "version": 1,
            "task": "NW-08",
            "session": self.session.to_report(),
            "preview": self.pipeline.report(),
            "size_attribution": estimate_size_attribution(self.session.document),
            "gpu_cache_budget_bytes": self.gpu_cache_budget_bytes,
            "export_settings": dict(self.export_settings),
            "export_configuration": {
                "dxc_executable": self.dxc_executable,
                "player_template": self.player_template,
                "cache_dir": self.cache_dir,
                "optimization": self.optimization,
                "allow_test_payloads": self.allow_test_payloads,
                "preview_mode": self.preview_mode,
                "player_executable": self.player_executable,
                "width": self.width,
                "height": self.height,
            },
            "hardware_evidence": bool(
                self.pipeline.last_valid_frame
                and self.pipeline.last_valid_frame.hardware_evidence
            ),
        }


def _emit(payload: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), file=stream)


def _parse_pairs(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise EditorError(f"expected NAME=VALUE, got '{value}'")
        key, item = value.split("=", 1)
        result[key] = item
    return result


def command_report(args: argparse.Namespace) -> int:
    try:
        application = StudioApplication.create(
            args.graph,
            gpu_cache_budget_bytes=args.gpu_cache_budget,
        )
        for change in args.param or ():
            node_id, name, value = change.split(":", 2)
            result = application.set_parameter(node_id, name, value)
            if not result.valid:
                _emit(result.to_dict(), stream=sys.stderr)
                return 1
        if args.tick is not None:
            application.scrub(args.tick)
        if args.preview:
            application.preview(args.tick)
        _emit(application.report())
        return 0
    except (EditorError, OSError) as exc:
        _emit({"ok": False, "error": str(exc)}, stream=sys.stderr)
        return 1


def command_export(args: argparse.Namespace) -> int:
    try:
        graph = args.graph
        paths = list(args.paths)
        output: str | None = args.output_option
        if graph is None:
            if len(paths) >= 2:
                graph = paths.pop(0)
            elif len(paths) == 1 and output is None:
                graph = None
        if output is None:
            output = paths[0] if paths else None
        if output is None:
            raise EditorError("export requires an output directory")
        application = StudioApplication.create(
            graph,
            gpu_cache_budget_bytes=args.gpu_cache_budget,
        )
        for change in args.param or ():
            node_id, name, value = change.split(":", 2)
            result = application.set_parameter(node_id, name, value)
            if not result.valid:
                _emit(result.to_dict(), stream=sys.stderr)
                return 1
        if args.tick is not None:
            application.scrub(args.tick)
        settings = {
            "byte_ceiling": args.byte_ceiling,
            "memory_budget": args.memory_budget,
            "target_profile": args.target_profile,
            "requirements_digest": args.requirements_digest,
            "player_template": args.player_template or "",
            "present_sync_interval": args.present_sync_interval,
            "frames_in_flight": args.frames_in_flight,
            "fusion_enabled": not args.no_fusion,
        }
        if getattr(args, "audio_song", None):
            settings["audio_song_path"] = args.audio_song
        if args.capture_controls:
            settings["frozen_embedded_specialization"] = False
        report = application.export_report(
            output,
            settings=settings,
            report_path=args.report,
            dxc_executable=args.dxc,
            optimization=args.optimization,
            cache_dir=args.cache_dir,
            allow_unpinned_dxc=args.allow_unpinned_dxc,
            allow_test_payloads=args.allow_test_payloads,
        )
        _emit(report)
        return 0
    except (EditorError, nw05_export.ExportError, OSError) as exc:
        _emit(
            {
                "ok": False,
                "error": str(exc),
                "diagnostics": getattr(exc, "diagnostics", []),
            },
            stream=sys.stderr,
        )
        return 1


def command_new_proof(args: argparse.Namespace) -> int:
    try:
        session = GraphEditorSession.create_nw07_proof(args.graph)
        if args.output:
            path = session.save(args.output)
            _emit({"ok": True, "output": str(path), "report": session.to_report()})
        else:
            _emit(session.to_report())
        return 0
    except (EditorError, OSError) as exc:
        _emit({"ok": False, "error": str(exc)}, stream=sys.stderr)
        return 1


def command_gui(args: argparse.Namespace) -> int:
    try:
        from nw08_gui import run_gui
    except Exception as exc:  # pragma: no cover - platform-dependent
        _emit({"ok": False, "error": f"GUI unavailable: {exc}"}, stream=sys.stderr)
        return 1
    return run_gui(
        graph=args.graph,
        player_executable=args.player_executable,
        capture_controls=args.capture_controls,
        gpu_cache_budget=args.gpu_cache_budget,
        preview_mode=args.preview_mode,
        dxc_executable=args.dxc,
        player_template=args.player_template,
        cache_dir=args.cache_dir,
        optimization=args.optimization,
        allow_test_payloads=args.allow_test_payloads,
        audio_song=getattr(args, "audio_song", None),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nodewerk-studio",
        description="NW-08 Nodewerk Studio authoring editor.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gui = subparsers.add_parser("gui", help="launch the interactive editor")
    gui.add_argument("--graph", help="graph to open (defaults to NW-07 proof)")
    gui.add_argument(
        "--player-executable",
        help="native studio preview host (or generated diagnostic player)",
    )
    gui.add_argument(
        "--capture-controls",
        dest="capture_controls",
        action="store_true",
        default=True,
    )
    gui.add_argument(
        "--no-capture-controls",
        dest="capture_controls",
        action="store_false",
    )
    gui.add_argument(
        "--gpu-cache-budget", type=int, default=DEFAULT_GPU_CACHE_BUDGET
    )
    gui.add_argument(
        "--preview-mode",
        choices=["package-host", "embedded-player"],
        default="package-host",
    )
    gui.add_argument("--dxc")
    gui.add_argument("--player-template")
    gui.add_argument("--audio-song", help="optional procedural song JSON for playback/export")
    gui.add_argument("--cache-dir")
    gui.add_argument("--optimization", default="O2", choices=["O0", "O1", "O2", "O3"])
    gui.add_argument("--allow-test-payloads", action="store_true")
    gui.set_defaults(handler=command_gui)

    new_proof = subparsers.add_parser(
        "new-proof", help="create an in-memory NW-07 proof editor session"
    )
    new_proof.add_argument("--graph", help="alternative proof graph")
    new_proof.add_argument("--output", help="write the emitted proof graph")
    new_proof.set_defaults(handler=command_new_proof)

    report = subparsers.add_parser("report", help="emit editor/preview JSON report")
    report.add_argument("--graph", help="graph to open")
    report.add_argument("--param", action="append", metavar="NODE:NAME:VALUE")
    report.add_argument("--tick", type=int)
    report.add_argument("--preview", action="store_true")
    report.add_argument(
        "--gpu-cache-budget", type=int, default=DEFAULT_GPU_CACHE_BUDGET
    )
    report.set_defaults(handler=command_report)

    export = subparsers.add_parser(
        "export", help="export through the existing nodewerkc export path"
    )
    export.add_argument("--graph", help="graph to open")
    export.add_argument("--param", action="append", metavar="NODE:NAME:VALUE")
    export.add_argument("--tick", type=int)
    export.add_argument(
        "paths",
        nargs="*",
        metavar="[GRAPH] OUTPUT",
        help="optional graph path followed by the output directory",
    )
    export.add_argument(
        "--output",
        dest="output_option",
        help="output directory (alternative to the positional form)",
    )
    export.add_argument("--report")
    export.add_argument("--dxc")
    export.add_argument("--cache-dir")
    export.add_argument("--optimization", default="O2", choices=["O0", "O1", "O2", "O3"])
    export.add_argument("--allow-unpinned-dxc", action="store_true")
    export.add_argument("--allow-test-payloads", action="store_true")
    export.add_argument("--player-template")
    export.add_argument("--audio-song", help="optional procedural song JSON for export")
    export.add_argument("--byte-ceiling", type=int, default=nw05_export.DEFAULT_BYTE_CEILING)
    export.add_argument("--memory-budget", type=int, default=nw05_export.DEFAULT_MEMORY_BUDGET)
    export.add_argument("--target-profile", default=nwp_package.COMMON_PROFILE_ID)
    export.add_argument(
        "--requirements-digest",
        default=nwp_package.COMMON_REQUIREMENTS_DIGEST,
    )
    export.add_argument("--present-sync-interval", type=int, default=1)
    export.add_argument("--frames-in-flight", type=int, default=2)
    export.add_argument("--no-fusion", action="store_true")
    export.add_argument("--capture-controls", action="store_true")
    export.add_argument(
        "--gpu-cache-budget", type=int, default=DEFAULT_GPU_CACHE_BUDGET
    )
    export.set_defaults(handler=command_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


StudioSession = GraphEditorSession
StudioEditor = StudioApplication


def create_application(
    graph: str | os.PathLike[str] | None = None, **kwargs: Any
) -> StudioApplication:
    return StudioApplication.create(graph, **kwargs)


__all__ = [
    "DEFAULT_GPU_CACHE_BUDGET",
    "StudioApplication",
    "StudioEditor",
    "StudioSession",
    "build_parser",
    "create_application",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
