#!/usr/bin/env python3
"""Minimal Tk authoring GUI for ``nodewerk-studio`` (NW-08).

This is deliberately a small editor shell: graph node/connection editing,
parameter and curve inspection, timeline scrubbing, preview, diagnostics,
pass timings, size attribution, undo, selection, layout, and the GPU-cache
budget.  It is authoring-only and never part of the release player closure.

Importing this module does not require a display; ``run_gui`` creates the Tk
root.  Headless tests use :mod:`nw08_studio` instead.
"""

from __future__ import annotations

import json
import math
import os
import queue as queue_module
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from nw08_cache import (  # noqa: E402
    CompletionDecision,
    DeviceLostError,
    GenerationToken,
)
from nw08_editor import CurvePoint, EditorError  # noqa: E402
from nw08_preview import PreviewFrame, PreviewRecord  # noqa: E402
from nw08_studio import DEFAULT_GPU_CACHE_BUDGET, StudioApplication  # noqa: E402


def frame_to_ppm(
    frame: PreviewFrame, *, max_width: int = 480, max_height: int = 270
) -> bytes | None:
    """Convert a captured RGBA32F frame into a displayable PPM P6 image.

    Returns ``None`` when the frame carries no captured pixels (the
    deterministic contract/reference backend produces no real pixels).  The
    conversion is nearest-neighbour and only for display; it never feeds back
    into semantic identity or export.
    """
    data = frame.image_data
    if not data or frame.width <= 0 or frame.height <= 0:
        return None
    expected = frame.width * frame.height * 16
    if len(data) != expected:
        return None
    step = max(
        1,
        int(
            math.ceil(
                max(
                    frame.width / max(1, max_width),
                    frame.height / max(1, max_height),
                )
            )
        ),
    )
    out_width = max(1, frame.width // step)
    out_height = max(1, frame.height // step)
    row = bytearray()
    rows: list[bytes] = []
    for y in range(0, out_height * step, step):
        row.clear()
        base = y * frame.width * 16
        for x in range(0, out_width * step, step):
            offset = base + x * 16
            values = []
            for channel in range(3):
                (value,) = struct.unpack_from("<f", data, offset + channel * 4)
                if not math.isfinite(value):
                    value = 0.0
                values.append(max(0, min(255, int(round(value * 255.0)))))
            row.extend(values)
        rows.append(bytes(row))
    header = f"P6\n{out_width} {out_height}\n255\n".encode("ascii")
    return header + b"".join(rows)


@dataclass(frozen=True)
class PreviewOutcome:
    token: GenerationToken
    decision: CompletionDecision | None
    accepted: bool
    record: PreviewRecord | None
    error: str = ""

    @property
    def frame(self) -> PreviewFrame | None:
        return self.record.frame if self.record else None


class PreviewDispatcher:
    """Run preview compilation/rendering off the Tk event thread.

    Only the latest requested generation may be installed.  A stale, failed,
    cancelled, or device-epoch-obsolete completion is reported with
    ``accepted=False`` and never replaces the last valid preview.  The GUI
    drains outcomes from the Tk thread (``root.after``) and performs all widget
    updates there.
    """

    def __init__(self, application: StudioApplication):
        self.application = application
        self._lock = threading.Lock()
        self._results: queue_module.Queue[PreviewOutcome] = queue_module.Queue()
        self._latest: GenerationToken | None = None
        self._thread: threading.Thread | None = None
        self._serial = 0

    @property
    def busy(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def latest_token(self) -> GenerationToken | None:
        return self._latest

    def request(self, tick: int | None = None) -> GenerationToken | None:
        with self._lock:
            if self._latest is not None:
                self.application.cancel_preview(self._latest)
            try:
                token = self.application.submit_preview(tick)
            except DeviceLostError:
                self._latest = None
                return None
            self._latest = token
            self._serial += 1
            serial = self._serial
            self._thread = threading.Thread(
                target=self._work,
                args=(token, serial),
                name=f"nw08-preview-{token.job_id}",
                daemon=True,
            )
            self._thread.start()
            return token

    def cancel(self) -> bool:
        with self._lock:
            token = self._latest
            self._latest = None
            self._serial += 1
            if token is None:
                return False
            return self.application.cancel_preview(token)

    def _work(self, token: GenerationToken, serial: int) -> None:
        try:
            decision = self.application.pipeline.execute(token)
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            self._results.put(
                PreviewOutcome(
                    token=token,
                    decision=None,
                    accepted=False,
                    record=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        record = self.application.pipeline.last_valid_preview
        accepted = bool(
            decision.accepted
            and record is not None
            and record.token.job_id == token.job_id
        )
        # A runtime session may create a new device while servicing this job;
        # the installed record then carries the rebound epoch.  Validate and
        # report that actual token rather than the pre-execution one.
        effective = (
            record.token
            if record is not None and record.token.job_id == token.job_id
            else token
        )
        self._results.put(
            PreviewOutcome(
                token=effective,
                decision=decision,
                accepted=accepted,
                record=record,
            )
        )

    def drain(self) -> list[PreviewOutcome]:
        outcomes: list[PreviewOutcome] = []
        while True:
            try:
                outcomes.append(self._results.get_nowait())
            except queue_module.Empty:
                break
        return outcomes

    def install_latest(self, outcomes: list[PreviewOutcome]) -> PreviewOutcome | None:
        """Select the newest outcome that is still valid for this generation."""
        latest = self._latest
        pipeline = self.application.pipeline
        selected: PreviewOutcome | None = None
        for outcome in outcomes:
            if latest is None or outcome.token.job_id != latest.job_id:
                continue
            if outcome.token.snapshot_generation != pipeline.queue.snapshot_generation:
                continue
            if outcome.token.device_epoch != pipeline.device.epoch:
                continue
            selected = outcome
        return selected


class GuiActions:
    """Headless action layer shared by the Tk view and integration checks.

    Every interactive menu/button operation is implemented here and the Tk
    widgets are thin delegations.  Windows integration therefore exercises the
    same export/preview/edit code paths as the GUI instead of a parallel
    controller.
    """

    def __init__(self, application: StudioApplication):
        self.application = application
        self.dispatcher = PreviewDispatcher(application)

    def export_to(
        self,
        directory: str | os.PathLike[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "dxc_executable": self.application.dxc_executable or None,
            "cache_dir": self.application.cache_dir or None,
            "optimization": self.application.optimization,
        }
        options.update(kwargs)
        return self.application.export_report(directory, **options)

    def set_parameter(self, node_id: str, name: str, value: Any):
        return self.application.set_parameter(node_id, name, value)

    def connect(
        self, target_node: str, target_port: str, source_node: str, source_port: str
    ):
        return self.application.connect(
            target_node, target_port, source_node, source_port
        )

    def disconnect(self, target_node: str, target_port: str):
        return self.application.disconnect(target_node, target_port)

    def add_curve_key(self, node_id: str, tick: int, value: float):
        return self.application.add_curve_key(node_id, tick, value)

    def scrub(self, tick: int):
        return self.application.scrub(tick)

    def reinitialize_device(self):
        return self.application.reinitialize_device()

    def close(self) -> None:
        self.dispatcher.cancel()


class CurveEditorBinding:
    """Headless curve-keyframe controller used by the GUI.

    Keeping this binding outside the Tk widgets makes the exact GUI editing
    path testable without a display: every operation delegates to
    :class:`StudioApplication`, which in turn uses the canonical
    ``GraphEditorSession.set_curve_points`` / ``keys`` parameter path.
    """

    def __init__(self, application: StudioApplication):
        self.application = application

    def supports(self, node_id: str | None) -> bool:
        if not node_id or node_id not in self.application.session.node_ids:
            return False
        return "keys" in self.application.session.parameter_names(node_id)

    def points(self, node_id: str) -> tuple[CurvePoint, ...]:
        return self.application.session.curve_points(node_id)

    def add(self, node_id: str, tick: int, value: float):
        return self.application.add_curve_key(node_id, int(tick), float(value))

    def update(
        self,
        node_id: str,
        old_tick: int,
        new_tick: int,
        value: float | None = None,
    ):
        return self.application.move_curve_key(
            node_id, int(old_tick), int(new_tick), value
        )

    def remove(self, node_id: str, tick: int):
        return self.application.remove_curve_key(node_id, int(tick))


class StudioWindow:
    """Interactive editor window over :class:`StudioApplication`."""

    def __init__(self, root: Any, application: StudioApplication):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.application = application
        self._dragged: str | None = None
        self.curve_editor = CurveEditorBinding(application)
        self.dispatcher = PreviewDispatcher(application)
        self.actions = GuiActions(application)
        self._preview_photo: Any = None
        self._build()
        self.refresh()
        self._schedule_preview_poll()

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        self.root.title("Nodewerk Studio — NW-08 Authoring")
        self.root.geometry("1280x820")
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="New NW-07 Proof", command=self.new_proof)
        file_menu.add_command(label="Open…", command=self.open_graph)
        file_menu.add_command(label="Save As…", command=self.save_graph)
        file_menu.add_command(label="Export Settings…", command=self.edit_export_settings)
        file_menu.add_command(label="Export…", command=self.export)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)
        edit_menu = tk.Menu(menubar, tearoff=False)
        edit_menu.add_command(label="Undo", command=self.undo)
        edit_menu.add_command(label="Redo", command=self.redo)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        self.root.config(menu=menubar)

        outer = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True)
        graph_frame = ttk.Frame(outer)
        inspector = ttk.Frame(outer)
        outer.add(graph_frame, weight=3)
        outer.add(inspector, weight=2)

        self.canvas = tk.Canvas(graph_frame, background="#111827", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self._canvas_press)
        self.canvas.bind("<B1-Motion>", self._canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_release)
        self.canvas.bind("<Configure>", lambda _event: self.refresh_graph())

        timeline_frame = ttk.Frame(graph_frame)
        timeline_frame.pack(fill=tk.X)
        ttk.Label(timeline_frame, text="Timeline").pack(side=tk.LEFT)
        duration = self.application.session.timeline_duration() or 960_000
        self.timeline = tk.Scale(
            timeline_frame,
            from_=0,
            to=duration,
            orient=tk.HORIZONTAL,
            resolution=800,
            command=self._on_timeline,
            length=700,
        )
        self.timeline.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.tick_label = ttk.Label(timeline_frame, text="tick 0")
        self.tick_label.pack(side=tk.LEFT)

        notebook = ttk.Notebook(inspector)
        notebook.pack(fill=tk.BOTH, expand=True)
        self.inspector_tab = ttk.Frame(notebook)
        self.preview_tab = ttk.Frame(notebook)
        self.diagnostics_tab = ttk.Frame(notebook)
        self.timings_tab = ttk.Frame(notebook)
        self.size_tab = ttk.Frame(notebook)
        self.state_tab = ttk.Frame(notebook)
        notebook.add(self.inspector_tab, text="Inspector")
        notebook.add(self.preview_tab, text="Preview")
        notebook.add(self.diagnostics_tab, text="Diagnostics")
        notebook.add(self.timings_tab, text="Timings")
        notebook.add(self.size_tab, text="Size")
        notebook.add(self.state_tab, text="State")

        self.preview_label = ttk.Label(
            self.preview_tab,
            text="no preview yet",
            anchor="center",
            justify="center",
        )
        self.preview_label.pack(fill=tk.BOTH, expand=True)

        ttk.Label(self.inspector_tab, text="Nodes (select to inspect)").pack(anchor="w")
        self.node_list = tk.Listbox(self.inspector_tab, height=10, exportselection=False)
        self.node_list.pack(fill=tk.X)
        self.node_list.bind("<<ListboxSelect>>", self._on_node_select)
        ttk.Label(self.inspector_tab, text="Parameters (double-click to edit)").pack(
            anchor="w"
        )
        self.parameter_list = tk.Listbox(self.inspector_tab, height=10)
        self.parameter_list.pack(fill=tk.BOTH, expand=True)
        self.parameter_list.bind("<Double-Button-1>", self._edit_parameter)

        curve_frame = ttk.LabelFrame(
            self.inspector_tab, text="Curve keyframes (keys parameter)"
        )
        curve_frame.pack(fill=tk.X, pady=4)
        self.curve_list = tk.Listbox(curve_frame, height=5, exportselection=False)
        self.curve_list.pack(fill=tk.X)
        self.curve_list.bind("<<ListboxSelect>>", self._on_curve_select)
        curve_form = ttk.Frame(curve_frame)
        curve_form.pack(fill=tk.X)
        self.curve_tick_entry = ttk.Entry(curve_form, width=12)
        self.curve_value_entry = ttk.Entry(curve_form, width=12)
        ttk.Label(curve_form, text="tick").pack(side=tk.LEFT)
        self.curve_tick_entry.pack(side=tk.LEFT)
        ttk.Label(curve_form, text="value").pack(side=tk.LEFT)
        self.curve_value_entry.pack(side=tk.LEFT)
        ttk.Button(curve_form, text="Add", command=self.add_curve_key).pack(
            side=tk.LEFT
        )
        ttk.Button(curve_form, text="Update", command=self.update_curve_key).pack(
            side=tk.LEFT
        )
        ttk.Button(curve_form, text="Delete", command=self.delete_curve_key).pack(
            side=tk.LEFT
        )

        connection = ttk.LabelFrame(self.inspector_tab, text="Connection")
        connection.pack(fill=tk.X, pady=4)
        self.source_entry = ttk.Entry(connection)
        self.target_entry = ttk.Entry(connection)
        self.source_port_entry = ttk.Entry(connection, width=10)
        self.target_port_entry = ttk.Entry(connection, width=10)
        ttk.Label(connection, text="source").grid(row=0, column=0, sticky="w")
        self.source_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(connection, text="port").grid(row=0, column=2)
        self.source_port_entry.grid(row=0, column=3)
        ttk.Label(connection, text="target").grid(row=1, column=0, sticky="w")
        self.target_entry.grid(row=1, column=1, sticky="ew")
        ttk.Label(connection, text="port").grid(row=1, column=2)
        self.target_port_entry.grid(row=1, column=3)
        ttk.Button(connection, text="Connect", command=self.connect).grid(
            row=2, column=1, sticky="ew"
        )
        ttk.Button(connection, text="Disconnect", command=self.disconnect).grid(
            row=2, column=3, sticky="ew"
        )

        self.diagnostics_list = tk.Listbox(self.diagnostics_tab)
        self.diagnostics_list.pack(fill=tk.BOTH, expand=True)
        self.diagnostics_list.bind("<<ListboxSelect>>", self._on_diagnostic_select)

        self.timings_list = tk.Listbox(self.timings_tab)
        self.timings_list.pack(fill=tk.BOTH, expand=True)
        self.size_list = tk.Listbox(self.size_tab)
        self.size_list.pack(fill=tk.BOTH, expand=True)

        budget_frame = ttk.LabelFrame(self.state_tab, text="GPU cache budget (bytes)")
        budget_frame.pack(fill=tk.X, pady=4)
        self.budget_entry = ttk.Entry(budget_frame)
        self.budget_entry.insert(
            0, str(self.application.gpu_cache_budget_bytes)
        )
        self.budget_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(budget_frame, text="Apply", command=self.apply_budget).pack(
            side=tk.LEFT
        )
        self.state_text = tk.Text(self.state_tab, height=18)
        self.state_text.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(self.root)
        controls.pack(fill=tk.X)
        ttk.Button(controls, text="Preview", command=self.preview).pack(side=tk.LEFT)
        ttk.Button(controls, text="Cancel Preview", command=self.cancel_preview).pack(
            side=tk.LEFT
        )
        ttk.Button(
            controls, text="Reinitialize Device", command=self.reinitialize_device
        ).pack(side=tk.LEFT)
        ttk.Button(controls, text="Undo", command=self.undo).pack(side=tk.LEFT)
        ttk.Button(controls, text="Redo", command=self.redo).pack(side=tk.LEFT)
        ttk.Button(controls, text="Export…", command=self.export).pack(side=tk.LEFT)
        self.status = ttk.Label(controls, text="ready")
        self.status.pack(side=tk.LEFT, padx=8)

    # -- refresh ----------------------------------------------------------
    def _selected_node(self) -> str | None:
        selection = self.node_list.curselection()
        if not selection:
            return None
        return self.node_list.get(selection[0])

    def _sync_node_list(self) -> None:
        current = self._selected_node()
        self.node_list.delete(0, self.tk.END)
        for node_id in self.application.session.node_ids:
            self.node_list.insert(self.tk.END, node_id)
        if current:
            try:
                index = list(self.application.session.node_ids).index(current)
                self.node_list.selection_set(index)
            except ValueError:
                pass

    def select_node_if_present(self, node_id: str) -> bool:
        """Select ``node_id`` in the inspector list (used by integration)."""
        try:
            index = list(self.application.session.node_ids).index(node_id)
        except ValueError:
            return False
        self.node_list.selection_clear(0, self.tk.END)
        self.node_list.selection_set(index)
        self.node_list.activate(index)
        self._on_node_select()
        return True

    def refresh(self) -> None:
        self._sync_node_list()
        self.refresh_parameters()
        self.refresh_curve()
        self.refresh_graph()
        self.refresh_diagnostics()
        self.refresh_timings()
        self.refresh_size()
        self.refresh_preview_image()
        self.refresh_state()
        session = self.application.session
        self.status.configure(
            text=(
                f"snapshot {session.snapshot_generation} · "
                f"{session.semantic_digest[:22]}… · "
                f"undo={session.can_undo} redo={session.can_redo}"
            )
        )
        self.tick_label.configure(text=f"tick {session.timeline_tick}")

    def refresh_parameters(self) -> None:
        self.parameter_list.delete(0, self.tk.END)
        node_id = self._selected_node()
        if not node_id:
            return
        for name in self.application.session.parameter_names(node_id):
            value = " ".join(self.application.session.parameter(node_id, name))
            self.parameter_list.insert(self.tk.END, f"{name} = {value}")

    def refresh_curve(self) -> None:
        self.curve_list.delete(0, self.tk.END)
        node_id = self._selected_node()
        if not self.curve_editor.supports(node_id):
            return
        for point in self.curve_editor.points(node_id):
            self.curve_list.insert(self.tk.END, f"{point.tick}: {point.value:g}")

    def _on_curve_select(self, _event: Any = None) -> None:
        selection = self.curve_list.curselection()
        node_id = self._selected_node()
        if not selection or not self.curve_editor.supports(node_id):
            return
        point = self.curve_editor.points(node_id)[selection[0]]
        self.curve_tick_entry.delete(0, self.tk.END)
        self.curve_tick_entry.insert(0, str(point.tick))
        self.curve_value_entry.delete(0, self.tk.END)
        self.curve_value_entry.insert(0, format(point.value, ".17g"))

    def _curve_selection_point(self) -> CurvePoint | None:
        selection = self.curve_list.curselection()
        node_id = self._selected_node()
        if not selection or not self.curve_editor.supports(node_id):
            return None
        points = self.curve_editor.points(node_id)
        if selection[0] >= len(points):
            return None
        return points[selection[0]]

    def add_curve_key(self) -> None:
        node_id = self._selected_node()
        if not self.curve_editor.supports(node_id):
            self.status.configure(text="select a node with a keys curve")
            return
        try:
            result = self.curve_editor.add(
                node_id,
                int(self.curve_tick_entry.get()),
                float(self.curve_value_entry.get()),
            )
        except (EditorError, ValueError) as exc:
            self.status.configure(text=str(exc))
            return
        self._after_edit(result)

    def update_curve_key(self) -> None:
        node_id = self._selected_node()
        point = self._curve_selection_point()
        if not node_id or point is None:
            self.status.configure(text="select a curve key to update")
            return
        try:
            result = self.curve_editor.update(
                node_id,
                point.tick,
                int(self.curve_tick_entry.get()),
                float(self.curve_value_entry.get()),
            )
        except (EditorError, ValueError) as exc:
            self.status.configure(text=str(exc))
            return
        self._after_edit(result)

    def delete_curve_key(self) -> None:
        node_id = self._selected_node()
        point = self._curve_selection_point()
        if not node_id or point is None:
            self.status.configure(text="select a curve key to delete")
            return
        try:
            result = self.curve_editor.remove(node_id, point.tick)
        except EditorError as exc:
            self.status.configure(text=str(exc))
            return
        self._after_edit(result)

    def refresh_graph(self) -> None:
        self.canvas.delete("all")
        session = self.application.session
        layout = session.layout()
        positions: dict[str, tuple[float, float]] = {}
        for index, node_id in enumerate(session.node_ids):
            x, y = layout.get(node_id, (40.0 + (index % 4) * 220.0, 40.0 + (index // 4) * 120.0))
            positions[node_id] = (x, y)
        for connection in session.connections():
            source = positions.get(connection["source_node"])
            target = positions.get(connection["target_node"])
            if not source or not target:
                continue
            self.canvas.create_line(
                source[0] + 170,
                source[1] + 26,
                target[0],
                target[1] + 26,
                fill="#64748b",
                arrow=self.tk.LAST,
            )
        selection = set(session.selection)
        for node_id, (x, y) in positions.items():
            node = session.node(node_id)
            fill = "#1d4ed8" if node_id in selection else "#1f2937"
            self.canvas.create_rectangle(
                x,
                y,
                x + 170,
                y + 52,
                fill=fill,
                outline="#93c5fd",
                tags=(f"node:{node_id}",),
            )
            self.canvas.create_text(
                x + 8,
                y + 8,
                anchor="nw",
                fill="#e5e7eb",
                text=node_id,
                tags=(f"node:{node_id}",),
            )
            self.canvas.create_text(
                x + 8,
                y + 28,
                anchor="nw",
                fill="#94a3b8",
                text=node.operator_id,
                tags=(f"node:{node_id}",),
            )

    def refresh_diagnostics(self) -> None:
        self.diagnostics_list.delete(0, self.tk.END)
        for diagnostic in self.application.pipeline.last_diagnostics:
            source = diagnostic.get("source") or {}
            location = source.get("file", "")
            if source.get("line"):
                location += f":{source['line']}"
            self.diagnostics_list.insert(
                self.tk.END,
                f"{diagnostic.get('code', 'diagnostic')}: {diagnostic.get('message', '')} [{location}]",
            )

    def refresh_timings(self) -> None:
        self.timings_list.delete(0, self.tk.END)
        record = self.application.pipeline.last_valid_preview
        if record is None:
            self.timings_list.insert(self.tk.END, "no preview yet")
            return
        for timing in record.frame.pass_timings.timings:
            if not timing.available:
                self.timings_list.insert(
                    self.tk.END,
                    f"{timing.name}: unavailable (not measured)",
                )
                continue
            suffix = "" if timing.counts_toward_total else " [nested]"
            self.timings_list.insert(
                self.tk.END,
                f"{timing.name}: {timing.milliseconds:.3f} ms "
                f"({timing.kind}){suffix}",
            )
        self.timings_list.insert(
            self.tk.END,
            f"total: {record.frame.pass_timings.total_milliseconds:.3f} ms",
        )

    def refresh_preview_image(self) -> None:
        frame = self.application.pipeline.last_valid_frame
        if frame is None:
            self.preview_label.configure(
                text="no preview yet", image=""
            )
            self._preview_photo = None
            self._preview_image_sha = ""
            return
        if (
            self._preview_photo is not None
            and getattr(self, "_preview_image_sha", "") == frame.image_sha256
        ):
            return
        ppm = frame_to_ppm(frame)
        if ppm is None:
            self.preview_label.configure(
                image="",
                text=(
                    "no captured pixels\n"
                    f"backend: {frame.backend}\n"
                    f"execution: {frame.execution_kind}"
                ),
            )
            self._preview_photo = None
            self._preview_image_sha = ""
            return
        try:
            photo = self.tk.PhotoImage(data=ppm, format="PPM")
        except Exception:  # pragma: no cover - Tk build without PPM support
            self.preview_label.configure(
                image="", text="captured frame could not be decoded for display"
            )
            self._preview_photo = None
            self._preview_image_sha = ""
            return
        self._preview_photo = photo
        self._preview_image_sha = frame.image_sha256
        self.preview_label.configure(image=photo, text="")

    def refresh_size(self) -> None:
        self.size_list.delete(0, self.tk.END)
        attribution = (
            self.application.pipeline.last_valid_frame.size_attribution
            if self.application.pipeline.last_valid_frame
            else None
        )
        if not attribution:
            self.size_list.insert(
                self.tk.END, "CPU size attribution (no preview frame yet)"
            )
            attribution = self.application.report()["size_attribution"]
        if not attribution.get("available", False):
            self.size_list.insert(self.tk.END, str(attribution.get("reason", "unavailable")))
            return
        for name, value in attribution.get("pass_attribution", {}).items():
            self.size_list.insert(self.tk.END, f"{name}: {value} bytes")
        self.size_list.insert(
            self.tk.END,
            f"resident payload: {attribution.get('resident_payload_bytes', 0)} bytes",
        )

    def refresh_state(self) -> None:
        self.state_text.delete("1.0", self.tk.END)
        self.state_text.insert(
            "1.0", json.dumps(self.application.report(), indent=2, sort_keys=True)
        )

    # -- actions ----------------------------------------------------------
    def _on_node_select(self, _event: Any = None) -> None:
        node_id = self._selected_node()
        if node_id:
            self.application.select(node_id)
            self.refresh_parameters()
            self.refresh_curve()
            self.refresh_graph()

    def _edit_parameter(self, _event: Any = None) -> None:
        selection = self.parameter_list.curselection()
        node_id = self._selected_node()
        if not selection or not node_id:
            return
        text = self.parameter_list.get(selection[0])
        name = text.split("=", 1)[0].strip()
        current = " ".join(self.application.session.parameter(node_id, name))
        from tkinter import simpledialog

        value = simpledialog.askstring(
            "Edit parameter", f"{node_id}.{name}", initialvalue=current, parent=self.root
        )
        if value is None:
            return
        result = self.application.set_parameter(node_id, name, value)
        self._after_edit(result)

    def _after_edit(self, result: Any) -> None:
        if not result.valid:
            self.status.configure(text="edit rejected; last valid snapshot retained")
        self.refresh()

    def connect(self) -> None:
        try:
            result = self.application.connect(
                self.target_entry.get().strip(),
                self.target_port_entry.get().strip(),
                self.source_entry.get().strip(),
                self.source_port_entry.get().strip(),
            )
        except EditorError as exc:
            self.status.configure(text=str(exc))
            return
        self._after_edit(result)

    def disconnect(self) -> None:
        try:
            result = self.application.disconnect(
                self.target_entry.get().strip(),
                self.target_port_entry.get().strip(),
            )
        except EditorError as exc:
            self.status.configure(text=str(exc))
            return
        self._after_edit(result)

    def undo(self) -> None:
        try:
            result = self.application.undo()
        except EditorError as exc:
            self.status.configure(text=str(exc))
            return
        self._after_edit(result)

    def redo(self) -> None:
        try:
            result = self.application.redo()
        except EditorError as exc:
            self.status.configure(text=str(exc))
            return
        self._after_edit(result)

    def _on_timeline(self, value: str) -> None:
        try:
            self.application.scrub(int(float(value)))
        except (EditorError, ValueError):
            return
        self.tick_label.configure(text=f"tick {self.application.session.timeline_tick}")

    def preview(self) -> None:
        token = self.dispatcher.request(self.application.session.timeline_tick)
        if token is None:
            self.status.configure(
                text="preview unavailable: device lost; reinitialize the device"
            )
            return
        self.status.configure(
            text=f"preview job {token.job_id} compiling/rendering…"
        )

    def cancel_preview(self) -> None:
        cancelled = self.dispatcher.cancel()
        self.status.configure(
            text="preview cancelled" if cancelled else "no preview to cancel"
        )

    def reinitialize_device(self) -> None:
        try:
            generation = self.application.reinitialize_device()
        except Exception as exc:
            self.status.configure(text=f"device reinitialization failed: {exc}")
            return
        self.status.configure(
            text=f"device reinitialized; epoch {generation.epoch}"
        )
        self.refresh()

    def _schedule_preview_poll(self) -> None:
        try:
            self.root.after(75, self._poll_preview)
        except Exception:  # pragma: no cover - root already destroyed
            return

    def _poll_preview(self) -> None:
        outcomes = self.dispatcher.drain()
        if outcomes:
            outcome = self.dispatcher.install_latest(outcomes)
            if outcome is None:
                # Completion from a cancelled/obsolete generation: never
                # replace the last valid preview.
                self.status.configure(text="discarded stale preview completion")
            elif outcome.error:
                self.status.configure(
                    text=f"preview failed: {outcome.error}; last valid frame retained"
                )
                self.refresh()
            elif outcome.accepted:
                self.status.configure(
                    text=(
                        f"preview updated (job {outcome.token.job_id}, "
                        f"epoch {outcome.token.device_epoch})"
                    )
                )
                self.refresh()
            else:
                reason = (
                    outcome.decision.reason
                    if outcome.decision is not None
                    else "rejected"
                )
                self.status.configure(
                    text=f"preview rejected ({reason}); last valid frame retained"
                )
                self.refresh_diagnostics()
                self.refresh_timings()
                self.refresh_size()
                self.refresh_state()
        self._schedule_preview_poll()

    def apply_budget(self) -> None:
        try:
            self.application.configure_gpu_cache_budget(int(self.budget_entry.get()))
        except (ValueError, RuntimeError) as exc:
            self.status.configure(text=f"budget rejected: {exc}")
            return
        self.refresh_state()
        self.status.configure(text="GPU cache budget updated")

    def new_proof(self) -> None:
        self.dispatcher.cancel()
        self.actions.close()
        if not self.application.explicit_backend:
            self.application.close()
        self.application = self.application.reopen()
        self.curve_editor = CurveEditorBinding(self.application)
        self.dispatcher = PreviewDispatcher(self.application)
        self.actions = GuiActions(self.application)
        self.refresh()

    def open_graph(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="Open .nwgraph",
            defaultextension=".nwgraph",
            filetypes=[("Nodewerk graph", "*.nwgraph"), ("All files", "*")],
        )
        if not path:
            return
        try:
            self.dispatcher.cancel()
            self.actions.close()
            if not self.application.explicit_backend:
                self.application.close()
            self.application = self.application.reopen(path)
            self.curve_editor = CurveEditorBinding(self.application)
            self.dispatcher = PreviewDispatcher(self.application)
            self.actions = GuiActions(self.application)
        except (EditorError, OSError) as exc:
            self.status.configure(text=f"open failed: {exc}")
            return
        self.refresh()

    def save_graph(self) -> None:
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(
            title="Save .nwgraph",
            defaultextension=".nwgraph",
            filetypes=[("Nodewerk graph", "*.nwgraph")],
        )
        if not path:
            return
        try:
            self.application.session.save(path)
        except (EditorError, OSError) as exc:
            self.status.configure(text=f"save failed: {exc}")
            return
        self.status.configure(text=f"saved {path}")

    def edit_export_settings(self) -> None:
        from tkinter import simpledialog

        values: dict[str, str] = {}
        prompts = (
            ("dxc", "DXC executable", self.application.dxc_executable),
            ("player_template", "Player template", self.application.player_template),
            ("cache_dir", "Compiler cache directory", self.application.cache_dir),
            ("optimization", "Optimization (O0..O3)", self.application.optimization),
        )
        for key, label, current in prompts:
            value = simpledialog.askstring(
                "Export settings", label, initialvalue=current, parent=self.root
            )
            if value is None:
                return
            values[key] = value
        try:
            self.application.configure_export(
                dxc_executable=values["dxc"],
                player_template=values["player_template"],
                cache_dir=values["cache_dir"],
                optimization=values["optimization"],
            )
        except EditorError as exc:
            self.status.configure(text=str(exc))
            return
        self.status.configure(text="export settings updated")
        self.refresh_state()

    def export(self) -> None:
        from tkinter import filedialog

        directory = filedialog.askdirectory(title="Export directory")
        if not directory:
            return
        try:
            report = self.actions.export_to(directory)
        except Exception as exc:  # pragma: no cover - export backend boundary
            self.status.configure(text=f"export failed: {exc}")
            return
        self.status.configure(
            text=(
                f"exported {report['package_bytes']} package bytes, "
                f"{report['executable_bytes']} executable bytes"
            )
        )
        self.refresh_state()

    # -- layout dragging -------------------------------------------------
    def _node_at(self, x: float, y: float) -> str | None:
        session = self.application.session
        layout = session.layout()
        for index, node_id in enumerate(session.node_ids):
            nx, ny = layout.get(
                node_id,
                (40.0 + (index % 4) * 220.0, 40.0 + (index // 4) * 120.0),
            )
            if nx <= x <= nx + 170 and ny <= y <= ny + 52:
                return node_id
        return None

    def _canvas_press(self, event: Any) -> None:
        node_id = self._node_at(event.x, event.y)
        if node_id:
            self._dragged = node_id
            self.application.select(node_id)
            self.refresh_graph()

    def _canvas_drag(self, event: Any) -> None:
        if not self._dragged:
            return
        self.application.set_layout(self._dragged, event.x, event.y)
        self.refresh_graph()

    def _canvas_release(self, _event: Any) -> None:
        self._dragged = None
        self.refresh()

    def _on_diagnostic_select(self, _event: Any = None) -> None:
        selection = self.diagnostics_list.curselection()
        if not selection:
            return
        index = selection[0]
        diagnostics = self.application.pipeline.last_diagnostics
        if index >= len(diagnostics):
            return
        source = diagnostics[index].get("source") or {}
        node_id = source.get("node_id")
        if node_id and node_id in self.application.session.node_ids:
            self.application.select(node_id)
            self.refresh()


def run_gui(
    *,
    graph: str | os.PathLike[str] | None = None,
    player_executable: str | os.PathLike[str] | None = None,
    capture_controls: bool = True,
    gpu_cache_budget: int = DEFAULT_GPU_CACHE_BUDGET,
    preview_mode: str = "package-host",
    dxc_executable: str | os.PathLike[str] | None = None,
    player_template: str | os.PathLike[str] | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    optimization: str = "O2",
    allow_test_payloads: bool = False,
) -> int:
    try:
        import tkinter as tk
    except Exception as exc:  # pragma: no cover - platform-dependent
        print(f"nodewerk-studio GUI unavailable: {exc}", file=sys.stderr)
        return 1
    try:
        application = StudioApplication.create(
            graph,
            player_executable=player_executable,
            capture_controls=capture_controls,
            gpu_cache_budget_bytes=gpu_cache_budget,
            preview_mode=preview_mode,
            dxc_executable=dxc_executable,
            player_template=player_template,
            cache_dir=cache_dir,
            optimization=optimization,
            allow_test_payloads=allow_test_payloads,
        )
    except (EditorError, RuntimeError) as exc:
        print(f"cannot create studio application: {exc}", file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - headless display
        print(f"cannot create GUI root: {exc}", file=sys.stderr)
        return 1
    StudioWindow(root, application)
    root.mainloop()
    return 0


__all__ = [
    "CurveEditorBinding",
    "GuiActions",
    "PreviewDispatcher",
    "PreviewOutcome",
    "StudioWindow",
    "frame_to_ppm",
    "run_gui",
]
