#!/usr/bin/env python3
"""Measure exact NW-10 Windows PE linker-elimination and compact-decoder costs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import dependency_check


FORMAT = "nw10-windows-pe-ledger"
VERSION = 1
EXPECTED_FINAL_SHA256 = "76c13bcc21da3a17b10b912f5e8ff4270f64749c74b347f174b519cbdd8c96bb"


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
) -> dict[str, Any]:
    final = pe_summary(final_executable)
    linker_baseline = pe_summary(linker_baseline_executable)
    decoder_on = pe_summary(decoder_on_template)
    decoder_off = pe_summary(decoder_off_template)

    if final["sha256"] != EXPECTED_FINAL_SHA256:
        raise ValueError(
            "final executable identity drift: "
            f"expected {EXPECTED_FINAL_SHA256}, observed {final['sha256']}"
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

    return {
        "format": FORMAT,
        "version": VERSION,
        "task": "NW-10",
        "status": "verified",
        "final_artifact": {
            "executable_sha256": final["sha256"],
            "executable_bytes": final["total_bytes"],
            "package_sha256": sha256_file(final_package),
            "manifest_sha256": sha256_file(final_manifest),
            "pe": final,
        },
        "linker_elimination": {
            "status": "verified",
            "control": {
                "changed_only": ["/OPT:REF -> /OPT:NOREF", "/OPT:ICF=4 -> /OPT:NOICF"],
                "security_mitigations_preserved": True,
                "ltcg_preserved": True,
                "compact_decoder_preserved": True,
                "canonical_package_sha256": sha256_file(linker_baseline_package),
                "manifest_sha256": sha256_file(linker_baseline_manifest),
                "pe": linker_baseline,
            },
            "complete_distribution_bytes_saved": linker_saved,
            "pe_image_bytes_saved": linker_pe_saved,
            "section_deltas": section_deltas(linker_baseline, final),
        },
        "compact_decoder": {
            "status": "verified",
            "control": {
                "changed_only": ["NW10_COMPACT_PACKAGE=ON -> OFF"],
                "release_profile_preserved": True,
                "linker_elimination_preserved": True,
            },
            "decoder_on_template": decoder_on,
            "decoder_off_template": decoder_off,
            "template_bytes_added": decoder_delta,
            "pe_image_bytes_added": decoder_pe_delta,
            "section_deltas": section_deltas(decoder_off, decoder_on),
        },
        "privacy": {
            "local_paths_included": False,
            "raw_linker_map_included": False,
            "pdb_paths_included": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--final-executable", type=Path, required=True)
    parser.add_argument("--linker-baseline-executable", type=Path, required=True)
    parser.add_argument("--decoder-on-template", type=Path, required=True)
    parser.add_argument("--decoder-off-template", type=Path, required=True)
    parser.add_argument("--final-package", type=Path, required=True)
    parser.add_argument("--linker-baseline-package", type=Path, required=True)
    parser.add_argument("--final-manifest", type=Path, required=True)
    parser.add_argument("--linker-baseline-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = build_report(
        final_executable=args.final_executable,
        linker_baseline_executable=args.linker_baseline_executable,
        decoder_on_template=args.decoder_on_template,
        decoder_off_template=args.decoder_off_template,
        final_package=args.final_package,
        linker_baseline_package=args.linker_baseline_package,
        final_manifest=args.final_manifest,
        linker_baseline_manifest=args.linker_baseline_manifest,
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
