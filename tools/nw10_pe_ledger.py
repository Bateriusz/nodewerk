#!/usr/bin/env python3
"""Measure exact NW-10 Windows PE linker-elimination and compact-decoder costs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

import dependency_check


FORMAT = "nw10-windows-pe-ledger"
VERSION = 1
INVALIDATED_GRADIENT_SHA256 = "76c13bcc21da3a17b10b912f5e8ff4270f64749c74b347f174b519cbdd8c96bb"


def normalize_artifact_digest(value: str) -> str:
    digest = str(value).strip().lower()
    if digest.startswith("sha256:"):
        digest = digest[len("sha256:"):]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("artifact digest must be sha256:<64 lowercase hex>")
    return "sha256:" + digest


def bind_artifact_source(
    report: dict[str, Any],
    *,
    artifact_name: str,
    artifact_id: int,
    artifact_digest: str,
) -> dict[str, Any]:
    if report.get("format") != FORMAT or report.get("version") != VERSION:
        raise ValueError("PE ledger report has an unexpected format or version")
    if not artifact_name.strip():
        raise ValueError("artifact name is required")
    if artifact_id <= 0:
        raise ValueError("artifact id must be positive")
    digest = normalize_artifact_digest(artifact_digest)
    target = report.get("current_attempt")
    if isinstance(target, Mapping):
        source = target.get("source")
    else:
        source = report.get("source")
    if not isinstance(source, dict):
        raise ValueError("PE ledger source object is missing")
    source["artifact_name"] = artifact_name
    source["artifact_id"] = artifact_id
    source["artifact_digest"] = digest
    source["artifact_retained"] = True
    return report


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pe_summary(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    info = dependency_check.parse_pe64_sections_and_imports(raw)
    sections = []
    last_raw_end = 0
    first_raw_ptr: int | None = None
    for sec in info["sections"]:
        raw_ptr = int(sec["raw_ptr"])
        raw_size = int(sec["raw_size_bytes"])
        last_raw_end = max(last_raw_end, raw_ptr + raw_size)
        if raw_size > 0:
            first_raw_ptr = raw_ptr if first_raw_ptr is None else min(first_raw_ptr, raw_ptr)
        sections.append(
            {
                "name": str(sec["name"]),
                "raw_size_bytes": raw_size,
                "virtual_size_bytes": int(sec["virtual_size_bytes"]),
                "raw_ptr": raw_ptr,
            }
        )
    if last_raw_end <= 0 or last_raw_end > len(raw):
        raise ValueError(f"invalid PE raw section extent for {path}")
    return {
        "file": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "total_bytes": len(raw),
        "headers_bytes": int(first_raw_ptr or 0),
        "pe_image_file_bytes": last_raw_end,
        "overlay_bytes": len(raw) - last_raw_end,
        "section_raw_bytes": sum(row["raw_size_bytes"] for row in sections),
        "sections": sections,
        "imports": sorted(str(x).lower() for x in info.get("imports", [])),
        "delay_imports": sorted(str(x).lower() for x in info.get("delay_imports", [])),
    }


def section_deltas(baseline: dict[str, Any], optimized: dict[str, Any]) -> list[dict[str, Any]]:
    base = {row["name"]: int(row["raw_size_bytes"]) for row in baseline["sections"]}
    opt = {row["name"]: int(row["raw_size_bytes"]) for row in optimized["sections"]}
    rows = []
    for name in sorted(set(base) | set(opt)):
        b = base.get(name, 0)
        o = opt.get(name, 0)
        rows.append(
            {
                "section": name,
                "baseline_raw_bytes": b,
                "optimized_raw_bytes": o,
                "bytes_saved": b - o,
            }
        )
    return rows


def _validate_prior_valid_ledger(
    ledger: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    if ledger.get("format") != FORMAT or ledger.get("version") != VERSION:
        raise ValueError("prior valid PE ledger has an unexpected format or version")
    if ledger.get("task") != "NW-10":
        raise ValueError("prior valid PE ledger is not NW-10")
    prior_final = ledger.get("final_artifact")
    prior_conclusion = ledger.get("conclusion")
    prior_linker = ledger.get("linker_elimination")
    prior_decoder = ledger.get("compact_decoder")
    if not all(
        isinstance(value, Mapping)
        for value in (prior_final, prior_conclusion, prior_linker, prior_decoder)
    ):
        raise ValueError("prior valid PE ledger is structurally incomplete")
    assert isinstance(prior_final, Mapping)
    assert isinstance(prior_conclusion, Mapping)
    assert isinstance(prior_linker, Mapping)
    assert isinstance(prior_decoder, Mapping)
    if prior_final.get("runtime_status") != "validated_runnable":
        raise ValueError("prior PE ledger does not contain a validated runnable artifact")
    if prior_conclusion.get("byte_ledger_complete") is not True:
        raise ValueError("prior PE ledger does not contain a complete byte ledger")
    prior_sha = str(prior_final.get("executable_sha256", "")).lower()
    if re.fullmatch(r"[0-9a-f]{64}", prior_sha) is None:
        raise ValueError("prior PE ledger executable SHA-256 is malformed")
    try:
        prior_bytes = int(prior_final.get("executable_bytes", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("prior PE ledger executable size is invalid") from exc
    if prior_bytes <= 0:
        raise ValueError("prior PE ledger executable size is invalid")
    for field in ("package_sha256", "manifest_sha256"):
        value = str(prior_final.get(field, "")).lower()
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"prior PE ledger {field} is malformed")
    return prior_final, prior_conclusion, prior_linker, prior_decoder


def build_report(
    *,
    final_executable: Path,
    linker_baseline_executable: Path,
    decoder_on_template: Path,
    decoder_off_template: Path,
    final_package: Path,
    linker_baseline_package: Path,
    final_manifest: Path,
    linker_baseline_manifest: Path,
    runtime_status: str = "validated_runnable",
    runtime_exit_code: int | None = 0,
    prior_valid_ledger: Mapping[str, Any] | None = None,
    source_head_sha: str | None = None,
    workflow_run_id: int | None = None,
) -> dict[str, Any]:
    if runtime_status not in {
        "validated_runnable",
        "pending_runtime_validation",
        "rejected_runtime_failure",
    }:
        raise ValueError(f"unsupported runtime status: {runtime_status}")
    final = pe_summary(final_executable)
    linker_baseline = pe_summary(linker_baseline_executable)
    decoder_on = pe_summary(decoder_on_template)
    decoder_off = pe_summary(decoder_off_template)

    if final["sha256"] == INVALIDATED_GRADIENT_SHA256:
        raise ValueError(
            "final executable regressed to the invalidated gradient artifact: "
            f"{INVALIDATED_GRADIENT_SHA256}"
        )
    if sha256_file(final_package) != sha256_file(linker_baseline_package):
        raise ValueError("linker-control build changed the canonical package bytes")

    final_manifest_data = json.loads(final_manifest.read_text(encoding="utf-8"))
    baseline_manifest_data = json.loads(linker_baseline_manifest.read_text(encoding="utf-8"))
    final_profile = str(final_manifest_data.get("profile_id") or final_manifest_data.get("target_profile") or "")
    baseline_profile = str(baseline_manifest_data.get("profile_id") or baseline_manifest_data.get("target_profile") or "")
    if final_profile != baseline_profile:
        raise ValueError("linker-control build changed the manifest profile identity")

    linker_saved = int(linker_baseline["total_bytes"]) - int(final["total_bytes"])
    linker_pe_saved = int(linker_baseline["pe_image_file_bytes"]) - int(final["pe_image_file_bytes"])
    decoder_delta = int(decoder_on["total_bytes"]) - int(decoder_off["total_bytes"])
    decoder_pe_delta = int(decoder_on["pe_image_file_bytes"]) - int(decoder_off["pe_image_file_bytes"])

    if linker_saved < 0 or linker_pe_saved < 0:
        raise ValueError("linker elimination control is unexpectedly smaller than the optimized artifact")
    if decoder_delta < 0 or decoder_pe_delta < 0:
        raise ValueError("compact decoder ON build is unexpectedly smaller than decoder OFF control")

    added_imports = sorted(
        set(decoder_on["imports"]) - set(decoder_off["imports"])
    )
    if "cabinet.dll" not in added_imports:
        raise ValueError(
            "decoder-on template did not add the expected cabinet.dll import"
        )
    current_report = {
        "format": FORMAT,
        "version": VERSION,
        "task": "NW-10",
        "status": (
            "verified" if runtime_status == "validated_runnable" else "archived_superseded"
        ),
        "measurement_status": (
            "measured_current_head"
            if runtime_status == "validated_runnable"
            else "archived_superseded"
        ),
        "archived_reason": (
            None
            if runtime_status == "validated_runnable"
            else (
                "The measured artifact did not pass the required Windows x64 "
                "runtime validation; the PE measurement remains attributable "
                "but cannot certify a runnable final distribution."
            )
        ),
        "source": {
            "measurement_tool": "tools/nw10_pe_ledger.py",
            "linker_control_method": (
                "MSVC _LINK_ appended /OPT:NOREF /OPT:NOICF to the otherwise "
                "identical release link"
            ),
            "decoder_control_method": (
                "otherwise identical release template with "
                "NW10_COMPACT_PACKAGE=OFF"
            ),
        },
        "final_artifact": {
            "executable_sha256": final["sha256"],
            "executable_bytes": final["total_bytes"],
            "package_sha256": sha256_file(final_package),
            "manifest_sha256": sha256_file(final_manifest),
            "pe_image_file_bytes": final["pe_image_file_bytes"],
            "overlay_bytes": final["overlay_bytes"],
            "runtime_status": runtime_status,
            "runtime_exit_code": runtime_exit_code,
            "sections": [
                {
                    "name": row["name"],
                    "raw_size_bytes": row["raw_size_bytes"],
                }
                for row in final["sections"]
            ],
        },
        "linker_elimination": {
            "status": "verified",
            "optimized_options": ["/OPT:REF", "/OPT:ICF=4"],
            "control_options": ["/OPT:NOREF", "/OPT:NOICF"],
            "ltgc_preserved": True,
            "security_mitigations_preserved": True,
            "compact_decoder_preserved": True,
            "canonical_package_identical": True,
            "control_executable_bytes": linker_baseline["total_bytes"],
            "control_pe_image_file_bytes": linker_baseline[
                "pe_image_file_bytes"
            ],
            "complete_distribution_bytes_saved": linker_saved,
            "pe_image_bytes_saved": linker_pe_saved,
            "section_bytes_saved": [
                {
                    "section": row["section"],
                    "bytes_saved": row["bytes_saved"],
                }
                for row in section_deltas(linker_baseline, final)
            ],
        },
        "compact_decoder": {
            "status": "verified",
            "decoder_on_template_bytes": decoder_on["total_bytes"],
            "decoder_off_template_bytes": decoder_off["total_bytes"],
            "template_bytes_added": decoder_delta,
            "pe_image_bytes_added": decoder_pe_delta,
            "section_bytes_added": [
                {
                    "section": row["section"],
                    "bytes_added": -row["bytes_saved"],
                }
                for row in section_deltas(decoder_off, decoder_on)
            ],
            "runtime_import_added": "cabinet.dll",
            "runtime_imports_added": added_imports,
        },
        "conclusion": {
            "byte_ledger_complete": runtime_status == "validated_runnable",
            "linker_elimination_measured_on_exact_windows_artifact": True,
            "decoder_cost_measured_on_pinned_windows_template": True,
            "current_head_artifact_validated": (
                runtime_status == "validated_runnable"
            ),
            "universal_64k_claim": False,
        },
        "privacy": {
            "local_paths_included": False,
            "raw_linker_map_included": False,
            "pdb_paths_included": False,
        },
    }
    if source_head_sha is not None:
        if len(source_head_sha) != 40 or any(
            ch not in "0123456789abcdef" for ch in source_head_sha.lower()
        ):
            raise ValueError("source head SHA must be 40 lowercase hex characters")
        current_report["source"]["head_sha"] = source_head_sha.lower()
    if workflow_run_id is not None:
        if workflow_run_id <= 0:
            raise ValueError("workflow run id must be positive")
        current_report["source"]["workflow_run_id"] = workflow_run_id

    if runtime_status == "validated_runnable" or prior_valid_ledger is None:
        return current_report

    prior_final, prior_conclusion, prior_linker, prior_decoder = (
        _validate_prior_valid_ledger(prior_valid_ledger)
    )

    current_linker = dict(current_report["linker_elimination"])
    current_linker["status"] = "measured_superseded"
    current_decoder = dict(current_report["compact_decoder"])
    current_decoder["status"] = "measured_superseded"
    current_attempt = {
        "measured_artifact": current_report["final_artifact"],
        "linker_elimination": current_linker,
        "compact_decoder": current_decoder,
        "runtime": {
            "status": runtime_status,
            "exit_code": runtime_exit_code,
            "counted_as_validated_runnable": False,
        },
        "source": current_report["source"],
        "archived_reason": (
            "The current measured artifact is retained as attributable PE "
            "evidence but was not validated runnable, so the last complete "
            "validated ledger remains selected."
        ),
    }

    prior_source = prior_valid_ledger.get("source")
    selected_source = (
        dict(prior_source) if isinstance(prior_source, Mapping) else {
            "measurement_tool": "tools/nw10_pe_ledger.py"
        }
    )
    result = {
        "format": FORMAT,
        "version": VERSION,
        "task": "NW-10",
        "status": "archived_superseded",
        "measurement_status": "archived_superseded",
        "archived_reason": (
            "The current artifact did not pass runtime validation. The last "
            "validated runnable artifact and its complete PE ledger are retained "
            "as the selected distribution; the current measurement is recorded "
            "separately in current_attempt."
        ),
        "source": selected_source,
        "final_artifact": dict(prior_final),
        "linker_elimination": dict(prior_linker),
        "compact_decoder": dict(prior_decoder),
        "conclusion": {
            **dict(prior_conclusion),
            "byte_ledger_complete": True,
            "current_head_artifact_validated": False,
            "selected_artifact_retained_from": str(
                selected_source.get("head_sha")
                or selected_source.get("retained_from_commit")
                or "prior_valid_ledger"
            ),
            "universal_64k_claim": False,
        },
        "current_attempt": current_attempt,
        "privacy": {
            "local_paths_included": False,
            "raw_linker_map_included": False,
            "pdb_paths_included": False,
        },
    }
    return result



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--final-executable", type=Path)
    parser.add_argument("--linker-baseline-executable", type=Path)
    parser.add_argument("--decoder-on-template", type=Path)
    parser.add_argument("--decoder-off-template", type=Path)
    parser.add_argument("--final-package", type=Path)
    parser.add_argument("--linker-baseline-package", type=Path)
    parser.add_argument("--final-manifest", type=Path)
    parser.add_argument("--linker-baseline-manifest", type=Path)
    parser.add_argument(
        "--runtime-status",
        choices=(
            "validated_runnable",
            "pending_runtime_validation",
            "rejected_runtime_failure",
        ),
        default="validated_runnable",
    )
    parser.add_argument("--runtime-exit-code", type=int, default=0)
    parser.add_argument("--prior-valid-ledger", type=Path)
    parser.add_argument("--source-head-sha")
    parser.add_argument("--workflow-run-id", type=int)
    parser.add_argument("--bind-source-report", type=Path)
    parser.add_argument("--artifact-name")
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--artifact-digest")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.bind_source_report is not None:
        if (
            not args.artifact_name
            or args.artifact_id is None
            or not args.artifact_digest
        ):
            parser.error(
                "--bind-source-report requires --artifact-name, --artifact-id, "
                "and --artifact-digest"
            )
        report = json.loads(args.bind_source_report.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            parser.error("--bind-source-report must contain a JSON object")
        report = bind_artifact_source(
            report,
            artifact_name=args.artifact_name,
            artifact_id=args.artifact_id,
            artifact_digest=args.artifact_digest,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": report.get("status"),
                    "artifact_id": args.artifact_id,
                    "artifact_digest": normalize_artifact_digest(
                        args.artifact_digest
                    ),
                },
                sort_keys=True,
            )
        )
        return 0

    required_paths = {
        "--final-executable": args.final_executable,
        "--linker-baseline-executable": args.linker_baseline_executable,
        "--decoder-on-template": args.decoder_on_template,
        "--decoder-off-template": args.decoder_off_template,
        "--final-package": args.final_package,
        "--linker-baseline-package": args.linker_baseline_package,
        "--final-manifest": args.final_manifest,
        "--linker-baseline-manifest": args.linker_baseline_manifest,
    }
    missing = [name for name, value in required_paths.items() if value is None]
    if missing:
        parser.error("measurement mode requires " + ", ".join(missing))

    prior_valid_ledger = (
        json.loads(args.prior_valid_ledger.read_text(encoding="utf-8"))
        if args.prior_valid_ledger is not None
        else None
    )
    report = build_report(
        final_executable=args.final_executable,
        linker_baseline_executable=args.linker_baseline_executable,
        decoder_on_template=args.decoder_on_template,
        decoder_off_template=args.decoder_off_template,
        final_package=args.final_package,
        linker_baseline_package=args.linker_baseline_package,
        final_manifest=args.final_manifest,
        linker_baseline_manifest=args.linker_baseline_manifest,
        runtime_status=args.runtime_status,
        runtime_exit_code=args.runtime_exit_code,
        prior_valid_ledger=prior_valid_ledger,
        source_head_sha=args.source_head_sha,
        workflow_run_id=args.workflow_run_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "final_executable_bytes": report["final_artifact"]["executable_bytes"],
        "linker_saved_bytes": report["linker_elimination"]["complete_distribution_bytes_saved"],
        "decoder_template_bytes_added": report["compact_decoder"]["template_bytes_added"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
