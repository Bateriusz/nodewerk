from __future__ import annotations

import json
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw08_cache as cache  # noqa: E402
import nw08_editor as editor  # noqa: E402
import nw08_gui as gui  # noqa: E402
import nw08_preview as preview  # noqa: E402
import nw08_studio as studio  # noqa: E402


def _rgba(width: int, height: int, value: float = 0.5) -> bytes:
    return b"".join(struct.pack("<ffff", value, value, value, 1.0) for _ in range(width * height))


class FakeExporter:
    def __init__(
        self,
        package_bytes: bytes = b"edited-package",
        *,
        capture_width: int | None = None,
        capture_height: int | None = None,
    ):
        self.package_bytes = package_bytes
        self.capture_width = capture_width
        self.capture_height = capture_height
        self.calls = 0

    def export(self, document, work_directory, *, width, height):
        self.calls += 1
        package = work_directory / "edited-preview.nwp"
        package.write_bytes(self.package_bytes)
        return preview.PreviewExportBundle(
            package_sha256=__import__("hashlib").sha256(self.package_bytes).hexdigest(),
            package_path=package,
            capture_width=self.capture_width or width,
            capture_height=self.capture_height or height,
        )


def _runner_factory(width: int, height: int, *, returncode: int = 0):
    state = {"returncode": returncode, "produce_output": True}

    def runner(command, **kwargs):
        if state["returncode"] != 0:
            return subprocess.CompletedProcess(
                command, state["returncode"], "", "device removed"
            )
        capture_dir = Path(command[command.index("--capture-dir") + 1])
        tick = int(command[command.index("--tick") + 1])
        capture_dir.mkdir(parents=True, exist_ok=True)
        if state["produce_output"]:
            (capture_dir / f"tick_{tick}.rgba32f").write_bytes(_rgba(width, height))
        if "--stats-json" in command:
            stats_path = Path(command[command.index("--stats-json") + 1])
            stats_path.write_text(
                json.dumps(
                    {
                        "engine_resource_bytes": 4096,
                        "peak_engine_resource_bytes": 4096,
                        "runtime_cpu_submit_ms": 1.25,
                        "runtime_cpu_available": True,
                        "runtime_gpu_ms": 2.5,
                        "runtime_gpu_available": True,
                        "frame_instructions": ["frame.generate", "frame.render"],
                        "runtime_pass_timings": [
                            {"name": "frame.generate", "milliseconds": 0.125},
                            {"name": "frame.render", "milliseconds": 0.250},
                        ],
                        "fence_retired": True,
                        "adapter_identity_queried": True,
                        "adapter_vendor_id": 0x10DE,
                        "adapter_device_id": 0x2805,
                        "adapter_subsystem_id": 0x12345678,
                        "adapter_luid_low": 0x89ABCDEF,
                        "adapter_luid_high": 0x01234567,
                        "adapter_is_software": False,
                        "driver_version": "566.36",
                        "windows_build_number": 26100,
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    return runner, state


def _backend(
    tmp_path: Path,
    *,
    width: int = 8,
    height: int = 8,
    capture_width: int | None = None,
    capture_height: int | None = None,
):
    executable = tmp_path / "preview-host.exe"
    executable.write_bytes(b"MZ")
    runner, state = _runner_factory(
        capture_width or width, capture_height or height
    )
    exporter = FakeExporter(
        capture_width=capture_width,
        capture_height=capture_height,
    )
    backend = preview.WindowsRuntimePreviewBackend(
        executable,
        runner=runner,
        exporter=exporter,
        execution_kind="simulated",
    )
    return backend, exporter, runner, state


class _BlockingBackend(preview.ReferencePreviewBackend):
    name = "blocking-reference"

    def __init__(self) -> None:
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def render(self, document, tick, *, width, height):
        if self.block:
            self.entered.set()
            if not self.release.wait(10):
                raise preview.PreviewError("test gate timed out")
        return super().render(document, tick, width=width, height=height)


def test_cancel_during_render_prevents_installation() -> None:
    session = editor.GraphEditorSession.create_nw07_proof()
    backend = _BlockingBackend()
    pipeline = preview.PreviewPipeline(session, backend=backend)
    assert pipeline.preview_now(tick=0).accepted
    previous = pipeline.last_valid_preview
    assert previous is not None
    previous_image = previous.frame.image_sha256

    backend.block = True
    token = pipeline.submit_preview(tick=10)
    results: list[preview.CompletionDecision] = []
    worker = threading.Thread(target=lambda: results.append(pipeline.execute(token)))
    worker.start()
    assert backend.entered.wait(10)
    # The job must still be cancellable while the backend is rendering.
    assert pipeline.cancel(token) is True
    backend.release.set()
    worker.join(10)

    assert results and results[0].accepted is False
    assert results[0].reason == "canceled"
    assert pipeline.last_valid_preview is previous
    assert pipeline.last_valid_preview.frame.image_sha256 == previous_image


def test_preview_dispatcher_installs_only_latest_generation() -> None:
    application = studio.StudioApplication.create()
    dispatcher = gui.PreviewDispatcher(application)
    first = dispatcher.request(0)
    assert first is not None
    deadline = time.monotonic() + 10
    while dispatcher.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    outcomes = dispatcher.drain()
    selected = dispatcher.install_latest(outcomes)
    assert selected is not None and selected.accepted is True
    assert selected.record is not None
    assert selected.frame is not None

    # A newer request supersedes the older one; a completion from the older
    # generation is never selected for display.
    token = dispatcher.request(240_000)
    assert token is not None
    deadline = time.monotonic() + 10
    while dispatcher.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    outcomes = dispatcher.drain()
    selected = dispatcher.install_latest(outcomes)
    assert selected is not None and selected.accepted
    stale = gui.PreviewOutcome(
        token=first,
        decision=None,
        accepted=True,
        record=application.pipeline.last_valid_preview,
    )
    assert dispatcher.install_latest([stale]) is None


def test_frame_to_ppm_converts_captured_rgba32f() -> None:
    frame = preview.PreviewFrame(
        tick=0,
        width=2,
        height=2,
        image_sha256="x",
        semantic_digest="sha256:v1:" + "a" * 64,
        image_data=_rgba(2, 2, 0.5),
    )
    ppm = gui.frame_to_ppm(frame)
    assert ppm is not None
    assert ppm.startswith(b"P6\n2 2\n255\n")
    assert len(ppm.split(b"\n", 3)[3]) == 2 * 2 * 3
    assert gui.frame_to_ppm(
        preview.PreviewFrame(tick=0, width=2, height=2, image_sha256="", semantic_digest="")
    ) is None


def test_gui_photoimage_receives_raw_ppm_bytes() -> None:
    frame = preview.PreviewFrame(
        tick=0,
        width=2,
        height=2,
        image_sha256="preview",
        semantic_digest="sha256:v1:" + "a" * 64,
        image_data=_rgba(2, 2, 0.25),
    )

    class FakeTk:
        def __init__(self):
            self.calls = []

        def PhotoImage(self, **kwargs):
            self.calls.append(kwargs)
            return object()

    class FakeLabel:
        def __init__(self):
            self.config = {}

        def configure(self, **kwargs):
            self.config.update(kwargs)

    fake_tk = FakeTk()
    window = object.__new__(gui.StudioWindow)
    window.tk = fake_tk
    window.root = object()
    window.application = SimpleNamespace(
        pipeline=SimpleNamespace(last_valid_frame=frame)
    )
    window.preview_label = FakeLabel()
    window._preview_photo = None
    window._preview_image_sha = ""
    window.refresh_preview_image()

    assert fake_tk.calls
    assert fake_tk.calls[0]["format"] == "ppm"
    assert isinstance(fake_tk.calls[0]["data"], bytes)
    assert fake_tk.calls[0]["data"].startswith(b"P6\n")
    assert window.preview_label.config["image"] is window._preview_photo


def test_gui_photoimage_falls_back_to_binary_ppm_file_on_windows_tk() -> None:
    frame = preview.PreviewFrame(
        tick=0,
        width=2,
        height=2,
        image_sha256="preview-file",
        semantic_digest="sha256:v1:" + "b" * 64,
        image_data=_rgba(2, 2, 0.5),
    )

    class FakeTk:
        def __init__(self):
            self.calls = []
            self.file_bytes = b""

        def PhotoImage(self, **kwargs):
            self.calls.append(kwargs)
            if "data" in kwargs:
                raise RuntimeError("binary -data rejected by Tcl")
            path = Path(kwargs["file"])
            self.file_bytes = path.read_bytes()
            return object()

    class FakeLabel:
        def __init__(self):
            self.config = {}

        def configure(self, **kwargs):
            self.config.update(kwargs)

    fake_tk = FakeTk()
    window = object.__new__(gui.StudioWindow)
    window.tk = fake_tk
    window.root = object()
    window.application = SimpleNamespace(
        pipeline=SimpleNamespace(last_valid_frame=frame)
    )
    window.preview_label = FakeLabel()
    window._preview_photo = None
    window._preview_image_sha = ""
    window.refresh_preview_image()

    assert len(fake_tk.calls) == 2
    assert "data" in fake_tk.calls[0]
    assert "file" in fake_tk.calls[1]
    assert fake_tk.file_bytes.startswith(b"P6\n")
    assert window._preview_image_sha == frame.image_sha256
    assert window.preview_label.config["image"] is window._preview_photo


class _DummyProcess:
    stdin = None
    stdout = None
    stderr = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return None

    def kill(self):
        return None


def test_authoring_session_hot_reloads_compatible_package_without_device_recreation(
    monkeypatch,
) -> None:
    session = preview.AuthoringRuntimeSession("/tmp/preview-host", mode="package-host")
    starts: list[str] = []
    commands: list[str] = []
    reload_responses = [
        {
            "event": "reload",
            "ok": True,
            "requires_device_recreation": False,
            "exit_code": 0,
            "reused_resources": 5,
            "recreated_resources": 2,
            "reused_root_signatures": 1,
            "recreated_root_signatures": 0,
            "reused_pipelines": 3,
            "recreated_pipelines": 1,
            "package_sha256": "bbb",
        },
        {
            "event": "reload",
            "ok": True,
            "requires_device_recreation": True,
            "exit_code": 0,
            "reused_resources": 0,
            "recreated_resources": 0,
            "reused_root_signatures": 0,
            "recreated_root_signatures": 0,
            "reused_pipelines": 0,
            "recreated_pipelines": 0,
            "package_sha256": "bbb",
        },
    ]

    def fake_start(bundle, *, width, height, memory_budget_bytes):
        starts.append(bundle.package_sha256)
        session._package_sha = bundle.package_sha256
        session._process = _DummyProcess()
        session._identity = cache.HostDeviceIdentity.from_runtime_stats(
            {
                "adapter_identity_queried": True,
                "adapter_vendor_id": 0x10DE,
                "adapter_device_id": 0x2805,
                "adapter_subsystem_id": 0x12345678,
                "adapter_luid_low": 0x89ABCDEF,
                "adapter_luid_high": 0x01234567,
                "adapter_is_software": False,
                "driver_version": "566.36",
                "windows_build_number": 26100,
            }
        )

    def fake_write(command):
        commands.append(command)

    def fake_read_response(*, timeout):
        assert reload_responses
        return reload_responses.pop(0)

    monkeypatch.setattr(session, "_start_process", fake_start)
    monkeypatch.setattr(session, "_write", fake_write)
    monkeypatch.setattr(session, "_read_response", fake_read_response)

    bundle_a = preview.PreviewExportBundle(
        package_sha256="aaa", package_path=Path("/tmp/a.nwp")
    )
    bundle_b = preview.PreviewExportBundle(
        package_sha256="bbb", package_path=Path("/tmp/b.nwp")
    )
    bundle_c = preview.PreviewExportBundle(
        package_sha256="ccc", package_path=Path("/tmp/c.nwp")
    )

    assert session.prepare(bundle_a, width=8, height=8) == 0
    assert session.persistent is True
    assert session.identity.available is True
    assert session.prepare(bundle_a, width=8, height=8) == 0
    assert len(starts) == 1

    # A layout-compatible edited package is loaded into the same native
    # PackagePlayer/device and retains real GPU objects.
    assert session.prepare(bundle_b, width=8, height=8) == 0
    assert len(starts) == 1
    assert commands[-1] == f"load {bundle_b.package_path}"
    assert session.report()["hot_reloads"] == 1
    assert session.report()["last_reload"]["reused_resources"] == 5
    assert session.report()["last_reload"]["reused_pipelines"] == 3

    # Only a layout incompatibility forces a real device recreation/epoch bump.
    assert session.prepare(bundle_c, width=8, height=8) == 1
    assert len(starts) == 2
    assert starts[-1] == "ccc"
    assert session.reinitialize(minimum_epoch=5) == 5
    assert session.prepare(bundle_c, width=8, height=8) == 5


def test_one_shot_runtime_advances_epoch_for_every_device_instance() -> None:
    session = preview.AuthoringRuntimeSession(
        None, mode="embedded-player", runner=lambda *args, **kwargs: None
    )
    bundle = preview.PreviewExportBundle(
        package_sha256="same-package",
        executable_path=Path("/tmp/generated-player.exe"),
    )
    assert session.prepare(bundle, width=8, height=8) == 0
    assert session.prepare(bundle, width=8, height=8) == 1
    assert session.prepare(bundle, width=8, height=8) == 2


def test_runtime_backend_reports_measured_timings_and_no_fabricated_readback(
    tmp_path: Path,
) -> None:
    backend, _, _, _ = _backend(tmp_path)
    session = editor.GraphEditorSession.create_nw07_proof()
    frame = backend.render(session.document, 0, width=8, height=8)
    names = {timing.name for timing in frame.pass_timings.timings}
    assert "present_readback" not in names
    assert "runtime_process_wall" in names
    gpu = next(item for item in frame.pass_timings.timings if item.name == "runtime_gpu")
    assert gpu.measured is True and gpu.counts_toward_total is False
    pass_rows = [
        item
        for item in frame.pass_timings.timings
        if item.kind == "runtime_pass_cpu"
    ]
    assert [item.milliseconds for item in pass_rows] == pytest.approx([0.125, 0.250])
    assert all(item.measured and item.available for item in pass_rows)
    wall = next(
        item for item in frame.pass_timings.timings if item.name == "runtime_process_wall"
    )
    export = next(
        item for item in frame.pass_timings.timings if item.name == "offline_compile_export"
    )
    assert frame.pass_timings.total_milliseconds == pytest.approx(
        export.milliseconds + wall.milliseconds
    )
    assert frame.has_image is True
    assert len(frame.image_data) == 8 * 8 * 16
    assert frame.device_identity["available"] is True
    assert frame.device_identity["adapter"]["vendor_id"] == 0x10DE
    backend.close()


def test_runtime_capture_uses_exported_package_dimensions(tmp_path: Path) -> None:
    backend, _, _, _ = _backend(
        tmp_path, width=8, height=8, capture_width=2, capture_height=2
    )
    session = editor.GraphEditorSession.create_nw07_proof()
    frame = backend.render(session.document, 0, width=8, height=8)
    assert (frame.width, frame.height) == (2, 2)
    assert len(frame.image_data) == 2 * 2 * 16
    backend.close()


def test_runtime_requires_fresh_capture_for_same_tick(tmp_path: Path) -> None:
    backend, _, _, state = _backend(tmp_path)
    session = editor.GraphEditorSession.create_nw07_proof()
    assert backend.render(session.document, 0, width=8, height=8).has_image
    state["produce_output"] = False
    with pytest.raises(preview.PreviewError, match="no presented-output readback"):
        backend.render(session.document, 0, width=8, height=8)
    backend.close()


def test_cached_export_reports_zero_current_compile_time(tmp_path: Path) -> None:
    backend, exporter, _, _ = _backend(tmp_path)
    session = editor.GraphEditorSession.create_nw07_proof()
    first = backend.render(session.document, 0, width=8, height=8)
    second = backend.render(session.document, 10, width=8, height=8)
    first_export = next(
        item for item in first.pass_timings.timings
        if item.name == "offline_compile_export"
    )
    second_export = next(
        item for item in second.pass_timings.timings
        if item.name == "offline_compile_export"
    )
    assert exporter.calls == 1
    assert first_export.milliseconds >= 0.0
    assert second_export.milliseconds == 0.0
    backend.close()


def test_cancelled_prepare_does_not_leak_compile_timing_to_next_request(
    tmp_path: Path,
) -> None:
    backend, exporter, _, _ = _backend(tmp_path)
    session = editor.GraphEditorSession.create_nw07_proof()

    # First request exports but is abandoned before render.
    backend.prepare(session.document, width=8, height=8)
    assert exporter.calls == 1

    # A new request for the same cached bundle did no compilation itself.
    backend.prepare(session.document, width=8, height=8)
    frame = backend.render(session.document, 0, width=8, height=8)
    export = next(
        item for item in frame.pass_timings.timings
        if item.name == "offline_compile_export"
    )
    assert exporter.calls == 1
    assert export.milliseconds == 0.0
    backend.close()


def test_preview_history_keeps_pixels_only_for_last_valid_frame(tmp_path: Path) -> None:
    backend, _, _, _ = _backend(tmp_path)
    pipeline = preview.PreviewPipeline(
        editor.GraphEditorSession.create_nw07_proof(),
        backend=backend,
        width=8,
        height=8,
    )
    assert pipeline.preview_now(tick=0).accepted
    assert pipeline.preview_now(tick=10).accepted
    assert len(pipeline.history) == 2
    assert pipeline.history[0].frame.image_data == b""
    assert pipeline.history[1].frame.has_image
    backend.close()


class _PrepareEpochBackend(preview.ReferencePreviewBackend):
    def __init__(self) -> None:
        self.epoch = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def prepare(self, document, *, width, height):
        self.entered.set()
        if not self.release.wait(10):
            raise preview.PreviewError("prepare gate timed out")
        self.epoch += 1
        return self.epoch


def test_cancelled_prepare_still_synchronizes_actual_device_epoch() -> None:
    backend = _PrepareEpochBackend()
    pipeline = preview.PreviewPipeline(
        editor.GraphEditorSession.create_nw07_proof(), backend=backend
    )
    token = pipeline.submit_preview(tick=0)
    results = []
    worker = threading.Thread(target=lambda: results.append(pipeline.execute(token)))
    worker.start()
    assert backend.entered.wait(10)
    assert pipeline.cancel(token)
    backend.release.set()
    worker.join(10)
    assert results and not results[0].accepted
    assert results[0].reason == "canceled"
    assert pipeline.device.epoch == 1
    assert pipeline.queue.device_epoch == 1


class _DelayedDeviceLossBackend(preview.ReferencePreviewBackend):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def render(self, document, tick, *, width, height):
        self.entered.set()
        if not self.release.wait(10):
            raise preview.PreviewError("render gate timed out")
        error = cache.DeviceLostError("old device removed")
        error.device_epoch = 0
        raise error


def test_delayed_old_epoch_device_loss_does_not_poison_new_device() -> None:
    backend = _DelayedDeviceLossBackend()
    pipeline = preview.PreviewPipeline(
        editor.GraphEditorSession.create_nw07_proof(), backend=backend
    )
    token = pipeline.submit_preview(tick=0)
    results = []
    worker = threading.Thread(target=lambda: results.append(pipeline.execute(token)))
    worker.start()
    assert backend.entered.wait(10)
    generation = pipeline.reinitialize_device()
    backend.release.set()
    worker.join(10)
    assert generation.epoch == 1
    assert results and not results[0].accepted
    assert results[0].reason == "obsolete_device_epoch"
    assert pipeline.device.epoch == 1
    assert pipeline.device.lost is False


def test_runtime_device_loss_propagates_and_explicit_reinit_advances_epoch(
    tmp_path: Path,
) -> None:
    backend, _, _, state = _backend(tmp_path)
    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(
        session, backend=backend, width=8, height=8
    )
    # This first render is accepted.
    assert pipeline.preview_now(tick=0).accepted
    # Simulate the real host reporting DeviceRemoved (exit code 51) on the
    # next render and assert the pipeline enters its lost-device state.
    state["returncode"] = 51
    decision = pipeline.preview_now(tick=10)
    assert decision.accepted is False
    assert decision.reason == "preview_device_lost"
    assert pipeline.device.lost is True
    assert pipeline.last_valid_preview is None
    assert pipeline.device.epoch == 1
    generation = pipeline.reinitialize_device()
    assert generation.epoch == 2
    state["returncode"] = 0
    assert pipeline.preview_now(tick=0).accepted
    assert pipeline.last_valid_preview.token.device_epoch == 2
    backend.close()


def test_host_identity_from_runtime_stats_uses_selected_adapter_only() -> None:
    identity = cache.HostDeviceIdentity.from_runtime_stats(
        {
            "adapter_identity_queried": True,
            "adapter_vendor_id": 0x1002,
            "adapter_device_id": 0x744C,
            "adapter_subsystem_id": 0x0B361002,
            "adapter_luid_low": 0xDEADBEEF,
            "adapter_luid_high": -2,
            "adapter_is_software": False,
            "driver_version": "31.0.2",
            "windows_build_number": 26100,
            "gpu_model": "RTX 5070 Ti",
            "gpu_generation": "Blackwell",
        }
    )
    assert identity.available is True
    assert identity.software is False
    assert identity.adapter.vendor_id == 0x1002
    assert identity.adapter.device_id == 0x744C
    assert identity.adapter.subsystem_id == 0x0B361002
    assert identity.adapter.adapter_luid == "fffffffe:deadbeef"
    assert identity.driver_version == "31.0.2"
    assert "windows-build:26100" == identity.os_build
    # Marketing labels are deliberately absent from the cache identity.
    assert "RTX" not in json.dumps(identity.to_key_fields())


def test_configure_export_retires_old_backend_and_advances_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosableBackend(preview.ReferencePreviewBackend):
        def __init__(self) -> None:
            self.closed = False
            self.aligned_epoch = None

        def close(self) -> None:
            self.closed = True

        def reinitialize_device(self, minimum_epoch=None):
            self.aligned_epoch = int(minimum_epoch or 0)
            return self.aligned_epoch

    old_backend = ClosableBackend()
    new_backend = ClosableBackend()
    application = studio.StudioApplication.create(backend=old_backend)
    application.explicit_backend = False
    initial = application.pipeline.reinitialize_device().epoch
    monkeypatch.setattr(application, "_build_backend", lambda: new_backend)
    application.configure_export(optimization="O1")

    assert old_backend.closed is True
    assert application.pipeline.device.epoch == initial + 1
    assert new_backend.aligned_epoch == application.pipeline.device.epoch


def test_gui_actions_route_export_through_the_shared_action_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = studio.StudioApplication.create()
    calls: list[tuple[Path, dict]] = []

    def fake_export(directory, **kwargs):
        calls.append((Path(directory), kwargs))
        return {"package_bytes": 1}

    monkeypatch.setattr(application, "export_report", fake_export)
    actions = gui.GuiActions(application)
    result = actions.export_to(tmp_path / "gui-export")
    assert result == {"package_bytes": 1}
    assert calls and calls[0][0] == tmp_path / "gui-export"
    actions.close()


def test_studio_native_host_links_player_implementation_and_profile() -> None:
    cmake = (REPO_ROOT / "src" / "studio" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )
    assert "nw08_player_bootstrap" in cmake
    assert "nw08_package_player" in cmake
    assert "nw_apply_player_profile" in cmake
    assert "NW_STUDIO_RUNTIME_BUILD_DIR" in cmake
    assert "gdi32" in cmake
    workflow = (REPO_ROOT / ".github" / "workflows" / "nw08-studio.yml").read_text(
        encoding="utf-8"
    )
    assert "nodewerk-studio-preview" in workflow
    assert "cmake --preset nw07-release -DNW_ENFORCE_PINNED_TOOLCHAIN=ON" in workflow
    assert "cmake --build --preset nw07-release --target nw_procedural_image_player" in workflow
    assert "build/nw08-runtime" not in workflow
    assert "cmake/toolchains/msvc-x64.cmake" in workflow
    assert "cmake -S . -B build/nw08-runtime" in workflow
    assert "--target nw_procedural_image_player" in workflow
    assert "cmake -S src/studio -B build/nw08-studio" in workflow
    assert "NW_STUDIO_RUNTIME_BUILD_DIR" in workflow


def test_runtime_integration_checks_remain_pending_without_a_real_host() -> None:
    import nw08_windows_integration as integration

    for function in (
        integration._runtime_shader_recovery,
        integration._runtime_device_recreation,
        integration._runtime_fence_safe_cache,
    ):
        result = function(
            player_executable=None,
            player_template=None,
            dxc=None,
            cache_dir=None,
            execution_kind="physical",
        )
        assert result["status"] == "pending_unverified"


def test_full_verification_requires_gui_and_runtime_evidence(
    tmp_path: Path,
) -> None:
    import nw08_windows_integration as integration
    import verify_nw08

    # A report that only contains the old contract checks plus an unrelated
    # successful real render is not sufficient evidence.
    report = {
        "format": integration.REPORT_FORMAT,
        "version": integration.REPORT_VERSION,
        "hardware_evidence": True,
        "checks": [
            {"name": "gui_cli_export_equivalence", "status": "passed"},
            {"name": "shader_error_recovery", "status": "passed"},
            {"name": "device_recreation", "status": "passed"},
            {"name": "fence_safe_cache", "status": "passed"},
            {"name": "real_preview", "status": "passed"},
        ],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    checks, status = verify_nw08.run_full(path)
    assert status == "failed"
    assert checks[0]["detail"]["missing_checks"]

    # With every required check present but a skipped GUI interaction, Full
    # cannot claim passing evidence.
    names = [
        "gui_cli_export_equivalence",
        "gui_window_interaction",
        "shader_error_recovery",
        "device_recreation",
        "fence_safe_cache",
        "runtime_shader_error_recovery",
        "runtime_device_recreation",
        "runtime_fence_safe_cache",
        "real_preview",
    ]
    report["checks"] = [
        {
            "name": name,
            "status": "skipped" if name == "gui_window_interaction" else "passed",
        }
        for name in names
    ]
    path.write_text(json.dumps(report), encoding="utf-8")
    _checks, status = verify_nw08.run_full(path)
    assert status == "pending_unverified"


def test_authoring_runtime_overlay_preserves_frozen_release_inputs(
    tmp_path: Path,
) -> None:
    studio_cmake = (REPO_ROOT / "src" / "studio" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )
    root_cmake = (REPO_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    frozen_header = (REPO_ROOT / "src" / "player" / "package_player.h").read_text(
        encoding="utf-8"
    )
    frozen_cpp = (REPO_ROOT / "src" / "player" / "package_player.cpp").read_text(
        encoding="utf-8"
    )
    overlay = REPO_ROOT / "tools" / "nw08_authoring_runtime_overlay.py"

    # The release sources remain exactly the NW-07 inputs; authoring changes are
    # generated into the build tree and compiled only into nw08_player_bootstrap.
    assert "NW08_AUTHORING_RUNTIME" not in frozen_header
    assert "reloadForAuthoringFromFile" not in frozen_cpp
    assert "NW08_AUTHORING_RUNTIME=1" not in root_cmake
    assert "nw08_authoring_runtime_overlay.py" in studio_cmake
    assert "add_library(nw08_player_bootstrap STATIC" in studio_cmake

    generated = tmp_path / "overlay"
    subprocess.run(
        [
            sys.executable,
            str(overlay),
            "--repo-root",
            str(REPO_ROOT),
            "--output-dir",
            str(generated),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    generated_header = (generated / "package_player.h").read_text(encoding="utf-8")
    generated_cpp = (generated / "package_player.cpp").read_text(encoding="utf-8")
    assert "#if NW08_AUTHORING_RUNTIME" in generated_header
    assert "reloadForAuthoringFromFile" in generated_cpp
    assert "AdapterLuid.LowPart" in generated_cpp
    assert "authoring_retained_resource_ids_" in generated_cpp
    assert "authoring_pass_timings_" in generated_cpp
    host_text = (
        REPO_ROOT / "src" / "studio" / "nw_studio_preview_host.cpp"
    ).read_text(encoding="utf-8")
    assert "runtime_pass_timings" in host_text
