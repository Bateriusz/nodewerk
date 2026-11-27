#!/usr/bin/env python3
"""Measure NW-13 common/optional artifacts when a native toolchain is present.

The host CI job that has the Windows player template and pinned DXC calls this
tool.  Linux/contract jobs intentionally call it without those inputs, which
records every byte/section/import/smoke observation as unavailable instead of
inventing a pass.  The common fallback is exported independently and remains
usable when the optional path fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import nw13_evidence as evidence
import nw13_production as production
import nw05_export
import nwp_package as nwp


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _pe_imports(executable: Path) -> list[str] | None:
    reader = getattr(nw05_export, "_pe_runtime_dependencies", None)
    if reader is None:
        return None
    try:
        result = reader(executable.read_bytes())
    except Exception:
        return None
    if isinstance(result, (list, tuple)):
        return [str(item) for item in result]
    return None


def _dependency_identities(size_report: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            str(item.get("name"))
            for item in size_report.get("dependencies", [])
            if item.get("included")
        }
    )


def _measure_export(result: nw05_export.ExportResult, directory: Path) -> dict[str, Any]:
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    if not artifact.executable_name:
        raise evidence.EvidenceError("common export did not produce a standalone executable")
    executable = directory / artifact.executable_name
    package = directory / (nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID) + ".nwp")
    if not executable.is_file() or not package.is_file():
        raise evidence.EvidenceError("common export is missing its executable or package")
    package_bytes = package.read_bytes()
    size_report = artifact.size_report
    distribution = size_report["distribution"]
    observation = {
        "distribution_bytes": int(distribution["total_distributed_bytes"]),
        "shader_bytes": int(size_report["content"]["unique_shader_blob_bytes"]),
        "sections": sorted(nw05_export.section_sizes(package_bytes)),
        "imports": _pe_imports(executable) or sorted(
            {
                item["name"]
                for item in size_report.get("dependencies", [])
                if item.get("included") and item.get("kind") == "windows_inbox"
            }
        ),
        "executable_sha256": _sha256_bytes(executable.read_bytes()),
        "package_sha256": _sha256_bytes(package_bytes),
        "manifest_sha256": artifact.manifest.get("manifest_sha256"),
        "dependency_identities": _dependency_identities(size_report),
        "startup_ms": None,
        "committed_memory_bytes": None,
        "vram_bytes": None,
        "cpu_submit_us": None,
        "gpu_time_us": None,
        "image_max_abs_error": None,
        "image_rmse": None,
    }
    observation["smoke"] = evidence.smoke_observation(
        str(executable),
        ["--frames", "1", "--allow-software-adapter", "--emit-image-hash"],
    )
    return observation


def _export(graph: Path, directory: Path, template: Path, dxc: Path) -> nw05_export.ExportResult:
    settings = nw05_export.ExportSettings(
        byte_ceiling=64 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        player_template=str(template),
    )
    return nw05_export.export_graph(
        graph,
        directory,
        settings=settings,
        dxc_executable=str(dxc),
    )


def _independent_observations(
    *, graph: Path, output_dir: Path, template: Path, dxc: Path
) -> tuple[dict[str, Any], bool]:
    first_dir = output_dir / "independent-a"
    second_dir = output_dir / "independent-b"
    first = _export(graph, first_dir, template, dxc)
    second = _export(graph, second_dir, template, dxc)
    first_artifact = first.artifacts[nwp.COMMON_PROFILE_ID]
    second_artifact = second.artifacts[nwp.COMMON_PROFILE_ID]
    identical = (
        first_artifact.artifact_key == second_artifact.artifact_key
        and first_artifact.package_bytes == second_artifact.package_bytes
        and first_artifact.executable_bytes == second_artifact.executable_bytes
        and first_artifact.manifest.get("manifest_sha256")
        == second_artifact.manifest.get("manifest_sha256")
    )
    return _measure_export(first, first_dir), identical


def write_evidence(
    *,
    repo_root: Path,
    production_path: Path,
    output_dir: Path,
    template: Path | None = None,
    dxc: Path | None = None,
) -> dict[str, Any]:
    document = production.load_production(repo_root, production_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    common_observation = None
    independent_builds = False
    if template is not None and dxc is not None:
        if os.name != "nt":
            raise evidence.EvidenceError("native artifact measurement requires Windows")
        common_observation, independent_builds = _independent_observations(
            graph=repo_root / document["graph"],
            output_dir=output_dir,
            template=template,
            dxc=dxc,
        )
    bundle = evidence.export_artifacts(
        document,
        capabilities=document.get("capabilities", {}),
        requested_features=document.get("optional_requests", []),
        common_observation=common_observation,
        independent_builds=independent_builds,
    )
    diagnostics = evidence.validate_bundle(bundle)
    if diagnostics:
        raise evidence.EvidenceError("measured artifact bundle is invalid", diagnostics)
    (output_dir / "artifact-bundle.v1.json").write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    observation = common_observation or {
        "startup_ms": None,
        "committed_memory_bytes": None,
        "vram_bytes": None,
        "cpu_submit_us": None,
        "gpu_time_us": None,
        "image_max_abs_error": None,
        "image_rmse": None,
    }
    ledger = evidence.matched_arms(
        revision=str(document.get("revision", "")),
        common={**bundle["common"], **observation},
        optional=({**bundle["optional"][0]} if bundle["optional"] else None),
    )
    (output_dir / "matched-arms.v1.json").write_text(
        json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"bundle": bundle, "ledger": ledger}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument(
        "--production",
        type=Path,
        default=Path("fixtures/nw13/hotel_corridor.production.json"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("build/nw13-evidence"))
    parser.add_argument("--template", type=Path)
    parser.add_argument("--dxc", type=Path)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    production_path = args.production if args.production.is_absolute() else repo_root / args.production
    output_dir = args.output_dir if args.output_dir.is_absolute() else repo_root / args.output_dir
    template = args.template if args.template is None or args.template.is_absolute() else repo_root / args.template
    dxc = args.dxc if args.dxc is None or args.dxc.is_absolute() else repo_root / args.dxc
    result = write_evidence(
        repo_root=repo_root,
        production_path=production_path,
        output_dir=output_dir,
        template=template,
        dxc=dxc,
    )
    bundle = result["bundle"]
    print(
        json.dumps(
            {
                "common": bundle["common"]["artifact_id"],
                "common_measurement": bundle["common"]["measurement_status"],
                "independent_builds": bundle["independent_builds"],
                "smoke": bundle["common"]["smoke"]["status"],
                "optional": [entry["artifact_id"] for entry in bundle["optional"]],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
