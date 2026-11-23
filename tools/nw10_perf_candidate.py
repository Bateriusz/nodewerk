#!/usr/bin/env python3
"""Build the NW-10 matched-performance "after" common artifact.

This is deliberately evidence-only tooling.  It retains the exact frozen NW-07
common graph/profile/quality/toolchain path while allowing the generated
executable to be retained when it exceeds NW-07's 256 KiB proof ceiling.
The ceiling miss is reported, never relabelled as a NW-07 proof PASS.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import dependency_check  # noqa: E402
import nw05_export  # noqa: E402
import nw07_contract  # noqa: E402
import nw07_export  # noqa: E402
import nw10_contract  # noqa: E402
import nwp_package as nwp  # noqa: E402


FORMAT = "nw10-performance-preservation-candidate"
VERSION = 1


def _require_empty(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"output directory is not empty: {path}")


def _release_template_configuration(template: Path) -> dict[str, Any]:
    config = nw05_export._template_build_configuration(template)
    if str(config.get("NW10_COMPACT_PACKAGE", "")).strip().upper() not in {
        "ON",
        "1",
        "TRUE",
    }:
        raise ValueError(
            "NW-10 preservation candidate requires a release template built "
            "with -DNW10_COMPACT_PACKAGE=ON"
        )
    frozen = nw07_export.load_canonical_config()
    nw07_export._validate_release_template_build_configuration(
        template, frozen, repo_root=REPO_ROOT
    )
    return config


def build_candidate(
    output: Path,
    *,
    player_template: Path,
    dxc_executable: str,
    report_path: Path | None = None,
    allow_test_payloads: bool = False,
) -> dict[str, Any]:
    _require_empty(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    frozen = nw07_export.load_canonical_config()
    graph = nw07_export.verify_canonical_graph(frozen, repo_root=REPO_ROOT)
    pinned = nw07_export.verify_pinned_inputs(frozen, repo_root=REPO_ROOT)
    if not pinned["matches_frozen_digest"]:
        raise ValueError("NW-07 pinned build inputs no longer match the frozen digest")

    template_config: dict[str, Any] = {}
    if not allow_test_payloads:
        template_config = _release_template_configuration(player_template)

    staging_root = Path(
        tempfile.mkdtemp(prefix=".nw10-perf-after-", dir=str(output.parent))
    )
    artifact = staging_root / "artifact"
    build_root = staging_root / "specialized-build"
    try:
        settings = nw05_export.ExportSettings(
            byte_ceiling=nw10_contract.STANDARD_TIER_BYTES,
            memory_budget=nw07_contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
            target_profile=nw07_contract.PROFILE_ID,
            requirements_digest=nw07_contract.REQUIREMENTS_DIGEST,
            frames_in_flight=2,
            player_template=str(player_template),
            specialized_build_root=str(build_root),
            present_sync_interval=0,
            frozen_embedded_specialization=True,
            include_optional_artifacts=False,
            allow_optional_profiles=False,
        )
        nw05_export.export_graph(
            REPO_ROOT / str(frozen["graph"]),
            artifact,
            settings=settings,
            dxc_executable=dxc_executable,
            optimization="O2",
            allow_test_payloads=allow_test_payloads,
        )
        identity = nw07_export.inspect_artifact_directory(artifact, config=frozen)
        if identity["has_readback_resource"]:
            raise ValueError("common artifact unexpectedly contains a readback resource")
        package = nwp.decode_package(
            (artifact / f"{nw05_export._profile_file_stem(nw07_contract.PROFILE_ID)}.nwp").read_bytes(),
            allow_dev=False,
        )
        expected_ops = {
            "init.dispatch": 5,
            "frame.execute_indirect": 1,
            "frame.dispatch": 2,
        }
        for opcode, expected in expected_ops.items():
            observed = (
                identity["init_opcodes"].count(opcode)
                if opcode.startswith("init.")
                else identity["frame_opcodes"].count(opcode)
            )
            if observed != expected:
                raise ValueError(
                    f"common artifact expected {expected} {opcode!r}, observed {observed}"
                )
        if identity["frame_opcodes"][-1:] != ["frame.present"]:
            raise ValueError("common artifact frame stream does not end in present")
        if any(item.usage == "readback" for item in package.resources):
            raise ValueError("common package contains CPU-observable readback")

        pe_closure: dict[str, Any] | None = None
        runtime_probe: dict[str, Any] = {
            "status": "not_executed_test_payload" if allow_test_payloads else "pending"
        }
        stem = nw05_export._profile_file_stem(nw07_contract.PROFILE_ID)
        executable = artifact / f"{stem}.exe"
        if not allow_test_payloads:
            imports = dependency_check.parse_pe64_sections_and_imports(
                executable.read_bytes()
            ).get("imports", [])
            names = sorted(
                {str(name).lower().removesuffix(".dll") for name in imports if name}
            )
            declared = set(identity["dependency_closure"]["dependencies"])
            if set(names) != declared:
                raise ValueError(
                    "candidate PE imports do not match generated manifest: "
                    f"actual={names}, declared={sorted(declared)}"
                )
            forbidden = sorted(
                name
                for name in names
                if any(
                    token in name
                    for token in nw07_export.FORBIDDEN_RUNTIME_DEPENDENCIES
                )
            )
            if forbidden:
                raise ValueError(f"candidate imports forbidden dependencies: {forbidden}")
            pe_closure = {"imports": names, "matches_manifest": True}
            if os.name != "nt":
                raise ValueError("physical NW-10 preservation candidate must be built on Windows")
            probe = subprocess.run(
                [str(executable), "--frames", "2"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            runtime_probe = {
                "status": "passed" if probe.returncode == 0 else "failed",
                "exit_code": probe.returncode,
                "uncapped_present_verified": (
                    "NW05_PRESENT_SYNC_INTERVAL=0" in probe.stdout
                ),
            }
            if (
                probe.returncode != 0
                or not runtime_probe["uncapped_present_verified"]
            ):
                raise ValueError(
                    "after common artifact failed runtime/uncapped presentation probe"
                )

        bytes_total = int(identity["executable_bytes"])
        if bytes_total > nw10_contract.STANDARD_TIER_BYTES:
            raise ValueError("after common artifact exceeds the preserved 1 MiB standard tier")

        report = {
            "format": FORMAT,
            "version": VERSION,
            "task": nw10_contract.TASK,
            "implementation_base": nw10_contract.IMPLEMENTATION_BASE,
            "comparison_role": "after_common_artifact",
            "performance_contract": nw10_contract.PERF_CONTRACT_ID,
            "performance_protocol": nw10_contract.PERF_PROTOCOL_ID,
            "common_graph": {
                "path": str(frozen["graph"]),
                "sha256": graph["graph_sha256"],
                "ir_sha256": graph["ir_sha256"],
            },
            "profile_id": nw07_contract.PROFILE_ID,
            "requirements_digest": nw07_contract.REQUIREMENTS_DIGEST,
            "quality": frozen["quality"],
            "build_inputs": {
                "nw07_frozen_digest": pinned["frozen_digest"],
                "observed_digest": pinned["digest"],
                "matches_frozen_digest": pinned["matches_frozen_digest"],
                "nw10_digest": nw10_contract.build_inputs_digest(
                    nw10_contract.compute_build_inputs()
                ),
            },
            "template": {
                "nw10_compact_package": template_config.get(
                    "NW10_COMPACT_PACKAGE", "test-payload"
                )
            },
            "artifact": {
                "artifact_key": identity["artifact_key"],
                "package_sha256": identity["package_sha256"],
                "manifest_sha256": identity["manifest_sha256"],
                "executable_sha256": identity["executable_sha256"],
                "complete_distribution_bytes": bytes_total,
            },
            "size_classification": {
                "nw07_proof_ceiling_bytes": nw07_contract.COMPLETE_DISTRIBUTION_CEILING_BYTES,
                "within_nw07_proof_ceiling": bool(
                    identity["size_accounting"]["within_ceiling"]
                ),
                "nw07_proof_ceiling_is_not_relaxed": True,
                "retained_for_performance_comparison_only": True,
                "standard_tier_bytes": nw10_contract.STANDARD_TIER_BYTES,
                "within_standard_tier": bytes_total <= nw10_contract.STANDARD_TIER_BYTES,
            },
            "dependency_closure": pe_closure,
            "runtime_probe": runtime_probe,
            "release_grade": not allow_test_payloads,
            "test_payload": bool(allow_test_payloads),
        }
        nw05_export._publish_directory(artifact, output)
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return report
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--player-template", type=Path, required=True)
    parser.add_argument("--dxc", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = build_candidate(
            args.output,
            player_template=args.player_template,
            dxc_executable=args.dxc,
            report_path=args.report,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"format": FORMAT, "status": "failed", "error": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "built",
                "artifact": report["artifact"],
                "size_classification": report["size_classification"],
                "runtime_probe": report["runtime_probe"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
