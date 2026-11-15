from __future__ import annotations

import threading
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw08_editor as editor  # noqa: E402
import nw08_preview as preview  # noqa: E402


PROBE = REPO_ROOT / "fixtures" / "nw08" / "shader_edit_probe.nwgraph"


def _pipeline(graph: Path | None = None) -> preview.PreviewPipeline:
    session = (
        editor.GraphEditorSession.open(graph)
        if graph is not None
        else editor.GraphEditorSession.create_nw07_proof()
    )
    return preview.PreviewPipeline(session, gpu_budget_bytes=512 * 1024 * 1024)


def test_pass_timings_and_size_attribution_are_reported() -> None:
    pipeline = _pipeline()
    assert pipeline.preview_now(tick=240_000).accepted
    record = pipeline.last_valid_preview
    assert record is not None
    names = {item.name for item in record.frame.pass_timings.timings}
    assert {"initialization_reference", "timeline_sample", "size_attribution"} <= names
    assert record.frame.pass_timings.total_milliseconds >= 0.0
    attribution = record.frame.size_attribution
    assert attribution["available"] is True
    assert attribution["resident_payload_bytes"] > 0
    assert attribution["baked_asset_bytes"] == 0
    assert attribution["texture"]["width"] == 256


def test_failed_shader_edit_retains_last_valid_and_links_diagnostic() -> None:
    pipeline = _pipeline(PROBE)
    assert pipeline.preview_now(tick=0).accepted
    previous = pipeline.last_valid_preview
    assert previous is not None
    previous_generation = pipeline.snapshot_generation

    invalid = pipeline.session.set_parameter("shader", "expression", "vec4(sin(")
    pipeline.apply_edit(invalid)
    assert invalid.valid is False
    assert pipeline.last_valid_preview is not None
    assert pipeline.last_valid_preview.frame.image_sha256 == previous.frame.image_sha256
    assert pipeline.snapshot_generation == previous_generation
    assert pipeline.last_diagnostics
    diagnostic = pipeline.last_diagnostics[0]
    assert diagnostic["code"] == "invalid_expression"
    assert diagnostic["source"]["node_id"] == "shader"
    assert diagnostic["source"]["line"] > 0
    assert diagnostic["editor_selection"] == {"node_id": "shader"}

    valid = pipeline.session.set_parameter(
        "shader",
        "expression",
        "vec4(mix(uv.x, sin(uv.y * 5.0 + time), 0.5), 0.25, 0.5, 1.0)",
    )
    pipeline.apply_edit(valid)
    assert valid.valid is True
    assert pipeline.snapshot_generation == previous_generation + 1
    assert pipeline.preview_now(tick=0).accepted
    assert (
        pipeline.last_valid_preview.frame.image_sha256
        != previous.frame.image_sha256
    )


def test_cancelled_and_out_of_order_jobs_never_replace_valid_preview() -> None:
    pipeline = _pipeline()
    assert pipeline.preview_now(tick=10).accepted
    original = pipeline.last_valid_preview
    assert original is not None

    pipeline.apply_parameter_edit("texture", "seed", "1297036694")
    first = pipeline.submit_preview(tick=100)
    second = pipeline.submit_preview(tick=200)
    assert pipeline.execute(second).accepted
    assert pipeline.last_valid_preview.frame.tick == 200
    rejected = pipeline.execute(first)
    assert rejected.accepted is False
    assert rejected.reason == "superseded_by_newer_job"
    assert pipeline.last_valid_preview.frame.tick == 200

    cancelled = pipeline.submit_preview(tick=300)
    assert pipeline.cancel(cancelled)
    assert pipeline.execute(cancelled).reason == "canceled"
    assert pipeline.last_valid_preview.frame.tick == 200


def test_obsolete_snapshot_completion_is_rejected() -> None:
    pipeline = _pipeline()
    token = pipeline.submit_preview(tick=10)
    pipeline.apply_parameter_edit("camera", "fov_degrees", "55")
    decision = pipeline.execute(token)
    assert decision.accepted is False
    assert decision.reason == "obsolete_snapshot_generation"
    assert pipeline.last_valid_preview is None


def test_device_loss_and_recreation_reject_old_jobs_and_realizations() -> None:
    pipeline = _pipeline()
    assert pipeline.preview_now(tick=10).accepted
    assert pipeline.cache.live_bytes > 0
    pending = pipeline.submit_preview(tick=20)
    pipeline.handle_device_loss()
    assert pipeline.last_valid_preview is None
    assert pipeline.cache.live_bytes == 0
    assert pipeline.cache.retiring_bytes == 0
    generation = pipeline.reinitialize_device()
    assert generation.epoch == 1
    decision = pipeline.execute(pending)
    assert decision.accepted is False
    assert decision.reason in {"obsolete_device_epoch", "device_lost"}
    snapshot_generation = pipeline.snapshot_generation
    assert pipeline.preview_now(tick=30).accepted
    assert pipeline.last_valid_preview.frame.tick == 30
    assert pipeline.last_valid_preview.token.device_epoch == 1
    assert pipeline.snapshot_generation == snapshot_generation


def test_windows_backend_uses_existing_player_command(tmp_path: Path) -> None:
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ")
    calls: list[list[str]] = []
    exported: list[preview.PreviewExportBundle] = []
    rendered_documents: list[str] = []

    class FakeExporter:
        def export(self, document, work_directory, *, width, height):
            rendered_documents.append(editor.emit_graph_text(document))
            package = work_directory / "edited-preview.nwp"
            package.write_bytes(b"edited-package")
            bundle = preview.PreviewExportBundle(
                package_sha256="a" * 64,
                package_path=package,
            )
            exported.append(bundle)
            return bundle

    def runner(command, **kwargs):
        calls.append(command)
        capture_dir = Path(command[command.index("--capture-dir") + 1])
        tick = int(command[command.index("--tick") + 1])
        capture_dir.mkdir(parents=True, exist_ok=True)
        (capture_dir / f"tick_{tick}.rgba32f").write_bytes(
            b"\x00" * (1920 * 1080 * 16)
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = preview.WindowsRuntimePreviewBackend(
        executable, runner=runner, exporter=FakeExporter()
    )
    session = editor.GraphEditorSession.create_nw07_proof()
    session.set_parameter("camera", "fov_degrees", "55")
    frame = backend.render(session.document, 480_000, width=1920, height=1080)
    assert calls
    assert "--package" in calls[0]
    assert str(exported[0].package_path) in calls[0]
    assert "480000" in calls[0]
    assert "55" in rendered_documents[0]
    assert frame.backend == "windows-package-player"
    # A stub runner is simulated execution and must never claim physical
    # hardware evidence, even when the process exits successfully.
    assert frame.hardware_evidence is False
    assert frame.execution_kind == "simulated_runtime"
    assert frame.image_sha256


def test_windows_backend_missing_readback_is_a_failure(tmp_path: Path) -> None:
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ")

    class FakeExporter:
        def export(self, document, work_directory, *, width, height):
            package = work_directory / "edited-preview.nwp"
            package.write_bytes(b"edited-package")
            return preview.PreviewExportBundle(
                package_sha256="a" * 64, package_path=package
            )

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = preview.WindowsRuntimePreviewBackend(
        executable, runner=runner, exporter=FakeExporter()
    )
    session = editor.GraphEditorSession.create_nw07_proof()
    with pytest.raises(preview.PreviewError) as error:
        backend.render(session.document, 0, width=8, height=8)
    assert "no presented-output readback" in str(error.value)


def test_embedded_preview_launches_the_exported_player(tmp_path: Path) -> None:
    template = tmp_path / "template.exe"
    template.write_bytes(b"MZ")
    calls: list[list[str]] = []
    exported_executable: list[Path] = []

    class FakeExporter:
        def export(self, document, work_directory, *, width, height):
            executable = work_directory / "edited-preview-player.exe"
            executable.write_bytes(b"MZ" + b"edited")
            exported_executable.append(executable)
            return preview.PreviewExportBundle(
                package_sha256="a" * 64,
                executable_path=executable,
            )

    def runner(command, **kwargs):
        calls.append(command)
        capture_dir = Path(command[command.index("--capture-dir") + 1])
        tick = int(command[command.index("--capture-tick") + 1])
        capture_dir.mkdir(parents=True, exist_ok=True)
        (capture_dir / f"tick_{tick}.rgba32f").write_bytes(b"\x00" * (8 * 8 * 16))
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = preview.WindowsRuntimePreviewBackend(
        template,
        mode="embedded-player",
        runner=runner,
        exporter=FakeExporter(),
    )
    session = editor.GraphEditorSession.create_nw07_proof()
    frame = backend.render(session.document, 7, width=8, height=8)
    assert calls
    assert calls[0][0] == str(exported_executable[0])
    assert "--capture-tick" in calls[0]
    assert "--memory-budget" not in calls[0]
    assert "--stats-json" not in calls[0]
    assert frame.hardware_evidence is False


def test_embedded_preview_backend_is_selected_without_a_host_executable() -> None:
    backend = preview.create_preview_backend(
        None,
        mode="embedded-player",
        dxc_executable="/tmp/unused-dxc",
        player_template="/tmp/unused-template",
        execution_kind="simulated",
    )
    assert isinstance(backend, preview.WindowsRuntimePreviewBackend)
    assert backend.mode == "embedded-player"
    assert backend.executable is None


def test_windows_backend_explicit_physical_with_stub_is_not_evidence(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ")

    class FakeExporter:
        def export(self, document, work_directory, *, width, height):
            package = work_directory / "edited-preview.nwp"
            package.write_bytes(b"edited-package")
            return preview.PreviewExportBundle(
                package_sha256="a" * 64, package_path=package
            )

    def runner(command, **kwargs):
        capture_dir = Path(command[command.index("--capture-dir") + 1])
        tick = int(command[command.index("--tick") + 1])
        capture_dir.mkdir(parents=True, exist_ok=True)
        (capture_dir / f"tick_{tick}.rgba32f").write_bytes(
            b"\x00" * (8 * 8 * 16)
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = preview.WindowsRuntimePreviewBackend(
        executable,
        runner=runner,
        exporter=FakeExporter(),
        execution_kind="physical",
    )
    session = editor.GraphEditorSession.create_nw07_proof()
    frame = backend.render(session.document, 0, width=8, height=8)
    assert frame.hardware_evidence is False
    assert frame.execution_kind == "simulated_runtime"


def test_reference_backend_is_never_reported_as_hardware_evidence() -> None:
    pipeline = _pipeline()
    pipeline.preview_now(tick=0)
    frame = pipeline.last_valid_frame
    assert frame is not None
    assert frame.hardware_evidence is False
    assert frame.execution_kind == "contract_reference"


class _RecordingBackend(preview.ReferencePreviewBackend):
    name = "recording-reference"

    def __init__(self) -> None:
        self.documents: list[str] = []

    def render(self, document, tick, *, width, height):
        self.documents.append(editor.emit_graph_text(document))
        return super().render(document, tick, width=width, height=height)


class _GateBackend(preview.ReferencePreviewBackend):
    name = "gate-reference"

    def __init__(self) -> None:
        self.block = False
        self.fail_after_release = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def render(self, document, tick, *, width, height):
        self.calls += 1
        if self.block:
            self.entered.set()
            if not self.release.wait(10):
                raise preview.PreviewError("test gate timed out")
            if self.fail_after_release:
                raise preview.PreviewError(
                    "compiler failed after the job became obsolete",
                    [
                        {
                            "category": "compiler",
                            "severity": "error",
                            "code": "should_not_win",
                            "message": "stale failure diagnostic",
                        }
                    ],
                )
        return super().render(document, tick, width=width, height=height)


class _IdentityBackend(preview.ReferencePreviewBackend):
    name = "identity-reference"

    def __init__(self) -> None:
        self.shader_identity = "sha256:v1:" + "1" * 64
        self.binding_identity = "sha256:v1:" + "2" * 64

    def render(self, document, tick, *, width, height):
        frame = super().render(document, tick, width=width, height=height)
        return replace(
            frame,
            shader_identity=self.shader_identity,
            binding_identity=self.binding_identity,
        )


def test_submitted_job_renders_its_frozen_snapshot() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _RecordingBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    token = pipeline.submit_preview(tick=10)
    decision = pipeline.execute(token)
    assert decision.accepted is True
    assert backend.documents
    assert "param fov_degrees 60" in backend.documents[0]
    # A later edit must not rewrite the digest of the frame that already
    # installed from the frozen snapshot.
    pipeline.apply_edit(session.set_parameter("camera", "fov_degrees", "55"))
    assert pipeline.last_valid_preview is not None
    assert pipeline.last_valid_preview.semantic_digest == token.semantic_digest


def test_inflight_edit_cannot_publish_old_frame() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _GateBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted
    original = pipeline.last_valid_preview
    assert original is not None

    backend.block = True
    token = pipeline.submit_preview(tick=1)
    results: list[preview.CompletionDecision] = []
    worker = threading.Thread(target=lambda: results.append(pipeline.execute(token)))
    worker.start()
    assert backend.entered.wait(10)
    pipeline.apply_edit(session.set_parameter("camera", "fov_degrees", "55"))
    backend.release.set()
    worker.join(10)

    assert results and results[0].accepted is False
    assert results[0].reason == "obsolete_snapshot_generation"
    assert pipeline.last_valid_preview is not None
    assert pipeline.last_valid_preview.frame.tick == 0


def test_inflight_completion_after_device_loss_is_rejected() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _GateBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted

    backend.block = True
    token = pipeline.submit_preview(tick=1)
    results: list[preview.CompletionDecision] = []
    worker = threading.Thread(target=lambda: results.append(pipeline.execute(token)))
    worker.start()
    assert backend.entered.wait(10)
    pipeline.handle_device_loss()
    backend.release.set()
    worker.join(10)

    assert results and results[0].accepted is False
    assert results[0].reason in {"device_lost", "obsolete_device_epoch"}
    assert pipeline.last_valid_preview is None
    generation = pipeline.reinitialize_device()
    assert generation.epoch == 1
    assert pipeline.last_valid_preview is None


def test_inflight_completion_after_device_recreation_is_rejected() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _GateBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted

    backend.block = True
    token = pipeline.submit_preview(tick=1)
    results: list[preview.CompletionDecision] = []
    worker = threading.Thread(target=lambda: results.append(pipeline.execute(token)))
    worker.start()
    assert backend.entered.wait(10)
    pipeline.handle_device_loss()
    generation = pipeline.reinitialize_device()
    backend.release.set()
    worker.join(10)

    assert generation.epoch == 1
    assert results and results[0].accepted is False
    assert results[0].reason == "obsolete_device_epoch"
    assert pipeline.last_valid_preview is None


def test_failing_render_after_edit_reports_obsolete_snapshot() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _GateBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted

    backend.block = True
    backend.fail_after_release = True
    token = pipeline.submit_preview(tick=1)
    results: list[preview.CompletionDecision] = []
    worker = threading.Thread(target=lambda: results.append(pipeline.execute(token)))
    worker.start()
    assert backend.entered.wait(10)
    pipeline.apply_edit(session.set_parameter("camera", "fov_degrees", "55"))
    backend.release.set()
    worker.join(10)

    assert results and results[0].accepted is False
    assert results[0].reason == "obsolete_snapshot_generation"
    assert all(
        diagnostic.get("code") != "should_not_win"
        for diagnostic in pipeline.last_diagnostics
    )


def test_failing_render_after_device_loss_reports_device_rejection() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _GateBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted

    backend.block = True
    backend.fail_after_release = True
    token = pipeline.submit_preview(tick=1)
    results: list[preview.CompletionDecision] = []
    worker = threading.Thread(target=lambda: results.append(pipeline.execute(token)))
    worker.start()
    assert backend.entered.wait(10)
    pipeline.handle_device_loss()
    backend.release.set()
    worker.join(10)

    assert results and results[0].accepted is False
    assert results[0].reason in {"device_lost", "obsolete_device_epoch"}
    assert all(
        diagnostic.get("code") != "should_not_win"
        for diagnostic in pipeline.last_diagnostics
    )


def test_render_failure_returns_rejected_and_retains_last_valid() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _GateBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted
    previous = pipeline.last_valid_preview
    assert previous is not None

    class _Failing(preview.ReferencePreviewBackend):
        def render(self, document, tick, *, width, height):
            raise preview.PreviewError(
                "shader compilation failed",
                [
                    {
                        "category": "compiler",
                        "severity": "error",
                        "code": "invalid_expression",
                        "message": "unexpected end of expression",
                    }
                ],
            )

    pipeline.backend = _Failing()
    decision = pipeline.preview_now(tick=30)
    assert decision.accepted is False
    assert decision.reason == "preview_failed"
    assert pipeline.last_valid_preview is previous
    assert pipeline.last_diagnostics[0]["code"] == "invalid_expression"
    # StudioApplication.preview must not report success merely because an
    # older valid record still exists.
    import nw08_studio as studio

    application = studio.StudioApplication(
        session=session,
        pipeline=pipeline,
        gpu_cache_budget_bytes=pipeline.cache.budget_bytes,
    )
    assert application.preview(30) is False


def test_parameter_edit_reuses_unchanged_per_node_realizations() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(
        session, gpu_budget_bytes=512 * 1024 * 1024
    )
    assert pipeline.preview_now(tick=0).accepted
    first = pipeline.last_valid_preview
    assert first is not None
    assert first.recomputed_nodes
    assert first.cached_nodes == ()

    pipeline.apply_parameter_edit("texture", "seed", "1297036694")
    assert pipeline.preview_now(tick=0).accepted
    second = pipeline.last_valid_preview
    assert second is not None
    assert set(second.cached_nodes) == {
        "camera",
        "instances",
        "mesh_gpu",
        "scene_gpu",
        "sun",
        "torus",
        "twist",
    }
    assert set(second.recomputed_nodes) == {
        "bloom",
        "material",
        "render",
        "screen",
        "texture",
        "texture_gpu",
        "tonemap",
    }
    first_keys = dict(first.node_realization_keys)
    second_keys = dict(second.node_realization_keys)
    for node_id in second.cached_nodes:
        assert first_keys[node_id] == second_keys[node_id]
    for node_id in second.recomputed_nodes:
        assert first_keys[node_id] != second_keys[node_id]
    assert pipeline.cache.stats["hits"] >= len(second.cached_nodes)


def test_shader_or_binding_identity_change_invalidates_all_realizations() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _IdentityBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted
    first = pipeline.last_valid_preview
    assert first is not None
    first_keys = dict(first.node_realization_keys)
    assert first.frame.shader_identity == backend.shader_identity
    assert first.frame.binding_identity == backend.binding_identity

    pipeline.apply_parameter_edit("texture", "seed", "1297036694")
    assert pipeline.preview_now(tick=0).accepted
    stable = pipeline.last_valid_preview
    assert stable is not None
    assert set(stable.cached_nodes) == {
        "camera",
        "instances",
        "mesh_gpu",
        "scene_gpu",
        "sun",
        "torus",
        "twist",
    }

    backend.shader_identity = "sha256:v1:" + "3" * 64
    assert pipeline.preview_now(tick=0).accepted
    changed = pipeline.last_valid_preview
    assert changed is not None
    assert changed.cached_nodes == ()
    changed_keys = dict(changed.node_realization_keys)
    assert all(first_keys[node_id] != changed_keys[node_id] for node_id in first_keys)


def test_runtime_preview_reservation_bounds_real_gpu_budget() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()

    class _NeverRendered(preview.ReferencePreviewBackend):
        persistent_realizations = False
        reserves_gpu_memory = True

        def __init__(self) -> None:
            self.calls = 0

        def render(self, document, tick, *, width, height):
            self.calls += 1
            raise AssertionError("render must not launch above the GPU budget")

    backend = _NeverRendered()
    pipeline = preview.PreviewPipeline(
        session, backend=backend, gpu_budget_bytes=1
    )
    decision = pipeline.preview_now(tick=0)
    assert backend.calls == 0
    assert decision.accepted is False
    assert decision.reason == "preview_gpu_budget_exceeded"
    assert pipeline.cache.total_bytes <= pipeline.cache.budget_bytes


def test_repeated_edits_stay_within_budget_by_evicting_stale_realizations() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(
        session, gpu_budget_bytes=200 * 1024 * 1024
    )
    for seed in range(12):
        pipeline.apply_parameter_edit("texture", "seed", str(1297036693 + seed))
        assert pipeline.preview_now(tick=0).accepted
        assert pipeline.cache.within_budget
        assert pipeline.cache.total_bytes <= pipeline.cache.budget_bytes
    assert pipeline.cache.stats["evictions"] > 0


def test_runtime_preview_command_carries_budget_and_stats(tmp_path: Path) -> None:
    executable = tmp_path / "player.exe"
    executable.write_bytes(b"MZ")
    calls: list[list[str]] = []
    stats = {"engine_resource_bytes": 12345, "package_plan_bytes": 777}

    class FakeExporter:
        def export(self, document, work_directory, *, width, height):
            package = work_directory / "edited-preview.nwp"
            package.write_bytes(b"edited-package")
            return preview.PreviewExportBundle(
                package_sha256="a" * 64, package_path=package
            )

    def runner(command, **kwargs):
        calls.append(command)
        capture_dir = Path(command[command.index("--capture-dir") + 1])
        tick = int(command[command.index("--tick") + 1])
        capture_dir.mkdir(parents=True, exist_ok=True)
        (capture_dir / f"tick_{tick}.rgba32f").write_bytes(b"\x00" * (8 * 8 * 16))
        stats_path = Path(command[command.index("--stats-json") + 1])
        stats_path.write_text(__import__("json").dumps(stats), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = preview.WindowsRuntimePreviewBackend(
        executable,
        runner=runner,
        exporter=FakeExporter(),
        memory_budget_bytes=64 * 1024 * 1024,
    )
    session = editor.GraphEditorSession.create_nw07_proof()
    frame = backend.render(session.document, 0, width=8, height=8)
    assert "--memory-budget" in calls[0]
    assert "67108864" in calls[0]
    assert "--stats-json" in calls[0]
    assert frame.resident_bytes == 12345


def test_preview_resolution_is_part_of_realization_keys() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(session, width=1920, height=1080)
    assert pipeline.preview_now(tick=0).accepted
    first = pipeline.last_valid_preview
    assert first is not None
    first_keys = dict(first.node_realization_keys)
    pipeline.width = 1280
    pipeline.height = 720
    assert pipeline.preview_now(tick=0).accepted
    second = pipeline.last_valid_preview
    assert second is not None
    second_keys = dict(second.node_realization_keys)
    assert second.cached_nodes == ()
    assert all(first_keys[node_id] != second_keys[node_id] for node_id in first_keys)


def test_submitted_job_freezes_preview_resolution() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(session, width=1920, height=1080)
    token = pipeline.submit_preview(tick=2)
    pipeline.width = 1280
    pipeline.height = 720
    decision = pipeline.execute(token)
    assert decision.accepted is True
    record = pipeline.last_valid_preview
    assert record is not None
    assert record.frame.width == 1920
    assert record.frame.height == 1080


def test_toolchain_identity_is_part_of_realization_keys() -> None:
    import nw08_cache

    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(session)
    assert pipeline.preview_now(tick=0).accepted
    first = pipeline.last_valid_preview
    assert first is not None
    first_keys = dict(first.node_realization_keys)
    pipeline.set_toolchain(
        nw08_cache.ToolchainIdentity(
            dxc_version="9.9.9-test",
            optimization="O1",
            os_build=pipeline.toolchain.os_build,
        )
    )
    assert pipeline.preview_now(tick=0).accepted
    second = pipeline.last_valid_preview
    assert second is not None
    second_keys = dict(second.node_realization_keys)
    assert second.cached_nodes == ()
    assert all(first_keys[node_id] != second_keys[node_id] for node_id in first_keys)
    assert pipeline.device.generation.toolchain_identity


def test_toolchain_change_cancels_inflight_jobs() -> None:
    import nw08_cache

    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(session)
    token = pipeline.submit_preview(tick=1)
    pipeline.set_toolchain(
        nw08_cache.ToolchainIdentity(
            dxc_version="9.9.9-test",
            os_build=pipeline.toolchain.os_build,
        )
    )
    decision = pipeline.execute(token)
    assert decision.accepted is False
    assert decision.reason == "canceled"


def test_toolchain_change_during_render_rejects_completion() -> None:
    import nw08_cache

    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _GateBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted
    original = pipeline.last_valid_preview
    assert original is not None

    backend.block = True
    token = pipeline.submit_preview(tick=1)
    results: list[preview.CompletionDecision] = []
    worker = threading.Thread(target=lambda: results.append(pipeline.execute(token)))
    worker.start()
    assert backend.entered.wait(10)
    pipeline.set_toolchain(
        nw08_cache.ToolchainIdentity(
            dxc_version="9.9.9-test",
            os_build=pipeline.toolchain.os_build,
        )
    )
    backend.release.set()
    worker.join(10)

    assert results and results[0].accepted is False
    assert results[0].reason == "obsolete_snapshot_generation"
    assert pipeline.last_valid_preview is original


def test_generated_shader_and_binding_identities_are_keyed() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(session)
    assert pipeline.preview_now(tick=0).accepted
    first = pipeline.last_valid_preview
    assert first is not None
    # The reference backend has no compiled shader package, but the key inputs
    # still carry explicit empty identities.  The real runtime backend fills
    # these from the export manifest/package.
    assert first.frame.shader_identity == ""
    assert first.frame.binding_identity == ""


def test_explicit_adapter_merges_discovered_driver_and_os() -> None:
    import nw08_cache

    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(
        session,
        adapter=nw08_cache.AdapterDeviceIdentity(0x1002, 0x744C, 1, "explicit"),
    )
    generation = pipeline.device.generation
    assert generation.driver_version == pipeline.host_identity.driver_version
    assert generation.os_build == pipeline.host_identity.os_build
    assert generation.driver_version
    assert generation.os_build


def test_export_bundle_carries_compiled_shader_and_binding_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    import nw08_test_toolchain

    toolchain = nw08_test_toolchain.create_toolchain(tmp_path / "toolchain")
    session = editor.GraphEditorSession.create_nw07_proof()
    exporter = preview.RuntimePackageExporter(
        mode="package-host",
        dxc_executable=toolchain["dxc"],
        player_template=toolchain["player_template"],
        allow_test_payloads=True,
    )
    bundle = exporter.export(
        session.document, tmp_path / "work", width=1920, height=1080
    )
    assert bundle.package_sha256
    assert bundle.shader_identity.startswith("sha256:v1:")
    assert bundle.binding_identity.startswith("sha256:v1:")
    assert isinstance(bundle.node_shader_identities, dict)

