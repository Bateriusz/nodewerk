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
import os
import sys
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from nw08_editor import CurvePoint, EditorError, GraphEditorSession  # noqa: E402
from nw08_studio import DEFAULT_GPU_CACHE_BUDGET, StudioApplication  # noqa: E402


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
        self._build()
        self.refresh()

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
        self.diagnostics_tab = ttk.Frame(notebook)
        self.timings_tab = ttk.Frame(notebook)
        self.size_tab = ttk.Frame(notebook)
        self.state_tab = ttk.Frame(notebook)
        notebook.add(self.inspector_tab, text="Inspector")
        notebook.add(self.diagnostics_tab, text="Diagnostics")
        notebook.add(self.timings_tab, text="Timings")
        notebook.add(self.size_tab, text="Size")
        notebook.add(self.state_tab, text="State")

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

    def refresh(self) -> None:
        self._sync_node_list()
        self.refresh_parameters()
        self.refresh_graph()
        self.refresh_diagnostics()
        self.refresh_timings()
        self.refresh_size()
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
            self.timings_list.insert(
                self.tk.END,
                f"{timing.name}: {timing.milliseconds:.3f} ms ({timing.kind})",
            )
        self.timings_list.insert(
            self.tk.END,
            f"total: {record.frame.pass_timings.total_milliseconds:.3f} ms",
        )

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
        try:
            ok = self.application.preview()
        except Exception as exc:  # pragma: no cover - runtime backend boundary
            self.status.configure(text=f"preview failed: {exc}")
            return
        self.status.configure(text="preview updated" if ok else "preview rejected")
        self.refresh()

    def apply_budget(self) -> None:
        try:
            self.application.configure_gpu_cache_budget(int(self.budget_entry.get()))
        except ValueError:
            self.status.configure(text="budget must be an integer")
            return
        self.refresh_state()
        self.status.configure(text="GPU cache budget updated")

    def new_proof(self) -> None:
        self.application = StudioApplication.create(
            gpu_cache_budget_bytes=self.application.gpu_cache_budget_bytes
        )
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
            session = GraphEditorSession.create_nw07_proof(path)
            self.application = StudioApplication.create(
                path,
                gpu_cache_budget_bytes=self.application.gpu_cache_budget_bytes,
            )
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

    def export(self) -> None:
        from tkinter import filedialog

        directory = filedialog.askdirectory(title="Export directory")
        if not directory:
            return
        try:
            report = self.application.export_report(directory)
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
) -> int:
    try:
        import tkinter as tk
    except Exception as exc:  # pragma: no cover - platform-dependent
        print(f"nodewerk-studio GUI unavailable: {exc}", file=sys.stderr)
        return 1
    application = StudioApplication.create(
        graph,
        player_executable=player_executable,
        capture_controls=capture_controls,
        gpu_cache_budget_bytes=gpu_cache_budget,
    )
    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - headless display
        print(f"cannot create GUI root: {exc}", file=sys.stderr)
        return 1
    StudioWindow(root, application)
    root.mainloop()
    return 0


__all__ = ["StudioWindow", "run_gui"]
