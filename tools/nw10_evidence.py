#!/usr/bin/env python3
"""Generate deterministic NW-10 contract evidence from the canonical paths.

The generator runs the repository-owned NW-07 proof export and the restricted
NW-10 intro export with structural test payloads on a GPU-less host.  It never
promotes those numbers to physical Windows evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_compact as compact  # noqa: E402
import nw10_contract as contract  # noqa: E402
import nw10_intro as intro  # noqa: E402
import nw10_proof as proof  # noqa: E402

EVIDENCE_FORMAT = "nw10-compact-evidence"
EVIDENCE_VERSION = 1


def _sha256(path: Path) -> str:
    # Evidence files are text JSON.  Hash canonical LF bytes so a Windows
    # checkout with CRLF conversion records the same identity as POSIX CI.
    return hashlib.sha256(
        path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def _sanitize_paths(value: Any, root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_paths(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_paths(item, root) for item in value]
    if isinstance(value, str):
        text = value.replace(str(root), "<nw10-temporary-root>")
        text = re.sub(r"\.nw10-(?:proof|intro|evidence|export)-[A-Za-z0-9_]+",
                      ".nw10-<temporary-dir>", text)
        text = re.sub(r"nw10ev[0-9]*\.[A-Za-z0-9_]+",
                      "<nw10-temporary-root>", text)
        return text
    return value


def _write_text_lf(path: Path, text: str) -> None:
    # Keep archived evidence byte-identical across Windows and POSIX checkouts.
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def build_evidence(output_directory: Path | str) -> dict[str, Any]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("NODEWERK_NW03_TEST_DIGEST", "1")
    with tempfile.TemporaryDirectory(prefix="nw10-evidence-") as temporary:
        root = Path(temporary)
        proof_report_path = root / "proof-report.json"
        intro_report_path = root / "intro-report.json"
        proof_report = proof.export_proof(
            root / "proof-artifacts",
            contracts_only=True,
            report_path=proof_report_path,
            reproducibility=True,
        )
        intro_report = intro.export_intro(
            root / "intro-artifacts",
            contracts_only=True,
            report_path=intro_report_path,
        )
        archived_proof_report = output / "proof-export-report.v1.json"
        archived_intro_report = output / "intro-export-report.v1.json"
        _write_text_lf(
            archived_proof_report,
            json.dumps(
                _sanitize_paths(
                    json.loads(proof_report_path.read_text(encoding="utf-8")),
                    root,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        _write_text_lf(
            archived_intro_report,
            json.dumps(
                _sanitize_paths(
                    json.loads(intro_report_path.read_text(encoding="utf-8")),
                    root,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        decoder = compact.measure_host_decoder_code_bytes()
        nw10_inputs = contract.compute_build_inputs()
        toolchain_path = REPO_ROOT / "benchmarks" / "toolchain-manifest.v1.json"
        toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
        toolchain_pins = toolchain.get("pinned_dependencies", {})
        evidence = {
            "format": EVIDENCE_FORMAT,
            "version": EVIDENCE_VERSION,
            "task": contract.TASK,
            "implementation_base": contract.IMPLEMENTATION_BASE,
            "source_revision": contract.IMPLEMENTATION_BASE,
            "planning_provenance": contract.PLANNING_PROVENANCE,
            "historical_nw07_build_inputs_digest": (
                contract.HISTORICAL_NW07_BUILD_INPUTS_DIGEST
            ),
            "nw10_build_inputs_digest": contract.build_inputs_digest(nw10_inputs),
            "nw10_build_inputs": nw10_inputs,
            "toolchain": {
                "manifest": "benchmarks/toolchain-manifest.v1.json",
                "manifest_sha256": _sha256(toolchain_path),
                "pinned_dependencies": toolchain_pins,
            },
            "commands": {
                "contract_evidence": "NODEWERK_NW03_TEST_DIGEST=1 python tools/nw10_evidence.py --output-dir benchmarks/nw10",
                "pytest": "python -m pytest -q tests/test_nw10_*.py",
                "windows_proof": "python tools/nw10_proof.py --output build/nw10-proof --player-template <release-template> --dxc <pinned-dxc> --reproducibility",
                "windows_intro": "python tools/nw10_intro.py --output build/nw10-intro --player-template <release-template> --dxc <pinned-dxc>",
            },
            "evidence_status": "informational_contract_only",
            "physical_certification": "pending_physical_windows_capture",
            "reference_capture": "pending_reference_capture",
            "correctness_coverage": {
                "desktop_ada_representative": {
                    "status": "pending_unverified",
                    "artifact": "exact final compact executable",
                    "protocol": "nw07-correctness-capture.v1",
                    "packed_unpacked_visual_comparison": "pending_physical",
                },
                "desktop_blackwell_representative": {
                    "status": "pending_unverified",
                    "artifact": "exact final compact executable",
                    "protocol": "nw07-correctness-capture.v1",
                    "packed_unpacked_visual_comparison": "pending_physical",
                },
                "synthetic_substitute_forbidden": True,
            },
            "preserved_contracts": {
                "performance_contract": contract.PERF_CONTRACT_ID,
                "performance_protocol": contract.PERF_PROTOCOL_ID,
                "quality_profile": "nw07.quality.v1",
                "resolution": {"width": 1920, "height": 1080},
                "loop_ticks": contract.LOOP_TICKS,
                "frame_rate_hz": contract.FRAME_RATE_HZ,
                "numeric_image_tolerances": contract.image_tolerances(),
                "runs": contract.PERF_RUN_COUNT,
                "warmup_seconds": contract.WARMUP_SECONDS,
                "run_seconds": contract.PERF_RUN_SECONDS,
                "fresh_process_per_run": True,
                "application_pipeline_cache": False,
            },
            "tiers": contract.budget_tiers(),
            "packaging_modes": [
                {
                    "id": contract.COMPACT_CONTAINER_ID,
                    "codec": proof_report["compact_candidate"]["codec_id"],
                    "layout": proof_report["compact_candidate"]["layout_id"],
                    "status": "selected_contract_only",
                    "clean_system_checked": False,
                },
                {
                    "id": "crinkler",
                    "status": "excluded",
                    "reason": "x64 support is neither assumed nor verified; no executable packer is invoked",
                },
                {
                    "id": "zlib-bzip2-lzma-decoder",
                    "status": "excluded",
                    "reason": "no measured player decoder/dependency closure for these options",
                },
            ],
            "compact_target": {
                "bytes": contract.COMPACT_TARGET_BYTES,
                "universal_feasibility_claim": False,
                "physical_result": None,
                "classification": "continue",
            },
            "minimum_measured_artifact": {
                "bytes": min(
                    proof_report["ledgers"]["compact"][
                        "complete_runnable_distribution_bytes"
                    ],
                    intro_report["ledgers"]["compact"][
                        "complete_runnable_distribution_bytes"
                    ],
                ),
                "proof_compact_bytes": proof_report["ledgers"]["compact"][
                    "complete_runnable_distribution_bytes"
                ],
                "intro_compact_bytes": intro_report["ledgers"]["compact"][
                    "complete_runnable_distribution_bytes"
                ],
                "evidence_status": "informational_contract_only",
                "physical_result": None,
            },
            "proof": {
                "report_path": "benchmarks/nw10/proof-export-report.v1.json",
                "report_sha256": _sha256(archived_proof_report),
                "canonical_graph": contract.CANONICAL_PROOF_GRAPH,
                "canonical_config": contract.CANONICAL_PROOF_CONFIG,
                "canonical_artifact": proof_report["canonical_artifact"],
                "compact_candidate": proof_report["compact_candidate"],
                "ledgers": proof_report["ledgers"],
                "measurements": proof_report["measurements"],
                "reproducibility": {
                    "matching": (
                        proof_report.get("reproducibility", {}).get("matching")
                        if isinstance(proof_report.get("reproducibility"), dict)
                        else None
                    ),
                    "fields": (
                        proof_report.get("reproducibility", {}).get("fields")
                        if isinstance(proof_report.get("reproducibility"), dict)
                        else None
                    ),
                },
                "decoder_code": decoder,
            },
            "intro": {
                "report_path": "benchmarks/nw10/intro-export-report.v1.json",
                "report_sha256": _sha256(archived_intro_report),
                "profile": intro_report["profile"],
                "canonical_artifact": intro_report["canonical_artifact"],
                "compact_artifact": intro_report["compact_artifact"],
                "ledgers": intro_report["ledgers"],
                "measurements": intro_report["measurements"],
                "decision": intro_report["decision"],
            },
            "alternatives": {
                "parameter_packing": proof_report["measurements"]["parameter_packing"],
                "delta_references": proof_report["measurements"]["delta_references"],
                "layouts": proof_report["measurements"]["layouts"],
                "shader_specialization": proof_report["measurements"]["shaders"],
                "compression": proof_report["measurements"]["compression_candidates"],
                "linker_elimination": {
                    "status": "pending_physical_pe",
                    "reason": (
                        "Exact release-PE section and symbol elimination bytes are "
                        "not substituted with source-level estimates."
                    ),
                },
            },
            "dominant_costs": _dominant_costs(proof_report),
            "blockers": [
                {
                    "id": "physical_windows_artifact",
                    "status": "pending",
                    "reason": (
                        "The exact Windows x64 release executable, clean-system "
                        "startup, and dependency checks require the pinned Windows "
                        "toolchain and a clean target host."
                    ),
                },
                {
                    "id": "nw07_reference_capture",
                    "status": "pending",
                    "reason": (
                        "nw07_perf_ref_v1 exact reference capture and bindings are "
                        "unavailable; no substitute hardware can certify preservation."
                    ),
                },
                {
                    "id": "linker_elimination_bytes",
                    "status": "pending",
                    "reason": (
                        "Release PE section/symbol elimination costs are measured only "
                        "on the exact Windows artifact; source-level estimates are not "
                        "substituted."
                    ),
                },
            ],
            "decision": {
                "classification": "continue",
                "reason": (
                    "Contract-only exports, codec fuzzing, byte accounting, and "
                    "deterministic compact payloads are implemented. Physical Windows "
                    "x64 and reference-capture evidence remain pending."
                ),
                "custom_compressor_started": False,
                "existing_compression_selected": (
                    proof_report["compact_candidate"]["codec_id"]
                ),
                "crt_free_experiment": "not_pursued_no_measured_net_benefit",
                "universal_64k_claim": False,
            },
        }
        proof_path = output / "compact-proof.v1.json"
        intro_path = output / "intro-profile.v1.json"
        status_path = output / "evidence-status.v1.json"
        _write_text_lf(
            proof_path,
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        )
        _write_text_lf(
            intro_path,
            json.dumps(
                {
                    "format": "nw10-intro-profile",
                    "version": 1,
                    "task": contract.TASK,
                    "implementation_base": contract.IMPLEMENTATION_BASE,
                    **intro_report["profile"],
                    "artifact": intro_report["compact_artifact"],
                    "ledger": intro_report["ledgers"]["compact"],
                    "evidence_status": "informational_contract_only",
                    "physical_measurements": "pending_physical_windows_capture",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        _write_text_lf(
            status_path,
            json.dumps(
                {
                    "format": "nw10-evidence-status",
                    "version": 1,
                    "task": contract.TASK,
                    "implementation_base": contract.IMPLEMENTATION_BASE,
                    "evidence_status": "pending_physical_windows_capture",
                    "contract_evidence": "verified_by_pytest",
                    "physical_evidence": "pending",
                    "reference_capture": "pending_reference_capture",
                    "ada_correctness": "pending_unverified",
                    "blackwell_correctness": "pending_unverified",
                    "clean_system": "pending_unverified",
                    "startup": "pending_unverified",
                    "memory": "pending_unverified",
                    "frame_time": "pending_unverified",
                    "files": [
                        {
                            "path": "benchmarks/nw10/compact-proof.v1.json",
                            "sha256": _sha256(proof_path),
                        },
                        {
                            "path": "benchmarks/nw10/intro-profile.v1.json",
                            "sha256": _sha256(intro_path),
                        },
                        {
                            "path": "benchmarks/nw10/proof-export-report.v1.json",
                            "sha256": _sha256(archived_proof_report),
                        },
                        {
                            "path": "benchmarks/nw10/intro-export-report.v1.json",
                            "sha256": _sha256(archived_intro_report),
                        },
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    return evidence


def _dominant_costs(report: dict[str, Any]) -> list[dict[str, Any]]:
    canonical = report["canonical_artifact"]
    ledger = report["ledgers"]["canonical"]
    measurements = report["measurements"]
    return [
        {
            "component": "player_image",
            "bytes": ledger["components"]["player_image_bytes"],
            "status": "contract_only_template",
        },
        {
            "component": "package",
            "bytes": canonical["package_bytes"],
            "status": "contract_only_structural_dxil",
        },
        {
            "component": "shader_payload",
            "bytes": measurements["package"]["shader_bytes"],
            "status": "contract_only_structural_dxil",
        },
        {
            "component": "parameter_payload",
            "bytes": measurements["package"]["parameter_bytes"],
            "status": "measured",
        },
        {
            "component": "decoder_code",
            "bytes": report.get("decoder_code", {}).get("bytes"),
            "status": report.get("decoder_code", {}).get("status", "pending_physical_windows"),
        },
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "nw10",
    )
    args = parser.parse_args(argv)
    evidence = build_evidence(args.output_dir)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
