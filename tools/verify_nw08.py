#!/usr/bin/env python3
"""NW-08 verification entry point.

``ContractsOnly`` exercises the editor, cache identity, async generation,
preview retention, and release-closure contracts without claiming physical
coverage.  ``BuildChecks`` runs the real Windows/DXC GUI-versus-CLI export
equivalence when a pinned DXC and player template are supplied.  ``Full`` only
accepts a retained Windows integration report from
``tools/nw08_windows_integration.py``; it never relabels simulated execution as
hardware evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import nw07_contract  # noqa: E402
import nw08_cache as cache  # noqa: E402
import nw08_editor as editor  # noqa: E402
import nw08_preview as preview  # noqa: E402
import nw08_studio as studio  # noqa: E402
import nwp_package as nwp  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402

VERIFICATION_FORMAT = "nw08-verification"
VERIFICATION_VERSION = 1
SCHEMA_PATH = REPO_ROOT / "schemas" / "nw08-verification.schema.json"


class VerificationFailure(RuntimeError):
    pass


def _check(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    detail: Any = None,
) -> None:
    entry: dict[str, Any] = {"name": name, "status": status}
    if detail is not None:
        entry["detail"] = detail
    checks.append(entry)


def _run(name: str, checks: list[dict[str, Any]], function: Any) -> bool:
    try:
        detail = function()
    except VerificationFailure as exc:
        _check(checks, name, "failed", {"error": str(exc)})
        return False
    except Exception as exc:  # pragma: no cover - defensive verification boundary
        _check(checks, name, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        return False
    _check(checks, name, "passed", detail)
    return True


# ---------------------------------------------------------------------------
# ContractsOnly
# ---------------------------------------------------------------------------


def _profile_contract() -> dict[str, Any]:
    session = editor.GraphEditorSession.create_nw07_proof()
    if nwp.COMMON_PROFILE_ID != nw07_contract.PROFILE_ID:
        raise VerificationFailure("common capability profile changed")
    if nwp.COMMON_REQUIREMENTS_DIGEST != nw07_contract.REQUIREMENTS_DIGEST:
        raise VerificationFailure("requirements digest changed")
    return {
        "profile_id": session.document.profile_id,
        "requirements_digest": session.document.requirements_digest,
        "semantic_digest_provenance": session.semantic_digest_provenance,
    }


def _editor_semantics() -> dict[str, Any]:
    session = editor.GraphEditorSession.create_nw07_proof()
    baseline = session.semantic_digest
    session.set_layout("texture", 11.0, 12.0)
    session.select("texture", "camera")
    session.scrub(240_000)
    if session.semantic_digest != baseline:
        raise VerificationFailure("editor-only metadata changed semantic identity")
    result = session.set_parameter("texture", "seed", "1297036694")
    if not result.valid or result.semantic_digest == baseline:
        raise VerificationFailure("parameter edit did not advance semantic identity")
    expected_cached = {
        "camera",
        "instances",
        "mesh_gpu",
        "scene_gpu",
        "sun",
        "torus",
        "twist",
    }
    if not expected_cached <= set(result.cached_nodes):
        raise VerificationFailure("unaffected graph branch was recomputed")
    if "render" not in result.recomputed_nodes:
        raise VerificationFailure("dependent render pass was not recomputed")
    return result.to_dict()


def _async_and_cache_contracts() -> dict[str, Any]:
    queue = cache.AsyncJobQueue("sha256:v1:" + "a" * 64)
    first = queue.submit()
    second = queue.submit()
    if not queue.complete(second).accepted:
        raise VerificationFailure("newest completion was not accepted")
    if queue.complete(first).reason != "superseded_by_newer_job":
        raise VerificationFailure("out-of-order completion was not rejected")
    pending = queue.submit()
    queue.handle_device_loss()
    if queue.complete(pending).reason != "device_lost":
        raise VerificationFailure("completion after device loss was accepted")
    queue.reinitialize()
    if queue.complete(pending).reason != "obsolete_device_epoch":
        raise VerificationFailure("stale device-epoch completion was accepted")

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
        raise VerificationFailure("GPU cache exceeded its budget")
    if gpu.total_bytes > gpu.budget_bytes:
        raise VerificationFailure("retirement bytes escaped the GPU budget")
    gpu.release("a")
    if gpu.retiring_bytes != 600:
        raise VerificationFailure("submitted resource was destroyed before fence")
    gpu.signal_fence(10)
    if gpu.retiring_bytes != 0:
        raise VerificationFailure("retired fence did not release resource")
    return {
        "queue": queue.stats,
        "gpu": gpu.stats,
        "total_bytes": gpu.total_bytes,
    }


def _preview_recovery() -> dict[str, Any]:
    session = editor.GraphEditorSession.open(
        REPO_ROOT / "fixtures" / "nw08" / "shader_edit_probe.nwgraph"
    )
    pipeline = preview.PreviewPipeline(session, gpu_budget_bytes=256 * 1024 * 1024)
    if not pipeline.preview_now(tick=0).accepted:
        raise VerificationFailure("initial contract preview failed")
    previous = pipeline.last_valid_preview
    invalid = pipeline.session.set_parameter("shader", "expression", "vec4(sin(")
    pipeline.apply_edit(invalid)
    if invalid.valid:
        raise VerificationFailure("broken shader edit was accepted")
    if pipeline.last_valid_preview is None:
        raise VerificationFailure("broken shader edit replaced the valid preview")
    if not pipeline.last_diagnostics:
        raise VerificationFailure("broken shader edit produced no diagnostic")
    source = pipeline.last_diagnostics[0].get("source") or {}
    if source.get("node_id") != "shader" or not source.get("line"):
        raise VerificationFailure("diagnostic is not source-linked")
    valid = pipeline.session.set_parameter(
        "shader",
        "expression",
        "vec4(mix(uv.x, sin(uv.y * 5.0 + time), 0.5), 0.25, 0.5, 1.0)",
    )
    pipeline.apply_edit(valid)
    if not valid.valid or not pipeline.preview_now(tick=0).accepted:
        raise VerificationFailure("valid edit did not recover the preview")
    return {
        "previous_image": previous.frame.image_sha256 if previous else "",
        "recovered_image": pipeline.last_valid_frame.image_sha256,
        "diagnostic": pipeline.last_diagnostics,
    }


def _preview_incremental_cache() -> dict[str, Any]:
    session = editor.GraphEditorSession.create_nw07_proof()
    pipeline = preview.PreviewPipeline(session, gpu_budget_bytes=512 * 1024 * 1024)
    if not pipeline.preview_now(tick=0).accepted:
        raise VerificationFailure("initial incremental-cache preview failed")
    first = pipeline.last_valid_preview
    if first is None or not first.recomputed_nodes:
        raise VerificationFailure("first preview did not admit realizations")
    pipeline.apply_parameter_edit("texture", "seed", "1297036694")
    if not pipeline.preview_now(tick=0).accepted:
        raise VerificationFailure("second incremental-cache preview failed")
    second = pipeline.last_valid_preview
    if second is None:
        raise VerificationFailure("second preview was not installed")
    expected_cached = {
        "camera",
        "instances",
        "mesh_gpu",
        "scene_gpu",
        "sun",
        "torus",
        "twist",
    }
    if not expected_cached <= set(second.cached_nodes):
        raise VerificationFailure(
            "unchanged branches did not reuse per-node realizations"
        )
    first_keys = dict(first.node_realization_keys)
    second_keys = dict(second.node_realization_keys)
    for node_id in expected_cached:
        if first_keys.get(node_id) != second_keys.get(node_id):
            raise VerificationFailure(
                f"cached node '{node_id}' changed its realization key"
            )
    return {
        "cached_nodes": list(second.cached_nodes),
        "recomputed_nodes": list(second.recomputed_nodes),
        "cache_stats": pipeline.cache.stats,
    }


def _key_identity() -> dict[str, Any]:
    semantic = "sha256:v1:" + "a" * 64
    base = cache.CacheKeyInputs(semantic_digest=semantic)
    marketing_a = {"gpu_model": "RTX 4060 Ti", "gpu_generation": "Ada"}
    marketing_b = {"gpu_model": "RTX 5070 Ti", "gpu_generation": "Blackwell"}
    if (
        cache.CacheKeyInputs(
            semantic_digest=semantic, observed_hardware=marketing_a
        ).compiler_key().key
        != cache.CacheKeyInputs(
            semantic_digest=semantic, observed_hardware=marketing_b
        ).compiler_key().key
    ):
        raise VerificationFailure("GPU marketing name affected a compiler key")
    device_a = cache.DeviceGeneration(
        epoch=0,
        adapter=cache.AdapterDeviceIdentity(0x10DE, 0x2805, 0, "luid-a"),
    )
    device_b = cache.DeviceGeneration(
        epoch=0,
        adapter=cache.AdapterDeviceIdentity(0x10DE, 0x2805, 0, "luid-b"),
    )
    if (
        base.device_realization_key(device_a).key
        == base.device_realization_key(device_b).key
    ):
        raise VerificationFailure("actual adapter identity did not affect device key")
    if (
        base.compiler_key().key
        != cache.CacheKeyInputs(
            semantic_digest=semantic,
        ).compiler_key().key
    ):
        raise VerificationFailure("compiler key is not deterministic")
    return {"compiler_key": base.compiler_key().key}


def _release_inputs() -> dict[str, Any]:
    import nw07_export

    result = nw07_export.verify_pinned_inputs(nw07_export.load_canonical_config())
    if not result["matches_frozen_digest"]:
        raise VerificationFailure("frozen NW-07 build-input digest changed")
    if any(path.startswith("tools/nw08_") for path in nw07_contract.PINNED_BUILD_INPUTS):
        raise VerificationFailure("editor code leaked into pinned release inputs")
    return {"digest": result["digest"]}


def _report_schema() -> dict[str, Any]:
    application = studio.StudioApplication.create()
    application.preview(0)
    report = application.report()
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        validate_file_against_schema(
            path, REPO_ROOT / "schemas" / "nw08-studio-report.schema.json"
        )
    return {"hardware_evidence": report["hardware_evidence"]}


def run_contracts() -> tuple[list[dict[str, Any]], bool]:
    checks: list[dict[str, Any]] = []
    all_passed = True
    for name, function in (
        ("profile_identity", _profile_contract),
        ("editor_semantics_and_invalidation", _editor_semantics),
        ("async_cache_and_fence_retirement", _async_and_cache_contracts),
        ("preview_error_recovery", _preview_recovery),
        ("preview_incremental_cache", _preview_incremental_cache),
        ("cache_key_identity", _key_identity),
        ("frozen_release_inputs", _release_inputs),
        ("studio_report_schema", _report_schema),
    ):
        all_passed = _run(name, checks, function) and all_passed
    return checks, all_passed


# ---------------------------------------------------------------------------
# BuildChecks
# ---------------------------------------------------------------------------


def _mock_dxc(directory: Path, *, real: bool) -> Path | None:
    if real:
        return None
    import nw04_fixture

    directory.mkdir(parents=True, exist_ok=True)
    dxc = directory / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
        "fo.parent.mkdir(parents=True, exist_ok=True)\n"
        f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\n",
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    return dxc


def run_build_checks(
    *,
    player_template: str | None,
    dxc_executable: str | None,
    work_directory: Path,
) -> tuple[list[dict[str, Any]], str]:
    checks: list[dict[str, Any]] = []
    real = bool(player_template and dxc_executable)
    if not real:
        _check(
            checks,
            "gui_cli_export_equivalence",
            "pending_unverified",
            {"reason": "player template and pinned DXC are required"},
        )
        return checks, "pending_unverified"
    export_dir = work_directory / "gui"
    cli_dir = work_directory / "cli"
    session = editor.GraphEditorSession.create_nw07_proof()
    session.set_parameter("camera", "fov_degrees", "55")
    source = work_directory / "edited.nwgraph"
    session.save(source)
    gui = studio.StudioApplication.create(source).export(
        export_dir,
        settings={"player_template": player_template},
        dxc_executable=dxc_executable,
        allow_test_payloads=False,
    )
    cli = subprocess.run(
        [
            sys.executable,
            str(TOOLS_DIR / "nodewerkc.py"),
            "export",
            str(source),
            str(cli_dir),
            "--dxc",
            dxc_executable,
            "--player-template",
            player_template,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if cli.returncode != 0:
        _check(
            checks,
            "gui_cli_export_equivalence",
            "failed",
            {"stderr": cli.stderr[-4000:]},
        )
        return checks, "failed"
    stem = "nw_common_d3d12_fl12_0_sm6_0_v1"
    gui_package = (export_dir / f"{stem}.nwp").read_bytes()
    cli_package = (cli_dir / f"{stem}.nwp").read_bytes()
    gui_exe = (export_dir / f"{stem}.exe").read_bytes()
    cli_exe = (cli_dir / f"{stem}.exe").read_bytes()
    if gui_package != cli_package or gui_exe != cli_exe:
        _check(
            checks,
            "gui_cli_export_equivalence",
            "failed",
            {"reason": "GUI and CLI artifacts differ"},
        )
        return checks, "failed"
    _check(
        checks,
        "gui_cli_export_equivalence",
        "passed",
        {
            "package_sha256": hashlib.sha256(gui_package).hexdigest(),
            "executable_sha256": hashlib.sha256(gui_exe).hexdigest(),
            "package_bytes": len(gui_package),
            "executable_bytes": len(gui_exe),
        },
    )
    _check(
        checks,
        "source_linked_diagnostics_and_recovery",
        "passed",
        {"contract": "covered by ContractsOnly"},
    )
    return checks, "passed"


# ---------------------------------------------------------------------------
# Full
# ---------------------------------------------------------------------------


def run_full(evidence_path: Path) -> tuple[list[dict[str, Any]], str]:
    checks: list[dict[str, Any]] = []
    if not evidence_path.is_file():
        _check(
            checks,
            "windows_integration_evidence",
            "pending_unverified",
            {"reason": f"missing {evidence_path}"},
        )
        return checks, "pending_unverified"
    report = json.loads(evidence_path.read_text(encoding="utf-8"))
    if report.get("format") != "nw08-windows-integration":
        _check(checks, "windows_integration_evidence", "failed", "wrong format")
        return checks, "failed"
    required = {
        "gui_cli_export_equivalence",
        "gui_window_interaction",
        "shader_error_recovery",
        "device_recreation",
        "fence_safe_cache",
        "runtime_shader_error_recovery",
        "runtime_device_recreation",
        "runtime_fence_safe_cache",
        "real_preview",
    }
    statuses = {
        item["name"]: item["status"] for item in report.get("checks", [])
    }
    missing = sorted(required - set(statuses))
    if missing:
        _check(
            checks,
            "windows_integration_evidence",
            "failed",
            {"missing_checks": missing},
        )
        return checks, "failed"
    if any(status == "failed" for status in statuses.values()):
        _check(
            checks,
            "windows_integration_evidence",
            "failed",
            {"statuses": statuses},
        )
        return checks, "failed"
    if any(status == "pending_unverified" for status in statuses.values()):
        _check(
            checks,
            "windows_integration_evidence",
            "pending_unverified",
            {"statuses": statuses},
        )
        return checks, "pending_unverified"
    if statuses.get("gui_window_interaction") != "passed":
        _check(
            checks,
            "windows_integration_evidence",
            "pending_unverified",
            {
                "reason": (
                    "the real authoring GUI was not exercised successfully "
                    "(a headless contract run is not GUI evidence)"
                ),
                "statuses": statuses,
            },
        )
        return checks, "pending_unverified"
    if statuses["real_preview"] != "passed" or not report.get(
        "hardware_evidence", False
    ):
        _check(
            checks,
            "windows_integration_evidence",
            "pending_unverified",
            {
                "reason": "real physical preview execution is required",
                "statuses": statuses,
            },
        )
        return checks, "pending_unverified"
    _check(
        checks,
        "windows_integration_evidence",
        "passed",
        {"statuses": statuses},
    )
    return checks, "passed"


def build_summary(
    mode: str,
    checks: Sequence[Mapping[str, Any]],
    status: str,
    *,
    hardware_evidence: bool = False,
) -> dict[str, Any]:
    return {
        "format": VERIFICATION_FORMAT,
        "version": VERIFICATION_VERSION,
        "task": "NW-08",
        "mode": mode,
        "status": status,
        "success": status == "passed",
        "hardware_evidence": hardware_evidence,
        "checks": list(checks),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["ContractsOnly", "BuildChecks", "Full"],
        default="ContractsOnly",
    )
    parser.add_argument("--player-template")
    parser.add_argument("--dxc")
    parser.add_argument("--evidence", default="build/nw08-windows-integration.json")
    parser.add_argument("--work-directory", default=None)
    parser.add_argument("--summary-json", default=None)
    args = parser.parse_args(argv)

    if args.mode in {"ContractsOnly", "BuildChecks"}:
        checks, passed = run_contracts()
    else:
        checks = []
        passed = True
    hardware_evidence = False
    if args.mode == "ContractsOnly":
        status = "passed" if passed else "failed"
    elif args.mode == "BuildChecks":
        work = Path(
            args.work_directory
            or (REPO_ROOT / "build" / "nw08-build-checks")
        )
        work.mkdir(parents=True, exist_ok=True)
        build_checks, build_status = run_build_checks(
            player_template=args.player_template,
            dxc_executable=args.dxc,
            work_directory=work,
        )
        checks.extend(build_checks)
        hard_failed = any(item["status"] == "failed" for item in checks) or not passed
        status = (
            "failed"
            if hard_failed
            else "pending_unverified"
            if build_status == "pending_unverified"
            else "passed"
        )
    else:
        full_checks, full_status = run_full(Path(args.evidence))
        checks.extend(full_checks)
        status = full_status
        if status == "passed" and Path(args.evidence).is_file():
            hardware_evidence = bool(
                json.loads(Path(args.evidence).read_text(encoding="utf-8")).get(
                    "hardware_evidence", False
                )
            )

    summary = build_summary(
        args.mode,
        checks,
        status,
        hardware_evidence=hardware_evidence,
    )
    if args.summary_json:
        path = Path(args.summary_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        validate_file_against_schema(path, SCHEMA_PATH)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if status == "failed":
        return 1
    if status == "pending_unverified":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
