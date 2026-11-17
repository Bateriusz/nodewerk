#!/usr/bin/env python3
"""Build/verify the transferable NW-10 correctness capture runner."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw07_contract  # noqa: E402
import nw07_export  # noqa: E402

FORMAT = "nw10-portable-correctness-transfer"
VERSION = 1
FORBIDDEN_DEBUG_RUNTIMES = (
    "msvcp140d",
    "vcruntime140d",
    "vcruntime140_1d",
    "ucrtbased",
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_probe(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("NW10_"):
            values[key] = value.strip()
    return values


def assemble(
    expected_package: Path,
    template_executable: Path,
    output_executable: Path,
    metadata_path: Path,
) -> dict[str, Any]:
    expected = nw07_export.inspect_artifact_directory(expected_package)
    stem = nw05_export._profile_file_stem(nw07_contract.PROFILE_ID)
    package_path = expected_package / f"{stem}.nwp"
    manifest_path = expected_package / f"{stem}.manifest.json"

    package_bytes = package_path.read_bytes()
    release_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    template_bytes = template_executable.read_bytes()
    dependencies = nw05_export._pe_runtime_dependencies(template_bytes)
    lowered = [item.lower() for item in dependencies]
    debug = sorted(
        item
        for item in lowered
        if any(token in item for token in FORBIDDEN_DEBUG_RUNTIMES)
    )
    if debug:
        raise ValueError(
            "portable correctness runner imports debug CRT dependencies: "
            + ", ".join(debug)
        )
    forbidden = sorted(
        item
        for item in lowered
        if any(token in item for token in nw07_export.FORBIDDEN_RUNTIME_DEPENDENCIES)
    )
    if forbidden:
        raise ValueError(
            "portable correctness runner imports forbidden dependencies: "
            + ", ".join(forbidden)
        )

    diagnostic_manifest = copy.deepcopy(release_manifest)
    retained = diagnostic_manifest.setdefault("retained", {})
    retained["dependencies"] = list(dependencies)
    diagnostic_manifest["dependencies_source"] = "portable-correctness-pe-imports"
    diagnostic_manifest["diagnostic_role"] = {
        "task": "NW-10",
        "role": "common_artifact_correctness",
        "capture_method": "PackagePlayer::readbackPresentedOutput",
        "release_executable": False,
        "portable_target": True,
    }
    nw05_export._refresh_manifest_sha256(diagnostic_manifest)
    diagnostic_manifest_bytes = optimizer.manifest_bytes(diagnostic_manifest)

    payload = nw05_export.embed_package(
        template_bytes, package_bytes, diagnostic_manifest_bytes
    )
    output_executable.parent.mkdir(parents=True, exist_ok=True)
    output_executable.write_bytes(payload)

    embedded_package, embedded_manifest = nw05_export.extract_embedded_package(payload)
    if embedded_package != package_bytes:
        raise ValueError("portable runner changed the common package")
    if embedded_manifest != diagnostic_manifest_bytes:
        raise ValueError("portable runner diagnostic manifest binding is invalid")

    required_mask = nw05_export._specialized_implementation_mask(release_manifest)
    metadata: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "task": "NW-10",
        "capture_protocol": nw07_contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "common_artifact": {
            "artifact_key": expected["artifact_key"],
            "package_sha256": expected["package_sha256"],
            "manifest_sha256": expected["manifest_sha256"],
            "profile_id": expected["profile_id"],
            "requirements_digest": expected["requirements_digest"],
            "final_executable_sha256": expected["executable_sha256"],
        },
        "portable_runner": {
            "file": output_executable.name,
            "sha256": _sha256_bytes(payload),
            "bytes": len(payload),
            "diagnostic_manifest_sha256": _sha256_bytes(
                diagnostic_manifest_bytes
            ),
            "dependencies": dependencies,
            "required_implementation_mask": required_mask,
            "capture_method": "readbackPresentedOutput",
            "debug_crt_dependencies": [],
        },
        "capture": {
            "fixed_ticks": list(nw07_contract.REQUIRED_FIXED_TICKS),
            "loop_boundary_ticks": list(nw07_contract.LOOP_BOUNDARY_TICKS),
            "ticks": list(nw07_contract.REQUIRED_FIXED_TICKS)
            + [nw07_contract.LOOP_BOUNDARY_TICKS[1]],
            "expected_bytes_per_capture": (
                nw07_contract.OUTPUT_WIDTH
                * nw07_contract.OUTPUT_HEIGHT
                * nw07_contract.CAPTURE_BYTES_PER_PIXEL
            ),
            "width": nw07_contract.OUTPUT_WIDTH,
            "height": nw07_contract.OUTPUT_HEIGHT,
            "format": nw07_contract.CAPTURE_FORMAT,
        },
        "privacy": {
            "local_paths_included": False,
            "raw_capture_bytes_embedded": False,
        },
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    if os.name == "nt":
        probe = subprocess.run(
            [
                str(output_executable),
                "--probe",
                "--required-mask",
                str(required_mask),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if probe.returncode != 0:
            raise ValueError(
                "portable correctness runner probe failed: "
                + (probe.stderr.strip() or probe.stdout.strip())
            )
        values = _parse_probe(probe.stdout)
        if values.get("NW10_PACKAGE_SHA") != expected["package_sha256"]:
            raise ValueError("portable runner runtime package SHA does not match")
        observed_mask = int(values.get("NW10_RUNTIME_IMPLEMENTATION_MASK", "-1"))
        if (observed_mask & required_mask) != required_mask:
            raise ValueError("portable runner lacks required runtime implementations")
        if values.get("NW10_PRESENT_SYNC_INTERVAL") != "0":
            raise ValueError("portable runner is not uncapped")
        metadata["build_host_probe"] = {
            "status": "passed",
            "package_sha256": values["NW10_PACKAGE_SHA"],
            "runtime_implementation_mask": observed_mask,
            "present_sync_interval": 0,
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-package", type=Path, required=True)
    parser.add_argument("--template-executable", type=Path, required=True)
    parser.add_argument("--output-executable", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = assemble(
            args.expected_package,
            args.template_executable,
            args.output_executable,
            args.metadata,
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "built",
                "package_sha256": result["common_artifact"]["package_sha256"],
                "runner_sha256": result["portable_runner"]["sha256"],
                "required_implementation_mask": result["portable_runner"][
                    "required_implementation_mask"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
