#!/usr/bin/env python3
"""NW-10 representative-hardware common-artifact correctness capture.

This tool deliberately does not claim NW-07 release-capture certification.
NW-10 acceptance requires common-artifact correctness coverage on representative
desktop Ada and Blackwell.  The diagnostic sibling is a separate executable
with capture controls enabled, while the package/manifest/artifact key must
match the final common artifact exactly.

Raw RGBA32F files stay outside tracked evidence.  A later cross-hardware
comparison can compare Blackwell captures against the retained Ada reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw05_export  # noqa: E402
import nw07_contract  # noqa: E402
import nw07_evidence  # noqa: E402
import nw07_export  # noqa: E402
import nwp_package as nwp  # noqa: E402

FORMAT = "nw10-common-artifact-correctness-capture"
VERSION = 1
ALL_TICKS = tuple(nw07_contract.REQUIRED_FIXED_TICKS) + (
    nw07_contract.LOOP_BOUNDARY_TICKS[1],
)
EXPECTED_RGBA32F_BYTES = (
    nw07_contract.OUTPUT_WIDTH
    * nw07_contract.OUTPUT_HEIGHT
    * nw07_contract.CAPTURE_BYTES_PER_PIXEL
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(path: Path) -> dict[str, Any]:
    return nw07_export.inspect_artifact_directory(path)


def _require_same_common_artifact(
    expected: Mapping[str, Any], observed: Mapping[str, Any]
) -> None:
    for field in ("artifact_key", "package_sha256", "manifest_sha256"):
        if expected.get(field) != observed.get(field):
            raise ValueError(
                f"diagnostic sibling changed common-artifact field {field}: "
                f"expected={expected.get(field)!r}, observed={observed.get(field)!r}"
            )


def build_diagnostic(
    output: Path,
    *,
    expected_package: Path,
    player_template: Path,
    dxc_executable: str,
    specialized_build_root: Path,
) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"diagnostic output directory is not empty: {output}")
    expected = _identity(expected_package)
    config = nw07_export.load_canonical_config()
    pinned = nw07_export.verify_pinned_inputs(config)
    if pinned.get("matches_frozen_digest") is not True:
        raise ValueError("frozen NW-07 pinned build-input digest does not match")

    settings = nw05_export.ExportSettings(
        byte_ceiling=64 * 1024 * 1024,
        memory_budget=nw07_contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
        target_profile=nw07_contract.PROFILE_ID,
        requirements_digest=nw07_contract.REQUIREMENTS_DIGEST,
        frames_in_flight=2,
        player_template=str(player_template),
        specialized_build_root=str(specialized_build_root),
        present_sync_interval=0,
        frozen_embedded_specialization=False,
        include_optional_artifacts=False,
        allow_optional_profiles=False,
    )
    nw05_export.export_graph(
        REPO_ROOT / str(config["graph"]),
        output,
        settings=settings,
        dxc_executable=dxc_executable,
        optimization="O2",
    )
    observed = _identity(output)
    _require_same_common_artifact(expected, observed)

    stem = nw05_export._profile_file_stem(nw07_contract.PROFILE_ID)
    executable = output / f"{stem}.exe"
    if not executable.is_file():
        raise ValueError("diagnostic sibling executable was not emitted")
    if observed["executable_sha256"] == expected["executable_sha256"]:
        raise ValueError("diagnostic sibling unexpectedly equals final executable")

    return {
        "format": "nw10-common-artifact-diagnostic-build",
        "version": 1,
        "status": "built",
        "common_artifact": {
            "artifact_key": observed["artifact_key"],
            "package_sha256": observed["package_sha256"],
            "manifest_sha256": observed["manifest_sha256"],
        },
        "final_executable_sha256": expected["executable_sha256"],
        "diagnostic_executable_sha256": observed["executable_sha256"],
        "diagnostic_executable_bytes": observed["executable_bytes"],
        "capture_controls_required": True,
        "capture_protocol": nw07_contract.CORRECTNESS_CAPTURE_PROTOCOL,
    }


def _parse_probe(stdout: str) -> dict[str, Any]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("NW05_"):
            values[key] = value.strip()
    required = (
        "NW05_ADAPTER",
        "NW05_DRIVER",
        "NW05_VENDOR_ID",
        "NW05_DEVICE_ID",
        "NW05_PRESENT_SYNC_INTERVAL",
    )
    missing = [key for key in required if key not in values]
    if missing:
        raise ValueError("diagnostic hardware probe omitted: " + ", ".join(missing))
    return {
        "adapter_description": values["NW05_ADAPTER"],
        "driver_version": values["NW05_DRIVER"],
        "vendor_id": int(values["NW05_VENDOR_ID"]),
        "device_id": int(values["NW05_DEVICE_ID"]),
        "present_sync_interval": int(values["NW05_PRESENT_SYNC_INTERVAL"]),
    }


def _compare_rgba32f(left: Path, right: Path) -> dict[str, float]:
    if left.stat().st_size != EXPECTED_RGBA32F_BYTES:
        raise ValueError(f"unexpected RGBA32F size: {left}")
    if right.stat().st_size != EXPECTED_RGBA32F_BYTES:
        raise ValueError(f"unexpected RGBA32F size: {right}")
    maximum_absolute = 0.0
    maximum_relative = 0.0
    with left.open("rb") as a, right.open("rb") as b:
        while True:
            left_chunk = a.read(4 * 16384)
            right_chunk = b.read(4 * 16384)
            if not left_chunk and not right_chunk:
                break
            if len(left_chunk) != len(right_chunk) or len(left_chunk) % 4:
                raise ValueError("RGBA32F capture streams have different lengths")
            left_values = struct.unpack("<" + "f" * (len(left_chunk) // 4), left_chunk)
            right_values = struct.unpack("<" + "f" * (len(right_chunk) // 4), right_chunk)
            for actual, expected in zip(left_values, right_values):
                if not math.isfinite(actual) or not math.isfinite(expected):
                    raise ValueError("RGBA32F capture contains non-finite values")
                difference = abs(actual - expected)
                relative = difference / max(1e-30, abs(expected))
                maximum_absolute = max(maximum_absolute, difference)
                maximum_relative = max(maximum_relative, relative)
    return {
        "maximum_error": maximum_absolute,
        "maximum_relative_error": maximum_relative,
    }


def capture(
    package: Path,
    *,
    expected_package: Path,
    diagnostic_executable: Path,
    capture_dir: Path,
    tier: str,
    output: Path,
) -> dict[str, Any]:
    expected = _identity(expected_package)
    observed = _identity(package)
    _require_same_common_artifact(expected, observed)
    if not diagnostic_executable.is_file():
        raise ValueError("diagnostic executable is unavailable")
    if _sha256(diagnostic_executable) != observed["executable_sha256"]:
        raise ValueError("diagnostic executable does not match diagnostic artifact")
    if observed["executable_sha256"] == expected["executable_sha256"]:
        raise ValueError("correctness capture must use a separate diagnostic sibling")

    probe = subprocess.run(
        [str(diagnostic_executable), "--frames", "1"],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if probe.returncode != 0:
        raise ValueError(
            "diagnostic hardware probe failed: "
            + (probe.stderr.strip() or probe.stdout.strip())
        )
    hardware = _parse_probe(probe.stdout)
    if hardware["present_sync_interval"] != 0:
        raise ValueError("diagnostic sibling is not uncapped")
    observed_tier = nw07_evidence.classify_hardware_tier(
        hardware["adapter_description"], device_form_factor="desktop"
    )
    if observed_tier != tier:
        raise ValueError(
            f"observed hardware tier {observed_tier!r}, expected {tier!r}"
        )

    capture_dir.mkdir(parents=True, exist_ok=True)
    command = [str(diagnostic_executable), "--capture-dir", str(capture_dir)]
    for tick in ALL_TICKS:
        command.extend(["--capture-tick", str(tick)])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            "fixed-tick diagnostic capture failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )

    captures: list[dict[str, Any]] = []
    for tick in ALL_TICKS:
        path = capture_dir / f"tick_{tick}.rgba32f"
        if not path.is_file():
            raise ValueError(f"missing fixed-tick capture: {tick}")
        if path.stat().st_size != EXPECTED_RGBA32F_BYTES:
            raise ValueError(
                f"fixed-tick capture {tick} has {path.stat().st_size} bytes; "
                f"expected {EXPECTED_RGBA32F_BYTES}"
            )
        captures.append(
            {
                "tick": tick,
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    loop = _compare_rgba32f(
        capture_dir / f"tick_{nw07_contract.LOOP_BOUNDARY_TICKS[0]}.rgba32f",
        capture_dir / f"tick_{nw07_contract.LOOP_BOUNDARY_TICKS[1]}.rgba32f",
    )
    if loop["maximum_error"] > nw07_contract.IMAGE_ABSOLUTE_TOLERANCE:
        raise ValueError(
            "loop-boundary image does not satisfy frozen NW-07 image tolerance"
        )

    record = {
        "format": FORMAT,
        "version": VERSION,
        "task": "NW-10",
        "coverage_role": "common_artifact_correctness",
        "status": "captured_reference" if tier == "ada" else "captured_candidate",
        "tier": tier,
        "execution_kind": "physical",
        "capture_protocol": nw07_contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "common_artifact": {
            "artifact_key": expected["artifact_key"],
            "package_sha256": expected["package_sha256"],
            "manifest_sha256": expected["manifest_sha256"],
            "final_executable_sha256": expected["executable_sha256"],
        },
        "diagnostic_sibling": {
            "executable_sha256": observed["executable_sha256"],
            "executable_bytes": observed["executable_bytes"],
            "separate_from_final_executable": True,
        },
        "hardware": {
            **hardware,
            "device_form_factor": "desktop",
            "classified_tier": observed_tier,
        },
        "fixed_ticks": list(nw07_contract.REQUIRED_FIXED_TICKS),
        "loop_boundary_ticks": list(nw07_contract.LOOP_BOUNDARY_TICKS),
        "captures": captures,
        "loop_boundary": {
            **loop,
            "absolute_tolerance": nw07_contract.IMAGE_ABSOLUTE_TOLERANCE,
            "relative_tolerance": nw07_contract.IMAGE_RELATIVE_TOLERANCE,
            "status": "passed",
        },
        "privacy": {
            "raw_capture_bytes_embedded": False,
            "local_paths_included": False,
            "stdout_stderr_included": False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return record


def compare(
    ada_record_path: Path,
    blackwell_record_path: Path,
    *,
    ada_capture_dir: Path,
    blackwell_capture_dir: Path,
    output: Path,
) -> dict[str, Any]:
    ada = json.loads(ada_record_path.read_text(encoding="utf-8"))
    blackwell = json.loads(blackwell_record_path.read_text(encoding="utf-8"))
    if ada.get("tier") != "ada" or blackwell.get("tier") != "blackwell":
        raise ValueError("comparison requires Ada reference and Blackwell candidate")
    if ada.get("common_artifact") != blackwell.get("common_artifact"):
        raise ValueError("Ada and Blackwell captures do not name the same common artifact")
    per_tick: list[dict[str, Any]] = []
    worst_absolute = 0.0
    worst_relative = 0.0
    for tick in nw07_contract.REQUIRED_FIXED_TICKS:
        result = _compare_rgba32f(
            ada_capture_dir / f"tick_{tick}.rgba32f",
            blackwell_capture_dir / f"tick_{tick}.rgba32f",
        )
        passed = result["maximum_error"] <= nw07_contract.IMAGE_ABSOLUTE_TOLERANCE
        per_tick.append({"tick": tick, **result, "status": "passed" if passed else "failed"})
        worst_absolute = max(worst_absolute, result["maximum_error"])
        worst_relative = max(worst_relative, result["maximum_relative_error"])
    status = (
        "verified"
        if all(row["status"] == "passed" for row in per_tick)
        else "failed"
    )
    public = {
        "format": "nw10-common-artifact-correctness",
        "version": 1,
        "task": "NW-10",
        "status": status,
        "capture_protocol": nw07_contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "common_artifact": ada["common_artifact"],
        "ada": {
            "hardware": ada["hardware"],
            "capture_manifest": ada["captures"],
            "loop_boundary": ada["loop_boundary"],
        },
        "blackwell": {
            "hardware": blackwell["hardware"],
            "capture_manifest": blackwell["captures"],
            "loop_boundary": blackwell["loop_boundary"],
        },
        "comparison": {
            "per_tick": per_tick,
            "worst_absolute_error": worst_absolute,
            "worst_relative_error": worst_relative,
            "absolute_tolerance": nw07_contract.IMAGE_ABSOLUTE_TOLERANCE,
            "relative_tolerance": nw07_contract.IMAGE_RELATIVE_TOLERANCE,
            "absolute_required": True,
        },
        "privacy": {
            "raw_capture_bytes_embedded": False,
            "local_paths_included": False,
            "stdout_stderr_included": False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(public, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-diagnostic")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--expected-package", type=Path, required=True)
    build.add_argument("--player-template", type=Path, required=True)
    build.add_argument("--dxc", required=True)
    build.add_argument("--build-root", type=Path, required=True)
    build.add_argument("--report", type=Path, required=True)

    cap = sub.add_parser("capture")
    cap.add_argument("--package", type=Path, required=True)
    cap.add_argument("--expected-package", type=Path, required=True)
    cap.add_argument("--diagnostic-executable", type=Path, required=True)
    cap.add_argument("--capture-dir", type=Path, required=True)
    cap.add_argument("--tier", choices=("ada", "blackwell"), required=True)
    cap.add_argument("--output", type=Path, required=True)

    comp = sub.add_parser("compare")
    comp.add_argument("--ada-record", type=Path, required=True)
    comp.add_argument("--blackwell-record", type=Path, required=True)
    comp.add_argument("--ada-captures", type=Path, required=True)
    comp.add_argument("--blackwell-captures", type=Path, required=True)
    comp.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "build-diagnostic":
            report = build_diagnostic(
                args.output,
                expected_package=args.expected_package,
                player_template=args.player_template,
                dxc_executable=args.dxc,
                specialized_build_root=args.build_root,
            )
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            summary = report
        elif args.command == "capture":
            summary = capture(
                args.package,
                expected_package=args.expected_package,
                diagnostic_executable=args.diagnostic_executable,
                capture_dir=args.capture_dir,
                tier=args.tier,
                output=args.output,
            )
        else:
            summary = compare(
                args.ada_record,
                args.blackwell_record,
                ada_capture_dir=args.ada_captures,
                blackwell_capture_dir=args.blackwell_captures,
                output=args.output,
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
                "status": summary.get("status"),
                "tier": summary.get("tier"),
                "package_sha256": (
                    summary.get("common_artifact", {}).get("package_sha256")
                    if isinstance(summary.get("common_artifact"), Mapping)
                    else None
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if summary.get("status") != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
