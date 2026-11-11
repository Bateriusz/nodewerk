#!/usr/bin/env python3
"""Record NW-04 hardware evidence through the NW-01B evidence conventions.

The aggregate record always distinguishes executed results from missing
coverage.  A single successful run is recorded as execution evidence; the Ada
and Blackwell qualification entries stay `pending_unverified` until a
representative device is actually exercised with the same NW-04 player/package
artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nwp_package as nwp  # noqa: E402
import verify_nw04  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_kv(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _pending_record() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "task": "NW-04",
        "verification_status": "pending_unverified",
        "qualifications": {
            "ada": {
                "hardware_tier": "desktop_ada_representative",
                "status": "pending_unverified",
                "reason": "No representative desktop Ada execution is recorded for the NW-04 player/package artifacts.",
            },
            "blackwell": {
                "hardware_tier": "desktop_blackwell_representative",
                "status": "pending_unverified",
                "reason": "No representative desktop Blackwell execution is recorded for the NW-04 player/package artifacts.",
            },
        },
        "executions": [],
        "unavailable_telemetry": [
            "driver_version",
            "wddm_version",
            "cpu_submit_ms",
            "gpu_ms",
        ],
        "toolchain_manifest_sha256": "unavailable",
        "policy": (
            "Historical NW-01 measurements cannot certify NW-04 artifacts. "
            "Missing coverage stays pending/unverified; executed failures remain failures. "
            "Adapter names, marketing generations, evidence tiers, and PCI identifiers are metadata only."
        ),
    }


def _load_record(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return _pending_record()


def record_execution(
    fixture: Path,
    package_path: Path,
    tier: str,
    output_path: Path,
) -> int:
    package_check = verify_nw04._validate_hardware_package(package_path)
    if not package_check.passed:
        print(f"package check failed: {package_check.detail}", file=sys.stderr)
        return 1
    if not fixture.exists():
        print(f"player fixture is unavailable: {fixture}", file=sys.stderr)
        return 1
    command = [
        str(fixture),
        "--package",
        str(package_path),
        "--frames",
        "120",
        "--tick-start",
        "0",
        "--tick-step",
        "1",
        "--resize-sequence",
        "--emit-benchmark-metrics",
        "--validation",
        "--diagnostics",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    metrics = _parse_kv(proc.stdout)
    package = nwp.decode_package(package_path.read_bytes())
    toolchain_manifest = REPO_ROOT / "benchmarks" / "toolchain-manifest.v1.json"
    record = _load_record(output_path)
    record["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    record["verification_status"] = (
        "executed_failure" if proc.returncode != 0 else "execution_recorded"
    )
    execution: dict[str, Any] = {
        "hardware_tier": tier,
        "execution_status": "passed" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "package": {
            "path": str(package_path),
            "sha256": _sha256(package_path),
            "size_bytes": package_path.stat().st_size,
            "profile_id": package.capability.profile_id,
            "requirements_digest": package.capability.requirements_digest,
            "profile_fingerprint": package.capability.profile_fingerprint,
            "toolchain_identity": package.capability.toolchain_identity,
            "toolchain_dxc_version": package.capability.toolchain_dxc_version,
        },
        "player": {
            "path": str(fixture),
            "sha256": _sha256(fixture),
            "size_bytes": fixture.stat().st_size,
        },
        "adapter": {
            "description": metrics.get("NW_PLAYER_ADAPTER_DESCRIPTION", "unavailable"),
            "generation_metadata": metrics.get(
                "NW_PLAYER_ADAPTER_GENERATION", "metadata_only"
            ),
            "vendor_id": metrics.get("NW_PLAYER_ADAPTER_VENDOR_ID", "unavailable"),
            "device_id": metrics.get("NW_PLAYER_ADAPTER_DEVICE_ID", "unavailable"),
            "dedicated_video_memory_bytes": metrics.get(
                "NW_PLAYER_DEDICATED_VIDEO_MEMORY_BYTES", "unavailable"
            ),
        },
        "environment": {
            "windows_build": metrics.get("NW_PLAYER_WINDOWS_BUILD", "unavailable"),
            "driver_version": metrics.get("NW_PLAYER_DRIVER_VERSION", "unavailable"),
            "wddm_version": metrics.get("NW_PLAYER_WDDM_VERSION", "unavailable"),
        },
        "queried_capabilities": {
            "max_feature_level": metrics.get("NW_PLAYER_MAX_FEATURE_LEVEL"),
            "max_shader_model": metrics.get("NW_PLAYER_MAX_SHADER_MODEL"),
            "resource_binding_tier": metrics.get("NW_PLAYER_RESOURCE_BINDING_TIER"),
            "resource_heap_tier": metrics.get("NW_PLAYER_RESOURCE_HEAP_TIER"),
            "max_cbv_srv_uav_descriptors_per_heap": metrics.get(
                "NW_PLAYER_MAX_CBV_SRV_UAV_DESCRIPTORS_PER_HEAP"
            ),
            "max_sampler_descriptors_per_heap": metrics.get(
                "NW_PLAYER_MAX_SAMPLER_DESCRIPTORS_PER_HEAP"
            ),
            "max_root_signature_dword_cost": metrics.get(
                "NW_PLAYER_MAX_ROOT_SIGNATURE_DWORD_COST"
            ),
            "max_texture_2d_dimension": metrics.get(
                "NW_PLAYER_MAX_TEXTURE_2D_DIMENSION"
            ),
            "max_compute_threads_per_group": metrics.get(
                "NW_PLAYER_MAX_COMPUTE_THREADS_PER_GROUP"
            ),
            "max_buffer_bytes": metrics.get("NW_PLAYER_MAX_BUFFER_BYTES"),
            "max_swap_chain_buffers": metrics.get(
                "NW_PLAYER_MAX_SWAP_CHAIN_BUFFERS"
            ),
        },
        "memory": {
            "engine_accounted_bytes": metrics.get(
                "NW_PLAYER_ENGINE_ACCOUNTED_BYTES", "unavailable"
            ),
            "driver_process_private_bytes": metrics.get(
                "NW_PLAYER_DRIVER_PROCESS_PRIVATE_BYTES", "unavailable"
            ),
            "driver_gpu_local_bytes": metrics.get(
                "NW_PLAYER_DRIVER_GPU_LOCAL_BYTES", "unavailable"
            ),
        },
        "timing": {
            "cpu_submit_ms": metrics.get("NW_PLAYER_CPU_SUBMIT_MS", "unavailable"),
            "gpu_ms": metrics.get("NW_PLAYER_GPU_MS", "unavailable"),
        },
        "post_initialization_allocation_violation": metrics.get(
            "NW_PLAYER_POST_INIT_ALLOCATION_VIOLATION", "unavailable"
        ),
        "non_aliased_placements": metrics.get(
            "NW_PLAYER_NON_ALIASED_PLACEMENTS", "unavailable"
        ),
        "d3d12_validation_clean": proc.returncode == 0
        and "NW_D3D12_VALIDATION_ERROR" not in (proc.stdout + proc.stderr),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "command": command,
    }
    record.setdefault("executions", []).append(execution)
    record["toolchain_manifest_sha256"] = (
        _sha256(toolchain_manifest) if toolchain_manifest.exists() else "unavailable"
    )
    record["unavailable_telemetry"] = sorted(
        key
        for key, value in {
            "driver_version": execution["environment"]["driver_version"],
            "wddm_version": execution["environment"]["wddm_version"],
            "cpu_submit_ms": execution["timing"]["cpu_submit_ms"],
            "gpu_ms": execution["timing"]["gpu_ms"],
        }.items()
        if value == "unavailable"
    )
    # A run only fills the tier that was actually executed.  It never marks the
    # other representative tier as verified, and a failed run stays failed.
    if tier in record["qualifications"]:
        if proc.returncode == 0:
            record["qualifications"][tier] = {
                "hardware_tier": record["qualifications"][tier]["hardware_tier"],
                "status": "execution_recorded",
                "adapter_description": execution["adapter"]["description"],
                "generation_metadata": execution["adapter"]["generation_metadata"],
                "player_sha256": execution["player"]["sha256"],
                "package_sha256": execution["package"]["sha256"],
                "profile_id": execution["package"]["profile_id"],
                "requirements_digest": execution["package"]["requirements_digest"],
            }
        else:
            record["qualifications"][tier] = {
                "hardware_tier": record["qualifications"][tier]["hardware_tier"],
                "status": "failed",
                "exit_code": proc.returncode,
            }
    statuses = {
        entry["status"] for entry in record["qualifications"].values()
    }
    if any(
        execution.get("execution_status") != "passed"
        for execution in record.get("executions", [])
    ):
        record["verification_status"] = "executed_failure"
    if statuses == {"execution_recorded"}:
        successful = [
            execution
            for execution in record.get("executions", [])
            if execution.get("execution_status") == "passed"
        ]
        any_failed = any(
            execution.get("execution_status") != "passed"
            for execution in record.get("executions", [])
        )
        package_hashes = {
            execution["package"]["sha256"] for execution in successful
        }
        player_hashes = {
            execution["player"]["sha256"] for execution in successful
        }
        if (
            not any_failed
            and len(package_hashes) == 1
            and len(player_hashes) == 1
            and all(
                execution.get("d3d12_validation_clean") is True
                for execution in successful
            )
        ):
            record["verification_status"] = "verified"
            for entry in record["qualifications"].values():
                entry["status"] = "verified"
        else:
            record["verification_status"] = "execution_recorded"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if proc.returncode == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument(
        "--tier", choices=("ada", "blackwell"), default="ada"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "nw04-hardware-evidence.json",
    )
    parser.add_argument(
        "--write-pending",
        action="store_true",
        help="Write the explicit pending/unverified aggregate record without running.",
    )
    args = parser.parse_args()
    if args.write_pending:
        pending = _pending_record()
        toolchain_manifest = REPO_ROOT / "benchmarks" / "toolchain-manifest.v1.json"
        if toolchain_manifest.exists():
            pending["toolchain_manifest_sha256"] = _sha256(toolchain_manifest)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(pending, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    if args.fixture is None or args.package is None:
        parser.error("--fixture and --package are required unless --write-pending")
    return record_execution(args.fixture, args.package, args.tier, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
