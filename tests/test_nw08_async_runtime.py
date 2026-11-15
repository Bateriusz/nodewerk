from __future__ import annotations

import json
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

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
    def __init__(self, package_bytes: bytes = b"edited-package"):
        self.package_bytes = package_bytes
        self.calls = 0

    def export(self, document, work_directory, *, width, height):
        self.calls += 1
        package = work_directory / "edited-preview.nwp"
        package.write_bytes(self.package_bytes)
        return preview.PreviewExportBundle(
            package_sha256=__import__("hashlib").sha256(self.package_bytes).hexdigest(),
            package_path=package,
        )


def _runner_factory(width: int, height: int, *, returncode: int = 0):
    state = {"returncode": returncode}

    def runner(command, **kwargs):
        if state["returncode"] != 0:
            return subprocess.CompletedProcess(
                command, state["returncode"], "", "device removed"
            )
        capture_dir = Path(command[command.index("--capture-dir") + 1])
        tick = int(command[command.index("--tick") + 1])
        capture_dir.mkdir(parents=True, exist_ok=True)
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


def _backend(tmp_path: Path, *, width: int = 8, height: int = 8):
    executable = tmp_path / "preview-host.exe"
    executable.write_bytes(b"MZ")
    runner, state = _runner_factory(width, height)
    exporter = FakeExporter()
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
    assert commands[-1] == "load /tmp/b.nwp"
    assert session.report()["hot_reloads"] == 1
    assert session.report()["last_reload"]["reused_resources"] == 5
    assert session.report()["last_reload"]["reused_pipelines"] == 3

    # Only a layout incompatibility forces a real device recreation/epoch bump.
    assert session.prepare(bundle_c, width=8, height=8) == 1
    assert len(starts) == 2
    assert starts[-1] == "ccc"
    assert session.reinitialize(minimum_epoch=5) == 5
    assert session.prepare(bundle_c, width=8, height=8) == 5


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
    generation = pipeline.reinitialize_device()
    assert generation.epoch == 1
    state["returncode"] = 0
    assert pipeline.preview_now(tick=0).accepted
    assert pipeline.last_valid_preview.token.device_epoch == 1
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
    assert "nw_player_bootstrap" in cmake
    assert "nw_package_player" in cmake
    assert "nw_apply_player_profile" in cmake
    assert "gdi32" in cmake
    workflow = (REPO_ROOT / ".github" / "workflows" / "nw08-studio.yml").read_text(
        encoding="utf-8"
    )
    assert "nodewerk-studio-preview" in workflow
    assert "cmake -S . -B build/nw08-authoring" in workflow
    assert "CMAKE_PROJECT_INCLUDE=$hook" in workflow
    hook = (
        REPO_ROOT / "tools" / "nw08_authoring_build" / "inject-studio.cmake"
    ).read_text(encoding="utf-8")
    assert "PROJECT_SOURCE_DIR STREQUAL CMAKE_SOURCE_DIR" in hook
    assert "cmake_language(DEFER CALL nw_add_studio_authoring_targets)" in hook


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
