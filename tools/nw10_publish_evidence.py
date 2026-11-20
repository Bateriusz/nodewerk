#!/usr/bin/env python3
"""Publish privacy-safe NW-10 physical evidence into tracked summaries.

This tool is deliberately publication-only. Capture tools keep raw files under
build/ or in transfer bundles; this publisher validates their identities,
removes raw/local-path material, writes tracked summaries, and advances the
NW-10 evidence state machine only when all required inputs are verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import nw07_clean_system as clean_system_contract

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = Path("benchmarks/nw10/evidence-status.v1.json")
CORRECTNESS_PATH = Path("benchmarks/nw10/common-artifact-correctness.v1.json")
SYSTEM_PATH = Path("benchmarks/nw10/system-validation.v1.json")
INVALIDATED_GRADIENT_SHA256 = (
    "76c13bcc21da3a17b10b912f5e8ff4270f64749c74b347f174b519cbdd8c96bb"
)
STARTUP_CEILING_MS = 2000.0
MEMORY_CEILING_BYTES = 536_870_912
EXPECTED_SCENARIO_EXIT_CODES = clean_system_contract.EXPECTED_SCENARIO_EXIT_CODES
ARCHIVED_PHYSICAL_SOURCE_HEAD = "4d216ddf3cb4d5e0cecdc5bf0c49af3ff7d20eee"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _json_strings(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
    elif isinstance(value, str):
        yield value


def _assert_privacy(value: Mapping[str, Any], forbid_tokens: list[str]) -> None:
    hits: list[str] = []
    tokens = [token for token in forbid_tokens if token]
    for text in _json_strings(value):
        lower = text.casefold()
        if re.search(r"(?i)^[a-z]:\\users\\", text) or lower.startswith("/home/"):
            hits.append(text)
            continue
        if any(token.casefold() in lower for token in tokens):
            hits.append(text)
    if hits:
        raise ValueError(
            "privacy audit found forbidden public strings: "
            + "; ".join(sorted(set(hits)))
        )


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("unable to resolve current git HEAD")
    value = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("current git HEAD is malformed")
    return value


def _artifact_from_intro_report(report: Mapping[str, Any]) -> dict[str, Any]:
    if report.get("test_payload") is True:
        raise ValueError("refusing to publish an intro report generated from test payloads")
    if report.get("release_grade") is not True:
        raise ValueError("refusing to publish a non-release-grade intro report")
    artifact = report.get("canonical_artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("intro report has no canonical_artifact")
    compact = report.get("compact_artifact")
    selected_mode = None
    if isinstance(compact, Mapping):
        selected_mode = compact.get("selected_mode")
        if selected_mode not in {"canonical", "compact"}:
            raise ValueError("intro report has no selected executable mode")
    required = (
        "executable_sha256",
        "executable_bytes",
        "package_sha256",
        "manifest_sha256",
    )
    missing = [name for name in required if artifact.get(name) in (None, "")]
    if missing:
        raise ValueError("intro canonical artifact is incomplete: " + ", ".join(missing))
    if selected_mode == "compact":
        if not isinstance(compact, Mapping):
            raise ValueError("intro report selected a missing compact artifact")
        if compact.get("selected_reason") != (
            "explicit_compact_selection_after_validation"
        ):
            raise ValueError(
                "intro report selected the compact executable without an "
                "explicit post-validation selection reason"
            )
        executable_sha256 = str(
            compact.get("selected_executable_sha256", "")
        ).lower()
        executable_bytes = int(compact.get("selected_executable_bytes", 0))
        selected_mode_value = "compact"
        if executable_sha256 != str(
            compact.get("executable_sha256", "")
        ).lower():
            raise ValueError(
                "intro compact selection does not name the compact executable"
            )
        if executable_bytes != int(compact.get("executable_bytes", -1)):
            raise ValueError(
                "intro compact selection size does not match the compact executable"
            )
        if str(compact.get("decoded_package_sha256", "")).lower() != str(
            artifact["package_sha256"]
        ).lower():
            raise ValueError(
                "intro compact payload does not decode to the canonical package"
            )
        if compact.get("codec_id") not in {"nwc.codec.stored.v1", "nwc.codec.mszip.v1"}:
            raise ValueError("intro compact selection uses an unsupported codec")
        if compact.get("layout_id") not in {
            "nwc.layout.canonical.v1",
            "nwc.layout.aos.v1",
            "nwc.layout.soa.v1",
        }:
            raise ValueError("intro compact selection uses an unsupported layout")
    else:
        executable_sha256 = str(artifact["executable_sha256"]).lower()
        executable_bytes = int(artifact["executable_bytes"])
        selected_mode_value = "canonical"
        if isinstance(compact, Mapping):
            if str(compact.get("selected_executable_sha256", "")).lower() != (
                executable_sha256
            ):
                raise ValueError(
                    "intro canonical selection does not name the canonical executable"
                )
            if int(compact.get("selected_executable_bytes", -1)) != executable_bytes:
                raise ValueError(
                    "intro canonical selection size does not match the canonical "
                    "executable"
                )
            if compact.get("selected_reason") not in {
                "runtime_validation_pending",
                "compression_rejected_for_increasing_distribution",
            }:
                raise ValueError(
                    "intro canonical selection has an unrecognized reason"
                )
    result = {
        "executable_sha256": executable_sha256,
        "executable_bytes": executable_bytes,
        "package_sha256": str(artifact["package_sha256"]).lower(),
        "manifest_sha256": str(artifact["manifest_sha256"]).lower(),
        "selected_mode": selected_mode_value,
    }
    for name in ("executable_sha256", "package_sha256", "manifest_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", result[name]):
            raise ValueError(f"intro artifact {name} is malformed")
    if result["executable_sha256"] == INVALIDATED_GRADIENT_SHA256:
        raise ValueError("refusing to publish evidence for invalidated gradient artifact")
    if result["executable_bytes"] <= 0:
        raise ValueError("intro executable size is invalid")
    return result


def _load_clean_bundle(path: Path) -> tuple[dict[str, Any], str, dict[str, bytes]]:
    raw = path.read_bytes()
    bundle_sha = _sha256_bytes(raw)
    retained: dict[str, bytes] = {}
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        if "bundle-manifest.json" not in names:
            raise ValueError("clean-system result bundle has no bundle-manifest.json")
        manifest = json.loads(archive.read("bundle-manifest.json").decode("utf-8-sig"))
        if manifest.get("format") != "nw10-clean-system-result-bundle":
            raise ValueError("unexpected clean-system result bundle format")
        if int(manifest.get("version", -1)) != 1:
            raise ValueError("unexpected clean-system result bundle version")
        rows = manifest.get("files")
        if not isinstance(rows, list):
            raise ValueError("clean-system bundle manifest files are absent")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("clean-system bundle manifest row is invalid")
            rel = PurePosixPath(str(row.get("path", "")))
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError("clean-system bundle contains unsafe path")
            name = rel.as_posix()
            if name not in names:
                raise ValueError(f"clean-system bundle file is missing: {name}")
            data = archive.read(name)
            if len(data) != int(row.get("bytes", -1)):
                raise ValueError(f"clean-system bundle size mismatch: {name}")
            if _sha256_bytes(data) != str(row.get("sha256", "")).lower():
                raise ValueError(f"clean-system bundle hash mismatch: {name}")
            retained[name] = data
        if "clean-system-report.json" not in names:
            raise ValueError("clean-system result bundle has no report")
        report = json.loads(
            archive.read("clean-system-report.json").decode("utf-8-sig")
        )
    if not isinstance(report, dict):
        raise ValueError("clean-system report is not a JSON object")
    return report, bundle_sha, retained


def _runtime_hardware_from_bundle(files: Mapping[str, bytes]) -> dict[str, Any]:
    values: dict[str, str] = {}
    for name, data in files.items():
        if not name.endswith(".stdout.txt"):
            continue
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if not line.startswith("NW_PLAYER_") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key, value.strip())
    result: dict[str, Any] = {}
    mapping = {
        "NW_PLAYER_ADAPTER_DESCRIPTION": "adapter_description",
        "NW_PLAYER_ADAPTER_GENERATION": "adapter_generation",
        "NW_PLAYER_DRIVER_VERSION": "driver_version",
    }
    for source, target in mapping.items():
        if values.get(source):
            result[target] = values[source]
    for source, target in (
        ("NW_PLAYER_ADAPTER_VENDOR_ID", "vendor_id"),
        ("NW_PLAYER_ADAPTER_DEVICE_ID", "device_id"),
    ):
        if values.get(source):
            try:
                result[target] = int(values[source], 0)
            except ValueError:
                pass
    return result


def _clean_summary(
    report: Mapping[str, Any],
    *,
    bundle_sha256: str,
    artifact: Mapping[str, Any],
    runtime_hardware: Mapping[str, Any],
) -> dict[str, Any]:
    if report.get("verification_status") != "verified":
        raise ValueError("clean-system result is not verified")
    host = report.get("host")
    deployment = report.get("deployment")
    scenarios = report.get("scenarios")
    if not isinstance(host, Mapping) or not isinstance(deployment, Mapping):
        raise ValueError("clean-system report host/deployment is missing")
    if not isinstance(scenarios, list) or len(scenarios) != 6:
        raise ValueError("clean-system report does not contain six scenarios")
    if host.get("execution_kind") != "physical":
        raise ValueError("clean-system evidence is not physical")
    if str(host.get("architecture", "")).lower() not in {"amd64", "x86_64"}:
        raise ValueError("clean-system evidence host is not x64")
    if not str(host.get("windows_build") or "").strip():
        raise ValueError("clean-system evidence host has no measured Windows build")
    if str(host.get("os_build") or host.get("windows_build") or "").strip() == "":
        raise ValueError("clean-system evidence host has no measured OS build")
    if host.get("clean_environment") is not True:
        raise ValueError("clean-system host is not clean")
    if host.get("development_dependencies_present_on_path") not in ([], tuple()):
        raise ValueError("clean-system host exposes development dependencies")
    if host.get("environment_cache_variables") not in ([], tuple()):
        raise ValueError("clean-system host exposes cache environment variables")
    if deployment.get("only_runtime_executable") is not True:
        raise ValueError("clean-system deployment is not a single executable")
    if str(deployment.get("executable_sha256", "")).lower() != artifact["executable_sha256"]:
        raise ValueError("clean-system executable does not match final intro artifact")
    if int(deployment.get("executable_bytes", -1)) != artifact["executable_bytes"]:
        raise ValueError("clean-system executable size does not match final intro artifact")
    rows: list[dict[str, Any]] = []
    for row in scenarios:
        if not isinstance(row, Mapping):
            raise ValueError("clean-system scenario row is invalid")
        name = row.get("scenario")
        if name not in EXPECTED_SCENARIO_EXIT_CODES:
            raise ValueError(f"clean-system scenario is not frozen: {name}")
        expected = EXPECTED_SCENARIO_EXIT_CODES[str(name)]
        if row.get("expected_exit_code") != expected:
            raise ValueError(
                f"clean-system scenario {name} redefines the frozen expected exit code"
            )
        if row.get("status") != "passed" or row.get("timed_out") is not False:
            raise ValueError(f"clean-system scenario failed: {row.get('scenario')}")
        if row.get("observed_exit_code") != expected:
            raise ValueError(
                f"clean-system scenario exit mismatch: {row.get('scenario')}"
            )
        rows.append(
            {
                "scenario": name,
                "expected_exit_code": expected,
                "observed_exit_code": expected,
                "status": "passed",
                "timed_out": False,
            }
        )
    if sorted(str(row["scenario"]) for row in rows) != sorted(
        EXPECTED_SCENARIO_EXIT_CODES
    ):
        raise ValueError("clean-system scenario set is not the frozen six")
    probe = host.get("virtualization_probe")
    if not isinstance(probe, Mapping):
        raise ValueError("clean-system virtualization probe is absent")
    if probe.get("hypervisor_present") is True and not probe.get(
        "hypervisor_explained_by_vbs"
    ):
        raise ValueError("clean-system host has an unexplained active hypervisor")
    markers = list(host.get("vm_markers") or [])
    markers.extend(host.get("virtualization_indicators") or [])
    markers.extend(probe.get("vm_markers") or [])
    if markers:
        raise ValueError(
            "clean-system host exposes virtualization markers: "
            + ", ".join(sorted({str(item) for item in markers}))
        )
    hardware = dict(runtime_hardware)
    if not hardware:
        artifact_record = report.get("artifact")
        if isinstance(artifact_record, Mapping):
            candidate = artifact_record.get("hardware")
            if isinstance(candidate, Mapping):
                hardware = dict(candidate)
    if not hardware:
        candidate = host.get("hardware")
        if isinstance(candidate, Mapping):
            hardware = dict(candidate)
    return {
        "status": "verified",
        "execution_kind": "physical",
        "captured_at_utc": str(report.get("captured_at_utc", "")),
        "source_result_bundle_sha256": bundle_sha256,
        "host": {
            "virtualization_probe": {
                "manufacturer": probe.get("manufacturer"),
                "model": probe.get("model"),
                "product_vendor": probe.get("product_vendor"),
                "product_name": probe.get("product_name"),
                "bios_manufacturer": probe.get("bios_manufacturer"),
                "bios_version": probe.get("bios_version"),
                "baseboard_manufacturer": probe.get("baseboard_manufacturer"),
                "baseboard_product": probe.get("baseboard_product"),
                "hypervisor_present": probe.get("hypervisor_present"),
                "virtualization_based_security_status": probe.get(
                    "virtualization_based_security_status"
                ),
                "hypervisor_explained_by_vbs": probe.get(
                    "hypervisor_explained_by_vbs"
                ),
                "vm_markers": markers,
            },
            "manufacturer": probe.get("manufacturer"),
            "model": probe.get("model"),
            "product_vendor": probe.get("product_vendor"),
            "product_name": probe.get("product_name"),
            "architecture": host.get("architecture"),
            "windows_build": host.get("windows_build"),
            "hypervisor_present": probe.get("hypervisor_present"),
            "virtualization_based_security_status": probe.get(
                "virtualization_based_security_status"
            ),
            "hypervisor_explained_by_vbs": probe.get(
                "hypervisor_explained_by_vbs"
            ),
            "virtualization_indicators": host.get("virtualization_indicators", []),
            "vm_markers": markers,
            "clean_environment": True,
        },
        "hardware": {
            "adapter_description": hardware.get("adapter_description", "unavailable"),
            "adapter_generation": hardware.get("adapter_generation", "unavailable"),
            "vendor_id": hardware.get("vendor_id", 0),
            "device_id": hardware.get("device_id", 0),
            "driver_version": hardware.get("driver_version", "unavailable"),
        },
        "development_dependencies_present_on_path": [],
        "environment_cache_variables": [],
        "deployment": {
            "only_runtime_executable": True,
            "sidecars": list(deployment.get("sidecars") or []),
            "executable_sha256": artifact["executable_sha256"],
            "executable_bytes": artifact["executable_bytes"],
            "forbidden_runtime_dependencies": list(
                deployment.get("forbidden_runtime_dependencies") or []
            ),
        },
        "scenarios": rows,
    }


def _upsert_status_file(
    status: dict[str, Any], *, path: str, sha256: str
) -> None:
    normalized = path.replace("\\", "/")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError(f"status file hash is malformed for {normalized}")
    rows = status.get("files")
    if rows is None:
        rows = []
        status["files"] = rows
    if not isinstance(rows, list):
        raise ValueError("evidence status files ledger is not a list")
    retained: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("evidence status files ledger row is invalid")
        row_path = str(row.get("path", "")).replace("\\", "/")
        if row_path == normalized:
            continue
        retained.append(dict(row))
    retained.append({"path": normalized, "sha256": sha256})
    retained.sort(key=lambda row: str(row.get("path", "")))
    status["files"] = retained


class UnsupportedEvidenceStatusError(ValueError):
    """Raised when this revision cannot safely interpret an aggregate status."""


_KNOWN_EVIDENCE_STATUSES = frozenset(
    {
        "pending_physical_windows_capture",
        "performance_verified_correctness_pending",
        "performance_and_compact_verified_correctness_pending",
        "performance_compact_and_correctness_verified_system_pending",
        "performance_and_correctness_verified_system_pending",
        "performance_correctness_and_system_verified_packaging_pending",
        "verified_complete",
        "correctness_and_system_reused_performance_pending",
    }
)


def _require_known_evidence_status(status: Mapping[str, Any]) -> str:
    value = status.get("evidence_status")
    if not isinstance(value, str) or value not in _KNOWN_EVIDENCE_STATUSES:
        raise UnsupportedEvidenceStatusError(
            "unsupported NW-10 evidence_status "
            f"{value!r}; refusing to reinterpret or downgrade a status "
            "produced by another revision"
        )
    return value


def _load_status_for_mutation(path: Path) -> dict[str, Any]:
    status = _load(path)
    if status.get("format") != "nw10-evidence-status":
        raise ValueError("NW-10 evidence-status file has an unexpected format")
    _require_known_evidence_status(status)
    return status


def _refresh_status(status: dict[str, Any]) -> dict[str, Any]:
    _require_known_evidence_status(status)
    perf = (
        isinstance(status.get("performance_preservation"), Mapping)
        and status["performance_preservation"].get("status") == "verified_preserved"
    )
    correctness = (
        isinstance(status.get("correctness_evidence"), Mapping)
        and status["correctness_evidence"].get("status") == "verified"
    )
    system = (
        isinstance(status.get("system_validation"), Mapping)
        and status["system_validation"].get("status") == "verified"
    )
    compact = (
        isinstance(status.get("compact_feasibility"), Mapping)
        and status["compact_feasibility"].get("status") == "verified_windows_runtime"
    )
    pe = (
        isinstance(status.get("pe_ledger"), Mapping)
        and status["pe_ledger"].get("status") == "verified"
    )

    status["reference_capture"] = (
        "verified_reference_capture" if perf else "pending_reference_capture"
    )
    status["frame_time"] = "verified_preserved" if perf else "pending_unverified"
    status["ada_correctness"] = "verified" if correctness else "pending_unverified"
    status["blackwell_correctness"] = "verified" if correctness else "pending_unverified"
    status["clean_system"] = "verified" if system else "pending_unverified"
    status["startup"] = "verified" if system else "pending_unverified"
    status["memory"] = "verified" if system else "pending_unverified"

    if perf and correctness and system:
        status["physical_evidence"] = "performance_correctness_and_system_verified"
    elif perf and correctness:
        status["physical_evidence"] = "performance_and_correctness_verified"
    elif correctness and system:
        status["physical_evidence"] = "correctness_and_system_reused"
    elif perf:
        status["physical_evidence"] = "performance_verified"
    else:
        status["physical_evidence"] = "pending"

    if perf and correctness and system and compact and pe:
        status["evidence_status"] = "verified_complete"
    elif perf and correctness and system:
        status["evidence_status"] = (
            "performance_correctness_and_system_verified_packaging_pending"
        )
    elif perf and correctness and compact:
        status["evidence_status"] = (
            "performance_compact_and_correctness_verified_system_pending"
        )
    elif perf and correctness:
        status["evidence_status"] = "performance_and_correctness_verified_system_pending"
    elif perf and compact:
        status["evidence_status"] = (
            "performance_and_compact_verified_correctness_pending"
        )
    elif perf:
        status["evidence_status"] = "performance_verified_correctness_pending"
    elif correctness and system:
        status["evidence_status"] = "correctness_and_system_reused_performance_pending"
    else:
        status["evidence_status"] = "pending_physical_windows_capture"
    return status


def build_pending_correctness(
    *, implementation_head: str | None = None
) -> dict[str, Any]:
    """Build the explicit non-certifying correctness state.

    The physical record can only be replaced by ``publish_correctness`` after a
    direct separate packed-GPU capture set exists on both representative
    desktop tiers.  This pending record keeps the status machine honest and is
    reproducible from a clean checkout.
    """
    if implementation_head not in (None, ""):
        raise ValueError("pending correctness cannot bind an implementation head")
    head = None
    return {
        "format": "nw10-common-artifact-correctness-summary",
        "version": 1,
        "task": "NW-10",
        "status": "pending_unverified",
        "capture_protocol": "nw07-correctness-capture.v1",
        "common_artifact": {"package_sha256": None},
        "ada": {
            "hardware": {
                "adapter_description": "unavailable",
                "classified_tier": "ada",
                "device_form_factor": "unknown",
                "device_form_factor_source": "pending_physical_probe",
                "driver_version": "unavailable",
            },
            "status": "pending_unverified",
        },
        "blackwell": {
            "hardware": {
                "adapter_description": "unavailable",
                "classified_tier": "blackwell",
                "device_form_factor": "unknown",
                "device_form_factor_source": "pending_physical_probe",
                "driver_version": "unavailable",
            },
            "status": "pending_unverified",
        },
        "comparison": {
            "absolute_required": True,
            "absolute_tolerance": 0.0001,
            "ada_loop_boundary_status": "pending",
            "blackwell_loop_boundary_status": "pending",
            "fixed_tick_status": "pending",
            "fixed_ticks": [0, 240000, 480000, 720000, 959999],
            "loop_boundary_ticks": [0, 960000],
            "relative_tolerance": 0.0001,
            "worst_absolute_error": None,
            "worst_relative_error": None,
        },
        "packed_unpacked_comparison": {
            "direct_separate_packed_gpu_capture_recorded": False,
            "codec_id": None,
            "layout_id": None,
            "packed_package_sha256": None,
            "runtime": {
                "ada": {"status": "pending_physical"},
                "blackwell": {"status": "pending_physical"},
            },
            "status": "pending_physical",
            "unpacked_package_sha256": None,
            "worst_absolute_error": None,
        },
        "privacy": {
            "local_paths_included": False,
            "raw_capture_bytes_embedded": False,
            "stdout_stderr_included": False,
        },
        "source": {
            "execution_kind": "none",
            "implementation_head": head,
            "mode": "pending",
            "raw_capture_manifests_tracked": False,
            "tool": "tools/nw10/run-correctness-evidence.ps1",
        },
        "archived_physical_evidence": {
            "path": "benchmarks/nw10/reused-physical-evidence.v1.json",
            "reason": (
                "The prior cross-hardware captures were not a direct "
                "packed/unpacked GPU comparison and were not captured at the "
                "current source head."
            ),
            "source_head_sha": ARCHIVED_PHYSICAL_SOURCE_HEAD,
            "status": "archived_not_current_head",
        },
    }


def build_pending_system_validation(
    *, implementation_head: str | None = None
) -> dict[str, Any]:
    """Build the explicit non-certifying system-validation state."""
    if implementation_head not in (None, ""):
        raise ValueError("pending system validation cannot bind an implementation head")
    head = None
    return {
        "format": "nw10-system-validation",
        "version": 1,
        "task": "NW-10",
        "status": "pending_unverified",
        "source_head_sha": head,
        "final_artifact": None,
        "startup": None,
        "memory": None,
        "clean_system": None,
        "windows_build": None,
        "reason": (
            "No single physical environment captured the exact current-head "
            "final artifact with clean-system startup and engine-committed "
            "memory. The previous record mixed clean-system and metric hosts, "
            "omitted the Windows build, and referenced a non-runnable "
            "0xC0000005 artifact; those claims were withdrawn."
        ),
        "archived_physical_evidence": {
            "path": "benchmarks/nw10/reused-physical-evidence.v1.json",
            "status": "archived_not_current_head",
            "source_head_sha": ARCHIVED_PHYSICAL_SOURCE_HEAD,
            "current_executable_sha256": (
                "063bb45b72db60037e743e971157b0cbd8d3096c7ee69680fc34f70d705027c8"
            ),
            "measured_executable_sha256": (
                "6c6fdb40461bfecc02427efeccfcbeb00c2a8424217d893a15e5d2711a9b154e"
            ),
            "reason": (
                "The historical system measurements are retained for "
                "attribution but cannot certify the current checkout."
            ),
        },
        "privacy": {
            "local_paths_included": False,
            "raw_result_bundle_tracked": False,
            "raw_stdout_stderr_included": False,
            "user_profile_paths_included": False,
        },
    }


def publish_correctness(args: argparse.Namespace) -> dict[str, Any]:
    status_path = REPO_ROOT / args.status_file
    status = _load_status_for_mutation(status_path)
    source = _load(Path(args.comparison))
    if source.get("format") != "nw10-common-artifact-correctness":
        raise ValueError("unexpected correctness comparison format")
    if source.get("status") != "verified":
        raise ValueError("correctness comparison is not verified")
    if source.get("test_payload") is True:
        raise ValueError("refusing to publish test-payload correctness evidence")
    source_meta = source.get("source")
    if not isinstance(source_meta, Mapping):
        raise ValueError("correctness comparison has no source provenance")
    if source_meta.get("execution_kind") != "physical_cross_hardware":
        raise ValueError(
            "correctness comparison is not physical cross-hardware evidence"
        )
    if source_meta.get("tool") != "tools/nw10/run-correctness-evidence.ps1":
        raise ValueError("correctness comparison came from an unrecognized tool")
    source_head = str(source_meta.get("implementation_head", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", source_head):
        raise ValueError("correctness comparison has no implementation head")
    requested_head = args.implementation_head or _git_head()
    if source_head != requested_head:
        raise ValueError(
            "correctness comparison was captured at a different implementation head"
        )
    privacy = source.get("privacy")
    if not isinstance(privacy, Mapping):
        raise ValueError("correctness comparison has no privacy declaration")
    if privacy.get("raw_capture_bytes_embedded") is not False:
        raise ValueError("correctness comparison embeds raw capture bytes")
    if privacy.get("local_paths_included") is not False:
        raise ValueError("correctness comparison exposes local paths")

    common = source.get("common_artifact")
    ada = source.get("ada")
    blackwell = source.get("blackwell")
    comparison = source.get("comparison")
    if not all(isinstance(item, Mapping) for item in (common, ada, blackwell, comparison)):
        raise ValueError("correctness comparison structure is incomplete")
    fixed_ticks = [0, 240000, 480000, 720000, 959999]
    if comparison.get("fixed_ticks") not in (None, fixed_ticks):
        raise ValueError("correctness comparison changed the frozen fixed ticks")
    if comparison.get("absolute_tolerance") != 0.0001:
        raise ValueError("correctness comparison changed the frozen image absolute tolerance")
    if comparison.get("relative_tolerance") != 0.0001:
        raise ValueError("correctness comparison changed the frozen image relative tolerance")
    if comparison.get("absolute_required") is not True:
        raise ValueError("correctness comparison must require absolute image error")
    per_tick = comparison.get("per_tick")
    if not isinstance(per_tick, list) or not per_tick:
        raise ValueError("correctness comparison has no per-tick rows")
    if [int(row.get("tick", -1)) for row in per_tick if isinstance(row, Mapping)] != fixed_ticks:
        raise ValueError("correctness comparison is missing an exact frozen fixed tick")
    if any(row.get("status") != "passed" for row in per_tick if isinstance(row, Mapping)):
        raise ValueError("correctness comparison contains failed fixed ticks")
    worst_absolute = comparison.get("worst_absolute_error")
    worst_relative = comparison.get("worst_relative_error")
    if worst_absolute is None or worst_relative is None:
        raise ValueError("correctness comparison omitted measured error values")
    if float(worst_absolute) > 0.0001 or float(worst_relative) > 0.0001:
        raise ValueError("correctness comparison exceeds the frozen image tolerances")
    packed = source.get("packed_unpacked_comparison")
    if not isinstance(packed, Mapping):
        raise ValueError(
            "correctness comparison has no packed/unpacked artifact comparison"
        )
    if packed.get("status") != "verified":
        raise ValueError("packed/unpacked correctness comparison is not verified")
    if packed.get("direct_separate_packed_gpu_capture_recorded") is not True:
        raise ValueError(
            "packed/unpacked correctness requires a direct separate packed "
            "GPU capture set; compositional decoder parity is not physical proof"
        )
    if str(packed.get("packed_package_sha256", "")).lower() != str(
        packed.get("unpacked_package_sha256", "")
    ).lower():
        raise ValueError("packed and unpacked packages are not the same artifact")
    common_package_sha = str(common.get("package_sha256", "")).lower()
    if str(packed.get("packed_package_sha256", "")).lower() != common_package_sha:
        raise ValueError(
            "packed/unpacked comparison is not bound to the common artifact"
        )
    codec_id = packed.get("codec_id")
    layout_id = packed.get("layout_id")
    if codec_id not in {"nwc.codec.stored.v1", "nwc.codec.mszip.v1"}:
        raise ValueError("packed/unpacked comparison has no explicit supported codec identity")
    if layout_id not in {
        "nwc.layout.canonical.v1",
        "nwc.layout.aos.v1",
        "nwc.layout.soa.v1",
    }:
        raise ValueError("packed/unpacked comparison has no explicit supported layout identity")
    packed_error = packed.get("worst_absolute_error")
    if packed_error is None or float(packed_error) > 0.0001:
        raise ValueError(
            "packed/unpacked visual comparison exceeds the frozen image tolerance"
        )
    packed_runtime = packed.get("runtime")
    if not isinstance(packed_runtime, Mapping):
        raise ValueError("packed/unpacked runtime comparison is absent")
    for tier in ("ada", "blackwell"):
        entry = packed_runtime.get(tier)
        if not isinstance(entry, Mapping) or entry.get("status") != "passed":
            raise ValueError(
                f"packed/unpacked {tier} runtime comparison did not pass"
            )
        for key in (
            "unpacked_executable_sha256",
            "packed_executable_sha256",
            "encoded_container_sha256",
        ):
            if not re.fullmatch(
                r"[0-9a-f]{64}", str(entry.get(key, "")).lower()
            ):
                raise ValueError(
                    f"packed/unpacked {tier} runtime is missing {key}"
                )
        if entry.get("codec_id") != codec_id or entry.get("layout_id") != layout_id:
            raise ValueError(
                f"packed/unpacked {tier} runtime changed codec/layout identity"
            )
        form_factor_source = str(entry.get("device_form_factor_source", ""))
        if (
            not form_factor_source
            or form_factor_source in {"asserted", "pending_physical_probe", "unsupported_host"}
            or "reused" in form_factor_source.casefold()
            or "inventory" in form_factor_source.casefold()
        ):
            raise ValueError(
                f"packed/unpacked {tier} runtime lacks a measured chassis probe"
            )
        if int(entry.get("frames_per_tick", -1)) != 1:
            raise ValueError(
                f"packed/unpacked {tier} runtime changed frames-per-tick"
            )
        if int(entry.get("present_sync_interval", -1)) != 0:
            raise ValueError(
                f"packed/unpacked {tier} runtime is not uncapped"
            )
    ada_loop = ada.get("loop_boundary")
    blackwell_loop = blackwell.get("loop_boundary")
    if not isinstance(ada_loop, Mapping) or not isinstance(blackwell_loop, Mapping):
        raise ValueError("correctness comparison has no loop-boundary results")
    if ada_loop.get("status") != "passed" or blackwell_loop.get("status") != "passed":
        raise ValueError("correctness loop-boundary check failed")

    def hardware_summary(row: Mapping[str, Any], tier: str) -> dict[str, Any]:
        hardware = row.get("hardware")
        if not isinstance(hardware, Mapping):
            raise ValueError(f"{tier} correctness hardware is missing")
        if hardware.get("classified_tier") not in (None, tier):
            raise ValueError(f"{tier} correctness hardware tier is inconsistent")
        if str(hardware.get("device_form_factor", "")).lower() != "desktop":
            raise ValueError(
                f"{tier} correctness hardware was not measured as desktop"
            )
        form_factor_source = str(hardware.get("device_form_factor_source", ""))
        if (
            form_factor_source in ("", "asserted")
            or "reused" in form_factor_source.casefold()
            or "inventory" in form_factor_source.casefold()
        ):
            raise ValueError(
                f"{tier} correctness form factor was not measured by a chassis probe"
            )
        result = {
            "adapter_description": hardware.get("adapter_description"),
            "classified_tier": tier,
            "device_form_factor": hardware.get("device_form_factor"),
            "device_form_factor_source": hardware.get("device_form_factor_source"),
        }
        if hardware.get("driver_version"):
            result["driver_version"] = hardware.get("driver_version")
        return result

    public = {
        "format": "nw10-common-artifact-correctness-summary",
        "version": 1,
        "task": "NW-10",
        "source_head_sha": args.implementation_head or _git_head(),
        "status": "verified",
        "capture_protocol": source.get("capture_protocol"),
        "common_artifact": {
            "package_sha256": str(common.get("package_sha256", "")).lower()
        },
        "ada": {"status": "verified", "hardware": hardware_summary(ada, "ada")},
        "blackwell": {
            "status": "verified",
            "hardware": hardware_summary(blackwell, "blackwell"),
        },
        "comparison": {
            "fixed_ticks": [int(row["tick"]) for row in per_tick],
            "fixed_tick_status": "passed",
            "loop_boundary_ticks": [0, 960000],
            "ada_loop_boundary_status": "passed",
            "blackwell_loop_boundary_status": "passed",
            "worst_absolute_error": float(worst_absolute),
            "worst_relative_error": float(worst_relative),
            "absolute_tolerance": float(comparison.get("absolute_tolerance")),
            "relative_tolerance": float(comparison.get("relative_tolerance")),
            "absolute_required": bool(comparison.get("absolute_required")),
        },
        "packed_unpacked_comparison": {
            "status": "verified",
            "direct_separate_packed_gpu_capture_recorded": True,
            "codec_id": codec_id,
            "layout_id": layout_id,
            "packed_package_sha256": str(
                packed.get("packed_package_sha256")
            ).lower(),
            "unpacked_package_sha256": str(
                packed.get("unpacked_package_sha256")
            ).lower(),
            "worst_absolute_error": float(packed_error),
            "runtime": {
                "ada": {
                    "status": "passed",
                    "unpacked_executable_sha256": packed_runtime["ada"].get(
                        "unpacked_executable_sha256"
                    ),
                    "packed_executable_sha256": packed_runtime["ada"].get(
                        "packed_executable_sha256"
                    ),
                    "encoded_container_sha256": packed_runtime["ada"].get(
                        "encoded_container_sha256"
                    ),
                    "codec_id": packed_runtime["ada"].get("codec_id"),
                    "layout_id": packed_runtime["ada"].get("layout_id"),
                    "device_form_factor_source": packed_runtime["ada"].get(
                        "device_form_factor_source"
                    ),
                    "frames_per_tick": packed_runtime["ada"].get("frames_per_tick"),
                    "present_sync_interval": packed_runtime["ada"].get(
                        "present_sync_interval"
                    ),
                },
                "blackwell": {
                    "status": "passed",
                    "unpacked_executable_sha256": packed_runtime[
                        "blackwell"
                    ].get("unpacked_executable_sha256"),
                    "packed_executable_sha256": packed_runtime["blackwell"].get(
                        "packed_executable_sha256"
                    ),
                    "encoded_container_sha256": packed_runtime["blackwell"].get(
                        "encoded_container_sha256"
                    ),
                    "codec_id": packed_runtime["blackwell"].get("codec_id"),
                    "layout_id": packed_runtime["blackwell"].get("layout_id"),
                    "device_form_factor_source": packed_runtime["blackwell"].get(
                        "device_form_factor_source"
                    ),
                    "frames_per_tick": packed_runtime["blackwell"].get(
                        "frames_per_tick"
                    ),
                    "present_sync_interval": packed_runtime["blackwell"].get(
                        "present_sync_interval"
                    ),
                },
            },
        },
        "privacy": {
            "raw_capture_bytes_embedded": False,
            "local_paths_included": False,
            "stdout_stderr_included": False,
        },
        "source": {
            "mode": "compare",
            "execution_kind": "physical_cross_hardware",
            "tool": "tools/nw10/run-correctness-evidence.ps1",
            "implementation_head": args.implementation_head or _git_head(),
            "raw_capture_manifests_tracked": False,
        },
    }
    package_sha = public["common_artifact"]["package_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", package_sha):
        raise ValueError("correctness package SHA-256 is malformed")
    _assert_privacy(public, list(args.forbid_token or []))

    destination = REPO_ROOT / args.destination
    _write(destination, public)
    correctness_sha = _text_sha256(destination)
    correctness_path = args.destination.replace("\\", "/")
    status["correctness_evidence"] = {
        "status": "verified",
        "path": correctness_path,
        "sha256": correctness_sha,
        "package_sha256": package_sha,
        "worst_absolute_error": public["comparison"]["worst_absolute_error"],
        "worst_relative_error": public["comparison"]["worst_relative_error"],
    }
    _upsert_status_file(
        status, path=correctness_path, sha256=correctness_sha
    )
    status["source_head_sha"] = public["source_head_sha"]
    _refresh_status(status)
    _assert_privacy(status, list(args.forbid_token or []))
    _write(status_path, status)
    return public


def publish_system(args: argparse.Namespace) -> dict[str, Any]:
    status_path = REPO_ROOT / args.status_file
    status = _load_status_for_mutation(status_path)
    intro = _load(Path(args.intro_report))
    artifact = _artifact_from_intro_report(intro)
    clean_report, clean_bundle_sha, clean_files = _load_clean_bundle(
        Path(args.clean_system_bundle)
    )
    clean = _clean_summary(
        clean_report,
        bundle_sha256=clean_bundle_sha,
        artifact=artifact,
        runtime_hardware=_runtime_hardware_from_bundle(clean_files),
    )
    metrics = _load(Path(args.metrics))
    metadata = metrics.get("metadata")
    measurements = metrics.get("measurements")
    if not isinstance(metadata, Mapping) or not isinstance(measurements, Mapping):
        raise ValueError("metrics JSON requires metadata and measurements objects")
    if str(metadata.get("executable_sha256", "")).lower() != artifact["executable_sha256"]:
        raise ValueError(
            "metrics JSON is not bound to the exact final intro executable"
        )
    if str(metadata.get("package_sha256", "")).lower() != artifact["package_sha256"]:
        raise ValueError("metrics JSON is not bound to the exact final package")
    if str(metadata.get("execution_kind", "physical")) != "physical":
        raise ValueError("metrics JSON is not a physical measurement")
    clean_hardware = clean.get("hardware", {})
    metrics_adapter = str(metadata.get("adapter_description", ""))
    if metrics_adapter != str(clean_hardware.get("adapter_description", "")):
        raise ValueError(
            "metrics hardware does not match the clean-system evidence host"
        )
    if str(metadata.get("driver_version", "")) != str(
        clean_hardware.get("driver_version", "")
    ):
        raise ValueError(
            "metrics driver does not match the clean-system evidence host"
        )
    metrics_windows_build = str(
        metadata.get("windows_build") or metadata.get("os_build") or ""
    )
    if metrics_windows_build != str(clean.get("host", {}).get("windows_build", "")):
        raise ValueError(
            "metrics Windows build does not match the clean-system evidence host"
        )
    startup_ms = float(measurements.get("process_start_to_first_present_ms"))
    peak_gpu = int(measurements.get("peak_engine_committed_gpu_bytes"))
    if startup_ms < 0 or startup_ms > STARTUP_CEILING_MS:
        raise ValueError("startup measurement exceeds the NW-10 ceiling")
    if peak_gpu < 0 or peak_gpu > MEMORY_CEILING_BYTES:
        raise ValueError("GPU memory measurement exceeds the NW-10 ceiling")
    post_init = measurements.get("post_initialization_allocation_violation")
    if post_init is None:
        raise ValueError(
            "metrics JSON must retain post_initialization_allocation_violation"
        )
    if int(post_init) != 0:
        raise ValueError("post-initialization allocation violation is nonzero")
    cache_kind = str(measurements.get("cache_kind", ""))
    if cache_kind not in {"cold_driver_cache", "warm"}:
        raise ValueError("metrics JSON has invalid cache_kind")
    adapter = str(metadata.get("adapter_description", ""))
    driver = str(metadata.get("driver_version", ""))
    if not adapter or not driver:
        raise ValueError("metrics JSON has incomplete hardware identity")

    startup = {
        "status": "verified",
        "protocol": "nw07-startup.v1",
        "execution_kind": "physical",
        "hardware": {
            "adapter_description": adapter,
            "driver_version": driver,
            "windows_build": metrics_windows_build,
        },
        "cache_state": {
            "kind": cache_kind,
            "application_pipeline_cache_used": False,
        },
        "warm_preparation": str(measurements.get("warm_preparation", "")),
        "cold_driver_cache_preparation": str(
            measurements.get("cold_driver_cache_preparation", "")
        ),
        "measurement_method": str(
            measurements.get(
                "startup_measurement_method",
                "fresh physical metrics collector bound to the exact NW-10 artifact",
            )
        ),
        "process_start_to_first_present_ms": startup_ms,
        "ceiling_ms": STARTUP_CEILING_MS,
    }
    for key in ("lower_bound_ms", "process_creation_window_ms"):
        if measurements.get(key) is not None:
            startup[key] = float(measurements[key])

    memory = {
        "status": "verified",
        "execution_kind": "physical",
        "hardware": {
            "adapter_description": adapter,
            "driver_version": driver,
            "windows_build": metrics_windows_build,
        },
        "package_sha256": artifact["package_sha256"],
        "peak_engine_committed_gpu_bytes": peak_gpu,
        "ceiling_bytes": MEMORY_CEILING_BYTES,
        "post_initialization_allocation_violation": 0,
        "measurement_source": str(measurements.get("memory_source", "")),
        "resolution": {"width": 1920, "height": 1080},
    }
    optional_ints = {
        "engine_resource_bytes": "engine_resource_bytes",
        "engine_upload_bytes": "engine_upload_bytes",
        "engine_cumulative_committed_bytes": "engine_cumulative_committed_bytes",
        "frames": "frames",
    }
    for source_key, public_key in optional_ints.items():
        if measurements.get(source_key) is not None:
            memory[public_key] = int(measurements[source_key])
    if measurements.get("memory_accounting_scope"):
        memory["accounting_scope"] = str(measurements["memory_accounting_scope"])

    public = {
        "format": "nw10-system-validation",
        "version": 1,
        "task": "NW-10",
        "status": "verified",
        "source_head_sha": _git_head(),
        "final_artifact": artifact,
        "windows_build": str(clean.get("host", {}).get("windows_build", "")),
        "environment": {
            "adapter_description": adapter,
            "driver_version": driver,
            "windows_build": str(clean.get("host", {}).get("windows_build", "")),
        },
        "startup": startup,
        "memory": memory,
        "clean_system": clean,
        "privacy": {
            "local_paths_included": False,
            "raw_stdout_stderr_included": False,
            "user_profile_paths_included": False,
            "raw_result_bundle_tracked": False,
        },
    }
    _assert_privacy(public, list(args.forbid_token or []))

    destination = REPO_ROOT / args.destination
    _write(destination, public)
    system_sha = _text_sha256(destination)
    system_path = args.destination.replace("\\", "/")
    status["system_validation"] = {
        "status": "verified",
        "path": system_path,
        "sha256": system_sha,
        "final_executable_sha256": artifact["executable_sha256"],
        "package_sha256": artifact["package_sha256"],
    }
    _upsert_status_file(status, path=system_path, sha256=system_sha)
    status["source_head_sha"] = public["source_head_sha"]
    _refresh_status(status)
    _assert_privacy(status, list(args.forbid_token or []))
    _write(status_path, status)
    return public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    correctness = sub.add_parser("correctness")
    correctness.add_argument("--comparison", required=True)
    correctness.add_argument(
        "--destination",
        default=CORRECTNESS_PATH.as_posix(),
    )
    correctness.add_argument("--status-file", default=STATUS_PATH.as_posix())
    correctness.add_argument("--implementation-head", default="")
    correctness.add_argument("--forbid-token", action="append", default=[])

    system = sub.add_parser("system")
    system.add_argument("--clean-system-bundle", required=True)
    system.add_argument("--metrics", required=True)
    system.add_argument("--intro-report", required=True)
    system.add_argument("--destination", default=SYSTEM_PATH.as_posix())
    system.add_argument("--status-file", default=STATUS_PATH.as_posix())
    system.add_argument("--forbid-token", action="append", default=[])

    pending_correctness = sub.add_parser("pending-correctness")
    pending_correctness.add_argument(
        "--destination", default=CORRECTNESS_PATH.as_posix()
    )

    pending_system = sub.add_parser("pending-system")
    pending_system.add_argument("--destination", default=SYSTEM_PATH.as_posix())

    args = parser.parse_args(argv)
    try:
        if args.command == "correctness":
            public = publish_correctness(args)
        elif args.command == "system":
            public = publish_system(args)
        elif args.command == "pending-correctness":
            public = build_pending_correctness()
            _write(REPO_ROOT / args.destination, public)
        else:
            public = build_pending_system_validation()
            _write(REPO_ROOT / args.destination, public)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "format": "nw10-physical-evidence-publisher",
                    "status": "failed",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": public.get("status"),
                "format": public.get("format"),
                "destination": args.destination,
                "status_file": getattr(args, "status_file", None),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
