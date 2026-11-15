#!/usr/bin/env python3
"""NW-07 verification entry point.

Usage:
    python tools/verify_nw07.py --mode ContractsOnly [--summary-json out.json]
    python tools/verify_nw07.py --mode BuildChecks [--player-template ... --dxc ...]
    python tools/verify_nw07.py --mode Full --fixture ... --package ... --evidence ... \
        [--build-report ... | --player-template ... --dxc ...]
    python tools/verify_nw07.py --mode CleanSystem --binary-dir ...

ContractsOnly and BuildChecks never claim hardware certification.  Full
distinguishes an executed failure from pending evidence and reports milestone
success only when every hard gate and required coverage target is verified.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw07_contract as contract  # noqa: E402
import nw07_clean_system as clean_system  # noqa: E402
import nw07_evidence as evidence  # noqa: E402
import nw07_export as export  # noqa: E402
import nw07_regression as regression  # noqa: E402
import nwp_package as nwp  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402

CANONICAL_CONFIG = REPO_ROOT / "benchmarks" / "nw07" / "canonical-production.v1.json"
PERF_REF = REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"
METRIC_BASELINES = REPO_ROOT / "benchmarks" / "nw07" / "metric-baselines.v1.json"
EVIDENCE = REPO_ROOT / "benchmarks" / "nw07" / "hardware-evidence.json"


@dataclass
class CheckResult:
    name: str
    required: bool = True
    passed: bool = False
    status: str = "failed"
    detail: str = ""
    unavailable: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "passed": self.passed,
            "status": self.status,
            "detail": self.detail,
            "unavailable": self.unavailable,
            "evidence": self.evidence,
        }


def _ok(name: str, detail: str = "", evidence: dict[str, Any] | None = None) -> CheckResult:
    return CheckResult(name=name, passed=True, status="passed", detail=detail, evidence=evidence or {})


def _fail(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, passed=False, status="failed", detail=detail)


def _pending(name: str, detail: str) -> CheckResult:
    return CheckResult(
        name=name,
        required=True,
        passed=False,
        status="pending_unverified",
        detail=detail,
        unavailable=True,
    )


def _mock_dxc(directory: Path) -> Path:
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


def check_canonical_graph() -> CheckResult:
    try:
        config = export.load_canonical_config()
        evidence_payload = export.verify_canonical_graph(config)
        if evidence_payload["loop_boundary"]["start"]["camera_angle_modulo_turn"] != (
            evidence_payload["loop_boundary"]["boundary"]["camera_angle_modulo_turn"]
        ):
            return _fail("nw07_canonical_graph", "camera loop boundary is not seamless")
        if evidence_payload["frame_count_per_loop"] != 1200:
            return _fail("nw07_canonical_graph", "20-second loop is not 1,200 frames at 60 Hz")
        return _ok("nw07_canonical_graph", evidence=evidence_payload)
    except Exception as exc:
        return _fail("nw07_canonical_graph", str(exc))


def check_frozen_contracts() -> CheckResult:
    try:
        validate_file_against_schema(CANONICAL_CONFIG, REPO_ROOT / "schemas" / "nw07-production-config.schema.json")
        validate_file_against_schema(PERF_REF, REPO_ROOT / "schemas" / "nw07-perf-ref.schema.json")
        validate_file_against_schema(
            METRIC_BASELINES, REPO_ROOT / "schemas" / "nw07-metric-baselines.schema.json"
        )
        diagnostic_path = REPO_ROOT / "benchmarks" / "nw07" / "diagnostic-overhead.v1.json"
        validate_file_against_schema(
            diagnostic_path,
            REPO_ROOT / "schemas" / "nw07-diagnostic-overhead.schema.json",
        )
        diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
        if (
            diagnostic.get("release_build_profile") != contract.RELEASE_BUILD_PROFILE
            or diagnostic.get("diagnostic_build_profile") != contract.DIAGNOSTIC_BUILD_PROFILE
            or diagnostic.get("release_contains_capture_controls") is not False
            or diagnostic.get("separate_artifact_required") is not True
        ):
            return _fail(
                "nw07_frozen_contracts",
                "diagnostic sibling-build record violates the release separation",
            )
        config = export.load_canonical_config()
        inputs = export.verify_pinned_inputs(config)
        perf_contract = json.loads(PERF_REF.read_text(encoding="utf-8"))
        validation = evidence.validate_perf_contract(perf_contract)
        if validation["status"] == "invalid":
            return _fail("nw07_frozen_contracts", "; ".join(validation["reasons"]))
        if not inputs["matches_frozen_digest"]:
            return _fail(
                "nw07_frozen_contracts",
                "pinned build inputs do not match the frozen build-input digest",
            )
        baselines = json.loads(METRIC_BASELINES.read_text(encoding="utf-8"))
        tolerances = baselines["image_tolerances"]
        if tolerances["numeric"]["absolute"] != contract.NUMERIC_ABSOLUTE_TOLERANCE:
            return _fail("nw07_frozen_contracts", "numeric absolute tolerance changed")
        if tolerances["image"]["absolute"] != contract.IMAGE_ABSOLUTE_TOLERANCE:
            return _fail("nw07_frozen_contracts", "image absolute tolerance changed")
        if tolerances["resolution_requirements"]["proof"] != {
            "width": 1920,
            "height": 1080,
        }:
            return _fail("nw07_frozen_contracts", "proof resolution changed")
        return _ok(
            "nw07_frozen_contracts",
            evidence={
                "build_inputs_digest": inputs["digest"],
                "frozen_build_inputs": inputs["frozen"],
                "perf_contract_status": validation["status"],
                "pending_reference_bindings": validation["missing_bindings"],
            },
        )
    except Exception as exc:
        return _fail("nw07_frozen_contracts", str(exc))


def _synthetic_export(root: Path) -> dict[str, Any]:
    dxc = _mock_dxc(root / "toolchain")
    template = root / "template.exe"
    template.write_bytes(b"MZ" + b"\x00" * 256)
    return export.export_reproducibility(
        root / "repro",
        player_template=template,
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )


def check_export_and_size() -> CheckResult:
    try:
        with tempfile.TemporaryDirectory(prefix="nw07-contracts-") as temporary:
            root = Path(temporary)
            reproducibility = _synthetic_export(root)
            if not reproducibility["matching"]:
                return _fail("nw07_export_size", "clean-directory exports are not reproducible")
            report_path = root / "repro" / "clean-a-report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            validate_file_against_schema(report_path, REPO_ROOT / "schemas" / "nw07-export-report.schema.json")
            distribution = report["distribution"]
            size = report["artifact"]["size_accounting"]
            if distribution["complete_distribution_bytes"] > contract.COMPLETE_DISTRIBUTION_CEILING_BYTES:
                return _fail("nw07_export_size", "complete distribution exceeds 262,144 bytes")
            if not size["all_required_bytes_accounted"]:
                return _fail("nw07_export_size", "distribution byte accounting is incomplete")
            if report["artifact"]["init_opcodes"].count("init.dispatch") != 5:
                return _fail("nw07_export_size", "expected five GPU initialization generators")
            if report["artifact"]["frame_opcodes"].count("frame.execute_indirect") != 1:
                return _fail("nw07_export_size", "expected exactly one indirect draw")
            if report["artifact"]["frame_opcodes"].count("frame.dispatch") != 2:
                return _fail("nw07_export_size", "expected bloom and tonemap dispatches")
            if report["artifact"]["frame_opcodes"][-1] != "frame.present":
                return _fail("nw07_export_size", "frame stream does not present")
            if report["artifact"]["has_readback_resource"]:
                return _fail("nw07_export_size", "distribution contains a readback resource")
            if report["artifact"]["dependency_closure"]["runtime_dxc_dependency"]:
                return _fail("nw07_export_size", "distribution requires runtime DXC")
            return _ok(
                "nw07_export_size",
                evidence={
                    "complete_distribution_bytes": distribution["complete_distribution_bytes"],
                    "ceiling": distribution["ceiling"],
                    "within_stretch": distribution["within_stretch"],
                    "stretch_ceiling": distribution["stretch_ceiling"],
                    "stretch_mandatory_gate": distribution["stretch_mandatory_gate"],
                    "package_sha256": reproducibility["fields"]["package_sha256"],
                    "executable_sha256": reproducibility["fields"]["executable_sha256"],
                    "manifest_sha256": reproducibility["fields"]["manifest_sha256"],
                    "build_inputs_digest": reproducibility["build_inputs_digest"],
                    "test_payload": True,
                },
            )
    except Exception as exc:
        return _fail("nw07_export_size", str(exc))


def _sample_list(count: int, value: float, jitter: float) -> list[float]:
    return [value + (index % 7) * jitter for index in range(count)]


def _run_record(
    index: int,
    *,
    gpu_values: list[float],
    cpu_values: list[float],
    executable_sha256: str,
) -> dict[str, Any]:
    base = datetime(2026, 12, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    timing = {
        "run_started_utc": base.isoformat(),
        "run_ended_utc": (
            base + timedelta(seconds=contract.PERF_RUN_SECONDS)
        ).isoformat(),
        "uncapped": True,
        "fresh_process": True,
        "application_pipeline_cache": False,
        "player_pid": 1000 + index,
        "artifact_sha256": executable_sha256,
        "collector_protocol": contract.PERF_COLLECTION_PROTOCOL,
        "collector_exit_code": 0,
        "player_exited_early": False,
    }
    return {
        "run_index": index,
        "duration_seconds": contract.PERF_RUN_SECONDS,
        "uncapped": True,
        "fresh_process": True,
        "application_pipeline_cache": False,
        "player_pid": 1000 + index,
        "artifact_sha256": executable_sha256,
        "collector_protocol": contract.PERF_COLLECTION_PROTOCOL,
        "collector_exit_code": 0,
        "player_exited_early": False,
        "timing": timing,
        "sample_density_hz": (
            min(len(gpu_values), len(cpu_values)) / contract.PERF_RUN_SECONDS
        ),
        "gpu_frame_time_ms": {
            "samples": gpu_values,
            "sample_count": len(gpu_values),
            "reported_p99_ms": evidence.nearest_rank_percentile(gpu_values),
        },
        "cpu_submit_ms": {
            "samples": cpu_values,
            "sample_count": len(cpu_values),
            "reported_p99_ms": evidence.nearest_rank_percentile(cpu_values),
        },
        "displayed_frame_intervals_ms": {
            "samples": _sample_list(60, 16.6, 0.01),
            "gate": False,
        },
    }


def _synthetic_artifact_identity() -> dict[str, Any]:
    return {
        "artifact_key": "sha256:v1:" + "a" * 64,
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "executable_sha256": "3" * 64,
        "executable_bytes": 120000,
        "link_manifest_sha256": "2" * 64,
    }


def _synthetic_perf_capture(contract_record: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    reference = copy.deepcopy(contract_record["reference"])
    return {
        "capture_id": "perf-ref-run-1",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": contract.PERF_CONTRACT_ID,
        "evidence_role": contract.PERFORMANCE_CERTIFICATION_ROLE,
        "present_sync_interval": 0,
        "presentation_probe": {
            "exit_code": 0,
            "present_sync_interval_line": "NW05_PRESENT_SYNC_INTERVAL=0",
            "stdout": "NW05_PRESENT_SYNC_INTERVAL=0\n",
        },
        "contract_id": contract.PERF_CONTRACT_ID,
        "execution_environment": {"kind": "physical"},
        "reference": reference,
        "reference_binding_digest": contract_record.get("binding_digest"),
        "reference_binding_id": contract_record.get("binding_id"),
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "executable_bytes": identity["executable_bytes"],
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "tools": copy.deepcopy(contract_record["tools"]),
        "protocol": {
            "protocol_id": contract.PERF_RUN_PROTOCOL,
            "warmup_seconds": contract.WARMUP_SECONDS,
            "runs": contract.PERF_RUN_COUNT,
            "run_seconds": contract.PERF_RUN_SECONDS,
            "uncapped": True,
            "fresh_process_per_run": True,
            "application_pipeline_cache": False,
        },
        "warmup": {
            "warmup_started_utc": "2026-12-01T00:00:00+00:00",
            "warmup_ended_utc": "2026-12-01T00:00:05+00:00",
            "duration_seconds": contract.WARMUP_SECONDS,
            "player_pid": 999,
            "artifact_sha256": identity["executable_sha256"],
            "completed": True,
        },
        "runs": [
            _run_record(
                index,
                gpu_values=_sample_list(600, 10.0, 0.001),
                cpu_values=_sample_list(600, 6.0, 0.001),
                executable_sha256=identity["executable_sha256"],
            )
            for index in range(1, 4)
        ],
    }


def check_perf_reference_rules() -> CheckResult:
    try:
        contract_record = json.loads(PERF_REF.read_text(encoding="utf-8"))
        validation = evidence.validate_perf_contract(contract_record)
        if validation["status"] == "invalid":
            return _fail("nw07_perf_reference", "; ".join(validation["reasons"]))
        identity = _synthetic_artifact_identity()
        bound = evidence.fully_bound_reference(
            contract_record,
            artifact_identity=identity,
            bound_at_utc="2026-11-30T00:00:00+00:00",
        )
        capture = _synthetic_perf_capture(bound, identity)
        verified = evidence.validate_perf_capture(
            capture, bound, artifact_identity=identity
        )
        if verified["status"] != "verified":
            return _fail(
                "nw07_perf_reference",
                "a fully matching three-run record was not verified: "
                + json.dumps(verified.get("reasons"))
                + " runs="
                + json.dumps(verified.get("runs")),
            )

        # Strict operator: exactly 16.67 ms must fail.
        boundary = copy.deepcopy(capture)
        boundary["runs"][1]["gpu_frame_time_ms"]["samples"] = [16.67] * 600
        boundary["runs"][1]["gpu_frame_time_ms"]["reported_p99_ms"] = 16.67
        boundary_result = evidence.validate_perf_capture(
            boundary, bound, artifact_identity=identity
        )
        if boundary_result["status"] != "failed":
            return _fail("nw07_perf_reference", "exactly 16.67 ms was not rejected")

        # A single failed run cannot be concealed by an aggregate.
        failed_run = copy.deepcopy(capture)
        failed_run["runs"][2]["cpu_submit_ms"]["samples"] = [16.68] * 600
        failed_run["runs"][2]["cpu_submit_ms"]["reported_p99_ms"] = 16.68
        failed_run["aggregate_p99_cpu_submit_ms"] = 6.0
        if evidence.validate_perf_capture(
            failed_run, bound, artifact_identity=identity
        )["status"] != "failed":
            return _fail("nw07_perf_reference", "aggregation concealed a failed run")

        # Wrong board, wrong host, stale artifact and wrong protocol cannot certify.
        cases: dict[str, dict[str, Any]] = {}
        other_ada = copy.deepcopy(capture)
        other_ada["reference"]["board"]["model"] = "NVIDIA GeForce RTX 4070"
        cases["other_ada"] = other_ada
        blackwell = copy.deepcopy(capture)
        blackwell["reference"]["board"]["model"] = "NVIDIA GeForce RTX 5070 Ti"
        cases["blackwell"] = blackwell
        flagship = copy.deepcopy(capture)
        flagship["reference"]["board"]["model"] = "NVIDIA GeForce RTX 4090"
        cases["higher_tier"] = flagship
        stale = copy.deepcopy(capture)
        stale["executable_sha256"] = "9" * 64
        cases["stale_artifact"] = stale
        wrong_host = copy.deepcopy(capture)
        wrong_host["reference"]["host"]["cpu_model"] = "Intel Core i9-14900K"
        cases["wrong_host"] = wrong_host
        wrong_driver = copy.deepcopy(capture)
        wrong_driver["reference"]["environment"]["driver_version"] = "999.99"
        cases["wrong_driver"] = wrong_driver
        wrong_protocol = copy.deepcopy(capture)
        wrong_protocol["protocol"]["warmup_seconds"] = 4
        wrong_protocol["protocol"]["runs"] = 2
        cases["wrong_protocol"] = wrong_protocol
        legacy_label = copy.deepcopy(capture)
        legacy_label["reference"]["board"]["tier_label"] = "rtx_40_series_legacy"
        cases["legacy_tier_label"] = legacy_label
        missing_provenance = copy.deepcopy(capture)
        del missing_provenance["reference"]["toolchain"]["toolchain_identity"]
        cases["incomplete_provenance"] = missing_provenance
        for label, candidate in cases.items():
            status = evidence.validate_perf_capture(
                candidate, bound, artifact_identity=identity
            )["status"]
            if status == "verified":
                return _fail(
                    "nw07_perf_reference", f"{label} incorrectly certified the reference"
                )
        if evidence.validate_perf_capture(
            _synthetic_perf_capture(contract_record, identity),
            contract_record,
            artifact_identity=identity,
        )["status"] == "verified":
            return _fail("nw07_perf_reference", "an unbound contract certified a capture")
        return _ok(
            "nw07_perf_reference",
            evidence={
                "contract_status": validation["status"],
                "pending_reference_bindings": validation["missing_bindings"],
                "strict_operator_verified": True,
                "aggregation_cannot_conceal_failure": True,
                "non_certifying_cases": sorted(cases),
                "unbound_contract_pending": True,
            },
        )
    except Exception as exc:
        return _fail("nw07_perf_reference", str(exc))


def _base_correctness_capture(tier: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    target = contract.CORRECTNESS_COVERAGE_TARGETS[tier]
    adapter = (
        "NVIDIA GeForce RTX 4060 Ti"
        if tier == "ada"
        else "NVIDIA GeForce RTX 5070 Ti"
    )
    boundary_tick = contract.LOOP_BOUNDARY_TICKS[1]
    boundary_sha = hashlib.sha256(f"{tier}-{boundary_tick}".encode()).hexdigest()
    return {
        "capture_id": f"{tier}-correctness-1",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": target,
        "evidence_role": contract.FIXED_TICK_CORRECTNESS_ROLE,
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "executable_bytes": identity["executable_bytes"],
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "adapter_description": adapter,
        "device_form_factor": "desktop",
        "device": adapter,
        "driver_version": "566.36" if tier == "ada" else "572.83",
        "os_version": "10.0.26100.2314",
        "toolchain_identity": "msvc-ltcg",
        "queried_capabilities": {"feature_level": "12_0", "shader_model": "6_0"},
        "clock_information": {"core_mhz": 2310, "memory_mhz": 9001},
        "power_information": {"power_limit_w": 160},
        "thermal_information": {"temperature_c": 62},
        "cache_state": {"kind": "warm", "driver_shader_cache": "primed"},
        "capture_protocol": contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "renderer": "exported_player",
        "execution_environment": {"kind": "physical"},
        "fixed_ticks": list(contract.REQUIRED_FIXED_TICKS),
        "capture_artifacts": [
            {
                "kind": contract.CAPTURE_KIND,
                "format": contract.CAPTURE_FORMAT,
                "path": f"tick_{tick}.rgba32f",
                "sha256": hashlib.sha256(f"{tier}-{tick}".encode()).hexdigest(),
                "bytes": contract.OUTPUT_WIDTH
                * contract.OUTPUT_HEIGHT
                * contract.CAPTURE_BYTES_PER_PIXEL,
                "tick": tick,
                "width": contract.OUTPUT_WIDTH,
                "height": contract.OUTPUT_HEIGHT,
            }
            for tick in contract.REQUIRED_FIXED_TICKS
        ]
        + [
            {
                "kind": contract.CAPTURE_KIND,
                "format": contract.CAPTURE_FORMAT,
                "path": f"tick_{boundary_tick}.rgba32f",
                "sha256": boundary_sha,
                "bytes": contract.OUTPUT_WIDTH
                * contract.OUTPUT_HEIGHT
                * contract.CAPTURE_BYTES_PER_PIXEL,
                "tick": boundary_tick,
                "width": contract.OUTPUT_WIDTH,
                "height": contract.OUTPUT_HEIGHT,
            }
        ],
        "reference_execution": {
            "execution_id": f"{tier}-diagnostic-preview-1",
            "renderer": "diagnostic_preview",
            "artifact_key": identity["artifact_key"],
            "package_sha256": identity["package_sha256"],
            "executable_sha256": "4" * 64,
            "manifest_sha256": identity["manifest_sha256"],
            "capture_protocol": contract.CORRECTNESS_CAPTURE_PROTOCOL,
        },
        "comparison": {
            "per_tick": [
                {
                    "tick": tick,
                    "artifact_sha256": hashlib.sha256(
                        f"{tier}-{tick}".encode()
                    ).hexdigest(),
                    "reference_artifact_sha256": hashlib.sha256(
                        f"{tier}-{tick}-reference".encode()
                    ).hexdigest(),
                    "renderer": "exported_player",
                    "reference_renderer": "diagnostic_preview",
                    "numeric": {
                        "maximum_error": 1e-7,
                        "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
                        "maximum_relative_error": 1e-6,
                        "relative_tolerance": contract.NUMERIC_RELATIVE_TOLERANCE,
                    },
                    "image": {
                        "maximum_error": 1e-6,
                        "tolerance": contract.IMAGE_ABSOLUTE_TOLERANCE,
                        "maximum_relative_error": 1e-6,
                        "relative_tolerance": contract.IMAGE_RELATIVE_TOLERANCE,
                    },
                }
                for tick in contract.REQUIRED_FIXED_TICKS
            ],
            "loop_boundary": {
                "verified": True,
                "source": "captured_boundary_comparison",
                "start_tick": 0,
                "boundary_tick": boundary_tick,
                "state_digest_equal_modulo_turn": True,
                "state_digest_source": "verify_canonical_graph",
                "graph_loop_boundary": {"seamless_modulo_turn": True},
                "boundary_artifact_sha256": boundary_sha,
                "numeric": {
                    "maximum_error": 1e-9,
                    "tolerance": contract.NUMERIC_ABSOLUTE_TOLERANCE,
                    "maximum_relative_error": 1e-9,
                    "relative_tolerance": contract.NUMERIC_RELATIVE_TOLERANCE,
                },
            },
        },
        "startup": {
            "protocol": contract.STARTUP_PROTOCOL,
            "process_start_to_first_present_ms": 850.0,
            "cache_state": {
                "kind": "cold_driver_cache",
                "application_pipeline_cache_used": False,
            },
            "cold_driver_cache_preparation": "Driver cache cleared before the clean run.",
            "warm_preparation": "Artifact pre-run once to completion before the warm run.",
        },
        "memory": {
            "peak_engine_committed_gpu_bytes": 220 * 1024 * 1024,
            "measurement_source": "engine accounting plus driver local usage query",
        },
        "measurements": {
            "process_ram_bytes": 310 * 1024 * 1024,
            "driver_memory_bytes": 240 * 1024 * 1024,
            "startup_pipeline_ms": 420.0,
            "generation_ms": 12.5,
            "image_quality_metrics": {"ssim": 0.999, "psnr_db": 52.0},
            "scaling_1440p": {"width": 2560, "height": 1440, "p99_gpu_ms": 12.1},
        },
        "unavailable_telemetry": [],
    }


def _diagnostic_overhead_record(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "capture_id": "diagnostic-overhead-1",
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "coverage_target": "diagnostic_sibling_build",
        "evidence_role": contract.DIAGNOSTIC_OVERHEAD_ROLE,
        "protocol": contract.DIAGNOSTIC_OVERHEAD_PROTOCOL,
        "artifact_key": identity["artifact_key"],
        "package_sha256": identity["package_sha256"],
        "manifest_sha256": identity["manifest_sha256"],
        "executable_sha256": identity["executable_sha256"],
        "release": {
            "executable_sha256": identity["executable_sha256"],
            "contains_capture_controls": False,
        },
        "diagnostic": {
            "build_profile": contract.DIAGNOSTIC_BUILD_PROFILE,
            "executable_sha256": "d" * 64,
        },
        "measurements": {
            "release_size_bytes": 120000,
            "diagnostic_size_bytes": 180000,
            "size_overhead_bytes": 60000,
            "release_p99_gpu_frame_time_ms": 10.0,
            "diagnostic_p99_gpu_frame_time_ms": 10.4,
            "gpu_frame_time_overhead_ms": 0.4,
            "release_p99_cpu_submit_ms": 6.0,
            "diagnostic_p99_cpu_submit_ms": 6.2,
            "cpu_submit_overhead_ms": 0.2,
        },
    }


def check_correctness_and_evidence_rules() -> CheckResult:
    try:
        identity = _synthetic_artifact_identity()
        validate_file_against_schema(EVIDENCE, REPO_ROOT / "schemas" / "nw07-hardware-evidence.schema.json")
        pending = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        pending_result = evidence.validate_evidence_record(
            pending, artifact_identity=identity
        )
        if pending_result["status"] == "verified" or pending_result["milestone_complete"]:
            return _fail("nw07_correctness_evidence", "pending evidence was reported verified")

        ada = _base_correctness_capture("ada", identity)
        blackwell = _base_correctness_capture("blackwell", identity)
        # Structural checks that do not require 33 MiB raw artifacts.
        missing_ticks = copy.deepcopy(ada)
        missing_ticks["fixed_ticks"] = [0]
        if evidence.validate_correctness_capture(
            missing_ticks,
            coverage_target=ada["coverage_target"],
            artifact_identity=identity,
        )["status"] == "verified":
            return _fail("nw07_correctness_evidence", "missing fixed ticks were accepted")
        wrong_tier = copy.deepcopy(ada)
        wrong_tier["adapter_description"] = "NVIDIA GeForce RTX 5070 Ti"
        if evidence.validate_correctness_capture(
            wrong_tier,
            coverage_target=ada["coverage_target"],
            artifact_identity=identity,
        )["status"] == "verified":
            return _fail("nw07_correctness_evidence", "wrong GPU tier certified Ada coverage")
        stale = copy.deepcopy(ada)
        stale["executable_sha256"] = "e" * 64
        if evidence.validate_correctness_capture(
            stale,
            coverage_target=ada["coverage_target"],
            artifact_identity=identity,
        )["status"] == "verified":
            return _fail("nw07_correctness_evidence", "stale artifact certified correctness")
        relaxed = copy.deepcopy(ada)
        relaxed["comparison"]["per_tick"][0]["image"]["tolerance"] = 1e-3
        if evidence.validate_correctness_capture(
            relaxed,
            coverage_target=ada["coverage_target"],
            artifact_identity=identity,
        )["status"] != "failed":
            return _fail("nw07_correctness_evidence", "relaxed image tolerance was accepted")
        failed_comparison = copy.deepcopy(ada)
        failed_comparison["comparison"]["per_tick"][1]["numeric"]["maximum_error"] = 1.0
        failed_comparison["comparison"]["per_tick"][1]["numeric"]["maximum_relative_error"] = 1.0
        if evidence.validate_correctness_capture(
            failed_comparison,
            coverage_target=ada["coverage_target"],
            artifact_identity=identity,
        )["status"] != "failed":
            return _fail("nw07_correctness_evidence", "executed comparison failure was hidden")
        slow = copy.deepcopy(ada)
        slow["startup"]["process_start_to_first_present_ms"] = 2000.001
        if evidence.validate_correctness_capture(
            slow,
            coverage_target=ada["coverage_target"],
            artifact_identity=identity,
        )["status"] != "failed":
            return _fail("nw07_correctness_evidence", "startup above 2 s was accepted")
        oversized = copy.deepcopy(ada)
        oversized["memory"]["peak_engine_committed_gpu_bytes"] = 512 * 1024 * 1024 + 1
        if evidence.validate_correctness_capture(
            oversized,
            coverage_target=ada["coverage_target"],
            artifact_identity=identity,
        )["status"] != "failed":
            return _fail("nw07_correctness_evidence", "GPU memory above 512 MiB was accepted")
        nw06 = copy.deepcopy(ada)
        nw06["task"] = "NW-06"
        nw06["implementation_base"] = "23bf4b36c978ff2660a873d3aee293633a53ddac"
        if evidence.validate_correctness_capture(
            nw06,
            coverage_target=ada["coverage_target"],
            artifact_identity=identity,
        )["status"] != "failed":
            return _fail("nw07_correctness_evidence", "NW-06 evidence satisfied NW-07 coverage")
        same_renderer = copy.deepcopy(ada)
        same_renderer["reference_execution"]["renderer"] = "exported_player"
        if evidence.validate_correctness_capture(
            same_renderer,
            coverage_target=ada["coverage_target"],
            artifact_identity=identity,
        )["status"] != "failed":
            return _fail("nw07_correctness_evidence", "non-cross-mode comparison was accepted")
        diagnostic = _diagnostic_overhead_record(identity)
        if evidence.validate_diagnostic_overhead(
            diagnostic, artifact_identity=identity
        )["status"] != "verified":
            return _fail("nw07_correctness_evidence", "valid diagnostic overhead was rejected")
        same_artifact = copy.deepcopy(diagnostic)
        same_artifact["diagnostic"]["executable_sha256"] = identity["executable_sha256"]
        if evidence.validate_diagnostic_overhead(
            same_artifact, artifact_identity=identity
        )["status"] != "failed":
            return _fail(
                "nw07_correctness_evidence",
                "diagnostic build equal to release was accepted",
            )
        leaked = copy.deepcopy(diagnostic)
        leaked["release"]["contains_capture_controls"] = True
        if evidence.validate_diagnostic_overhead(
            leaked, artifact_identity=identity
        )["status"] != "failed":
            return _fail(
                "nw07_correctness_evidence",
                "release build with capture controls was accepted",
            )
        return _ok(
            "nw07_correctness_evidence",
            evidence={
                "pending_evidence_status": pending_result["status"],
                "missing_fixed_ticks_rejected": True,
                "wrong_tier_rejected": True,
                "stale_artifact_rejected": True,
                "relaxed_tolerance_rejected": True,
                "executed_failure_retained": True,
                "startup_gate_verified": True,
                "memory_gate_verified": True,
                "nw06_evidence_rejected": True,
                "cross_renderer_required": True,
                "diagnostic_overhead_verified": True,
            },
        )
    except Exception as exc:
        return _fail("nw07_correctness_evidence", str(exc))


def check_regression_policy() -> CheckResult:
    try:
        environment = {
            "board": "NVIDIA GeForce RTX 4060 Ti",
            "host": "AMD Ryzen 7 7800X3D",
            "driver_version": "566.36",
            "os_build": "10.0.26100.2314",
            "toolchain_identity": "msvc-ltcg",
            "resolution": "1920x1080",
            "quality_profile": "nw07.quality.v1",
            "cache_state": "warm",
        }
        baseline = {
            "environment": environment,
            "metrics": {
                "complete_distribution_bytes": 120000,
                "process_start_to_first_present_ms": 800.0,
                "peak_engine_committed_gpu_bytes": 200 * 1024 * 1024,
                "p99_gpu_frame_time_ms": 10.0,
                "p99_cpu_submit_ms": 5.0,
            },
        }
        current = copy.deepcopy(baseline)
        current["metrics"]["p99_gpu_frame_time_ms"] = 10.5
        current["metrics"]["complete_distribution_bytes"] = 125000
        within = regression.evaluate_regression(baseline, current)
        if within["status"] not in {"passed", "flagged"}:
            return _fail("nw07_regression", "small size change failed the regression policy")
        if within["size"]["percentage_only_failure"]:
            return _fail("nw07_regression", "size percentage-only failure rule is active")
        alerted = copy.deepcopy(current)
        alerted["metrics"]["p99_gpu_frame_time_ms"] = 11.5  # +15%
        alert = regression.evaluate_regression(baseline, alerted)
        if alert["status"] != "flagged" or not alert["relative_alerts"]:
            return _fail("nw07_regression", "matched-environment 15% regression was not flagged")
        unmatched = copy.deepcopy(alerted)
        unmatched["environment"] = dict(environment, driver_version="999.99")
        unmatched_result = regression.evaluate_regression(baseline, unmatched)
        if unmatched_result["status"] == "flagged" or not unmatched_result["informational"]:
            return _fail("nw07_regression", "unmatched environment produced a regression flag")
        absolute = copy.deepcopy(current)
        absolute["metrics"]["complete_distribution_bytes"] = 262145
        absolute_result = regression.evaluate_regression(baseline, absolute)
        if absolute_result["status"] != "failed":
            return _fail("nw07_regression", "absolute size gate violation did not fail")
        startup = copy.deepcopy(current)
        startup["metrics"]["process_start_to_first_present_ms"] = 2001.0
        if regression.evaluate_regression(baseline, startup)["status"] != "failed":
            return _fail("nw07_regression", "startup gate violation did not fail")
        memory = copy.deepcopy(current)
        memory["metrics"]["peak_engine_committed_gpu_bytes"] = 512 * 1024 * 1024 + 1
        if regression.evaluate_regression(baseline, memory)["status"] != "failed":
            return _fail("nw07_regression", "memory gate violation did not fail")
        frametime = copy.deepcopy(current)
        frametime["metrics"]["p99_cpu_submit_ms"] = 16.67
        if regression.evaluate_regression(baseline, frametime)["status"] != "failed":
            return _fail("nw07_regression", "strict frame-time gate did not fail")
        return _ok(
            "nw07_regression",
            evidence={
                "matched_relative_alert": True,
                "unmatched_environment_informational": True,
                "size_percentage_only_failure": False,
                "absolute_gate_failures": True,
            },
        )
    except Exception as exc:
        return _fail("nw07_regression", str(exc))


def check_build_controls() -> CheckResult:
    try:
        presets = json.loads((REPO_ROOT / "CMakePresets.json").read_text(encoding="utf-8"))
        names = {item.get("name") for item in presets.get("configurePresets", [])}
        if "nw07-release" not in names or "nw07-diagnostics" not in names:
            return _fail("nw07_build_controls", "NW-07 release/diagnostic presets are missing")
        build_profiles = (REPO_ROOT / "cmake" / "BuildProfiles.cmake").read_text(encoding="utf-8")
        if "NW07_CAPTURE_CONTROLS" not in build_profiles:
            return _fail("nw07_build_controls", "diagnostic capture-control option is missing")
        instrumentation = (
            REPO_ROOT / "src" / "player" / "player_instrumentation.h"
        ).read_text(encoding="utf-8")
        if "NW_PLAYER_DIAGNOSTICS" not in instrumentation:
            return _fail("nw07_build_controls", "player diagnostic surface is missing")
        nightly = (REPO_ROOT / ".github" / "workflows" / "nw07-regression.yml").exists()
        if not nightly:
            return _fail("nw07_build_controls", "NW-07 regression workflow is missing")
        return _ok(
            "nw07_build_controls",
            evidence={
                "release_preset": "nw07-release",
                "diagnostic_preset": "nw07-diagnostics",
                "capture_controls_option": "NW07_CAPTURE_CONTROLS",
                "nightly_workflow": nightly,
            },
        )
    except Exception as exc:
        return _fail("nw07_build_controls", str(exc))


def contracts_only() -> list[CheckResult]:
    return [
        check_canonical_graph(),
        check_frozen_contracts(),
        check_export_and_size(),
        check_perf_reference_rules(),
        check_correctness_and_evidence_rules(),
        check_regression_policy(),
        check_build_controls(),
    ]


def _native_export_checks(args: argparse.Namespace) -> list[CheckResult]:
    if not args.player_template or not args.dxc:
        return [
            _pending(
                "nw07_native_release_export",
                "a real pinned DXC and Windows player template are required for a release-grade export",
            )
        ]
    if platform.system().lower() != "windows":
        return [
            _pending(
                "nw07_native_release_export",
                "release-grade Windows x64 export requires a Windows host",
            )
        ]
    try:
        # Retain the real distribution and all reproducibility evidence.  The
        # CI workflow uploads this directory, so a successful native build must
        # never disappear with a temporary directory.
        default_root = (
            REPO_ROOT
            / "build"
            / "nw07-verification"
            / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        root = Path(args.native_output) if args.native_output else default_root
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        artifact_dir = root / "artifact"
        report_path = root / "report.json"
        report = export.export_production(
            artifact_dir,
            player_template=args.player_template,
            dxc_executable=args.dxc,
            report_path=report_path,
            allow_test_payloads=False,
            specialized_build_root=root / "clean-build",
        )
        reproducibility = export.export_reproducibility(
            root / "repro",
            player_template=args.player_template,
            dxc_executable=args.dxc,
            allow_test_payloads=False,
        )
        return [
            _ok(
                "nw07_native_release_export",
                evidence={
                    "artifact_key": report["artifact"]["artifact_key"],
                    "package_sha256": report["artifact"]["package_sha256"],
                    "manifest_sha256": report["artifact"]["manifest_sha256"],
                    "complete_distribution_bytes": report["distribution"][
                        "complete_distribution_bytes"
                    ],
                    "within_stretch": report["distribution"]["within_stretch"],
                    "within_ceiling": report["distribution"]["within_ceiling"],
                    "release_grade": report.get("release_grade"),
                    "test_payload": report.get("test_payload"),
                    "reproducible": reproducibility["matching"],
                    "executable_sha256": reproducibility["fields"][
                        "executable_sha256"
                    ],
                    "build_inputs_digest": report["build_inputs"]["digest"],
                    "matches_frozen_digest": report["build_inputs"][
                        "matches_frozen_digest"
                    ],
                    "retained_distribution_directory": str(artifact_dir),
                    "retained_export_report": str(report_path),
                    "retained_reproducibility_directory": str(root / "repro"),
                },
            )
        ]
    except Exception as exc:
        return [_fail("nw07_native_release_export", str(exc))]


def _native_evidence_mismatches(
    evidence: Mapping[str, Any],
    identity: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    require_reproducible: bool,
) -> list[str]:
    mismatches: list[str] = []
    expected = {
        "artifact_key": identity.get("artifact_key"),
        "package_sha256": identity.get("package_sha256"),
        "manifest_sha256": identity.get("manifest_sha256"),
        "executable_sha256": identity.get("executable_sha256"),
        "complete_distribution_bytes": identity["size_accounting"][
            "complete_distribution_bytes"
        ],
        "build_inputs_digest": inputs["digest"],
    }
    for field, value in expected.items():
        if evidence.get(field) != value:
            mismatches.append(
                f"{field}: expected {value!r}, observed {evidence.get(field)!r}"
            )
    if evidence.get("matches_frozen_digest") is not True:
        mismatches.append("build inputs do not match the frozen digest")
    if evidence.get("within_ceiling") is not True:
        mismatches.append("complete distribution is not within the hard ceiling")
    if evidence.get("release_grade") is not True or evidence.get("test_payload") is not False:
        mismatches.append("export is not a release-grade non-test payload")
    if require_reproducible and evidence.get("reproducible") is not True:
        mismatches.append("clean-directory exports were not reproducible")
    return mismatches


def _validate_build_report(
    path: Path,
    *,
    identity: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> CheckResult:
    try:
        if config is None:
            config = export.load_canonical_config()
        report = json.loads(path.read_text(encoding="utf-8"))
        inputs = export.verify_pinned_inputs(config)
    except Exception as exc:
        return _fail("nw07_native_release_export", str(exc))
    if not isinstance(report, Mapping):
        return _fail(
            "nw07_native_release_export",
            "build/export report root is not a JSON object",
        )
    if not inputs["frozen"] or not inputs["matches_frozen_digest"]:
        return _fail(
            "nw07_native_release_export",
            "pinned build inputs are not anchored to a frozen digest",
        )
    if report.get("format") == "nw07-verification-summary":
        if (
            report.get("mode") != "BuildChecks"
            or report.get("implementation_base") != contract.IMPLEMENTATION_BASE
            or report.get("profile_id") != contract.PROFILE_ID
            or report.get("requirements_digest") != contract.REQUIREMENTS_DIGEST
        ):
            return _fail(
                "nw07_native_release_export",
                "BuildChecks summary is not bound to the NW-07 implementation and profile",
            )
        checks = report.get("checks")
        if not isinstance(checks, list):
            return _fail(
                "nw07_native_release_export",
                "BuildChecks summary checks are not a list",
            )
        native = next(
            (
                check
                for check in checks
                if isinstance(check, Mapping)
                and check.get("name") == "nw07_native_release_export"
            ),
            None,
        )
        if native is None:
            return _fail(
                "nw07_native_release_export",
                "BuildChecks summary has no native release export result",
            )
        if native.get("status") == "failed":
            return _fail(
                "nw07_native_release_export",
                "BuildChecks summary records an executed export failure",
            )
        if native.get("status") != "passed":
            return _pending(
                "nw07_native_release_export",
                "BuildChecks summary does not contain a passed native release export",
            )
        if report.get("success") is not True:
            return _fail(
                "nw07_native_release_export",
                "BuildChecks summary is not successful",
            )
        evidence_payload = native.get("evidence")
        if not isinstance(evidence_payload, Mapping):
            return _fail(
                "nw07_native_release_export",
                "BuildChecks native export evidence is not an object",
            )
        mismatches = _native_evidence_mismatches(
            evidence_payload,
            identity,
            inputs,
            require_reproducible=True,
        )
        if mismatches:
            return _fail(
                "nw07_native_release_export",
                "BuildChecks summary is not bound to the final artifact: "
                + "; ".join(mismatches),
            )
        return _ok(
            "nw07_native_release_export",
            "verified from a passed BuildChecks summary",
            evidence={
                "source": "build_checks_summary",
                "report": str(path),
                **{
                    key: value
                    for key, value in evidence_payload.items()
                    if key
                    in {
                        "artifact_key",
                        "package_sha256",
                        "manifest_sha256",
                        "executable_sha256",
                        "complete_distribution_bytes",
                        "build_inputs_digest",
                        "release_grade",
                        "test_payload",
                        "reproducible",
                    }
                },
            },
        )
    if report.get("format") in {
        "nw07-export-report",
        "nw07-reproducibility-report",
    }:
        if (
            report.get("success") is not True
            or report.get("implementation_base") != contract.IMPLEMENTATION_BASE
            or report.get("profile_id") != contract.PROFILE_ID
            or report.get("requirements_digest") != contract.REQUIREMENTS_DIGEST
        ):
            return _fail(
                "nw07_native_release_export",
                "export report is not a successful NW-07 release export",
            )
        artifact = report.get("artifact")
        distribution = report.get("distribution")
        build_inputs = report.get("build_inputs")
        reproducibility = report.get("reproducibility")
        if not isinstance(artifact, Mapping):
            return _fail(
                "nw07_native_release_export",
                "export report artifact is not an object",
            )
        if not isinstance(distribution, Mapping):
            return _fail(
                "nw07_native_release_export",
                "export report distribution is not an object",
            )
        if not isinstance(build_inputs, Mapping):
            return _fail(
                "nw07_native_release_export",
                "export report build_inputs is not an object",
            )
        if not isinstance(reproducibility, Mapping):
            return _fail(
                "nw07_native_release_export",
                "export report has no reproducibility evidence",
            )
        evidence_payload = {
            "artifact_key": artifact.get("artifact_key"),
            "package_sha256": artifact.get("package_sha256"),
            "manifest_sha256": artifact.get("manifest_sha256"),
            "executable_sha256": artifact.get("executable_sha256"),
            "complete_distribution_bytes": distribution.get(
                "complete_distribution_bytes"
            ),
            "within_ceiling": distribution.get("within_ceiling"),
            "build_inputs_digest": build_inputs.get("digest"),
            "matches_frozen_digest": build_inputs.get("matches_frozen_digest"),
            "release_grade": report.get("release_grade"),
            "test_payload": report.get("test_payload"),
            "reproducible": reproducibility.get("matching"),
        }
        mismatches = _native_evidence_mismatches(
            evidence_payload,
            identity,
            inputs,
            require_reproducible=True,
        )
        reproduction_fields = reproducibility.get("fields")
        if reproducibility.get("matching") is not True:
            mismatches.append("reproducibility report is not matching")
        if reproducibility.get("build_inputs_digest") != inputs["digest"]:
            mismatches.append(
                "reproducibility report is bound to different build inputs"
            )
        if not isinstance(reproduction_fields, Mapping):
            mismatches.append("reproducibility report has no artifact field binding")
        else:
            for field in (
                "artifact_key",
                "package_sha256",
                "manifest_sha256",
                "executable_sha256",
            ):
                if reproduction_fields.get(field) != evidence_payload.get(field):
                    mismatches.append(
                        f"reproducibility field '{field}' does not match the export"
                    )
        if mismatches:
            return _fail(
                "nw07_native_release_export",
                "export report is not bound to the final artifact or lacks "
                "clean-directory reproducibility: "
                + "; ".join(mismatches),
            )
        return _ok(
            "nw07_native_release_export",
            "verified from a reproducible export report",
            evidence={"source": "export_report", "report": str(path)},
        )
    return _fail(
        "nw07_native_release_export",
        "unrecognized build/export report format",
    )


def _full_clean_system_check(
    args: argparse.Namespace,
    identity: Mapping[str, Any] | None,
) -> CheckResult:
    """Mandatory Full gate: clean deployment bound to the final executable."""
    if not args.binary_dir:
        return _pending(
            "nw07_clean_system",
            "Full verification requires --binary-dir containing the deployed "
            "final executable and a clean-system execution report",
        )
    binary_dir = Path(args.binary_dir)
    executable = clean_system.deployment_executable(binary_dir)
    if executable is None:
        return _fail(
            "nw07_clean_system",
            "deployment directory has no NW-07 executable",
        )
    if identity is not None:
        if evidence.sha256_file(executable) != identity.get("executable_sha256"):
            return _fail(
                "nw07_clean_system",
                "clean-system deployment executable does not match the final artifact",
            )
    report_path = Path(args.clean_report) if args.clean_report else (
        binary_dir / "nw07-clean-system.json"
    )
    if not report_path.is_file():
        return _pending(
            "nw07_clean_system",
            "a retained clean-system execution report is required for Full verification",
        )
    try:
        validate_file_against_schema(
            report_path,
            REPO_ROOT / "schemas" / "nw07-clean-system-report.schema.json",
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _fail("nw07_clean_system", str(exc))
    if (
        report.get("implementation_base") != contract.IMPLEMENTATION_BASE
        or report.get("profile_id") != contract.PROFILE_ID
        or report.get("requirements_digest") != contract.REQUIREMENTS_DIGEST
    ):
        return _fail(
            "nw07_clean_system",
            "clean-system report is not bound to the NW-07 implementation/profile",
        )
    report_artifact = report.get("artifact")
    if isinstance(report_artifact, Mapping) and identity is not None:
        for field in ("artifact_key", "package_sha256", "manifest_sha256", "executable_sha256"):
            if report_artifact.get(field) != identity.get(field):
                return _fail(
                    "nw07_clean_system",
                    f"clean-system report artifact '{field}' does not match the "
                    "final artifact",
                )
    validation = clean_system.validate_report(
        report,
        deployment_directory=binary_dir,
        exclude_paths=(report_path,),
    )
    if validation["status"] == "verified":
        return _ok(
            "nw07_clean_system",
            evidence={
                "report": str(report_path),
                "deployment": report.get("deployment"),
                "scenarios": validation["scenarios"],
                "required_scenarios": list(clean_system.REQUIRED_SCENARIOS),
            },
        )
    if validation["status"] == "failed":
        return _fail("nw07_clean_system", "; ".join(validation["reasons"]))
    return _pending("nw07_clean_system", "; ".join(validation["reasons"]))


def build_checks(args: argparse.Namespace) -> list[CheckResult]:
    return contracts_only() + _native_export_checks(args)


def full_checks(args: argparse.Namespace) -> list[CheckResult]:
    # Full verification requires release-export provenance bound to the same
    # final artifact, either by running the native export now or by validating
    # a retained BuildChecks/export report.
    checks = contracts_only()
    native_requested = bool(args.player_template and args.dxc)
    if args.player_template and not args.dxc or args.dxc and not args.player_template:
        checks.append(
            _pending(
                "nw07_native_release_export",
                "both --player-template and --dxc are required for native export checks",
            )
        )
    elif not native_requested and not args.build_report:
        checks.append(
            _pending(
                "nw07_native_release_export",
                "a passed BuildChecks/export report or --player-template plus --dxc "
                "is required for release-export provenance",
            )
        )
    if not args.fixture or not args.package or not args.evidence:
        checks.append(
            _pending(
                "nw07_hardware_evidence",
                "--fixture, --package, and --evidence are required for hardware verification",
            )
        )
        checks.append(_full_clean_system_check(args, None))
        return checks
    executable = Path(args.fixture)
    package_dir = Path(args.package)
    if not executable.is_file():
        checks.append(_fail("nw07_artifact_identity", f"executable is unavailable: {executable}"))
        checks.append(_full_clean_system_check(args, None))
        return checks
    try:
        identity = export.inspect_artifact_directory(package_dir)
    except Exception as exc:
        checks.append(_fail("nw07_artifact_identity", str(exc)))
        checks.append(_full_clean_system_check(args, None))
        return checks
    executable_sha = evidence.sha256_file(executable)
    if executable_sha != identity["executable_sha256"]:
        checks.append(
            _fail(
                "nw07_artifact_identity",
                "supplied executable does not match the published artifact",
            )
        )
        checks.append(_full_clean_system_check(args, identity))
        return checks
    checks.append(
        _ok(
            "nw07_artifact_identity",
            evidence={
                "artifact_key": identity["artifact_key"],
                "package_sha256": identity["package_sha256"],
                "manifest_sha256": identity["manifest_sha256"],
                "executable_sha256": identity["executable_sha256"],
                "complete_distribution_bytes": identity["size_accounting"][
                    "complete_distribution_bytes"
                ],
                "within_ceiling": identity["size_accounting"]["within_ceiling"],
            },
        )
    )
    if native_requested:
        for result in _native_export_checks(args):
            if result.status == "passed":
                try:
                    inputs = export.verify_pinned_inputs(
                        export.load_canonical_config()
                    )
                    mismatches = _native_evidence_mismatches(
                        result.evidence,
                        identity,
                        inputs,
                        require_reproducible=True,
                    )
                except Exception as exc:
                    result = _fail(
                        "nw07_native_release_export",
                        f"native release export binding failed: {exc}",
                    )
                else:
                    if mismatches:
                        result = _fail(
                            "nw07_native_release_export",
                            "native release export is not bound to the supplied final "
                            "artifact: " + "; ".join(mismatches),
                        )
            checks.append(result)
    elif args.build_report:
        checks.append(
            _validate_build_report(
                Path(args.build_report),
                identity=identity,
                config=None,
            )
        )
    try:
        validate_file_against_schema(
            Path(args.evidence), REPO_ROOT / "schemas" / "nw07-hardware-evidence.schema.json"
        )
        record = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
        perf_contract = json.loads(
            Path(args.perf_ref or PERF_REF).read_text(encoding="utf-8")
        )
        result = evidence.validate_evidence_record(
            record,
            artifact_identity=identity,
            artifact_root=package_dir,
            perf_contract=perf_contract,
            config=export.load_canonical_config(),
        )
    except Exception as exc:
        checks.append(_fail("nw07_hardware_evidence", str(exc)))
        checks.append(_full_clean_system_check(args, identity))
        return checks
    if result["status"] == "verified":
        checks.append(_ok("nw07_hardware_evidence", evidence=result))
    elif result["status"] == "failed":
        checks.append(_fail("nw07_hardware_evidence", json.dumps(result, sort_keys=True)))
    else:
        checks.append(
            _pending(
                "nw07_hardware_evidence",
                "required Ada/Blackwell correctness, startup/memory, reference-performance, "
                "or diagnostic-overhead evidence is missing or incomplete",
            )
        )
    if identity["size_accounting"]["complete_distribution_bytes"] > contract.COMPLETE_DISTRIBUTION_CEILING_BYTES:
        checks.append(_fail("nw07_release_size", "release distribution exceeds 262,144 bytes"))
    else:
        checks.append(
            _ok(
                "nw07_release_size",
                evidence={
                    "complete_distribution_bytes": identity["size_accounting"][
                        "complete_distribution_bytes"
                    ],
                    "within_stretch": identity["size_accounting"]["within_stretch"],
                },
            )
        )
    checks.append(_full_clean_system_check(args, identity))
    return checks


def clean_system_checks(args: argparse.Namespace) -> list[CheckResult]:
    if platform.system().lower() != "windows":
        return [
            _pending(
                "nw07_clean_system",
                "clean-system deployment evidence requires a physical Windows 11 x64 host",
            )
        ]
    if not args.binary_dir:
        return [_pending("nw07_clean_system", "--binary-dir is required")]
    binary_dir = Path(args.binary_dir)
    exe = clean_system.deployment_executable(binary_dir)
    if exe is None:
        return [_fail("nw07_clean_system", "deployment directory has no NW-07 executable")]
    if not args.clean_report:
        discovered = binary_dir / "nw07-clean-system.json"
        if discovered.is_file():
            args.clean_report = str(discovered)
    evidence_files = set()
    if args.clean_report:
        try:
            evidence_files.add(Path(args.clean_report).resolve())
        except OSError:
            pass
    files = [path for path in binary_dir.rglob("*") if path.is_file()]
    unexpected = [
        str(path.relative_to(binary_dir))
        for path in files
        if path != exe
        and path.resolve() not in evidence_files
        and path.suffix.lower() in {".nwp", ".json", ".dll", ".pdb"}
    ]
    if unexpected:
        return [
            _fail(
                "nw07_clean_system",
                "deployment contains development or package sidecars: "
                + ", ".join(unexpected),
            )
        ]
    if args.fixture and args.package:
        report_path = Path(args.clean_report or (binary_dir / "nw07-clean-system.json"))
        try:
            report = clean_system.run_clean_system(
                fixture=Path(args.fixture),
                package_directory=Path(args.package),
                deployment_directory=binary_dir,
                output=report_path,
            )
        except Exception as exc:
            return [_fail("nw07_clean_system", str(exc))]
        validation = clean_system.validate_report(
            report,
            deployment_directory=binary_dir,
            exclude_paths=(report_path,),
        )
        if validation["status"] == "verified":
            return [
                _ok(
                    "nw07_clean_system",
                    evidence={
                        "report": str(report_path),
                        "deployment": report.get("deployment"),
                        "scenarios": validation["scenarios"],
                    },
                )
            ]
        if validation["status"] == "failed":
            return [_fail("nw07_clean_system", "; ".join(validation["reasons"]))]
        return [_pending("nw07_clean_system", "; ".join(validation["reasons"]))]
    if args.clean_report:
        try:
            validate_file_against_schema(
                Path(args.clean_report),
                REPO_ROOT / "schemas" / "nw07-clean-system-report.schema.json",
            )
            report = json.loads(Path(args.clean_report).read_text(encoding="utf-8"))
        except Exception as exc:
            return [_fail("nw07_clean_system", str(exc))]
        validation = clean_system.validate_report(
            report,
            deployment_directory=binary_dir,
            exclude_paths=(Path(args.clean_report),),
        )
        if validation["status"] == "verified":
            return [_ok("nw07_clean_system", evidence=report)]
        if validation["status"] == "failed":
            return [_fail("nw07_clean_system", "; ".join(validation["reasons"]))]
        return [_pending("nw07_clean_system", "; ".join(validation["reasons"]))]
    return [
        _pending(
            "nw07_clean_system",
            "a clean-system execution report is required to confirm corrupt-payload, "
            "missing-feature, initialization-failure, resize, and device-removal behavior",
        )
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("ContractsOnly", "BuildChecks", "Full", "CleanSystem"),
        default="ContractsOnly",
    )
    parser.add_argument("--fixture")
    parser.add_argument("--package")
    parser.add_argument("--evidence")
    parser.add_argument("--perf-ref")
    parser.add_argument("--player-template")
    parser.add_argument("--dxc")
    parser.add_argument("--build-report")
    parser.add_argument("--native-output")
    parser.add_argument("--binary-dir")
    parser.add_argument("--clean-report")
    parser.add_argument("--summary-json")
    args = parser.parse_args(argv)
    if args.mode == "ContractsOnly":
        checks = contracts_only()
    elif args.mode == "BuildChecks":
        checks = build_checks(args)
    elif args.mode == "Full":
        checks = full_checks(args)
    else:
        checks = clean_system_checks(args)

    required_failures = [
        check for check in checks if check.required and check.status == "failed"
    ]
    pending = [check for check in checks if check.status == "pending_unverified"]
    hardware_verified = args.mode == "Full" and not required_failures and not pending
    if args.mode == "ContractsOnly":
        success = not required_failures
    else:
        success = not required_failures and not pending
    payload = {
        "format": "nw07-verification-summary",
        "version": 1,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "mode": args.mode,
        "success": success,
        "milestone_complete": hardware_verified,
        "hardware_certification": (
            "verified" if hardware_verified else "failed" if required_failures else "pending_unverified"
        ),
        "contracts_only_success_does_not_imply_hardware_acceptance": True,
        "checks": [check.to_dict() for check in checks],
        "required_failures": len(required_failures),
        "pending_checks": len(pending),
        "hard_gates": contract.authoritative_hard_gates(),
        "stretch_measurement": {
            "complete_distribution_bytes_ceiling": contract.STRETCH_DISTRIBUTION_CEILING_BYTES,
            "mandatory_gate": False,
        },
        "profile_id": contract.PROFILE_ID,
        "requirements_digest": contract.REQUIREMENTS_DIGEST,
        "limitations": [
            "ContractsOnly and BuildChecks never claim hardware certification.",
            "Missing Ada, Blackwell, reference-performance, startup/memory, or diagnostic-overhead evidence remains pending.",
            "The 128 KiB stretch measurement is reported but never gates milestone success.",
            "Blackwell development-series measurements have no absolute gate.",
        ],
    }
    if args.summary_json:
        path = Path(args.summary_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    if required_failures:
        return 1
    if pending and args.mode in {"BuildChecks", "Full", "CleanSystem"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
