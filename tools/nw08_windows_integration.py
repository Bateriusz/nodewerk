#!/usr/bin/env python3
"""Windows integration validation for NW-08 (real runtime, not a substitute).

This script is intended to be executed on a Windows 11 x64 host with the
pinned DXC and a verified specialized/diagnostic player template.  It records:

* GUI-versus-CLI export equivalence;
* broken-shader recovery with source-linked diagnostics;
* explicit device-loss/reinitialization epoch handling;
* fence-safe, budget-bounded GPU realization retention;
* real preview execution through the existing package player, when supplied.

The report distinguishes ``passed`` from ``pending_unverified``.  Contract
simulations never set ``hardware_evidence``; only an executed real preview with
``--execution-kind physical`` (or documented GPU passthrough) can do so.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import nw08_cache as cache  # noqa: E402
import nw08_editor as editor  # noqa: E402
import nw08_preview as preview  # noqa: E402
import nw08_studio as studio  # noqa: E402
import nwp_package as nwp  # noqa: E402

REPORT_FORMAT = "nw08-windows-integration"
REPORT_VERSION = 1


def _check(name: str, status: str, detail: Any = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "status": status}
    if detail is not None:
        entry["detail"] = detail
    return entry


def _export_equivalence(
    work: Path,
    player_template: str,
    dxc: str,
    *,
    allow_test_payloads: bool = False,
) -> dict[str, Any]:
    session = editor.GraphEditorSession.create_nw07_proof()
    session.set_parameter("camera", "fov_degrees", "55")
    source = work / "edited.nwgraph"
    session.save(source)
    gui = studio.StudioApplication.create(source).export(
        work / "gui-export",
        settings={"player_template": player_template},
        dxc_executable=dxc,
        allow_test_payloads=allow_test_payloads,
    )
    cli_command = [
        sys.executable,
        str(TOOLS_DIR / "nodewerkc.py"),
        "export",
        str(source),
        str(work / "cli-export"),
        "--dxc",
        dxc,
        "--player-template",
        player_template,
    ]
    if allow_test_payloads:
        cli_command.append("--allow-test-payloads")
    cli = subprocess.run(
        cli_command,
        capture_output=True,
        text=True,
        check=False,
    )
    if cli.returncode != 0:
        return _check(
            "gui_cli_export_equivalence",
            "failed",
            {"returncode": cli.returncode, "stderr": cli.stderr[-8000:]},
        )
    stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    gui_package = (work / "gui-export" / f"{stem}.nwp").read_bytes()
    cli_package = (work / "cli-export" / f"{stem}.nwp").read_bytes()
    gui_exe = (work / "gui-export" / f"{stem}.exe").read_bytes()
    cli_exe = (work / "cli-export" / f"{stem}.exe").read_bytes()
    if gui_package != cli_package or gui_exe != cli_exe:
        return _check(
            "gui_cli_export_equivalence",
            "failed",
            {"reason": "artifact byte mismatch"},
        )
    return _check(
        "gui_cli_export_equivalence",
        "passed",
        {
            "package_sha256": hashlib.sha256(gui_package).hexdigest(),
            "executable_sha256": hashlib.sha256(gui_exe).hexdigest(),
            "package_bytes": len(gui_package),
            "executable_bytes": len(gui_exe),
        },
    )


def _shader_recovery(work: Path) -> dict[str, Any]:
    session = editor.GraphEditorSession.open(
        REPO_ROOT / "fixtures" / "nw08" / "shader_edit_probe.nwgraph"
    )
    pipeline = preview.PreviewPipeline(session)
    if not pipeline.preview_now(tick=0).accepted:
        return _check("shader_error_recovery", "failed", "initial preview failed")
    previous = pipeline.last_valid_frame
    invalid = session.set_parameter("shader", "expression", "vec4(sin(")
    pipeline.apply_edit(invalid)
    if invalid.valid or pipeline.last_valid_preview is None:
        return _check(
            "shader_error_recovery", "failed", "invalid edit replaced preview"
        )
    source = pipeline.last_diagnostics[0].get("source") or {}
    if source.get("node_id") != "shader" or not source.get("line"):
        return _check(
            "shader_error_recovery", "failed", "diagnostic is not source-linked"
        )
    valid = session.set_parameter(
        "shader",
        "expression",
        "vec4(mix(uv.x, sin(uv.y * 5.0 + time), 0.5), 0.25, 0.5, 1.0)",
    )
    pipeline.apply_edit(valid)
    if not pipeline.preview_now(tick=0).accepted:
        return _check("shader_error_recovery", "failed", "recovery preview failed")
    return _check(
        "shader_error_recovery",
        "passed",
        {
            "previous_image": previous.image_sha256 if previous else "",
            "recovered_image": pipeline.last_valid_frame.image_sha256,
            "diagnostic": pipeline.last_diagnostics,
        },
    )


def _device_recreation() -> dict[str, Any]:
    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(session, gpu_budget_bytes=256 * 1024 * 1024)
    if not pipeline.preview_now(tick=0).accepted:
        return _check("device_recreation", "failed", "initial preview failed")
    pending = pipeline.submit_preview(tick=100)
    pipeline.handle_device_loss()
    generation = pipeline.reinitialize_device()
    decision = pipeline.execute(pending)
    if decision.accepted or generation.epoch != 1:
        return _check(
            "device_recreation",
            "failed",
            {"decision": decision.to_dict(), "epoch": generation.epoch},
        )
    return _check(
        "device_recreation",
        "passed",
        {
            "epoch": generation.epoch,
            "rejected_reason": decision.reason,
            "gpu_live_bytes": pipeline.cache.live_bytes,
        },
    )


def _fence_safe_cache() -> dict[str, Any]:
    gpu = cache.GpuRealizationCache(1000)
    gpu.acquire("a", 600, epoch=0)
    gpu.submit(["a"], 10)
    gpu.acquire("b", 200, epoch=0)
    gpu.submit(["b"], 20)
    try:
        gpu.acquire("c", 300, epoch=0)
    except cache.GpuBudgetExceeded:
        pass
    else:
        return _check(
            "fence_safe_cache", "failed", "budget exceeded without rejection"
        )
    gpu.release("a")
    if gpu.retiring_bytes != 600:
        return _check(
            "fence_safe_cache", "failed", "submitted resource retired too early"
        )
    gpu.signal_fence(9)
    if gpu.retiring_bytes != 600:
        return _check(
            "fence_safe_cache", "failed", "resource retired before fence"
        )
    gpu.signal_fence(10)
    if gpu.retiring_bytes != 0:
        return _check(
            "fence_safe_cache", "failed", "fence did not retire resource"
        )
    return _check(
        "fence_safe_cache",
        "passed",
        {"stats": gpu.stats, "total_bytes": gpu.total_bytes},
    )


def _real_preview(
    player_executable: str | None,
    *,
    execution_kind: str,
    capture_controls: bool,
    ticks: Sequence[int],
    player_template: str | None,
    dxc: str | None,
    cache_dir: str | None,
    preview_mode: str,
) -> dict[str, Any]:
    if not player_executable and preview_mode != "embedded-player":
        return _check(
            "real_preview",
            "pending_unverified",
            {"reason": "no --preview-executable supplied"},
        )
    if execution_kind not in {"physical", "documented_gpu_passthrough"}:
        return _check(
            "real_preview",
            "failed",
            {"reason": f"unsupported execution kind '{execution_kind}'"},
        )
    if not (player_template and dxc):
        return _check(
            "real_preview",
            "pending_unverified",
            {
                "reason": (
                    "real preview requires both --player-template and --dxc "
                    "so the edited graph can be compiled before display"
                )
            },
        )
    if preview_mode != "package-host":
        return _check(
            "real_preview",
            "pending_unverified",
            {
                "reason": (
                    "physical preview evidence requires package-host mode; "
                    "use the embedded_preview_compatibility check for the "
                    "generated-player path"
                )
            },
        )
    session = editor.GraphEditorSession.create_nw07_proof()
    # Make the preview explicitly consume an edited graph, not merely the
    # frozen proof defaults.
    session.set_parameter("camera", "fov_degrees", "55")
    try:
        backend = preview.WindowsRuntimePreviewBackend(
            player_executable,
            capture_controls=capture_controls,
            mode=preview_mode,
            dxc_executable=dxc,
            player_template=player_template,
            cache_dir=cache_dir,
            execution_kind=execution_kind,
            memory_budget_bytes=512 * 1024 * 1024,
        )
    except preview.PreviewError as exc:
        return _check(
            "real_preview",
            "pending_unverified" if not capture_controls else "failed",
            {"reason": str(exc)},
        )
    pipeline = preview.PreviewPipeline(
        session,
        backend=backend,
        toolchain=preview.toolchain_identity_for(dxc, optimization="O2"),
    )
    results = []
    for tick in ticks:
        token = pipeline.submit_preview(tick=tick)
        decision = pipeline.execute(token)
        record = pipeline.last_valid_preview
        if (
            not decision.accepted
            or record is None
            or record.token.job_id != token.job_id
            or record.frame.tick != tick
            or not record.frame.image_sha256
            or record.semantic_digest != session.semantic_digest
        ):
            return _check(
                "real_preview",
                "failed",
                {
                    "tick": tick,
                    "decision": decision.to_dict(),
                    "diagnostics": pipeline.last_diagnostics,
                },
            )
        frame = record.frame
        expected_physical = execution_kind in {
            "physical",
            "documented_gpu_passthrough",
        }
        if bool(frame.hardware_evidence) != expected_physical:
            return _check(
                "real_preview",
                "failed",
                {
                    "tick": tick,
                    "reason": "hardware_evidence does not match execution kind",
                    "hardware_evidence": frame.hardware_evidence,
                    "execution_kind": frame.execution_kind,
                },
            )
        results.append(frame.to_dict())
    return _check(
        "real_preview",
        "passed",
        {
            "execution_kind": execution_kind,
            "frames": results,
        },
    )


def _embedded_preview_compatibility(
    *,
    player_template: str | None,
    dxc: str | None,
    cache_dir: str | None,
    ticks: Sequence[int],
    allow_test_payloads: bool,
    enabled: bool,
) -> dict[str, Any]:
    """Exercise the generated-player path without claiming hardware evidence."""
    if not enabled:
        return _check(
            "embedded_preview_compatibility",
            "skipped",
            {
                "reason": (
                    "pass --embedded-preview with a diagnostic capture-control "
                    "player template to exercise the generated-player path"
                )
            },
        )
    if not (player_template and dxc):
        return _check(
            "embedded_preview_compatibility",
            "pending_unverified",
            {"reason": "player template and pinned DXC are required"},
        )
    if allow_test_payloads:
        return _check(
            "embedded_preview_compatibility",
            "pending_unverified",
            {
                "reason": (
                    "fixture payloads cannot produce a runnable generated "
                    "player; use real pinned tools"
                )
            },
        )
    session = editor.GraphEditorSession.create_nw07_proof()
    session.set_parameter("camera", "fov_degrees", "55")
    try:
        backend = preview.WindowsRuntimePreviewBackend(
            None,
            mode="embedded-player",
            dxc_executable=dxc,
            player_template=player_template,
            cache_dir=cache_dir,
            execution_kind="simulated",
            memory_budget_bytes=512 * 1024 * 1024,
        )
    except preview.PreviewError as exc:
        return _check(
            "embedded_preview_compatibility",
            "failed",
            {"reason": str(exc)},
        )
    pipeline = preview.PreviewPipeline(
        session,
        backend=backend,
        toolchain=preview.toolchain_identity_for(dxc, optimization="O2"),
    )
    frames = []
    for tick in ticks:
        token = pipeline.submit_preview(tick=tick)
        decision = pipeline.execute(token)
        record = pipeline.last_valid_preview
        if (
            not decision.accepted
            or record is None
            or record.token.job_id != token.job_id
            or record.frame.tick != tick
            or not record.frame.image_sha256
            or record.frame.hardware_evidence
        ):
            return _check(
                "embedded_preview_compatibility",
                "failed",
                {
                    "tick": tick,
                    "decision": decision.to_dict(),
                    "diagnostics": pipeline.last_diagnostics,
                },
            )
        frames.append(record.frame.to_dict())
    return _check(
        "embedded_preview_compatibility",
        "passed",
        {"execution_kind": "simulated_runtime", "frames": frames},
    )


def _nw01_provisioning_contract() -> dict[str, Any]:
    """Consume the established NW-01 provisioning/evidence contracts."""
    sources = REPO_ROOT / "tools" / "nw01" / "toolchain-sources.json"
    identities = REPO_ROOT / "tools" / "nw01" / "tool-version-identities.json"
    harness = REPO_ROOT / "tools" / "nw01" / "Nw01.Windows.psm1"
    missing = [
        str(path)
        for path in (sources, identities, harness)
        if not path.is_file()
    ]
    if missing:
        return _check(
            "nw01_provisioning_contract",
            "failed",
            {"missing": missing},
        )
    source_record = json.loads(sources.read_text(encoding="utf-8"))
    identity_record = json.loads(identities.read_text(encoding="utf-8"))
    return _check(
        "nw01_provisioning_contract",
        "passed",
        {
            "toolchain_sources": sorted(source_record),
            "tool_version_identities": sorted(identity_record)[:8],
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player-template")
    parser.add_argument("--dxc")
    parser.add_argument("--preview-executable")
    parser.add_argument(
        "--preview-mode",
        choices=["package-host", "embedded-player"],
        default="package-host",
    )
    parser.add_argument("--cache-dir")
    parser.add_argument("--allow-test-payloads", action="store_true")
    parser.add_argument(
        "--embedded-preview",
        action="store_true",
        help=(
            "exercise the generated-player path; requires a diagnostic player "
            "template built with capture controls"
        ),
    )
    parser.add_argument(
        "--execution-kind",
        choices=["physical", "documented_gpu_passthrough"],
        default="physical",
    )
    parser.add_argument(
        "--passthrough-documentation", default=""
    )
    parser.add_argument(
        "--capture-controls",
        dest="capture_controls",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--no-capture-controls",
        dest="capture_controls",
        action="store_false",
        help="use --tick/--hold-ms on a release player without capture controls",
    )
    parser.add_argument("--capture-tick", action="append", type=int, default=[])
    parser.add_argument("--report", default="build/nw08-windows-integration.json")
    args = parser.parse_args(argv)

    checks: list[dict[str, Any]] = []
    platform_ok = os.name == "nt" and platform.machine().lower() in {
        "amd64",
        "x86_64",
    }
    checks.append(
        _check(
            "windows_x64_host",
            "passed" if platform_ok else "failed",
            {
                "os_name": os.name,
                "platform": platform.system(),
                "machine": platform.machine(),
            },
        )
    )
    if not platform_ok:
        report = {
            "format": REPORT_FORMAT,
            "version": REPORT_VERSION,
            "task": "NW-08",
            "status": "failed",
            "hardware_evidence": False,
            "checks": checks,
        }
        return _write_report(report, Path(args.report), 1)

    checks.append(_nw01_provisioning_contract())
    with tempfile.TemporaryDirectory(prefix="nw08-integration-") as raw:
        work = Path(raw)
        if args.player_template and args.dxc:
            checks.append(
                _export_equivalence(
                    work,
                    args.player_template,
                    args.dxc,
                    allow_test_payloads=args.allow_test_payloads,
                )
            )
        else:
            checks.append(
                _check(
                    "gui_cli_export_equivalence",
                    "pending_unverified",
                    {"reason": "player template and pinned DXC are required"},
                )
            )
        checks.append(_shader_recovery(work))
        checks.append(_device_recreation())
        checks.append(_fence_safe_cache())
        ticks = args.capture_tick or [0, 240_000, 480_000, 720_000, 959_999]
        checks.append(
            _real_preview(
                args.preview_executable,
                execution_kind=args.execution_kind,
                capture_controls=args.capture_controls,
                ticks=ticks,
                player_template=args.player_template,
                dxc=args.dxc,
                cache_dir=args.cache_dir,
                preview_mode=args.preview_mode,
            )
        )
        checks.append(
            _embedded_preview_compatibility(
                player_template=args.player_template,
                dxc=args.dxc,
                cache_dir=args.cache_dir,
                ticks=ticks,
                allow_test_payloads=args.allow_test_payloads,
                enabled=args.embedded_preview,
            )
        )

    real_preview = next(
        (item for item in checks if item["name"] == "real_preview"), None
    )
    hardware_evidence = bool(
        real_preview
        and real_preview["status"] == "passed"
        and args.execution_kind in {"physical", "documented_gpu_passthrough"}
        and (
            args.execution_kind == "physical"
            or bool(args.passthrough_documentation)
        )
    )
    statuses = [item["status"] for item in checks]
    if "failed" in statuses:
        status = "failed"
    elif "pending_unverified" in statuses:
        status = "pending_unverified"
    elif real_preview and real_preview["status"] == "passed" and not hardware_evidence:
        status = "pending_unverified"
    else:
        status = "passed"
    report = {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "task": "NW-08",
        "status": status,
        "hardware_evidence": hardware_evidence,
        "execution_kind": args.execution_kind,
        "passthrough_documentation": args.passthrough_documentation,
        "checks": checks,
    }
    return _write_report(
        report, Path(args.report), 0 if status == "passed" else 2 if status == "pending_unverified" else 1
    )


def _write_report(report: dict[str, Any], path: Path, exit_code: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
