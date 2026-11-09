#!/usr/bin/env python3
"""Nodewerk NW-01 Verification Runner backing tools/verify-nw01.ps1.

Supports two documented modes:
- Full:
    Performs build, schema, dependency, reproducibility, fixture-validation,
    and evidence-completeness checks, recording reproducibility results to
    benchmarks/reproducibility/release-build-reproducibility.json when built.
    Missing prerequisites or unverified physical hardware/clean-system evidence
    NEVER produce a passing Full result.
- CleanSystem:
    Probes SDK absence, verifies deployed dependency closure, executes release
    fixtures on a target clean physical Windows 11 x64 machine without requiring
    developer SDKs, DXC, or Agility SDK, and records the verified evidence back
    to benchmarks/clean-system/clean-system-validation.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from dependency_check import (
    DependencyClosureError,
    collect_deployment_files,
    parse_pe64_sections_and_imports,
    verify_all_recorded_ledgers,
    verify_closure,
)
from evidence_checks import desktop_cpu_model_matches
from offline_shader_pack import (
    check_hlsl_source_constraints,
    find_dxc_executable,
    query_dxc_version,
    validate_dxil_container,
)
from schema_validator import validate_all_repository_artifacts, validate_file_against_schema
from size_ledger import generate_size_comparison_report, record_release_ledgers


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_source_commit(repo_root: Path) -> str | None:
    """Return the checked-out Git revision without depending on CI environment variables."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    candidate = proc.stdout.strip().lower()
    return candidate if re.fullmatch(r"[0-9a-f]{40}", candidate) else None


READBACK_LINE_RE = re.compile(r"^NW_READBACK_U32=([0-9A-Fa-f,]+)$", re.MULTILINE)
READBACK_COUNT_RE = re.compile(r"^NW_READBACK_ELEMENT_COUNT=([0-9]+)$", re.MULTILINE)
WINDOWS_BUILD_LINE_RE = re.compile(r"^NW_WINDOWS_BUILD=([0-9]+)$", re.MULTILINE)
ADAPTER_DESC_LINE_RE = re.compile(r"^NW_ADAPTER_DESCRIPTION=(.+)$", re.MULTILINE)

NW_METRIC_LINE_RE = re.compile(r"^NW_([A-Z0-9_]+)=(.+)$", re.MULTILINE)
NW_VALIDATION_ERROR_RE = re.compile(r"^NW_D3D12_VALIDATION_ERROR=(.+)$", re.MULTILINE)
NW_VALIDATION_MESSAGE_RE = re.compile(r"^NW_D3D12_VALIDATION_MESSAGE=(.+)$", re.MULTILINE)
NW_LOADED_MODULE_RE = re.compile(r"^NW_LOADED_MODULE=(.+)$", re.MULTILINE)
NW_DIAGNOSTIC_LINE_RE = re.compile(
    r"^\[NW-DIAG\]\[exit_code=([0-9]+)\]\[name=([A-Za-z0-9_]+)\]"
    r"\[category=([a-z_]+)\]\[stage=([A-Za-z0-9_]+)\]\[hr=0x([0-9A-Fa-f]{8})\]",
    re.MULTILINE,
)


def resolve_fixture_executable(directory: Path, fixture_id: str, executable_name: str) -> Path | None:
    """Locate a fixture executable in a raw build tree or an isolated deployment directory."""
    candidates = (
        directory / "deploy" / executable_name,
        directory / "deploy" / fixture_id / executable_name,
        directory / "staging" / fixture_id / executable_name,
        directory / executable_name,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def parse_fixture_metrics(stdout: str) -> dict[str, Any]:
    """Parse machine-readable NW_* metric lines emitted by measurement fixtures."""
    metrics: dict[str, Any] = {}
    key_map = {
        "STARTUP_MS": "startup_time_ms",
        "CPU_TIME_MS": "cpu_time_ms",
        "GPU_TIME_MS": "gpu_time_ms",
    }
    for name, raw_value in NW_METRIC_LINE_RE.findall(stdout):
        value = raw_value.strip()
        if name in {
            "READBACK_U32",
            "READBACK_ELEMENT_COUNT",
            "WINDOWS_BUILD",
            "ADAPTER_DESCRIPTION",
            "LOADED_MODULE",
            "D3D12_VALIDATION_ERROR",
            "D3D12_VALIDATION_MESSAGE",
        }:
            continue
        if name in {
            "STARTUP_MS",
            "CPU_TIME_MS",
            "GPU_TIME_MS",
        }:
            try:
                metrics[key_map[name]] = float(value)
            except ValueError:
                pass
        elif name in {"MAX_SHADER_MODEL", "FEATURE_LEVEL", "MAX_FEATURE_LEVEL"}:
            metrics[name.lower()] = value
        elif name.endswith("_BYTES") or name.endswith("_SUPPORTED") or name.endswith("_QUERIED") or name in {
            "VENDOR_ID",
            "DEVICE_ID",
            "VRAM_MB",
            "RESOURCE_BINDING_TIER",
            "RESOURCE_HEAP_TIER",
            "MODULE_COUNT",
            "WARMUP_FRAMES",
            "MEASURED_FRAMES",
        }:
            try:
                metrics[name.lower()] = int(value)
            except ValueError:
                metrics[name.lower()] = value
        else:
            metrics[name.lower()] = value
    return metrics


def parse_validation_evidence(stdout: str, stderr: str) -> dict[str, Any]:
    """Extract D3D12 info-queue evidence emitted by the fixtures."""
    combined = f"{stdout}\n{stderr}"
    errors = NW_VALIDATION_ERROR_RE.findall(combined)
    messages = NW_VALIDATION_MESSAGE_RE.findall(combined)
    return {
        "d3d12_validation_clean": len(errors) == 0,
        "d3d12_validation_errors": errors,
        "d3d12_validation_messages": messages,
    }


def parse_loaded_modules(stdout: str) -> list[str]:
    """Extract observed runtime-loaded module paths emitted by the fixture."""
    modules: list[str] = []
    seen: set[str] = set()
    for raw in NW_LOADED_MODULE_RE.findall(stdout):
        path = raw.strip().replace("\\", "/")
        key = path.lower()
        if path and key not in seen:
            seen.add(key)
            modules.append(path)
    return modules


def parse_fixture_adapter_description(stdout: str) -> str | None:
    """Read the adapter description emitted by a single fixture execution."""
    match = ADAPTER_DESC_LINE_RE.search(stdout)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def reconcile_fixture_adapter_descriptions(
    fixture_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Require the raster and compute fixtures to report the same executing adapter."""
    descriptions: dict[str, str | None] = {}
    for run in fixture_runs:
        fixture_id = run.get("fixture_id")
        if fixture_id in {"clear_present", "compute_shader"}:
            descriptions[str(fixture_id)] = parse_fixture_adapter_description(
                str(run.get("stdout", ""))
            )
    present = [value for value in descriptions.values() if value]
    unique = {value.strip().lower() for value in present}
    consistent = (
        len(present) == len(descriptions) and len(unique) == 1
    )
    return {
        "descriptions": descriptions,
        "executing_adapter": present[0] if consistent else None,
        "consistent": consistent,
    }


def classify_loaded_module_evidence(
    module_paths: list[str],
    *,
    system_root: str | None = None,
) -> dict[str, Any]:
    """Classify observed module paths as inbox vs redistributable runtime components.

    Windows 11 itself ships D3D12Core.dll. Only a D3D12Core.dll loaded from outside the
    Windows system directory is an Agility SDK redistributable.
    """
    root = (system_root or os.environ.get("SystemRoot") or "C:/Windows").replace("\\", "/").lower()
    system32 = f"{root.rstrip('/')}/system32/"
    result: dict[str, Any] = {
        "runtime_dxcompiler_loaded": False,
        "runtime_d3dcompiler_loaded": False,
        "agility_d3d12core_redist_loaded": False,
        "d3d12core_classification_unknown": False,
        "inbox_d3d12core_observed": False,
    }
    for path in module_paths:
        lower = path.lower()
        base = lower.rsplit("/", 1)[-1]
        if base in {"dxcompiler.dll", "dxil.dll"}:
            result["runtime_dxcompiler_loaded"] = True
        elif base.startswith("d3dcompiler"):
            result["runtime_d3dcompiler_loaded"] = True
        elif base == "d3d12core.dll":
            if "/" not in lower:
                result["d3d12core_classification_unknown"] = True
            elif lower.startswith(system32) or "/windows/system32/" in lower:
                result["inbox_d3d12core_observed"] = True
            else:
                result["agility_d3d12core_redist_loaded"] = True
    return result


def _format_nvidia_driver_version(raw_version: str) -> str:
    """Convert a WDDM driver version (e.g. '32.0.15.6603') or NVIDIA version into '566.03' format."""
    cleaned = raw_version.strip()
    if re.match(r"^[0-9]{3}\.[0-9]{2}$", cleaned):
        return cleaned
    parts = [p for p in cleaned.split(".") if p.isdigit()]
    if len(parts) == 4 and len(parts[2]) >= 1 and len(parts[3]) == 4:
        tail = (parts[2] + parts[3])[-5:]
        if len(tail) == 5 and tail.isdigit():
            return f"{tail[:3]}.{tail[3:]}"
    return cleaned


def _is_supported_nvidia_gpu_and_driver(
    gpu_model: str | None,
    nvidia_driver_version: str | None,
) -> bool:
    """Verify that gpu_model describes an NVIDIA GPU and nvidia_driver_version has 'NNN.NN' NVIDIA format."""
    if not gpu_model or not nvidia_driver_version:
        return False
    lower_gpu = gpu_model.strip().lower()
    if "nvidia" not in lower_gpu:
        return False
    if any(non_nv in lower_gpu for non_nv in ("microsoft basic", "intel(", "intel ", "amd ", "radeon")):
        return False
    return bool(re.match(r"^[0-9]{3}\.[0-9]{2}$", nvidia_driver_version.strip()))


def hardware_tier_matches(
    gpu_model: str | None,
    hardware_tier: str,
    cpu_model: str | None = None,
) -> bool:
    """Return True only when the observed adapter (and optional CPU) matches the evidence tier."""
    if not gpu_model:
        return False
    lower = gpu_model.strip().lower()
    if "nvidia" not in lower:
        return False
    if "laptop" in lower or "mobile" in lower or "max-q" in lower:
        return False
    # CPU metadata is checked separately; GPU tier eligibility is determined only by
    # the observed GPU model and desktop/mobile classification.
    if hardware_tier == "rtx_4060_desktop":
        # Legacy tier identifier now represents the explicitly required desktop RTX 4060 Ti.
        # A non-Ti RTX 4060 must not silently certify this acceptance tier.
        return re.search(r"\brtx\s*4060\s*ti\b", lower) is not None
    if hardware_tier == "rtx_50_series_desktop":
        return re.search(r"\brtx\s*50[5-9]0(?:\s*ti)?\b", lower) is not None
    return False


def detect_host_system_environment(observed_stdout: str = "") -> dict[str, str | None]:
    """Detect the actual OS build, NVIDIA GPU model, and NVIDIA driver version from the host and/or fixture output."""
    os_build: str | None = None
    gpu_model: str | None = None
    nvidia_driver_version: str | None = None

    if platform.system().lower() == "windows":
        try:
            import winreg  # type: ignore[import-not-found]

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            ) as key:
                build_num, _ = winreg.QueryValueEx(key, "CurrentBuildNumber")
                try:
                    ubr, _ = winreg.QueryValueEx(key, "UBR")
                except OSError:
                    ubr = 0
                os_build = f"10.0.{build_num}.{ubr}"
        except Exception:
            pass

        if not os_build:
            try:
                import ctypes

                class _OsVersionInfoW(ctypes.Structure):
                    _fields_ = [
                        ("dwOSVersionInfoSize", ctypes.c_ulong),
                        ("dwMajorVersion", ctypes.c_ulong),
                        ("dwMinorVersion", ctypes.c_ulong),
                        ("dwBuildNumber", ctypes.c_ulong),
                        ("dwPlatformId", ctypes.c_ulong),
                        ("szCSDVersion", ctypes.c_wchar * 128),
                    ]

                ver = _OsVersionInfoW()
                ver.dwOSVersionInfoSize = ctypes.sizeof(ver)
                if ctypes.windll.ntdll.RtlGetVersion(ctypes.byref(ver)) == 0:  # type: ignore[attr-defined]
                    os_build = f"{ver.dwMajorVersion}.{ver.dwMinorVersion}.{ver.dwBuildNumber}.0"
            except Exception:
                pass

        try:
            import winreg  # type: ignore[import-not-found]

            class_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, class_path) as class_key:
                idx = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(class_key, idx)
                        idx += 1
                    except OSError:
                        break
                    if not (len(sub_name) == 4 and sub_name.isdigit()):
                        continue
                    try:
                        with winreg.OpenKey(class_key, sub_name) as dev_key:
                            desc, _ = winreg.QueryValueEx(dev_key, "DriverDesc")
                            drv_ver, _ = winreg.QueryValueEx(dev_key, "DriverVersion")
                            desc_str = str(desc).strip()
                            drv_str = _format_nvidia_driver_version(str(drv_ver))
                            if _is_supported_nvidia_gpu_and_driver(desc_str, drv_str):
                                gpu_model = desc_str
                                nvidia_driver_version = drv_str
                                break
                    except OSError:
                        continue
        except Exception:
            pass

        if not gpu_model or not nvidia_driver_version:
            nvsmi = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
            if nvsmi:
                try:
                    proc = subprocess.run(
                        [nvsmi, "--query-gpu=name,driver_version", "--format=csv,noheader,nounits"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        first_line = proc.stdout.strip().splitlines()[0]
                        parts = [p.strip() for p in first_line.split(",", 1)]
                        if len(parts) == 2:
                            cand_gpu = gpu_model or parts[0]
                            cand_drv = nvidia_driver_version or _format_nvidia_driver_version(parts[1])
                            if _is_supported_nvidia_gpu_and_driver(cand_gpu, cand_drv):
                                gpu_model = cand_gpu
                                nvidia_driver_version = cand_drv
                except Exception:
                    pass

    if observed_stdout:
        adapter_match = ADAPTER_DESC_LINE_RE.search(observed_stdout)
        if adapter_match and not gpu_model:
            cand_gpu = adapter_match.group(1).strip()
            if "nvidia" in cand_gpu.lower():
                gpu_model = cand_gpu
        build_match = WINDOWS_BUILD_LINE_RE.search(observed_stdout)
        if build_match and not os_build:
            os_build = f"10.0.{build_match.group(1)}.0"

    return {
        "os_build": os_build,
        "gpu_model": gpu_model,
        "nvidia_driver_version": nvidia_driver_version,
    }


def load_compute_expected_output(repo_root: Path) -> dict[str, Any]:
    """Load the committed deterministic compute readback expectation."""
    expected_path = repo_root / "fixtures" / "compute_shader" / "expected_output.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    if expected.get("output_sha256") is None or not expected.get("elements_u32"):
        raise ValueError(f"Compute expectation {expected_path} is missing measured elements/hash")
    return expected


def parse_observed_compute_readback(stdout: str, expected: dict[str, Any]) -> dict[str, Any]:
    """Parse fixture-emitted GPU readback values and recompute the observed SHA-256.

    The observed digest is derived only from values the executable actually printed after
    D3D12 dispatch + readback. Static expected hashes are never copied into the observed field.
    """
    count_match = READBACK_COUNT_RE.search(stdout)
    values_match = READBACK_LINE_RE.search(stdout)
    if not values_match:
        return {
            "observed_element_count": None,
            "observed_output_sha256": None,
            "expected_output_sha256": expected["output_sha256"],
            "elements_match": False,
            "error": "fixture did not emit NW_READBACK_U32 readback evidence",
        }

    values = [int(token, 16) for token in values_match.group(1).split(",")]
    observed_count = int(count_match.group(1)) if count_match else len(values)
    expected_elements = expected["elements_u32"]
    elements_match = observed_count == expected["element_count"] == len(values) and values == expected_elements
    raw = struct.pack(f"<{len(values)}I", *values)
    observed_sha256 = hashlib.sha256(raw).hexdigest()
    return {
        "observed_element_count": observed_count,
        "observed_output_sha256": observed_sha256,
        "expected_output_sha256": expected["output_sha256"],
        "elements_match": elements_match,
        "hash_match": observed_sha256 == expected["output_sha256"],
    }


def find_embedded_dxil_container(binary_bytes: bytes) -> bytes | None:
    """Locate and validate a DXC-produced DXBC/DXIL container embedded in a player executable."""
    search_from = 0
    while True:
        start = binary_bytes.find(b"DXBC", search_from)
        if start < 0:
            return None
        if start + 28 <= len(binary_bytes):
            total_size = struct.unpack_from("<I", binary_bytes, start + 24)[0]
            end = start + total_size
            if 44 <= total_size and end <= len(binary_bytes):
                candidate = binary_bytes[start:end]
                try:
                    validate_dxil_container(candidate)
                    return candidate
                except ValueError:
                    pass
        search_from = start + 4


def check_shader_manifest_evidence(
    repo_root: Path,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """Return recorded offline DXC evidence from the repo or a CI evidence bundle."""
    manifest_path = (
        evidence_dir / "shaders-manifest.json"
        if evidence_dir is not None
        else repo_root / "shaders" / "manifest.json"
    )
    hlsl_path = repo_root / "shaders" / "compute_fixture.hlsl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shader = manifest.get("shaders", [{}])[0]
    current_hlsl_bytes = hlsl_path.read_bytes() if hlsl_path.exists() else b""
    current_hlsl_sha256 = hashlib.sha256(current_hlsl_bytes).hexdigest() if current_hlsl_bytes else None
    hlsl_matches = (
        shader.get("hlsl_sha256") == current_hlsl_sha256
        and shader.get("hlsl_source_bytes") == len(current_hlsl_bytes)
    )
    verified = (
        manifest.get("verification_status") == "verified"
        and shader.get("compiled_offline_with_dxc") is True
        and shader.get("offline_dxil_bytes") is not None
        and shader.get("offline_dxil_sha256") is not None
        and hlsl_matches
    )
    return {
        "verified": verified,
        "hlsl_matches": hlsl_matches,
        "manifest_verification_status": manifest.get("verification_status"),
        "compiled_offline_with_dxc": shader.get("compiled_offline_with_dxc"),
        "measured_dxc_version": shader.get("measured_dxc_version"),
        "offline_dxil_bytes": shader.get("offline_dxil_bytes"),
        "offline_dxil_sha256": shader.get("offline_dxil_sha256"),
    }


def record_shader_manifest_evidence(
    repo_root: Path,
    dxil_path: Path,
    dxc_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Record measured offline DXC output into shaders/manifest.json after validating the container."""
    manifest_path = repo_root / "shaders" / "manifest.json"
    hlsl_path = repo_root / "shaders" / "compute_fixture.hlsl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shader = manifest["shaders"][0]

    blob = dxil_path.read_bytes()
    allow_test_blob = dxc_executable is None or str(dxc_executable).lower().endswith(".py")
    info = validate_dxil_container(blob, allow_test_container=allow_test_blob)
    hlsl_bytes = hlsl_path.read_bytes()

    shader["measured_dxc_version"] = (
        query_dxc_version(str(dxc_executable)) if dxc_executable is not None else "unknown"
    )
    shader["compiled_offline_with_dxc"] = True
    shader["hlsl_source_bytes"] = len(hlsl_bytes)
    shader["hlsl_sha256"] = hashlib.sha256(hlsl_bytes).hexdigest()
    shader["offline_dxil_bytes"] = info["size_bytes"]
    shader["offline_dxil_sha256"] = info["sha256"]
    # Keep canonical build paths in tracked manifest rather than ephemeral build/repro-clean-* paths.
    shader["dxil_build_output_path"] = "build/clang-cl-release/shaders/compute_fixture.dxil"
    shader["embedded_header_build_output_path"] = (
        "build/clang-cl-release/shaders/generated/compute_fixture_dxil.h"
    )
    manifest["verification_status"] = "verified"
    manifest.pop("unverified_reason", None)
    manifest["unavailable_measurements"] = []
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def evaluate_offline_shader(
    repo_root: Path,
    dxc_executable: str | None,
    search_dirs: list[Path] | None = None,
    *,
    record_from_build: bool = False,
) -> dict[str, Any]:
    """Validate HLSL constraints and optionally record offline DXC evidence from a fresh build."""
    hlsl_path = repo_root / "shaders" / "compute_fixture.hlsl"
    hlsl_text = hlsl_path.read_text(encoding="utf-8")
    hlsl_violations = check_hlsl_source_constraints(hlsl_text)

    dxil_info: dict[str, Any] | None = None
    pinned_dxc = (
        json.loads((repo_root / "shaders" / "manifest.json").read_text(encoding="utf-8"))["shaders"][0]
        .get("pinned_dxc_version", "")
    )
    dxc_version_matches_pin = False
    if dxc_executable is not None:
        measured_version = query_dxc_version(str(dxc_executable))
        dxc_version_matches_pin = bool(pinned_dxc) and pinned_dxc in measured_version

    # Only inspect build directories explicitly supplied by the caller; never pick up stale
    # pre-existing build/... artifacts before a fresh build runs.
    explicit_dxil_paths = [
        path / "shaders" / "compute_fixture.dxil"
        for path in (search_dirs or [])
    ]
    all_explicit_exist = bool(explicit_dxil_paths) and all(p.exists() for p in explicit_dxil_paths)

    if all_explicit_exist and dxc_executable is not None and dxc_version_matches_pin:
        allow_test_blob = str(dxc_executable).lower().endswith(".py")
        digests = {hashlib.sha256(p.read_bytes()).hexdigest() for p in explicit_dxil_paths}
        if len(digests) == 1:
            primary_dxil = explicit_dxil_paths[0]
            dxil_info = validate_dxil_container(
                primary_dxil.read_bytes(), allow_test_container=allow_test_blob
            )
            if record_from_build and not hlsl_violations:
                record_shader_manifest_evidence(repo_root, primary_dxil, dxc_executable)

    evidence = check_shader_manifest_evidence(repo_root)
    passed = (
        not hlsl_violations
        and evidence["verified"]
        and dxil_info is not None
        and dxc_version_matches_pin
    )
    return {
        "passed": passed,
        "verification_status": "verified" if passed else "unverified",
        "hlsl_constraints_passed": not hlsl_violations,
        "dxil_validated": dxil_info is not None,
        "dxc_version_matches_pin": dxc_version_matches_pin,
        "compiled_offline_with_dxc": evidence["compiled_offline_with_dxc"],
        "measured_dxc_version": evidence["measured_dxc_version"],
        "dxil": dxil_info,
        "reason": (
            None
            if passed
            else "Offline DXC evidence is unverified; run on Windows 11 x64 with the pinned dxc "
            f"({pinned_dxc or 'unknown'}) and a freshly built artifact to record shaders/manifest.json."
        ),
    }


def compare_two_build_directories(
    dir_a: Path,
    dir_b: Path,
    artifact_names: list[tuple[str, str]] | None = None,
    output_report_path: Path | None = None,
    report_id: str = "nw01-clang-cl-release-reproducibility-v1",
) -> dict[str, Any]:
    """Compare release PE32+ artifacts across two independent clean build directories and optionally record the report."""
    if artifact_names is None:
        artifact_names = [
            ("nw_empty_player.exe", "empty_player"),
            ("nw_clear_present_fixture.exe", "clear_present"),
            ("nw_compute_shader_fixture.exe", "compute_shader"),
        ]

    artifacts_report: list[dict[str, Any]] = []
    mismatches_found = 0
    missing_files: list[str] = []

    for exe_name, fixture_id in artifact_names:
        file_a = resolve_fixture_executable(dir_a, fixture_id, exe_name)
        file_b = resolve_fixture_executable(dir_b, fixture_id, exe_name)
        if file_a is None or file_b is None:
            missing_files.append(exe_name)
            artifacts_report.append(
                {
                    "artifact_name": exe_name,
                    "fixture_id": fixture_id,
                    "size_bytes_a": None,
                    "size_bytes_b": None,
                    "sha256_a": None,
                    "sha256_b": None,
                    "hash_match": None,
                }
            )
            continue

        bytes_a = file_a.read_bytes()
        bytes_b = file_b.read_bytes()

        # /Brepro may write either a deterministic zero timestamp or a deterministic
        # hash-derived timestamp (lld-link does the latter). The reproducibility contract
        # is deterministic equality across both clean directories, not a zero value.
        pe_a = parse_pe64_sections_and_imports(bytes_a)
        pe_b = parse_pe64_sections_and_imports(bytes_b)
        timestamp_identical = pe_a["time_date_stamp"] == pe_b["time_date_stamp"]

        hash_a = hashlib.sha256(bytes_a).hexdigest()
        hash_b = hashlib.sha256(bytes_b).hexdigest()
        matched = (
            (len(bytes_a) == len(bytes_b))
            and (hash_a == hash_b)
            and timestamp_identical
        )
        if not matched:
            mismatches_found += 1
        artifacts_report.append(
            {
                "artifact_name": exe_name,
                "fixture_id": fixture_id,
                "size_bytes_a": len(bytes_a),
                "size_bytes_b": len(bytes_b),
                "sha256_a": hash_a,
                "sha256_b": hash_b,
                "hash_match": matched,
                "time_date_stamp_a": pe_a["time_date_stamp"],
                "time_date_stamp_b": pe_b["time_date_stamp"],
                "time_date_stamp_identical": timestamp_identical,
            }
        )

    all_match = (len(missing_files) == 0) and (mismatches_found == 0) and (len(artifacts_report) == len(artifact_names))
    if missing_files:
        verification_status = "unverified"
    elif mismatches_found > 0:
        verification_status = "mismatch_detected"
    else:
        verification_status = "verified"

    unavailable_measurements: list[dict[str, str]] = []
    if missing_files:
        unavailable_measurements = [
            {
                "metric": "artifacts[*].size_bytes_a",
                "reason": f"Missing release build artifacts ({', '.join(missing_files)}) in {dir_a} or {dir_b}.",
            },
            {
                "metric": "artifacts[*].sha256_a",
                "reason": f"Missing release build artifacts ({', '.join(missing_files)}); hashes are recorded as null rather than self-referential placeholders.",
            },
        ]

    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "report_id": report_id,
        "toolchain_manifest_ref": "benchmarks/toolchain-manifest.v1.json",
        "build_profile": "release",
        "directory_a": str(dir_a),
        "directory_b": str(dir_b),
        "verification_status": verification_status,
        "all_artifacts_match": all_match,
        "missing_files": missing_files,
        "artifacts": artifacts_report,
        "mismatch_investigation": {
            "mismatches_found": mismatches_found,
            "root_causes_investigated": [
                "PE COFF header TimeDateStamp determinism achieved via /Brepro on both clang-cl/lld-link and MSVC; "
                "deterministic zero and deterministic hash-derived timestamps are both accepted.",
                "Embedded absolute build-directory PDB paths mitigated via /PDBALTPATH:%_PDB%.",
                "Compiler __FILE__ and debug prefix paths normalized via -ffile-prefix-map=${CMAKE_SOURCE_DIR}=.",
            ],
            "remediation_applied": (
                "All artifacts matched bit-for-bit across independent clean directories."
                if all_match
                else "Investigate section-by-section PE diffs or missing toolchain prerequisites before claiming reproducibility."
            ),
        },
        "unavailable_measurements": unavailable_measurements,
    }
    if verification_status == "unverified":
        report["unverified_reason"] = (
            f"Missing release build artifacts ({', '.join(missing_files)}); build twice on Windows 11 x64 with pinned clang-cl/lld-link/DXC."
        )

    if output_report_path is not None:
        output_report_path.parent.mkdir(parents=True, exist_ok=True)
        output_report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    return report


def build_and_compare_two_clean_directories(
    repo_root: Path,
    dir_a: Path | None = None,
    dir_b: Path | None = None,
    output_report_path: Path | None = None,
    *,
    enforce_pinned_toolchain: bool = True,
) -> dict[str, Any]:
    """Invoke CMake + Ninja + clang-cl/lld-link + DXC in two clean directories and record the reproducibility report."""
    out_dir_a = dir_a or (repo_root / "build" / "repro-clean-a")
    out_dir_b = dir_b or (repo_root / "build" / "repro-clean-b")
    repro_path = output_report_path or (repo_root / "benchmarks" / "reproducibility" / "release-build-reproducibility.json")
    repro_schema = repo_root / "schemas" / "reproducibility-report.schema.json"

    cmake_bin = shutil.which("cmake")
    ninja_bin = shutil.which("ninja")
    clang_cl_bin = shutil.which("clang-cl") or shutil.which("clang-cl.exe")
    lld_link_bin = shutil.which("lld-link") or shutil.which("lld-link.exe")
    dxc_bin = find_dxc_executable()

    missing_tools = []
    if not cmake_bin:
        missing_tools.append("cmake")
    if not ninja_bin:
        missing_tools.append("ninja")
    if not clang_cl_bin:
        missing_tools.append("clang-cl")
    if not lld_link_bin:
        missing_tools.append("lld-link")
    if not dxc_bin:
        missing_tools.append("dxc")

    if missing_tools:
        # Never compare stale artifacts from an earlier local build when the pinned toolchain
        # is missing: remove the dedicated repro directories first so the report stays honest.
        for d in (out_dir_a, out_dir_b):
            if d.exists():
                shutil.rmtree(d)
        report = compare_two_build_directories(out_dir_a, out_dir_b, output_report_path=repro_path)
        report["missing_toolchain_tools"] = missing_tools
        if repro_schema.exists():
            validate_file_against_schema(repro_path, repro_schema)
        return report

    for d in (out_dir_a, out_dir_b):
        if d.exists():
            shutil.rmtree(d)
        subprocess.run(
            [
                cmake_bin,
                "-S",
                str(repo_root),
                "-B",
                str(d),
                "-G",
                "Ninja",
                f"-DCMAKE_TOOLCHAIN_FILE={repo_root / 'cmake/toolchains/clang-cl-x64.cmake'}",
                "-DCMAKE_BUILD_TYPE=MinSizeRel",
                "-DNW_BUILD_PROFILE=release",
                f"-DNW_ENFORCE_PINNED_TOOLCHAIN={'ON' if enforce_pinned_toolchain else 'OFF'}",
            ],
            check=True,
        )
        subprocess.run([cmake_bin, "--build", str(d)], check=True)

    report = compare_two_build_directories(out_dir_a, out_dir_b, output_report_path=repro_path)
    if repro_schema.exists():
        validate_file_against_schema(repro_path, repro_schema)
    return report


def build_msvc_ltcg_release_directory(
    repo_root: Path,
    out_dir: Path | None = None,
    *,
    enforce_pinned_toolchain: bool = True,
) -> dict[str, Any]:
    """Build identical release fixtures with the MSVC/LTCG comparison toolchain in a clean directory."""
    msvc_dir = out_dir or (repo_root / "build" / "msvc-release-ltcg")
    cmake_bin = shutil.which("cmake")
    ninja_bin = shutil.which("ninja")
    cl_bin = shutil.which("cl") or shutil.which("cl.exe")
    link_bin = shutil.which("link") or shutil.which("link.exe")
    dxc_bin = find_dxc_executable()

    missing_tools: list[str] = []
    if not cmake_bin:
        missing_tools.append("cmake")
    if not ninja_bin:
        missing_tools.append("ninja")
    if not cl_bin:
        missing_tools.append("cl")
    if not link_bin:
        missing_tools.append("link")
    if not dxc_bin:
        missing_tools.append("dxc")

    if missing_tools:
        if msvc_dir.exists():
            shutil.rmtree(msvc_dir)
        return {
            "built": False,
            "output_dir": str(msvc_dir),
            "missing_toolchain_tools": missing_tools,
        }

    if msvc_dir.exists():
        shutil.rmtree(msvc_dir)

    subprocess.run(
        [
            cmake_bin,
            "-S",
            str(repo_root),
            "-B",
            str(msvc_dir),
            "-G",
            "Ninja",
            f"-DCMAKE_TOOLCHAIN_FILE={repo_root / 'cmake/toolchains/msvc-x64.cmake'}",
            "-DCMAKE_BUILD_TYPE=MinSizeRel",
            "-DNW_BUILD_PROFILE=release",
            f"-DNW_ENFORCE_PINNED_TOOLCHAIN={'ON' if enforce_pinned_toolchain else 'OFF'}",
        ],
        check=True,
    )
    subprocess.run([cmake_bin, "--build", str(msvc_dir)], check=True)

    for fixture_id, exe_name in (
        ("empty_player", "nw_empty_player.exe"),
        ("clear_present", "nw_clear_present_fixture.exe"),
        ("compute_shader", "nw_compute_shader_fixture.exe"),
    ):
        exe_path = resolve_fixture_executable(msvc_dir, fixture_id, exe_name)
        if exe_path is None:
            raise ValueError(f"MSVC/LTCG build did not produce required release executable '{exe_name}'")
        pe_info = parse_pe64_sections_and_imports(exe_path.read_bytes())
        # MSVC/link.exe may emit a zero or hash-derived deterministic timestamp. The
        # two-directory hash comparison is the authoritative reproducibility check.
        _ = pe_info["time_date_stamp"]

    return {
        "built": True,
        "output_dir": str(msvc_dir),
        "time_date_stamp_policy": "deterministic_zero_or_hash_derived",
    }


def build_and_compare_two_msvc_clean_directories(
    repo_root: Path,
    dir_a: Path | None = None,
    dir_b: Path | None = None,
    output_report_path: Path | None = None,
    *,
    enforce_pinned_toolchain: bool = True,
) -> dict[str, Any]:
    """Build MSVC/LTCG release fixtures twice in independent directories and compare hashes."""
    out_dir_a = dir_a or (repo_root / "build" / "msvc-release-ltcg-clean-a")
    out_dir_b = dir_b or (repo_root / "build" / "msvc-release-ltcg-clean-b")
    repro_path = output_report_path or (
        repo_root / "benchmarks" / "reproducibility" / "msvc-ltcg-release-reproducibility.json"
    )
    repro_schema = repo_root / "schemas" / "reproducibility-report.schema.json"

    cmake_bin = shutil.which("cmake")
    ninja_bin = shutil.which("ninja")
    cl_bin = shutil.which("cl") or shutil.which("cl.exe")
    link_bin = shutil.which("link") or shutil.which("link.exe")
    dxc_bin = find_dxc_executable()

    missing_tools: list[str] = []
    if not cmake_bin:
        missing_tools.append("cmake")
    if not ninja_bin:
        missing_tools.append("ninja")
    if not cl_bin:
        missing_tools.append("cl")
    if not link_bin:
        missing_tools.append("link")
    if not dxc_bin:
        missing_tools.append("dxc")

    if missing_tools:
        for d in (out_dir_a, out_dir_b):
            if d.exists():
                shutil.rmtree(d)
        report = compare_two_build_directories(
            out_dir_a,
            out_dir_b,
            output_report_path=repro_path,
            report_id="nw01-msvc-ltcg-release-reproducibility-v1",
        )
        report["missing_toolchain_tools"] = missing_tools
        if repro_schema.exists():
            validate_file_against_schema(repro_path, repro_schema)
        return report

    for d in (out_dir_a, out_dir_b):
        if d.exists():
            shutil.rmtree(d)
        subprocess.run(
            [
                cmake_bin,
                "-S",
                str(repo_root),
                "-B",
                str(d),
                "-G",
                "Ninja",
                f"-DCMAKE_TOOLCHAIN_FILE={repo_root / 'cmake/toolchains/msvc-x64.cmake'}",
                "-DCMAKE_BUILD_TYPE=MinSizeRel",
                "-DNW_BUILD_PROFILE=release",
                f"-DNW_ENFORCE_PINNED_TOOLCHAIN={'ON' if enforce_pinned_toolchain else 'OFF'}",
            ],
            check=True,
        )
        subprocess.run([cmake_bin, "--build", str(d)], check=True)

    report = compare_two_build_directories(
        out_dir_a,
        out_dir_b,
        output_report_path=repro_path,
        report_id="nw01-msvc-ltcg-release-reproducibility-v1",
    )
    if repro_schema.exists():
        validate_file_against_schema(repro_path, repro_schema)
    return report


PDB_MSF_SIGNATURE = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"
DEBUG_FIXTURE_EXECUTABLES = (
    "nw_empty_player.exe",
    "nw_clear_present_fixture.exe",
    "nw_compute_shader_fixture.exe",
)
DEBUG_FIXTURE_IDS = {
    "nw_empty_player.exe": "empty_player",
    "nw_clear_present_fixture.exe": "clear_present",
    "nw_compute_shader_fixture.exe": "compute_shader",
}


def parse_pdb_guid_and_age(pdb_bytes: bytes) -> dict[str, Any] | None:
    """Parse the PDB Info Stream (Stream 1) GUID and age from a Microsoft MSF 7.00 PDB file."""
    if len(pdb_bytes) < 56 or not pdb_bytes.startswith(PDB_MSF_SIGNATURE):
        return None
    (
        block_size,
        _free_block_map,
        num_blocks,
        num_dir_bytes,
        _unknown,
        block_map_addr,
    ) = struct.unpack_from("<6I", pdb_bytes, 32)
    if (
        block_size < 64
        or (block_size & (block_size - 1)) != 0
        or num_blocks < 3
        or num_dir_bytes < 12
        or block_map_addr == 0
        or block_map_addr >= num_blocks
        or block_size * num_blocks > len(pdb_bytes)
    ):
        return None

    num_dir_blocks = (num_dir_bytes + block_size - 1) // block_size
    block_map_offset = block_map_addr * block_size
    if block_map_offset + num_dir_blocks * 4 > len(pdb_bytes):
        return None
    dir_block_indices = struct.unpack_from(f"<{num_dir_blocks}I", pdb_bytes, block_map_offset)
    dir_buf = bytearray()
    for blk_idx in dir_block_indices:
        if blk_idx >= num_blocks:
            return None
        blk_off = blk_idx * block_size
        dir_buf.extend(pdb_bytes[blk_off : blk_off + block_size])
    dir_bytes = bytes(dir_buf[:num_dir_bytes])

    num_streams = struct.unpack_from("<I", dir_bytes, 0)[0]
    if num_streams < 2 or 4 + num_streams * 4 > len(dir_bytes):
        return None
    stream_sizes = struct.unpack_from(f"<{num_streams}I", dir_bytes, 4)
    stream0_size = stream_sizes[0]
    stream1_size = stream_sizes[1]
    if stream1_size == 0xFFFFFFFF or stream1_size < 28:
        return None
    stream0_blocks = (
        0 if stream0_size == 0xFFFFFFFF else (stream0_size + block_size - 1) // block_size
    )
    stream1_blocks = (stream1_size + block_size - 1) // block_size
    stream1_block_list_off = 4 + (num_streams + stream0_blocks) * 4
    if stream1_blocks < 1 or stream1_block_list_off + stream1_blocks * 4 > len(dir_bytes):
        return None
    stream1_block_indices = struct.unpack_from(
        f"<{stream1_blocks}I", dir_bytes, stream1_block_list_off
    )
    stream1_buf = bytearray()
    for blk_idx in stream1_block_indices:
        if blk_idx >= num_blocks:
            return None
        blk_off = blk_idx * block_size
        stream1_buf.extend(pdb_bytes[blk_off : blk_off + block_size])
        if len(stream1_buf) >= 28:
            break
    if len(stream1_buf) < 28:
        return None

    version, signature, age = struct.unpack_from("<III", stream1_buf, 0)
    guid_bytes = bytes(stream1_buf[12:28])
    return {
        "version": version,
        "signature": signature,
        "age": age,
        "guid": guid_bytes.hex(),
    }


def verify_debug_symbol_evidence(binary_dir: Path) -> dict[str, Any]:
    """Verify that each fixture PE carries CodeView/RSDS records and resolves to its MSF PDB.

    A PDB only counts when its Info Stream GUID and age match the PE CodeView RSDS record.
    A stale PDB from another build/configuration whose filename happens to match the
    executable stem is therefore rejected rather than reported as debugging evidence.
    """
    artifacts: list[dict[str, Any]] = []
    missing: list[str] = []

    for exe_name in DEBUG_FIXTURE_EXECUTABLES:
        fixture_id = DEBUG_FIXTURE_IDS[exe_name]
        exe_path = resolve_fixture_executable(binary_dir, fixture_id, exe_name)
        record: dict[str, Any] = {
            "executable": exe_name,
            "codeview_rsds": False,
            "pdb_file": None,
            "pdb_msf_signature": False,
            "pdb_guid_age_match": False,
            "pdb_guid": None,
            "pdb_age": None,
            "pdb_mismatches": [],
        }
        if exe_path is None:
            record["missing"] = True
            missing.append(exe_name)
            artifacts.append(record)
            continue

        pe_info = parse_pe64_sections_and_imports(exe_path.read_bytes())
        codeview = [
            entry
            for entry in (pe_info.get("codeview_entries") or [])
            if entry.get("signature") == "RSDS"
            and entry.get("guid")
            and entry.get("guid") != "00" * 16
            and int(entry.get("age") or 0) > 0
            and (entry.get("pdb_path") or "").strip()
        ]
        record["codeview_rsds"] = len(codeview) > 0

        matched_pdb: tuple[Path, dict[str, Any]] | None = None
        for entry in codeview:
            raw_pdb_path = (entry.get("pdb_path") or "").strip()
            win_path = PureWindowsPath(raw_pdb_path)
            pdb_candidates: list[Path] = []
            if Path(raw_pdb_path).is_absolute():
                pdb_candidates.append(Path(raw_pdb_path))
            pdb_candidates.append(exe_path.parent / win_path.name)
            pdb_candidates.append(binary_dir / "staging" / fixture_id / win_path.name)
            pdb_candidates.append(binary_dir / "staging" / fixture_id / Path(win_path.as_posix()))
            pdb_candidates.append(binary_dir / "deploy" / fixture_id / win_path.name)
            pdb_candidates.append(binary_dir / "deploy" / win_path.name)
            pdb_candidates.append(binary_dir / Path(win_path.as_posix()))
            pdb_candidates.append(binary_dir / win_path.name)

            for cand in pdb_candidates:
                if not cand.is_file():
                    continue
                if record["pdb_file"] is None:
                    record["pdb_file"] = cand.name
                try:
                    pdb_bytes = cand.read_bytes()
                except OSError:
                    continue
                if not pdb_bytes.startswith(PDB_MSF_SIGNATURE):
                    continue
                record["pdb_msf_signature"] = True
                pdb_info = parse_pdb_guid_and_age(pdb_bytes)
                if pdb_info is None:
                    record["pdb_mismatches"].append(
                        {"file": cand.name, "reason": "unreadable_msf_info_stream"}
                    )
                    continue
                expected_guid = str(entry.get("guid") or "").lower()
                expected_age = int(entry.get("age") or 0)
                if (
                    str(pdb_info["guid"]).lower() == expected_guid
                    and int(pdb_info["age"]) == expected_age
                ):
                    matched_pdb = (cand, pdb_info)
                    break
                record["pdb_mismatches"].append(
                    {
                        "file": cand.name,
                        "reason": "guid_or_age_mismatch",
                        "expected_guid": expected_guid,
                        "expected_age": expected_age,
                        "observed_guid": str(pdb_info["guid"]).lower(),
                        "observed_age": int(pdb_info["age"]),
                    }
                )
            if matched_pdb is not None:
                break

        if matched_pdb is not None:
            matched_path, matched_info = matched_pdb
            record["pdb_file"] = matched_path.name
            record["pdb_guid_age_match"] = True
            record["pdb_guid"] = str(matched_info["guid"]).lower()
            record["pdb_age"] = int(matched_info["age"])

        if (
            not record["codeview_rsds"]
            or not record["pdb_msf_signature"]
            or not record["pdb_guid_age_match"]
        ):
            missing.append(exe_name)
        artifacts.append(record)

    verified = not missing
    return {
        "verified": verified,
        "binary_dir": str(binary_dir),
        "artifacts": artifacts,
        "missing_or_invalid": missing,
    }


def build_and_verify_debug_symbols(
    repo_root: Path,
    toolchain_id: str,
    out_dir: Path | None = None,
    *,
    enforce_pinned_toolchain: bool = True,
) -> dict[str, Any]:
    """Build the dev profile with /DEBUG:FULL and verify CodeView/PDB evidence for that toolchain."""
    if toolchain_id == "clang-cl-lld":
        toolchain_file = repo_root / "cmake/toolchains/clang-cl-x64.cmake"
        debug_dir = out_dir or (repo_root / "build" / "clang-cl-debug-symbols")
        required_tools = ("clang-cl", "lld-link")
    elif toolchain_id == "msvc-ltcg":
        toolchain_file = repo_root / "cmake/toolchains/msvc-x64.cmake"
        debug_dir = out_dir or (repo_root / "build" / "msvc-debug-symbols")
        required_tools = ("cl", "link")
    else:
        raise ValueError(f"Unknown toolchain_id '{toolchain_id}'")

    cmake_bin = shutil.which("cmake")
    ninja_bin = shutil.which("ninja")
    dxc_bin = find_dxc_executable()
    missing_tools: list[str] = []
    if not cmake_bin:
        missing_tools.append("cmake")
    if not ninja_bin:
        missing_tools.append("ninja")
    for tool in required_tools:
        found = shutil.which(tool) or shutil.which(f"{tool}.exe")
        if not found:
            missing_tools.append(tool)
    if not dxc_bin:
        missing_tools.append("dxc")

    if missing_tools:
        return {
            "built": False,
            "verified": False,
            "output_dir": str(debug_dir),
            "missing_toolchain_tools": missing_tools,
        }

    if debug_dir.exists():
        shutil.rmtree(debug_dir)
    subprocess.run(
        [
            cmake_bin,
            "-S",
            str(repo_root),
            "-B",
            str(debug_dir),
            "-G",
            "Ninja",
            f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}",
            "-DCMAKE_BUILD_TYPE=Debug",
            "-DNW_BUILD_PROFILE=dev",
            f"-DNW_ENFORCE_PINNED_TOOLCHAIN={'ON' if enforce_pinned_toolchain else 'OFF'}",
        ],
        check=True,
    )
    subprocess.run([cmake_bin, "--build", str(debug_dir)], check=True)
    verification = verify_debug_symbol_evidence(debug_dir)
    return {
        "built": True,
        "output_dir": str(debug_dir),
        **verification,
    }


def probe_sdk_absence(
    search_dir: Path,
    env: dict[str, str] | None = None,
    system32_dir: Path | None = None,
    program_files_x86_dir: Path | None = None,
) -> dict[str, bool]:
    """Actively probe the target machine and deployment directory to verify absence of developer SDKs and redistributables."""
    active_env = env if env is not None else dict(os.environ)
    sys32 = system32_dir or Path("C:/Windows/System32")
    active_path = active_env.get("PATH") if "PATH" in active_env else active_env.get("Path", "")

    win_sdk_dir = active_env.get("WindowsSdkDir", "")
    win_sdk_ver = active_env.get("WindowsSDKVersion", "")
    if program_files_x86_dir is not None:
        pf_x86 = program_files_x86_dir
    elif "ProgramFiles(x86)" in active_env:
        pf_x86 = Path(active_env["ProgramFiles(x86)"])
    elif env is not None:
        # When an isolated env dictionary is injected (e.g. in cross-platform unit tests),
        # do not read the host's C:/Program Files (x86) directory.
        pf_x86 = sys32.parent / "Program Files (x86)"
    else:
        pf_x86 = Path("C:/Program Files (x86)")
    default_kits_include = pf_x86 / "Windows Kits" / "10" / "Include"
    deployed_basenames = {
        name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        for name in collect_deployment_files(search_dir)
    }
    windows_sdk_absent = (
        not win_sdk_dir
        and not win_sdk_ver
        and shutil.which("fxc", path=active_path) is None
        and shutil.which("fxc.exe", path=active_path) is None
        and not default_kits_include.exists()
    )

    d3d12_debug_layer_absent = (
        "d3d12sdklayers.dll" not in deployed_basenames
        and not (sys32 / "d3d12sdklayers.dll").exists()
        and not (sys32 / "d3d12SDKLayers.dll").exists()
    )

    dxc_redist_absent = (
        "dxcompiler.dll" not in deployed_basenames
        and "dxil.dll" not in deployed_basenames
        and not (sys32 / "dxcompiler.dll").exists()
        and not (sys32 / "dxil.dll").exists()
        and shutil.which("dxc", path=active_path) is None
        and shutil.which("dxc.exe", path=active_path) is None
    )

    agility_sdk_redist_absent = (
        "d3d12core.dll" not in deployed_basenames
    )

    msvc_debug_crt_absent = (
        "ucrtbased.dll" not in deployed_basenames
        and "vcruntime140d.dll" not in deployed_basenames
        and not (sys32 / "ucrtbased.dll").exists()
        and not (sys32 / "vcruntime140d.dll").exists()
    )

    return {
        "windows_sdk_absent": windows_sdk_absent,
        "d3d12_debug_layer_absent": d3d12_debug_layer_absent,
        "dxc_redist_absent": dxc_redist_absent,
        "agility_sdk_redist_absent": agility_sdk_redist_absent,
        "msvc_debug_crt_absent": msvc_debug_crt_absent,
    }


def load_selected_toolchain_artifact_hashes(
    repo_root: Path,
) -> tuple[str | None, dict[str, str]]:
    """Return the selected toolchain and the verified ledger hashes for its release fixtures."""
    toolchain_path = repo_root / "benchmarks" / "toolchain-manifest.v1.json"
    toolchain_data = json.loads(toolchain_path.read_text(encoding="utf-8"))
    selected_toolchain = (
        toolchain_data.get("selected_toolchain", {}).get("toolchain_id")
    )
    hashes: dict[str, str] = {}
    if not selected_toolchain:
        return None, hashes
    for ledger_path in sorted((repo_root / "benchmarks" / "ledgers").glob("*.json")):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if ledger.get("verification_status") != "verified":
            continue
        if ledger.get("build_profile") != "release":
            continue
        if ledger.get("toolchain_id") != selected_toolchain:
            continue
        fixture_id = str(ledger.get("fixture_id"))
        for component in ledger.get("distributed_components", []):
            if (
                component.get("category") == "executable"
                and component.get("sha256")
            ):
                hashes[fixture_id] = str(component["sha256"]).lower()
                break
    return selected_toolchain, hashes


def _provenance_reconciliation_problems(
    *,
    evidence_kind: str,
    evidence_hash: str | None,
    evidence_toolchain: str | None,
    selected_toolchain: str | None,
    expected_hash: str | None,
    fixture_id: str,
) -> list[str]:
    """Return concrete reasons a physical-evidence artifact is not bound to the selected build."""
    problems: list[str] = []
    if not evidence_toolchain:
        problems.append(
            f"{evidence_kind} does not record toolchain provenance, so its {fixture_id} "
            "measurement cannot be bound to the selected build"
        )
    elif selected_toolchain and evidence_toolchain != selected_toolchain:
        problems.append(
            f"{evidence_kind} was recorded with toolchain '{evidence_toolchain}', but the "
            f"selected toolchain is '{selected_toolchain}'"
        )
    if expected_hash is None:
        problems.append(
            f"{evidence_kind} cannot be verified because the selected toolchain has no "
            f"verified release-ledger artifact hash for {fixture_id}"
        )
    elif not evidence_hash:
        problems.append(
            f"{evidence_kind} does not record the executed {fixture_id} artifact hash"
        )
    elif evidence_hash.lower() != expected_hash:
        problems.append(
            f"{evidence_kind} {fixture_id} artifact hash {evidence_hash.lower()} does not "
            f"match the selected toolchain ledger hash {expected_hash}"
        )
    return problems


def check_evidence_completeness(repo_root: Path) -> dict[str, Any]:
    """Check whether all physical hardware, reproducibility, and clean-system evidence is verified.

    Verification flags alone are insufficient: physical-machine results must also name the
    selected toolchain and carry executable hashes that match the currently recorded,
    verified release ledgers. This prevents new binaries from inheriting stale evidence.
    """
    unmet_requirements: list[str] = []

    shader_evidence = check_shader_manifest_evidence(repo_root)
    if not shader_evidence["verified"]:
        unmet_requirements.append(
            "Offline DXC shader evidence (shaders/manifest.json) is not verified "
            f"(verification_status={shader_evidence['manifest_verification_status']}, "
            f"compiled_offline_with_dxc={shader_evidence['compiled_offline_with_dxc']})"
        )

    selected_toolchain, expected_hashes = load_selected_toolchain_artifact_hashes(
        repo_root
    )
    required_artifact_fixtures = ("empty_player", "clear_present", "compute_shader")
    for fixture_id in required_artifact_fixtures:
        if expected_hashes.get(fixture_id) is None:
            unmet_requirements.append(
                f"Selected toolchain '{selected_toolchain}' has no verified release-ledger "
                f"artifact hash for fixture '{fixture_id}'; physical evidence cannot be bound "
                "to a reproducible build"
            )

    hw_files = [
        ("rtx_4060_desktop", repo_root / "benchmarks" / "hardware" / "rtx-4060-baseline.json"),
        ("rtx_50_series_desktop", repo_root / "benchmarks" / "hardware" / "rtx-50-series-baseline.json"),
    ]
    for tier, hw_path in hw_files:
        data = json.loads(hw_path.read_text(encoding="utf-8"))
        if data.get("verification_status") != "verified" or data.get("benchmark_passed") is not True:
            unmet_requirements.append(
                f"Hardware baseline '{tier}' ({hw_path.name}) is '{data.get('verification_status')}' "
                f"(benchmark_passed={data.get('benchmark_passed')})"
            )
            continue
        cpu_model = (data.get("environment_metadata") or {}).get("cpu_model")
        if not desktop_cpu_model_matches(
            str(cpu_model) if cpu_model is not None else None
        ):
            unmet_requirements.append(
                f"Hardware baseline '{tier}' lacks recorded non-generic CPU metadata "
                f"(cpu_model={cpu_model!r})"
            )
            continue
        provenance = data.get("artifact_provenance") or {}
        if provenance.get("source") != "recorded_ledger_artifact_hash":
            unmet_requirements.append(
                f"Hardware baseline '{tier}' does not certify its artifact provenance "
                "source as a recorded ledger hash"
            )
        evidence_hashes = provenance.get("artifact_sha256") or {}
        results_by_fixture = {
            str(result.get("fixture_id")): result.get("sha256")
            for result in data.get("fixture_results", [])
        }
        for fixture_id in ("clear_present", "compute_shader"):
            evidence_hash = evidence_hashes.get(fixture_id)
            if not evidence_hash:
                evidence_hash = results_by_fixture.get(fixture_id)
            for problem in _provenance_reconciliation_problems(
                evidence_kind=f"Hardware baseline '{tier}'",
                evidence_hash=evidence_hash,
                evidence_toolchain=provenance.get("toolchain_id"),
                selected_toolchain=selected_toolchain,
                expected_hash=expected_hashes.get(fixture_id),
                fixture_id=fixture_id,
            ):
                unmet_requirements.append(problem)

    repro_files = [
        ("clang-cl-lld", repo_root / "benchmarks" / "reproducibility" / "release-build-reproducibility.json"),
        ("msvc-ltcg", repo_root / "benchmarks" / "reproducibility" / "msvc-ltcg-release-reproducibility.json"),
    ]
    for toolchain_id, repro_path in repro_files:
        repro_data = json.loads(repro_path.read_text(encoding="utf-8"))
        if (
            repro_data.get("verification_status") != "verified"
            or repro_data.get("all_artifacts_match") is not True
        ):
            unmet_requirements.append(
                f"Reproducibility report for {toolchain_id} ({repro_path.name}) is "
                f"'{repro_data.get('verification_status')}' "
                f"(all_artifacts_match={repro_data.get('all_artifacts_match')})"
            )

    clean_path = repo_root / "benchmarks" / "clean-system" / "clean-system-validation.json"
    clean_data = json.loads(clean_path.read_text(encoding="utf-8"))
    if clean_data.get("verification_status") != "verified" or clean_data.get("clean_system_passed") is not True:
        unmet_requirements.append(
            f"Clean physical Windows 11 validation ({clean_path.name}) is '{clean_data.get('verification_status')}' "
            f"(clean_system_passed={clean_data.get('clean_system_passed')})"
        )
    else:
        provenance = clean_data.get("artifact_provenance") or {}
        if provenance.get("source") != "recorded_ledger_artifact_hash":
            unmet_requirements.append(
                "Clean-system report does not certify its artifact provenance source as "
                "a recorded ledger hash"
            )
        deployed_files = (
            clean_data.get("deployed_dependency_inventory", {}).get("deployed_files", [])
        )
        deployed_hashes = {
            str(item.get("file_name", "")).replace("\\", "/").rsplit("/", 1)[-1].lower(): item.get("sha256")
            for item in deployed_files
            if item.get("category") == "executable"
        }
        executable_names = {
            "empty_player": "nw_empty_player.exe",
            "clear_present": "nw_clear_present_fixture.exe",
            "compute_shader": "nw_compute_shader_fixture.exe",
        }
        for fixture_id in required_artifact_fixtures:
            for problem in _provenance_reconciliation_problems(
                evidence_kind="Clean-system report",
                evidence_hash=deployed_hashes.get(executable_names[fixture_id]),
                evidence_toolchain=provenance.get("toolchain_id"),
                selected_toolchain=selected_toolchain,
                expected_hash=expected_hashes.get(fixture_id),
                fixture_id=fixture_id,
            ):
                unmet_requirements.append(problem)

    toolchain_path = repo_root / "benchmarks" / "toolchain-manifest.v1.json"
    toolchain_data = json.loads(toolchain_path.read_text(encoding="utf-8"))
    for et in toolchain_data.get("evaluated_toolchains", []):
        if et.get("verification_status") != "verified":
            unmet_requirements.append(
                f"Evaluated toolchain '{et.get('toolchain_id')}' in {toolchain_path.name} is '{et.get('verification_status')}'"
            )
        if et.get("pdb_debugging_verified") is not True:
            unmet_requirements.append(
                f"Evaluated toolchain '{et.get('toolchain_id')}' lacks verified CodeView/PDB "
                "debugging evidence"
            )
        if et.get("deterministic_repro_verified") is not True:
            unmet_requirements.append(
                f"Evaluated toolchain '{et.get('toolchain_id')}' lacks verified "
                "two-clean-directory reproducibility evidence"
            )
        if et.get("fixture_execution_verified") is not True:
            unmet_requirements.append(
                f"Evaluated toolchain '{et.get('toolchain_id')}' lacks verified "
                "release-fixture execution evidence"
            )

    try:
        ledger_comparison = generate_size_comparison_report(repo_root)
        for comp in ledger_comparison.get("fixture_comparisons", []):
            if comp.get("verification_status") != "verified":
                unmet_requirements.append(
                    f"Toolchain byte comparison for fixture '{comp.get('fixture_id')}' is '{comp.get('verification_status')}'"
                )
    except Exception as exc:
        unmet_requirements.append(f"Size-ledger comparison report invalid: {exc}")

    return {
        "complete": len(unmet_requirements) == 0,
        "unmet_requirements": unmet_requirements,
    }


def execute_release_fixture_validation(
    repo_root: Path,
    binary_dir: Path,
    *,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Execute release fixtures with D3D12 validation and verify actual compute readback bytes.

    Also captures benchmark metadata (timing, committed memory, capabilities), observed
    validation-message evidence, and observed runtime module inventories.
    """
    expected = load_compute_expected_output(repo_root)
    specs = [
        ("empty_player", "nw_empty_player.exe", ["--no-validation"]),
        ("clear_present", "nw_clear_present_fixture.exe", ["--validation", "--frames", "2"]),
        (
            "compute_shader",
            "nw_compute_shader_fixture.exe",
            ["--validation", "--emit-readback-values"],
        ),
    ]

    runs: list[dict[str, Any]] = []
    all_passed = True
    compute_readback: dict[str, Any] | None = None
    observed_modules: list[str] = []

    for fixture_id, exe_name, extra_args in specs:
        exe_path = resolve_fixture_executable(binary_dir, fixture_id, exe_name)
        if exe_path is None:
            all_passed = False
            runs.append(
                {
                    "fixture_id": fixture_id,
                    "executable": exe_name,
                    "execution_status": "failed",
                    "exit_code": None,
                    "stdout_sha256": None,
                    "stderr_sha256": None,
                    "failure_reason": "release executable missing",
                }
            )
            continue
        run_args = [str(exe_path), *extra_args]
        run_args.extend(["--emit-benchmark-metrics", "--emit-loaded-modules"])
        try:
            proc = subprocess.run(
                run_args,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            ret_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            ret_code = None
            stdout = ""
            stderr = str(exc)

        passed = ret_code == 0
        run_record: dict[str, Any] = {
            "fixture_id": fixture_id,
            "executable": exe_name,
            "execution_status": "passed" if passed else "failed",
            "exit_code": ret_code,
            "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest() if stdout else None,
            "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest() if stderr else None,
            "stdout": stdout,
            "stderr": stderr,
        }

        metrics = parse_fixture_metrics(stdout)
        validation = parse_validation_evidence(stdout, stderr)
        modules = parse_loaded_modules(stdout)
        run_record["metrics"] = metrics
        run_record["d3d12_validation_clean"] = validation["d3d12_validation_clean"]
        run_record["d3d12_validation_errors"] = validation["d3d12_validation_errors"]
        run_record["d3d12_validation_messages"] = validation["d3d12_validation_messages"]
        run_record["observed_loaded_modules"] = modules
        for module in modules:
            if module not in observed_modules:
                observed_modules.append(module)

        if fixture_id == "compute_shader":
            compute_readback = parse_observed_compute_readback(stdout, expected)
            run_record["readback"] = compute_readback
            passed = (
                passed
                and compute_readback["elements_match"]
                and compute_readback.get("hash_match") is True
            )

        # A clean D3D12 validation run is part of fixture success. Any message at
        # D3D12_MESSAGE_SEVERITY_ERROR or CORRUPTION fails the run even when the
        # process returned zero.
        if not validation["d3d12_validation_clean"]:
            passed = False
            run_record["failure_reason"] = "D3D12 validation emitted error/corruption messages"
        run_record["execution_status"] = "passed" if passed else "failed"

        if not passed:
            all_passed = False
            if stderr and "failure_reason" not in run_record:
                run_record["failure_reason"] = stderr.strip().splitlines()[-1][:500]
        runs.append(run_record)

    return {
        "passed": all_passed,
        "binary_dir": str(binary_dir),
        "d3d12_validation_enabled": True,
        "fixture_runs": runs,
        "compute_readback": compute_readback,
        "observed_loaded_modules": observed_modules,
        "runtime_module_evidence_source": (
            "observed_fixture_module_inventory" if observed_modules else "unavailable"
        ),
    }


FAILURE_RECOVERY_CASES = (
    ("OsVersionUnsupported", "platform_bootstrap", 11),
    ("InboxRuntimeBlocker", "device_and_capability", 25),
    ("PresentationOccluded", "resource_and_queue_init", 36),
    ("ComputeVerificationFailed", "gpu_execution_and_validation", 60),
    ("D3D12ValidationError", "gpu_execution_and_validation", 61),
)


def run_failure_recovery_checks(
    binary_dir: Path,
    *,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Execute fixture failure injections and verify distinct exit codes plus diagnostic fields.

    This is an executable check, not a source-text inspection. Each simulated failure must
    return its documented numeric exit code and emit one structured NW-DIAG line whose
    name/category/stage/HRESULT fields match the contract. Inbox-runtime blockers must also
    carry every mandatory blocker field.
    """
    exe_path = resolve_fixture_executable(
        binary_dir, "empty_player", "nw_empty_player.exe"
    )
    if exe_path is None:
        return {
            "passed": False,
            "skipped": True,
            "reason": (
                "Failure-recovery diagnostics were not executed because "
                f"nw_empty_player.exe is absent from {binary_dir}."
            ),
            "cases": [],
        }

    observed_codes: set[int] = set()
    cases: list[dict[str, Any]] = []
    all_passed = True
    for name, category, expected_code in FAILURE_RECOVERY_CASES:
        command = [
            str(exe_path),
            "--simulate-failure",
            name,
            "--diagnostics",
        ]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            all_passed = False
            cases.append(
                {
                    "name": name,
                    "expected_exit_code": expected_code,
                    "passed": False,
                    "failure_reason": f"failed to execute diagnostic injection: {exc}",
                }
            )
            continue

        diagnostics = proc.stderr or ""
        match = NW_DIAGNOSTIC_LINE_RE.search(diagnostics)
        required_blocker_fields = (
            "win_build=",
            "d3d12_dll=",
            "feature=",
            "summary=",
            "recovery=",
        )
        missing_blocker_fields = (
            [
                field
                for field in required_blocker_fields
                if field not in diagnostics
            ]
            if name == "InboxRuntimeBlocker"
            else []
        )
        line_ok = bool(
            match
            and int(match.group(1)) == expected_code
            and match.group(2) == name
            and match.group(3) == category
            and match.group(4)
            and match.group(5)
        )
        code_ok = proc.returncode == expected_code
        case_passed = bool(
            code_ok and line_ok and not missing_blocker_fields
        )
        observed_codes.add(proc.returncode)
        if not case_passed:
            all_passed = False
        cases.append(
            {
                "name": name,
                "expected_exit_code": expected_code,
                "observed_exit_code": proc.returncode,
                "diagnostic_line_matched": line_ok,
                "missing_blocker_fields": missing_blocker_fields,
                "passed": case_passed,
                "stderr_tail": (
                    diagnostics.strip().splitlines()[-1][:500]
                    if diagnostics.strip()
                    else None
                ),
            }
        )

    distinct_exit_codes = len(observed_codes) == len(FAILURE_RECOVERY_CASES)
    if not distinct_exit_codes:
        all_passed = False
    return {
        "passed": all_passed,
        "distinct_exit_codes": distinct_exit_codes,
        "observed_exit_codes": sorted(observed_codes),
        "cases": cases,
    }


BENCHMARK_CAPTURE_FILES = {
    "rtx_4060_desktop": "rtx-4060-baseline.json",
    "rtx_50_series_desktop": "rtx-50-series-baseline.json",
}

_TOOLCHAIN_METADATA = {
    "clang-cl-lld": {
        "toolchain_id": "clang-cl-lld",
        "compiler_name": "LLVM clang-cl",
        "compiler_executable": "clang-cl.exe",
        "linker_name": "LLVM lld-link",
        "linker_executable": "lld-link.exe",
    },
    "msvc-ltcg": {
        "toolchain_id": "msvc-ltcg",
        "compiler_name": "Microsoft C/C++ Optimizing Compiler (MSVC)",
        "compiler_executable": "cl.exe",
        "linker_name": "Microsoft Incremental Linker",
        "linker_executable": "link.exe",
    },
}


def resolve_artifact_provenance(
    repo_root: Path,
    binary_dir: Path,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """Bind executed artifacts to the selected toolchain's verified release ledgers.

    Unverified ledgers never establish provenance, including for a packaged CleanSystem
    bundle. The selected toolchain entry must be verified with fixture-execution,
    CodeView/PDB, and two-clean-directory reproducibility flags, and all three fixture
    ledgers (empty_player, clear_present, compute_shader) must be verified and hash-match
    the executed artifacts. Optional CI build evidence must additionally show no concrete
    failures and passing reproducibility, debug-symbol, offline-shader, and
    failure-recovery checks.
    """
    fixture_specs = (
        ("empty_player", "nw_empty_player.exe"),
        ("clear_present", "nw_clear_present_fixture.exe"),
        ("compute_shader", "nw_compute_shader_fixture.exe"),
    )
    required_fixture_ids = tuple(fixture_id for fixture_id, _ in fixture_specs)
    hashes: dict[str, str | None] = {}
    sizes: dict[str, int | None] = {}
    for fixture_id, exe_name in fixture_specs:
        exe_path = resolve_fixture_executable(binary_dir, fixture_id, exe_name)
        if exe_path is None:
            hashes[fixture_id] = None
            sizes[fixture_id] = None
        else:
            payload = exe_path.read_bytes()
            hashes[fixture_id] = hashlib.sha256(payload).hexdigest()
            sizes[fixture_id] = len(payload)

    manifest_path = (
        evidence_dir / "toolchain-manifest.v1.json"
        if evidence_dir is not None
        else repo_root / "benchmarks" / "toolchain-manifest.v1.json"
    )
    selected_toolchain: str | None = None
    selected_manifest_verified = False
    if manifest_path.is_file():
        toolchain_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        selected_toolchain = toolchain_manifest.get("selected_toolchain", {}).get(
            "toolchain_id"
        )
        selected_entry = next(
            (
                entry
                for entry in toolchain_manifest.get("evaluated_toolchains", [])
                if entry.get("toolchain_id") == selected_toolchain
            ),
            None,
        )
        selected_manifest_verified = bool(
            selected_entry
            and selected_entry.get("verification_status") == "verified"
            and selected_entry.get("fixture_execution_verified") is True
            and selected_entry.get("pdb_debugging_verified") is True
            and selected_entry.get("deterministic_repro_verified") is True
            and selected_entry.get("deployment_closure_identical") is True
            and all(
                selected_entry.get(field) is not None
                for field in (
                    "empty_player_bytes",
                    "clear_present_bytes",
                    "compute_fixture_bytes",
                )
            )
        )

    bundle_build_checks: dict[str, Any] | None = None
    if evidence_dir is not None:
        build_checks_path = evidence_dir / "build-checks.json"
        if build_checks_path.is_file():
            bundle_build_checks = json.loads(build_checks_path.read_text(encoding="utf-8"))

    ledger_root = (
        evidence_dir / "ledgers"
        if evidence_dir is not None
        else repo_root / "benchmarks" / "ledgers"
    )
    ledger_matches: dict[str, set[str]] = {fixture_id: set() for fixture_id, _ in fixture_specs}
    if ledger_root.is_dir() and selected_toolchain and selected_manifest_verified:
        for ledger_path in sorted(ledger_root.glob("*.json")):
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            if ledger.get("verification_status") != "verified":
                continue
            if ledger.get("toolchain_id") != selected_toolchain:
                continue
            fixture_id = str(ledger.get("fixture_id"))
            if fixture_id not in ledger_matches:
                continue
            artifact_hash = hashes.get(fixture_id)
            if not artifact_hash:
                continue
            for component in ledger.get("distributed_components", []):
                if (
                    component.get("category") == "executable"
                    and str(component.get("sha256", "")).lower() == artifact_hash.lower()
                ):
                    ledger_matches[fixture_id].add(selected_toolchain)

    all_ledgers_verified = bool(selected_toolchain) and all(
        ledger_matches[fixture_id] == {selected_toolchain}
        for fixture_id in required_fixture_ids
    )
    toolchain_id = (
        selected_toolchain
        if all_ledgers_verified and selected_manifest_verified
        else None
    )

    if evidence_dir is not None and toolchain_id is not None:
        package_manifest_path = evidence_dir / "artifact-manifest.json"
        if not package_manifest_path.is_file():
            toolchain_id = None
        else:
            package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
            package_files = {
                str(item.get("name", "")).lower(): item
                for item in package_manifest.get("files", [])
            }
            if package_manifest.get("toolchain") != toolchain_id:
                toolchain_id = None
            for fixture_id, exe_name in fixture_specs:
                item = package_files.get(exe_name.lower())
                if (
                    item is None
                    or str(item.get("sha256", "")).lower()
                    != str(hashes.get(fixture_id) or "").lower()
                    or int(item.get("bytes", -1)) != int(sizes.get(fixture_id) or -1)
                ):
                    toolchain_id = None
                    break

        if toolchain_id is not None:
            checks = (bundle_build_checks or {}).get("checks", {})
            debug_check_name = (
                "msvc_debug_symbols"
                if toolchain_id == "msvc-ltcg"
                else "clang_debug_symbols"
            )
            build_checks_ok = bool(
                bundle_build_checks is not None
                and bundle_build_checks.get("concrete_failures") == []
                and checks.get("reproducibility_build", {}).get("passed") is True
                and checks.get(debug_check_name, {}).get("passed") is True
                and checks.get("offline_shader", {}).get("passed") is True
                and checks.get("failure_recovery", {}).get("passed") is True
            )
            if not build_checks_ok:
                toolchain_id = None

        if toolchain_id is not None and bundle_build_checks is not None:
            selected = (
                bundle_build_checks.get("checks", {})
                .get("size_ledgers", {})
                .get("recorded", {})
                .get(toolchain_id, {})
            )
            toolchain_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                selected.get("selected_toolchain") != toolchain_id
                or toolchain_manifest.get("selected_toolchain", {}).get("toolchain_id")
                != toolchain_id
            ):
                toolchain_id = None
            recorded_fixtures = selected.get("recorded_fixtures", {})
            for fixture_id in required_fixture_ids:
                if (
                    str(recorded_fixtures.get(fixture_id, {}).get("sha256", "")).lower()
                    != str(hashes.get(fixture_id) or "").lower()
                ):
                    toolchain_id = None
                    break

    return {
        "toolchain_id": toolchain_id,
        "artifact_sha256": hashes,
        "source": (
            "recorded_ledger_artifact_hash"
            if toolchain_id is not None
            else "unavailable"
        ),
    }

def capture_benchmark_result(
    repo_root: Path,
    binary_dir: Path,
    *,
    hardware_tier: str,
    system_environment: dict[str, str] | None = None,
    evidence_dir: Path | None = None,
    output_path: Path | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Execute fixtures and record measured benchmark/hardware metrics against the schema.

    The capture path never interpolates missing values. Metrics the fixture did not emit are
    recorded as null and listed in unavailable_measurements. Hardware coverage is marked
    unverified unless an NVIDIA adapter and driver version were measured.
    """
    if hardware_tier not in BENCHMARK_CAPTURE_FILES:
        raise ValueError(f"Unknown hardware tier '{hardware_tier}'")

    output = output_path or (
        repo_root / "benchmarks" / "hardware" / BENCHMARK_CAPTURE_FILES[hardware_tier]
    )
    validation = execute_release_fixture_validation(
        repo_root, binary_dir, timeout_seconds=timeout_seconds
    )
    runs_by_fixture = {run["fixture_id"]: run for run in validation["fixture_runs"]}

    adapter_evidence = reconcile_fixture_adapter_descriptions(
        validation["fixture_runs"]
    )
    override = system_environment or {}
    # The fixture emits only the major build number (e.g. 26100), not the installed
    # update revision (UBR). A full os_build value therefore requires an explicit
    # measured --os-build override; inferring ".0" would fabricate the revision.
    measured_os_build = override.get("os_build")
    observed_gpu_model = adapter_evidence["executing_adapter"]
    measured_gpu_model = override.get("gpu_model") or observed_gpu_model
    # Driver metadata is accepted only with an execution-bound adapter observation;
    # registry-only host enumeration is never sufficient for certification.
    measured_nvidia_driver = override.get("nvidia_driver_version")

    first_metrics: dict[str, Any] = {}
    for fixture in ("clear_present", "compute_shader"):
        metrics = runs_by_fixture.get(fixture, {}).get("metrics", {})
        if metrics:
            first_metrics.update({k: v for k, v in metrics.items() if v is not None})

    unavailable: list[dict[str, str]] = []

    def mark_unavailable(metric: str, reason: str) -> None:
        unavailable.append({"metric": metric, "reason": reason})

    if evidence_dir is not None:
        toolchain_manifest_path = evidence_dir / "toolchain-manifest.v1.json"
        toolchain_manifest = (
            json.loads(toolchain_manifest_path.read_text(encoding="utf-8"))
            if toolchain_manifest_path.is_file()
            else {}
        )
    else:
        toolchain_manifest_path = repo_root / "benchmarks" / "toolchain-manifest.v1.json"
        toolchain_manifest = json.loads(
            toolchain_manifest_path.read_text(encoding="utf-8")
        )
    pinned = toolchain_manifest.get("pinned_dependencies", {})
    provenance = resolve_artifact_provenance(
        repo_root, binary_dir, evidence_dir
    )
    provenance_toolchain = provenance.get("toolchain_id")
    evaluated = {
        entry.get("toolchain_id"): entry
        for entry in toolchain_manifest.get("evaluated_toolchains", [])
    }
    if provenance_toolchain in _TOOLCHAIN_METADATA and provenance_toolchain in evaluated:
        meta = _TOOLCHAIN_METADATA[provenance_toolchain]
        entry = evaluated[provenance_toolchain]
        toolchain = {
            "toolchain_id": str(provenance_toolchain),
            "compiler_name": meta["compiler_name"],
            "compiler_executable": meta["compiler_executable"],
            "compiler_version": str(entry.get("compiler_version", "unverified")),
            "linker_name": meta["linker_name"],
            "linker_executable": meta["linker_executable"],
            "linker_version": str(entry.get("linker_version", "unverified")),
            "windows_sdk_version": str(pinned.get("windows_sdk_version", "unverified")),
            "dxc_version": str(pinned.get("dxc_version", "unverified")),
        }
    else:
        toolchain = {
            "toolchain_id": None,
            "compiler_name": None,
            "compiler_executable": None,
            "compiler_version": None,
            "linker_name": None,
            "linker_executable": None,
            "linker_version": None,
            "windows_sdk_version": None,
            "dxc_version": None,
        }
        mark_unavailable(
            "environment_metadata.toolchain",
            "Executed artifacts could not be matched to a recorded ledger hash, so compiler/linker provenance is unverified.",
        )

    gpu_model = observed_gpu_model or measured_gpu_model
    nvidia_driver = measured_nvidia_driver
    cpu_model = (
        override.get("cpu_model")
        or os.environ.get("PROCESSOR_IDENTIFIER")
        or platform.processor()
    )
    gpu_architecture = override.get("gpu_architecture")
    wddm_driver_version = override.get("wddm_driver_version")
    vendor_id = first_metrics.get("vendor_id")
    device_id = first_metrics.get("device_id")
    vram_mb = first_metrics.get("vram_mb")
    power = {
        "windows_power_plan": override.get("windows_power_plan"),
        "nvidia_power_management_mode": override.get("nvidia_power_management_mode"),
        "pci_express_link_state_power_management": override.get(
            "pci_express_link_state_power_management"
        ),
    }
    driver_cache_state = override.get("driver_shader_cache_state", "unverified")
    os_file_cache_state = override.get("os_file_cache_state", "unverified")
    clear_metrics = runs_by_fixture.get("clear_present", {}).get("metrics", {})
    warmup_frames = int(clear_metrics.get("warmup_frames", first_metrics.get("warmup_frames", 0)))
    measured_frames = int(
        clear_metrics.get("measured_frames", first_metrics.get("measured_frames", 2))
    )

    for metric_name, value, reason in (
        ("environment_metadata.gpu_model", gpu_model, "Executing NVIDIA adapter was not observed."),
        ("environment_metadata.gpu_architecture", gpu_architecture, "GPU architecture was not provided by the operator or detected."),
        ("environment_metadata.gpu_vendor_id", vendor_id, "DXGI adapter vendor ID was not emitted."),
        ("environment_metadata.gpu_device_id", device_id, "DXGI adapter device ID was not emitted."),
        ("environment_metadata.vram_mb", vram_mb, "Dedicated video memory was not emitted."),
        ("environment_metadata.cpu_model", cpu_model, "CPU model was not detected or provided."),
        ("environment_metadata.os_build", measured_os_build, "Windows 11 build was not measured or provided."),
        ("environment_metadata.nvidia_driver_version", nvidia_driver, "NVIDIA driver version was not measured or provided."),
        ("environment_metadata.wddm_driver_version", wddm_driver_version, "WDDM driver version was not provided."),
        ("environment_metadata.power_settings.windows_power_plan", power["windows_power_plan"], "Windows power plan was not provided."),
        ("environment_metadata.power_settings.nvidia_power_management_mode", power["nvidia_power_management_mode"], "NVIDIA power management mode was not provided."),
        ("environment_metadata.power_settings.pci_express_link_state_power_management", power["pci_express_link_state_power_management"], "PCIe link-state power management was not provided."),
        ("environment_metadata.cache_conditions.driver_shader_cache_state", None if driver_cache_state == "unverified" else driver_cache_state, "Driver shader-cache state was not provided."),
        ("environment_metadata.cache_conditions.os_file_cache_state", None if os_file_cache_state == "unverified" else os_file_cache_state, "OS file-cache state was not provided."),
    ):
        if value is None or value == "":
            mark_unavailable(metric_name, reason)

    desktop_cpu_verified = desktop_cpu_model_matches(
        str(cpu_model) if cpu_model else None
    )
    if cpu_model and not desktop_cpu_verified:
        mark_unavailable(
            "environment_metadata.cpu_model",
            f"CPU model '{cpu_model}' is missing or is a generic host string rather than "
            "recorded CPU metadata.",
        )

    reported_adapter_descriptions = [
        value for value in adapter_evidence["descriptions"].values() if value
    ]
    if not reported_adapter_descriptions:
        mark_unavailable(
            "environment_metadata.gpu_model",
            "No adapter description was emitted by the executing fixtures; tier coverage cannot be bound to the observed GPU.",
        )
    elif not adapter_evidence["consistent"]:
        mark_unavailable(
            "environment_metadata.gpu_model",
            "Raster and compute fixtures did not report the same executing adapter.",
        )
    elif override.get("gpu_model") and str(override["gpu_model"]).strip().lower() != str(
        observed_gpu_model
    ).strip().lower():
        mark_unavailable(
            "environment_metadata.gpu_model",
            "Operator-provided GPU model does not match the adapter reported by the executing fixtures.",
        )

    environment_metadata = {
        "gpu_model": str(gpu_model) if gpu_model else None,
        "gpu_architecture": str(gpu_architecture) if gpu_architecture else None,
        "gpu_vendor_id": ("0x%04X" % int(vendor_id)) if vendor_id is not None else None,
        "gpu_device_id": ("0x%04X" % int(device_id)) if device_id is not None else None,
        "vram_mb": int(vram_mb) if vram_mb is not None else None,
        "cpu_model": str(cpu_model) if cpu_model else None,
        "os_name": "Windows 11 x64",
        "os_build": str(measured_os_build) if measured_os_build else None,
        "nvidia_driver_version": str(nvidia_driver) if nvidia_driver else None,
        "wddm_driver_version": str(wddm_driver_version) if wddm_driver_version else None,
        "toolchain": toolchain,
        "power_settings": power,
        "resolution": {
            "width": 1280,
            "height": 720,
            "refresh_hz": (
                int(override["refresh_hz"]) if override.get("refresh_hz") else None
            ),
            "format": "DXGI_FORMAT_R8G8B8A8_UNORM",
            "presentation_mode": "windowed_swapchain",
            "measurement_source": "requested_fixture_size",
        },
        "cache_conditions": {
            "driver_shader_cache_state": driver_cache_state,
            "os_file_cache_state": os_file_cache_state,
            "warmup_frames": warmup_frames,
            "measured_frames": measured_frames,
        },
    }
    if not override.get("refresh_hz"):
        mark_unavailable(
            "environment_metadata.resolution.refresh_hz",
            "Swapchain refresh rate was not measured or provided.",
        )

    fixture_results: list[dict[str, Any]] = []
    all_passed = True
    for fixture_id in ("clear_present", "compute_shader"):
        run = runs_by_fixture.get(fixture_id, {})
        metrics = run.get("metrics", {})
        exe_name = (
            "nw_clear_present_fixture.exe"
            if fixture_id == "clear_present"
            else "nw_compute_shader_fixture.exe"
        )
        exe_path = resolve_fixture_executable(binary_dir, fixture_id, exe_name)
        total_bytes = exe_path.stat().st_size if exe_path is not None else None
        if total_bytes is None:
            unavailable.append(
                {
                    "metric": f"fixture_results[{fixture_id}].total_distributed_bytes",
                    "reason": f"The {fixture_id} executable was not present in the deployment directory.",
                }
            )
        readback = run.get("readback")
        compute_ok = (
            bool(readback and readback.get("elements_match") is True and readback.get("hash_match") is True)
            if fixture_id == "compute_shader"
            else None
        )
        execution_status = run.get("execution_status", "unverified")
        if execution_status != "passed":
            all_passed = False
        measured_values = {
            "startup_time_ms": metrics.get("startup_time_ms"),
            "cpu_time_ms": metrics.get("cpu_time_ms"),
            "gpu_time_ms": metrics.get("gpu_time_ms"),
            "committed_memory_bytes": metrics.get("committed_memory_bytes"),
            "gpu_committed_memory_bytes": metrics.get("gpu_committed_memory_bytes"),
        }
        for metric_name, value in measured_values.items():
            if value is None:
                unavailable.append(
                    {
                        "metric": f"fixture_results[{fixture_id}].{metric_name}",
                        "reason": (
                            f"The {fixture_id} fixture did not emit a measured {metric_name}; "
                            "recorded as null rather than zero."
                        ),
                    }
                )
        fixture_results.append(
            {
                "fixture_id": fixture_id,
                "sha256": provenance["artifact_sha256"].get(fixture_id),
                "execution_status": execution_status,
                "d3d12_validation_clean": run.get("d3d12_validation_clean"),
                "compute_output_verified": compute_ok,
                "exit_code": run.get("exit_code"),
                "total_distributed_bytes": total_bytes,
                **measured_values,
            }
        )

    feature_level_value = first_metrics.get("max_feature_level") or first_metrics.get(
        "feature_level"
    )
    shader_model_value = first_metrics.get("max_shader_model")
    capabilities_measured = feature_level_value is not None and shader_model_value is not None
    wave_ops_queried = bool(first_metrics.get("wave_ops_queried"))
    int64_queried = bool(first_metrics.get("int64_shader_ops_queried"))
    fp16_queried = bool(first_metrics.get("fp16_shader_ops_queried"))
    captured_capabilities = {
        "max_supported_feature_level": (
            str(feature_level_value) if feature_level_value is not None else None
        ),
        "max_supported_shader_model": (
            str(shader_model_value)
            if shader_model_value is not None
            else None
        ),
        "baseline_feature_level_12_0_supported": (
            bool(first_metrics.get("baseline_feature_level_12_0_supported"))
            if "baseline_feature_level_12_0_supported" in first_metrics
            else None
        ),
        "baseline_shader_model_6_0_supported": (
            bool(first_metrics.get("baseline_shader_model_6_0_supported"))
            if "baseline_shader_model_6_0_supported" in first_metrics
            else None
        ),
        "resource_binding_tier": first_metrics.get("resource_binding_tier"),
        "resource_heap_tier": first_metrics.get("resource_heap_tier"),
        "wave_ops_queried": wave_ops_queried,
        "wave_ops_supported": (
            bool(first_metrics.get("wave_ops_supported"))
            if wave_ops_queried and "wave_ops_supported" in first_metrics
            else None
        ),
        "int64_shader_ops_queried": int64_queried,
        "int64_shader_ops_supported": (
            bool(first_metrics.get("int64_shader_ops_supported"))
            if int64_queried and "int64_shader_ops_supported" in first_metrics
            else None
        ),
        "fp16_shader_ops_queried": fp16_queried,
        "fp16_shader_ops_supported": (
            bool(first_metrics.get("fp16_shader_ops_supported"))
            if fp16_queried and "fp16_shader_ops_supported" in first_metrics
            else None
        ),
        "typed_uav_additional_formats_supported": (
            bool(first_metrics.get("typed_uav_additional_formats_supported"))
            if "typed_uav_additional_formats_supported" in first_metrics
            else None
        ),
    }
    if not capabilities_measured:
        unavailable.append(
            {
                "metric": "captured_capabilities",
                "reason": "Fixture did not emit D3D12 CheckFeatureSupport capability metrics.",
            }
        )
    if not wave_ops_queried:
        unavailable.append(
            {
                "metric": "captured_capabilities.wave_ops_supported",
                "reason": "The fixture did not query D3D12_FEATURE_D3D12_OPTIONS1 WaveOps.",
            }
        )
    if not int64_queried:
        mark_unavailable(
            "captured_capabilities.int64_shader_ops_supported",
            "The fixture did not query D3D12_FEATURE_D3D12_OPTIONS1 Int64ShaderOps.",
        )
    if not fp16_queried:
        mark_unavailable(
            "captured_capabilities.fp16_shader_ops_supported",
            "The fixture did not query D3D12_FEATURE_D3D12_OPTIONS4 native 16-bit shader ops.",
        )

    gpu_tier_matches = bool(
        observed_gpu_model
        and hardware_tier_matches(str(observed_gpu_model), hardware_tier)
    )
    hardware_verified = bool(
        observed_gpu_model
        and measured_nvidia_driver
        and gpu_tier_matches
        and desktop_cpu_verified
        and _is_supported_nvidia_gpu_and_driver(
            str(observed_gpu_model), str(measured_nvidia_driver)
        )
        and measured_os_build
        and re.match(
            r"^10\.0\.(22[0-9]{3}|26[0-9]{3})\.[0-9]+$", str(measured_os_build)
        )
    )
    if observed_gpu_model and not gpu_tier_matches:
        mark_unavailable(
            "environment_metadata.gpu_model",
            f"Observed adapter '{observed_gpu_model}' does not match requested tier '{hardware_tier}'.",
        )
    verified = (
        hardware_verified
        and all_passed
        and capabilities_measured
        and provenance_toolchain is not None
        and not unavailable
    )
    result: dict[str, Any] = {
        "schema_version": "1.0.0",
        "benchmark_id": f"nw01-hardware-{hardware_tier.replace('_', '-')}-baseline",
        "hardware_tier": hardware_tier,
        "verification_status": "verified" if verified else "unverified",
        "benchmark_passed": bool(verified),
        "environment_metadata": environment_metadata,
        "captured_capabilities": captured_capabilities,
        "artifact_provenance": provenance,
        "fixture_results": fixture_results,
        "unavailable_measurements": unavailable,
    }
    if not verified:
        result["unverified_reason"] = (
            "Benchmark capture did not produce a complete measured record: "
            + "; ".join(item["metric"] for item in unavailable)
            if unavailable
            else "Hardware coverage or fixture execution could not be verified."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    schema_path = repo_root / "schemas" / "benchmark-result.schema.json"
    if schema_path.exists():
        validate_file_against_schema(output, schema_path)
    return result


def extract_runtime_metrics_by_fixture(
    fixture_validation: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Extract ledger-compatible runtime metrics from fixture-validation evidence."""
    metric_keys = (
        "startup_time_ms",
        "cpu_time_ms",
        "gpu_time_ms",
        "committed_memory_bytes",
        "gpu_committed_memory_bytes",
    )
    result: dict[str, dict[str, Any]] = {}
    for run in fixture_validation.get("fixture_runs", []):
        metrics = run.get("metrics", {})
        result[str(run.get("fixture_id"))] = {
            key: metrics.get(key) for key in metric_keys
        }
    return result


def run_full_mode(
    repo_root: Path,
    *,
    gpu_fixtures_available: bool = True,
) -> tuple[int, dict[str, Any]]:
    """Execute Mode=Full verification checks and record reproducibility evidence when built."""
    report: dict[str, Any] = {
        "mode": "Full",
        "source_commit": resolve_source_commit(repo_root),
        "host_os": platform.system(),
        "host_arch": platform.machine(),
        "checks": {},
        "missing_prerequisites": [],
        "unmet_evidence": [],
        "full_baseline_passed": False,
    }

    # 1. Schema validation
    schema_res = validate_all_repository_artifacts(repo_root)
    report["checks"]["schemas"] = {"passed": True, "validated_count": schema_res["validated_count"]}

    # 2. Offline shader source & manifest verification (re-evaluated after the Windows build below)
    dxc_bin = find_dxc_executable()
    offline_shader = evaluate_offline_shader(repo_root, dxc_bin)
    shader_ok = offline_shader["passed"]
    report["checks"]["offline_shader"] = offline_shader

    # 3. Dependency closure & size ledger checks
    dep_reports = verify_all_recorded_ledgers(repo_root)
    ledger_comparison = generate_size_comparison_report(repo_root)
    comparison_verified = (
        bool(ledger_comparison.get("fixture_comparisons"))
        and all(
            c.get("verification_status") == "verified"
            and c.get("clang_cl_lld_bytes") is not None
            and c.get("msvc_ltcg_bytes") is not None
            for c in ledger_comparison.get("fixture_comparisons", [])
        )
        and ledger_comparison.get("smaller_verified_toolchain")
        == ledger_comparison.get("selected_toolchain")
    )
    report["checks"]["dependency_closures"] = {"passed": True, "verified_ledgers": len(dep_reports)}
    report["checks"]["size_ledgers"] = {
        "passed": comparison_verified,
        "contracts_valid": True,
        "comparison_verified": comparison_verified,
        "comparison": ledger_comparison,
    }

    # 4. Build, two-clean-directory reproducibility, PDB & MSVC/LTCG comparison checks.
    is_windows = platform.system().lower() == "windows"
    cmake_bin = shutil.which("cmake")
    ninja_bin = shutil.which("ninja")
    clang_cl_bin = shutil.which("clang-cl") or shutil.which("clang-cl.exe")
    lld_link_bin = shutil.which("lld-link") or shutil.which("lld-link.exe")
    cl_bin = shutil.which("cl") or shutil.which("cl.exe")
    link_bin = shutil.which("link") or shutil.which("link.exe")

    if not is_windows:
        report["missing_prerequisites"].append("Windows 11 x64 OS host required for native D3D12 build and execution")
    if not cmake_bin:
        report["missing_prerequisites"].append("cmake (pinned 3.30.2) not found in PATH")
    if not ninja_bin:
        report["missing_prerequisites"].append("ninja (pinned 1.12.1) not found in PATH")
    if not clang_cl_bin:
        report["missing_prerequisites"].append("clang-cl (pinned 18.1.8) not found in PATH")
    if not lld_link_bin:
        report["missing_prerequisites"].append("lld-link (pinned 18.1.8) not found in PATH")
    if not cl_bin:
        report["missing_prerequisites"].append("cl (pinned MSVC 19.40.33821) not found in PATH for toolchain comparison")
    if not link_bin:
        report["missing_prerequisites"].append("link (pinned MSVC 14.40.33821.0) not found in PATH for toolchain comparison")
    if not dxc_bin:
        report["missing_prerequisites"].append("dxc (pinned 1.8.2407) not found in PATH")

    clang_prereqs_ready = bool(
        is_windows and cmake_bin and ninja_bin and clang_cl_bin and lld_link_bin and dxc_bin
    )
    msvc_prereqs_ready = bool(
        is_windows and cmake_bin and ninja_bin and cl_bin and link_bin and dxc_bin
    )

    fixture_validation_passed = False
    msvc_fixture_validation_passed = False
    clang_recording: dict[str, Any] | None = None
    msvc_recording: dict[str, Any] | None = None
    recorded_any = False

    # 4a. clang-cl/lld-link path runs independently of MSVC availability.
    if clang_prereqs_ready:
        repro_cmp: dict[str, Any] | None = None
        try:
            repro_cmp = build_and_compare_two_clean_directories(
                repo_root,
                repo_root / "build" / "repro-clean-a",
                repo_root / "build" / "repro-clean-b",
            )
            report["checks"]["reproducibility_build"] = {
                "passed": repro_cmp["all_artifacts_match"],
                "all_artifacts_match": repro_cmp["all_artifacts_match"],
                "recorded_to": "benchmarks/reproducibility/release-build-reproducibility.json",
            }
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            report["checks"]["reproducibility_build"] = {
                "passed": False,
                "reason": f"Pinned clang-cl Windows build failed: {exc}",
            }
            report["checks"]["clang_debug_symbols"] = {
                "passed": False,
                "reason": "Skipped because the pinned clang-cl release reproducibility build failed",
            }
            report["checks"]["fixture_validation"] = {
                "passed": False,
                "reason": "Skipped because the pinned clang-cl Windows build failed",
            }
            report["checks"]["failure_recovery"] = {
                "passed": False,
                "skipped": True,
                "reason": "Skipped because the pinned clang-cl Windows build failed",
            }

        if repro_cmp is not None:
            if repro_cmp["all_artifacts_match"]:
                clang_symbols: dict[str, Any] = {"verified": False, "built": False}
                try:
                    clang_symbols = build_and_verify_debug_symbols(repo_root, "clang-cl-lld")
                    report["checks"]["clang_debug_symbols"] = {
                        "passed": clang_symbols["verified"],
                        "built": clang_symbols["built"],
                        "output_dir": clang_symbols["output_dir"],
                        "missing_or_invalid": clang_symbols.get("missing_or_invalid", []),
                        "missing_toolchain_tools": clang_symbols.get("missing_toolchain_tools", []),
                    }
                except Exception as exc:
                    # Debug-symbol failures are isolated from the (already successful)
                    # reproducibility comparison so they cannot clear its PASS result.
                    report["checks"]["clang_debug_symbols"] = {
                        "passed": False,
                        "built": False,
                        "reason": f"Pinned clang-cl dev /DEBUG:FULL build failed: {exc}",
                    }

                # The clean build is the authoritative source of offline DXC evidence.
                offline_shader = evaluate_offline_shader(
                    repo_root,
                    dxc_bin,
                    search_dirs=[
                        repo_root / "build" / "repro-clean-a",
                        repo_root / "build" / "repro-clean-b",
                    ],
                    record_from_build=True,
                )
                shader_ok = offline_shader["passed"]
                report["checks"]["offline_shader"] = offline_shader
                if gpu_fixtures_available:
                    fixture_validation = execute_release_fixture_validation(
                        repo_root, repo_root / "build" / "repro-clean-a"
                    )
                    fixture_validation_passed = fixture_validation["passed"]
                    report["checks"]["fixture_validation"] = fixture_validation
                else:
                    fixture_validation = {"passed": False}
                    report["checks"]["fixture_validation"] = {
                        "passed": False,
                        "skipped": True,
                        "reason": (
                            "Skipped because this host exposes no target D3D12 hardware; "
                            "run Mode=Full on the physical validation machine."
                        ),
                    }
                report["checks"]["failure_recovery"] = run_failure_recovery_checks(
                    repo_root / "build" / "repro-clean-a"
                )

                try:
                    clang_recording = record_release_ledgers(
                        repo_root,
                        repo_root / "build" / "repro-clean-a",
                        toolchain_id="clang-cl-lld",
                        deterministic_repro_verified=repro_cmp["all_artifacts_match"],
                        pdb_debugging_verified=clang_symbols["verified"],
                        fixture_execution_verified=fixture_validation["passed"],
                        runtime_metrics_by_fixture=extract_runtime_metrics_by_fixture(
                            fixture_validation
                        ),
                    )
                    recorded_any = True
                except Exception as exc:
                    report["checks"]["clang_ledger_recording"] = {
                        "passed": False,
                        "reason": f"Measured clang-cl size-ledger recording failed: {exc}",
                    }
            else:
                report["checks"]["clang_debug_symbols"] = {
                    "passed": False,
                    "skipped": True,
                    "reason": "Skipped because two clang-cl clean-directory hashes did not match",
                }
                report["checks"]["fixture_validation"] = {
                    "passed": False,
                    "skipped": True,
                    "reason": "Skipped because two clang-cl clean-directory hashes did not match",
                }
                report["checks"]["failure_recovery"] = {
                    "passed": False,
                    "skipped": True,
                    "reason": "Skipped because two clang-cl clean-directory hashes did not match",
                }
    else:
        report["checks"]["reproducibility_build"] = {
            "passed": False,
            "skipped": True,
            "reason": "Skipped live Windows clang-cl/DXC build due to missing host prerequisites",
        }
        report["checks"]["clang_debug_symbols"] = {
            "passed": False,
            "skipped": True,
            "reason": "Skipped live clang-cl CodeView/PDB verification due to missing host prerequisites",
        }
        report["checks"]["fixture_validation"] = {
            "passed": False,
            "skipped": True,
            "reason": "Skipped release fixture execution because clang-cl host prerequisites are missing",
        }
        report["checks"]["failure_recovery"] = {
            "passed": False,
            "skipped": True,
            "reason": "Skipped failure-recovery diagnostics because clang-cl host prerequisites are missing",
        }

    # 4b. MSVC/LTCG comparison path is independent: missing cl.exe only removes that evidence.
    if msvc_prereqs_ready:
        msvc_repro_cmp: dict[str, Any] | None = None
        try:
            msvc_repro_cmp = build_and_compare_two_msvc_clean_directories(
                repo_root,
                repo_root / "build" / "msvc-release-ltcg-clean-a",
                repo_root / "build" / "msvc-release-ltcg-clean-b",
            )
            report["checks"]["msvc_ltcg_build"] = {
                "passed": msvc_repro_cmp["all_artifacts_match"],
                "all_artifacts_match": msvc_repro_cmp["all_artifacts_match"],
                "recorded_to": "benchmarks/reproducibility/msvc-ltcg-release-reproducibility.json",
            }
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            report["checks"]["msvc_ltcg_build"] = {
                "passed": False,
                "reason": f"Pinned MSVC/LTCG Windows build failed: {exc}",
            }
            report["checks"]["msvc_debug_symbols"] = {
                "passed": False,
                "reason": "Skipped because the pinned MSVC/LTCG release reproducibility build failed",
            }
            report["checks"]["msvc_fixture_validation"] = {
                "passed": False,
                "skipped": True,
                "reason": "Skipped because the pinned MSVC/LTCG release reproducibility build failed",
            }

        if msvc_repro_cmp is not None:
            if msvc_repro_cmp["all_artifacts_match"]:
                msvc_symbols: dict[str, Any] = {"verified": False, "built": False}
                try:
                    msvc_symbols = build_and_verify_debug_symbols(repo_root, "msvc-ltcg")
                    report["checks"]["msvc_debug_symbols"] = {
                        "passed": msvc_symbols["verified"],
                        "built": msvc_symbols["built"],
                        "output_dir": msvc_symbols["output_dir"],
                        "missing_or_invalid": msvc_symbols.get("missing_or_invalid", []),
                        "missing_toolchain_tools": msvc_symbols.get("missing_toolchain_tools", []),
                    }
                except Exception as exc:
                    # Keep MSVC/LTCG reproducibility evidence independent of PDB failures.
                    report["checks"]["msvc_debug_symbols"] = {
                        "passed": False,
                        "built": False,
                        "reason": f"Pinned MSVC dev /DEBUG:FULL build failed: {exc}",
                    }
                # The MSVC/LTCG comparison is only eligible for selection when its identical
                # clear/present and compute fixtures actually execute successfully.
                if gpu_fixtures_available:
                    msvc_fixture_validation = execute_release_fixture_validation(
                        repo_root,
                        repo_root / "build" / "msvc-release-ltcg-clean-a",
                    )
                    msvc_fixture_validation_passed = msvc_fixture_validation["passed"]
                    report["checks"]["msvc_fixture_validation"] = msvc_fixture_validation
                else:
                    msvc_fixture_validation = {"passed": False}
                    report["checks"]["msvc_fixture_validation"] = {
                        "passed": False,
                        "skipped": True,
                        "reason": (
                            "Skipped because this host exposes no target D3D12 hardware; "
                            "run Mode=Full on the physical validation machine."
                        ),
                    }

                try:
                    msvc_recording = record_release_ledgers(
                        repo_root,
                        repo_root / "build" / "msvc-release-ltcg-clean-a",
                        toolchain_id="msvc-ltcg",
                        deterministic_repro_verified=msvc_repro_cmp["all_artifacts_match"],
                        pdb_debugging_verified=msvc_symbols["verified"],
                        fixture_execution_verified=msvc_fixture_validation["passed"],
                        runtime_metrics_by_fixture=extract_runtime_metrics_by_fixture(
                            msvc_fixture_validation
                        ),
                    )
                    recorded_any = True
                except Exception as exc:
                    report["checks"]["msvc_ledger_recording"] = {
                        "passed": False,
                        "reason": f"Measured MSVC/LTCG size-ledger recording failed: {exc}",
                    }

            else:
                report["checks"]["msvc_debug_symbols"] = {
                    "passed": False,
                    "reason": "Skipped because two MSVC/LTCG clean-directory hashes did not match",
                }
                report["checks"]["msvc_fixture_validation"] = {
                    "passed": False,
                    "skipped": True,
                    "reason": "Skipped because two MSVC/LTCG clean-directory hashes did not match",
                }
    else:
        report["checks"]["msvc_ltcg_build"] = {
            "passed": False,
            "skipped": True,
            "reason": "Skipped live Windows MSVC/LTCG comparison build due to missing host prerequisites",
        }
        report["checks"]["msvc_debug_symbols"] = {
            "passed": False,
            "skipped": True,
            "reason": "Skipped MSVC/LTCG CodeView/PDB verification due to missing host prerequisites",
        }
        report["checks"]["msvc_fixture_validation"] = {
            "passed": False,
            "skipped": True,
            "reason": "Skipped MSVC/LTCG release fixture execution due to missing host prerequisites",
        }

    # 4c. Re-validate recorded evidence after any measured ledger updates.
    if recorded_any:
        report["checks"]["schemas"] = {
            "passed": False,
            "reason": "Pending post-recording schema validation",
        }
        report["checks"]["dependency_closures"] = {
            "passed": False,
            "reason": "Pending post-recording dependency closure validation",
        }
        try:
            ledger_comparison = generate_size_comparison_report(repo_root)
            comparison_verified = (
                bool(ledger_comparison.get("fixture_comparisons"))
                and all(
                    c.get("verification_status") == "verified"
                    and c.get("clang_cl_lld_bytes") is not None
                    and c.get("msvc_ltcg_bytes") is not None
                    for c in ledger_comparison.get("fixture_comparisons", [])
                )
                and ledger_comparison.get("smaller_verified_toolchain")
                == ledger_comparison.get("selected_toolchain")
            )
            report["checks"]["size_ledgers"] = {
                "passed": comparison_verified,
                "contracts_valid": True,
                "comparison_verified": comparison_verified,
                "recorded": {
                    "clang-cl-lld": clang_recording,
                    "msvc-ltcg": msvc_recording,
                },
                "comparison": ledger_comparison,
            }
        except Exception as exc:
            report["checks"]["size_ledgers"] = {
                "passed": False,
                "contracts_valid": False,
                "comparison_verified": False,
                "reason": f"Measured size-ledger recording failed: {exc}",
            }

        try:
            dep_reports = verify_all_recorded_ledgers(repo_root)
            report["checks"]["dependency_closures"] = {
                "passed": True,
                "verified_ledgers": len(dep_reports),
            }
        except Exception as exc:
            report["checks"]["dependency_closures"] = {
                "passed": False,
                "reason": f"Post-recording dependency closure verification failed: {exc}",
            }

        try:
            schema_res = validate_all_repository_artifacts(repo_root)
            report["checks"]["schemas"] = {
                "passed": True,
                "validated_count": schema_res["validated_count"],
            }
        except Exception as exc:
            report["checks"]["schemas"] = {
                "passed": False,
                "reason": f"Post-recording schema validation failed: {exc}",
            }

    # 5. Evidence completeness check (RTX 4060 Ti, RTX 50-series, clean-system, reproducibility, toolchain comparison)
    evidence = check_evidence_completeness(repo_root)
    report["unmet_evidence"] = evidence["unmet_requirements"]
    report["checks"]["evidence_completeness"] = {
        "passed": evidence["complete"],
        "skipped": True,
        "reason": "Physical hardware/clean-system evidence remains pending and is not a code/build failure.",
    }

    # Separate concrete build/validation failures from expected missing physical evidence.
    # CI must fail only on concrete failures; missing RTX hardware or clean-system evidence
    # remains an explicit non-passing (exit 2) baseline gate rather than being reported green.
    concrete_failure_checks = (
        "schemas",
        "dependency_closures",
        "reproducibility_build",
        "msvc_ltcg_build",
        "clang_debug_symbols",
        "msvc_debug_symbols",
        "fixture_validation",
        "msvc_fixture_validation",
        "failure_recovery",
        "clang_ledger_recording",
        "msvc_ledger_recording",
    )
    concrete_failures: list[str] = []
    for check_name in concrete_failure_checks:
        check = report["checks"].get(check_name, {})
        if check.get("passed") is False and not check.get("skipped", False):
            concrete_failures.append(check_name)
    if report["checks"].get("size_ledgers", {}).get("contracts_valid") is False:
        concrete_failures.append("size_ledgers_contracts")
    if (
        report["checks"].get("offline_shader", {}).get("hlsl_constraints_passed") is False
    ):
        concrete_failures.append("offline_shader_constraints")
    elif (
        report["checks"].get("offline_shader", {}).get("passed") is False
        and dxc_bin is not None
        and (clang_prereqs_ready or msvc_prereqs_ready)
    ):
        concrete_failures.append("offline_shader_evidence")
    report["concrete_failures"] = concrete_failures
    report["evidence_gaps"] = evidence["unmet_requirements"]
    report["gate_status"] = (
        "failed"
        if concrete_failures
        else (
            "passed"
            if (
                not report["missing_prerequisites"]
                and evidence["complete"]
                and shader_ok
                and fixture_validation_passed
                and msvc_fixture_validation_passed
            )
            else "evidence_incomplete"
        )
    )

    # Full baseline passes only when every prerequisite and measured evidence item is verified.
    if (
        not report["missing_prerequisites"]
        and evidence["complete"]
        and shader_ok
        and fixture_validation_passed
        and report["checks"]["schemas"].get("passed") is True
        and report["checks"]["dependency_closures"].get("passed") is True
        and report["checks"]["size_ledgers"].get("passed") is True
        and report["checks"]["size_ledgers"].get("comparison_verified") is True
        and report["checks"]["reproducibility_build"].get("passed") is True
        and report["checks"].get("msvc_ltcg_build", {}).get("passed") is True
        and report["checks"].get("fixture_validation", {}).get("passed") is True
        and report["checks"].get("msvc_fixture_validation", {}).get("passed") is True
                and report["checks"].get("failure_recovery", {}).get("passed") is True
        and report["checks"].get("clang_debug_symbols", {}).get("passed") is True
        and report["checks"].get("msvc_debug_symbols", {}).get("passed") is True
    ):
        report["full_baseline_passed"] = True
        report["gate_status"] = "passed"
        return 0, report

    if concrete_failures:
        report["gate_status"] = "failed"
        return 1, report

    # Distinguish "concrete checks passed, physical evidence still missing" (2) from
    # "the required build/toolchain coverage was skipped or unavailable" (3). CI must
    # never treat missing build coverage as an acceptable evidence-only gap.
    build_coverage_checks = (
        "offline_shader",
        "reproducibility_build",
        "msvc_ltcg_build",
        "clang_debug_symbols",
        "msvc_debug_symbols",
        "failure_recovery",
    )
    missing_build_coverage = [
        name
        for name in build_coverage_checks
        if report["checks"].get(name, {}).get("passed") is not True
    ]
    report["missing_build_coverage"] = missing_build_coverage
    if missing_build_coverage:
        report["gate_status"] = "build_coverage_missing"
        return 3, report

    report["gate_status"] = "evidence_incomplete"
    return 2, report


def run_clean_system_mode(
    repo_root: Path,
    binary_dir: Path | None = None,
    *,
    evidence_dir: Path | None = None,
    allow_non_windows_simulation: bool = False,
    env: dict[str, str] | None = None,
    system32_dir: Path | None = None,
    system_environment: dict[str, str] | None = None,
    executor: Callable[[Path, str], int | tuple[int, str]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute Mode=CleanSystem validation on a target machine without developer SDKs and record the report."""
    clean_report_path = repo_root / "benchmarks" / "clean-system" / "clean-system-validation.json"
    clean_schema_path = repo_root / "schemas" / "clean-system-report.schema.json"
    inventory_path = repo_root / "benchmarks" / "dependency-inventory.v1.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))

    validate_file_against_schema(clean_report_path, clean_schema_path)
    existing_report = json.loads(clean_report_path.read_text(encoding="utf-8"))

    is_windows = (platform.system().lower() == "windows") or allow_non_windows_simulation
    default_build_dir = repo_root / "build" / "clang-cl-release"
    requested_dir = binary_dir or default_build_dir
    search_dir = (
        requested_dir / "deploy" if (requested_dir / "deploy").is_dir() else requested_dir
    )
    resolved_evidence_dir = evidence_dir
    if resolved_evidence_dir is None:
        bundled_evidence_dir = requested_dir / "evidence"
        if bundled_evidence_dir.is_dir():
            resolved_evidence_dir = bundled_evidence_dir
    required_exes = [
        ("empty_player", "nw_empty_player.exe"),
        ("clear_present", "nw_clear_present_fixture.exe"),
        ("compute_shader", "nw_compute_shader_fixture.exe"),
    ]

    missing_prereqs: list[str] = []
    if not is_windows:
        missing_prereqs.append("CleanSystem execution requires a physical Windows 11 x64 host")

    expected_exe_paths: dict[str, Path] = {}
    for fixture_id, exe_name in required_exes:
        exe_path = resolve_fixture_executable(search_dir, fixture_id, exe_name)
        if exe_path is None:
            missing_prereqs.append(f"Release fixture executable not found under {search_dir}")
        else:
            expected_exe_paths[fixture_id] = exe_path

    if missing_prereqs:
        return 2, {
            "mode": "CleanSystem",
            "clean_system_passed": False,
            "verification_status": existing_report["verification_status"],
            "missing_prerequisites": missing_prereqs,
            "recorded_report_valid": True,
        }

    if resolved_evidence_dir is not None:
        required_evidence = (
            resolved_evidence_dir / "artifact-manifest.json",
            resolved_evidence_dir / "build-checks.json",
            resolved_evidence_dir / "shaders-manifest.json",
            resolved_evidence_dir / "toolchain-manifest.v1.json",
            resolved_evidence_dir / "ledgers",
        )
        missing_evidence = [str(path) for path in required_evidence if not path.exists()]
        if missing_evidence:
            return 2, {
                "mode": "CleanSystem",
                "clean_system_passed": False,
                "verification_status": existing_report["verification_status"],
                "missing_prerequisites": [
                    "CI evidence bundle is incomplete: " + ", ".join(missing_evidence)
                ],
                "recorded_report_valid": True,
            }

    expected_compute = load_compute_expected_output(repo_root)
    shader_evidence = check_shader_manifest_evidence(repo_root, resolved_evidence_dir)

    # 1. Actively probe SDK and redistributable absence on the target system
    sdk_checks = probe_sdk_absence(search_dir, env=env, system32_dir=system32_dir)
    all_sdk_absent = all(sdk_checks.values())

    # 2. Inventory the complete deployment tree (recursive, every file) and verify the
    # dependency closure of each fixture. Known fixture executables are expected siblings,
    # not sidecars.
    sidecars = collect_deployment_files(
        search_dir,
        exclude_paths=set(expected_exe_paths.values()),
    )

    fixture_specs: dict[str, dict[str, Any]] = {}
    for fx_path in (repo_root / "fixtures").glob("*/fixture.json"):
        fx_data = json.loads(fx_path.read_text(encoding="utf-8"))
        fixture_specs[fx_data["fixture_id"]] = fx_data

    deployed_files: list[dict[str, Any]] = []
    resolved_inbox_dlls: set[str] = set()
    fixture_runs: list[dict[str, Any]] = []
    fixture_stdout: dict[str, str] = {}
    all_runs_passed = True
    compute_readback: dict[str, Any] | None = None
    compute_stdout = ""
    compute_stderr = ""
    embedded_dxil_verified = False
    closure_failures: list[str] = []
    observed_modules: list[str] = []

    for fixture_id, exe_name in required_exes:
        exe_path = expected_exe_paths.get(fixture_id)
        if exe_path is None:
            all_runs_passed = False
            fixture_runs.append(
                {
                    "fixture_id": fixture_id,
                    "executable": exe_name,
                    "execution_status": "failed",
                    "exit_code": None,
                    "failure_reason": "release executable missing",
                }
            )
            continue
        raw_bytes = exe_path.read_bytes()
        pe_info = parse_pe64_sections_and_imports(raw_bytes)
        if fixture_id == "compute_shader":
            embedded = find_embedded_dxil_container(raw_bytes)
            if embedded is not None:
                embedded_sha256 = hashlib.sha256(embedded).hexdigest()
                embedded_dxil_verified = (
                    shader_evidence["offline_dxil_sha256"] == embedded_sha256
                    and shader_evidence["offline_dxil_bytes"] == len(embedded)
                )
        expected_imports = fixture_specs.get(fixture_id, {}).get("expected_release_imports")
        try:
            verify_closure(
                profile="release",
                fixture_id=fixture_id,
                imported_dlls=pe_info["imports"],
                delay_imported_dlls=pe_info.get("delay_imports", []),
                sidecar_files=sidecars,
                inventory=inventory,
                expected_fixture_release_imports=expected_imports,
            )
        except DependencyClosureError as exc:
            closure_failures.append(f"{fixture_id}: {exc}")
        for dll in [*pe_info["imports"], *pe_info.get("delay_imports", [])]:
            resolved_inbox_dlls.add(dll.lower())

        deployed_files.append(
            {
                "file_name": exe_path.relative_to(search_dir).as_posix(),
                "size_bytes": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "category": "executable",
            }
        )

        run_args = [
            str(exe_path),
            "--no-validation",
            "--emit-benchmark-metrics",
            "--emit-loaded-modules",
        ]
        if fixture_id == "compute_shader":
            run_args.append("--emit-readback-values")
        stdout = ""
        stderr = ""
        if executor is not None:
            result = executor(exe_path, fixture_id)
            if isinstance(result, tuple):
                ret_code, stdout = int(result[0]), str(result[1])
            else:
                ret_code = int(result)
        else:
            proc = subprocess.run(run_args, capture_output=True, text=True)
            ret_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        fixture_stdout[fixture_id] = stdout

        passed = ret_code == 0
        validation = parse_validation_evidence(stdout, stderr)
        modules = parse_loaded_modules(stdout)
        for module in modules:
            if module not in observed_modules:
                observed_modules.append(module)
        if fixture_id == "compute_shader":
            compute_stdout = stdout
            compute_stderr = stderr
            compute_readback = parse_observed_compute_readback(stdout, expected_compute)
            passed = passed and compute_readback["elements_match"] and compute_readback.get("hash_match") is True
        if not passed:
            all_runs_passed = False

        run_record: dict[str, Any] = {
            "fixture_id": fixture_id,
            "executable": exe_name,
            "execution_status": "passed" if passed else "failed",
            "exit_code": ret_code,
            "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest() if stdout else None,
            "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest() if stderr else None,
            "stderr_tail": stderr.strip().splitlines()[-1][:500] if stderr.strip() else None,
            "d3d12_validation_clean": validation["d3d12_validation_clean"],
            "d3d12_validation_errors": validation["d3d12_validation_errors"],
            "d3d12_validation_messages": validation["d3d12_validation_messages"],
            "observed_loaded_modules": modules,
            "metrics": parse_fixture_metrics(stdout),
        }
        if fixture_id == "compute_shader":
            run_record["readback"] = compute_readback
        fixture_runs.append(run_record)

    for relative_name in sidecars:
        sidecar_path = search_dir / relative_name
        suffix = sidecar_path.suffix.lower()
        if suffix == ".dll":
            category = "dll"
        elif suffix in {".dxil", ".cso"}:
            category = "shader_payload"
        elif suffix in {".nwpkg", ".nwk", ".pak", ".pack", ".dat", ".bin"}:
            category = "data_payload"
        elif suffix in {".model", ".mesh"}:
            category = "model"
        else:
            category = "sidecar"
        raw = sidecar_path.read_bytes()
        deployed_files.append(
            {
                "file_name": relative_name,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "category": category,
            }
        )

    detected_sys_env = detect_host_system_environment(compute_stdout)
    override_env = system_environment or {}
    adapter_evidence = reconcile_fixture_adapter_descriptions(
        [
            {"fixture_id": fixture_id, "stdout": stdout}
            for fixture_id, stdout in fixture_stdout.items()
        ]
    )
    observed_adapter = adapter_evidence["executing_adapter"]
    override_gpu_model = override_env.get("gpu_model")
    detected_gpu_model = detected_sys_env.get("gpu_model")
    detected_driver = detected_sys_env.get("nvidia_driver_version")
    adapter_reconciliation_failures: list[str] = []
    if not observed_adapter:
        adapter_reconciliation_failures.append(
            "executing fixtures did not report an adapter description"
        )
    elif not adapter_evidence["consistent"]:
        adapter_reconciliation_failures.append(
            "raster and compute fixtures did not report the same executing adapter"
        )
    if (
        observed_adapter
        and override_gpu_model
        and str(override_gpu_model).strip().lower()
        != str(observed_adapter).strip().lower()
    ):
        adapter_reconciliation_failures.append(
            "operator-provided GPU model does not match the adapter reported by the executing fixtures"
        )
    if (
        observed_adapter
        and detected_gpu_model
        and str(detected_gpu_model).strip().lower()
        != str(observed_adapter).strip().lower()
    ):
        adapter_reconciliation_failures.append(
            "host-registry GPU model does not match the adapter reported by the executing fixtures"
        )
        detected_gpu_model = None
        detected_driver = None
    adapter_bound = bool(
        observed_adapter
        and adapter_evidence["consistent"]
        and not adapter_reconciliation_failures
    )
    measured_os_build = override_env.get("os_build") or detected_sys_env.get("os_build")
    measured_gpu_model = (
        observed_adapter or override_gpu_model or detected_gpu_model
    )
    measured_driver_ver = (
        (override_env.get("nvidia_driver_version") or detected_driver)
        if adapter_bound
        else None
    )
    system_env_measured = bool(
        measured_os_build
        and re.match(r"^10\.0\.(22[0-9]{3}|26[0-9]{3})\.[0-9]+$", str(measured_os_build))
        and observed_adapter
        and adapter_evidence["consistent"]
        and not adapter_reconciliation_failures
        and _is_supported_nvidia_gpu_and_driver(
            str(measured_gpu_model) if measured_gpu_model else None,
            str(measured_driver_ver) if measured_driver_ver else None,
        )
    )

    total_distributed_bytes = sum(f["size_bytes"] for f in deployed_files)
    artifact_provenance = resolve_artifact_provenance(
        repo_root, search_dir, resolved_evidence_dir
    )
    provenance_verified = bool(
        artifact_provenance.get("toolchain_id")
        and artifact_provenance.get("source") == "recorded_ledger_artifact_hash"
    )
    compute_output_verified = bool(
        compute_readback
        and compute_readback.get("elements_match") is True
        and compute_readback.get("hash_match") is True
    )
    all_validation_clean = all(
        run.get("d3d12_validation_clean") is True for run in fixture_runs
    )
    static_import_inference = {name.lower() for name in resolved_inbox_dlls}
    static_import_inference.update(name.lower() for name in sidecars)
    if observed_modules:
        runtime_module_evidence_source = "observed_fixture_module_inventory"
        module_classification = classify_loaded_module_evidence(observed_modules)
        runtime_dxcompiler_loaded = module_classification["runtime_dxcompiler_loaded"]
        runtime_d3dcompiler_loaded = module_classification["runtime_d3dcompiler_loaded"]
        agility_redist_loaded = module_classification["agility_d3d12core_redist_loaded"]
        d3d12core_classification_unknown = module_classification[
            "d3d12core_classification_unknown"
        ]
    else:
        runtime_module_evidence_source = "static_import_and_sidecar_inference"
        runtime_dxcompiler_loaded = any(
            "dxcompiler" in name or "dxil.dll" in name for name in static_import_inference
        )
        runtime_d3dcompiler_loaded = any(
            "d3dcompiler" in name for name in static_import_inference
        )
        agility_redist_loaded = any("d3d12core" in name for name in static_import_inference)
        d3d12core_classification_unknown = False
    clean_passed = (
        all_runs_passed
        and all_validation_clean
        and all_sdk_absent
        and (len(sidecars) == 0)
        and not closure_failures
        and shader_evidence["verified"]
        and embedded_dxil_verified
        and compute_output_verified
        and provenance_verified
        and system_env_measured
        and not runtime_dxcompiler_loaded
        and not runtime_d3dcompiler_loaded
        and not agility_redist_loaded
        and not d3d12core_classification_unknown
    )
    expected_sha256 = expected_compute["output_sha256"]
    if (
        measured_os_build
        or measured_gpu_model
        or measured_driver_ver
        or adapter_reconciliation_failures
    ):
        recorded_system_environment = {
            "os_name": "Windows 11 x64",
            "os_build": (
                measured_os_build
                or existing_report["system_environment"].get("os_build")
                or None
            ),
            "architecture": "x86_64",
            "gpu_model": str(
                measured_gpu_model or "unverified"
            ),
            "nvidia_driver_version": str(
                measured_driver_ver or "unverified"
            ),
        }
    else:
        recorded_system_environment = existing_report["system_environment"]

    updated_report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "report_id": existing_report["report_id"],
        "verification_status": "verified" if clean_passed else "unverified",
        "clean_system_passed": clean_passed,
        "system_environment": recorded_system_environment,
        "sdk_absence_checks": sdk_checks,
        "deployed_dependency_inventory": {
            "deployed_files": deployed_files,
            "total_distributed_bytes": total_distributed_bytes,
            "inbox_dlls_resolved": sorted(resolved_inbox_dlls),
            "unexpected_sidecars_found": sidecars,
        },
        "offline_shader_execution_evidence": {
            "shader_id": "compute_fixture_cs",
            "shader_model": "cs_6_0",
            "compiled_offline_with_dxc": bool(shader_evidence["verified"]),
            "runtime_dxcompiler_loaded": runtime_dxcompiler_loaded,
            "runtime_d3dcompiler_loaded": runtime_d3dcompiler_loaded,
            "agility_d3d12core_redist_loaded": agility_redist_loaded,
            "runtime_module_evidence_source": runtime_module_evidence_source,
            "d3d12core_classification_unknown": d3d12core_classification_unknown,
            "inbox_d3d12core_observed": (
                module_classification.get("inbox_d3d12core_observed", False)
                if observed_modules
                else False
            ),
            "observed_loaded_modules": observed_modules,
            "expected_output_sha256": expected_sha256,
            "observed_output_sha256": (
                compute_readback["observed_output_sha256"] if compute_readback else None
            ),
            "output_verified": compute_output_verified,
        },
        "adapter_execution_evidence": {
            "executing_adapter": observed_adapter,
            "consistent": adapter_evidence["consistent"],
            "fixture_descriptions": adapter_evidence["descriptions"],
        },
        "artifact_provenance": artifact_provenance,
        "fixture_runs": fixture_runs,
        "unavailable_measurements": (
            []
            if clean_passed
            else existing_report.get("unavailable_measurements", [])
        ),
    }
    if not clean_passed:
        reasons = []
        if not all_runs_passed:
            reasons.append("one or more fixtures failed")
        if not all_validation_clean:
            reasons.append("D3D12 info queue reported error/corruption messages")
        if not all_sdk_absent:
            reasons.append("developer SDK/redistributable absence checks failed")
        if sidecars:
            reasons.append("unexpected sidecar files were present")
        if closure_failures:
            reasons.append("deployment dependency closure failed: " + " | ".join(closure_failures))
        if not shader_evidence["verified"]:
            reasons.append("offline DXC shader evidence in shaders/manifest.json is unverified")
        if not embedded_dxil_verified:
            reasons.append(
                "deployed compute executable does not embed the validated, manifest-matched DXIL container"
            )
        if not compute_output_verified:
            reasons.append("observed compute readback did not match the committed expected bytes")
        if not provenance_verified:
            reasons.append(
                "executed artifacts could not be bound to the selected toolchain's verified "
                "release ledgers for all three fixtures (unverified ledgers are not accepted)"
            )
        if not system_env_measured:
            reasons.append(
                "target system_environment (os_build, gpu_model, nvidia_driver_version) could not be "
                "reconciled with the adapter reported by the executing fixtures: "
                + (
                    "; ".join(adapter_reconciliation_failures)
                    if adapter_reconciliation_failures
                    else "missing or unsupported measured fields"
                )
            )
        if d3d12core_classification_unknown:
            reasons.append(
                "observed D3D12Core.dll module path was unavailable, so inbox versus Agility "
                "redistributable classification could not be determined"
            )
        if runtime_dxcompiler_loaded:
            reasons.append("observed runtime DXC/DXIL module in the executing fixture process")
        if runtime_d3dcompiler_loaded:
            reasons.append("observed d3dcompiler runtime module in the executing fixture process")
        if agility_redist_loaded:
            reasons.append("observed app-local D3D12Core.dll Agility redistributable module")
        updated_report["unverified_reason"] = (
            "CleanSystem validation failed: " + "; ".join(reasons) if reasons else
            "CleanSystem validation failed; see command summary."
        )

    clean_report_path.write_text(
        json.dumps(updated_report, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    validate_file_against_schema(clean_report_path, clean_schema_path)

    return (0 if clean_passed else 1), {
        "mode": "CleanSystem",
        "clean_system_passed": clean_passed,
        "verification_status": updated_report["verification_status"],
        "system_environment_measured": system_env_measured,
        "system_environment": recorded_system_environment,
        "sdk_absence_checks": sdk_checks,
        "embedded_dxil_verified": embedded_dxil_verified,
        "artifact_provenance": artifact_provenance,
        "dependency_closure_failures": closure_failures,
        "total_distributed_bytes": total_distributed_bytes,
        "fixture_runs": fixture_runs,
        "recorded_to": "benchmarks/clean-system/clean-system-validation.json",
    }


def run_build_checks_mode(repo_root: Path) -> tuple[int, dict[str, Any]]:
    """Execute concrete build/schema/dependency checks without requiring target GPU hardware.

    This mode exists for GPU-less CI builders. It performs the same build, two-clean-directory
    reproducibility, MSVC/LTCG comparison, CodeView/PDB, schema, dependency, size-ledger and
    executable failure-injection checks as Full, but explicitly skips physical fixture
    execution and physical-evidence certification. It never claims a passing full baseline.
    """
    _code, report = run_full_mode(repo_root, gpu_fixtures_available=False)
    report["mode"] = "BuildChecks"
    concrete_failures = report.get("concrete_failures", [])
    required_check_names = (
        "schemas",
        "dependency_closures",
        "offline_shader",
        "reproducibility_build",
        "msvc_ltcg_build",
        "clang_debug_symbols",
        "msvc_debug_symbols",
        "failure_recovery",
    )
    missing_required_checks = [
        name
        for name in required_check_names
        if report["checks"].get(name, {}).get("passed") is not True
    ]
    size_ledgers = report["checks"].get("size_ledgers", {})
    if size_ledgers.get("contracts_valid") is not True:
        missing_required_checks.append("size_ledgers.contracts_valid")
    report["required_check_names"] = list(required_check_names)
    report["missing_required_checks"] = missing_required_checks
    build_checks_passed = not concrete_failures and not missing_required_checks
    report["build_checks_passed"] = build_checks_passed
    report["full_baseline_passed"] = False
    report["gate_status"] = "build_checks_passed" if build_checks_passed else "failed"
    return (0 if build_checks_passed else 1), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nodewerk NW-01 verification entry point.")
    parser.add_argument(
        "--mode",
        choices=["Full", "CleanSystem", "ContractsOnly", "BuildChecks"],
        default="Full",
        help="Verification mode: Full, CleanSystem, ContractsOnly, or BuildChecks",
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--binary-dir", type=Path, default=None)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="Optional CI evidence directory; inferred as <binary-dir>/evidence for packaged CleanSystem artifacts",
    )
    parser.add_argument("--os-build", type=str, default=None, help="Measured Windows 11 OS build (e.g. 10.0.26100.2033)")
    parser.add_argument("--gpu-model", type=str, default=None, help="Measured GPU model name")
    parser.add_argument("--nvidia-driver-version", type=str, default=None, help="Measured NVIDIA driver version")
    parser.add_argument("--cpu-model", type=str, default=None, help="Measured CPU model")
    parser.add_argument("--gpu-architecture", type=str, default=None, help="Measured GPU architecture")
    parser.add_argument("--wddm-driver-version", type=str, default=None, help="Measured WDDM driver version")
    parser.add_argument("--windows-power-plan", type=str, default=None, help="Measured Windows power plan")
    parser.add_argument("--nvidia-power-management-mode", type=str, default=None, help="Measured NVIDIA power management mode")
    parser.add_argument("--pcie-power-management", type=str, default=None, help="Measured PCIe link-state power management")
    parser.add_argument("--refresh-hz", type=int, default=None, help="Measured swapchain refresh rate")
    parser.add_argument("--driver-shader-cache-state", choices=["cold_cleared", "warm_primed"], default=None)
    parser.add_argument("--os-file-cache-state", choices=["cold", "warm"], default=None)
    parser.add_argument(
        "--record-benchmark",
        choices=sorted(BENCHMARK_CAPTURE_FILES),
        default=None,
        help="Execute fixtures and record measured benchmark/hardware evidence for the selected tier",
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        default=None,
        help="Write the verification summary JSON to this path (used by CI to separate logs from results)",
    )
    args = parser.parse_args(argv)

    try:
        sys_env: dict[str, str] = {}
        if args.os_build:
            sys_env["os_build"] = args.os_build
        if args.gpu_model:
            sys_env["gpu_model"] = args.gpu_model
        if args.nvidia_driver_version:
            sys_env["nvidia_driver_version"] = args.nvidia_driver_version
        if args.cpu_model:
            sys_env["cpu_model"] = args.cpu_model
        if args.gpu_architecture:
            sys_env["gpu_architecture"] = args.gpu_architecture
        if args.wddm_driver_version:
            sys_env["wddm_driver_version"] = args.wddm_driver_version
        if args.windows_power_plan:
            sys_env["windows_power_plan"] = args.windows_power_plan
        if args.nvidia_power_management_mode:
            sys_env["nvidia_power_management_mode"] = args.nvidia_power_management_mode
        if args.pcie_power_management:
            sys_env["pci_express_link_state_power_management"] = args.pcie_power_management
        if args.refresh_hz is not None:
            sys_env["refresh_hz"] = str(args.refresh_hz)
        if args.driver_shader_cache_state:
            sys_env["driver_shader_cache_state"] = args.driver_shader_cache_state
        if args.os_file_cache_state:
            sys_env["os_file_cache_state"] = args.os_file_cache_state
        benchmark_result = None
        if args.record_benchmark is not None:
            benchmark_binary_dir = args.binary_dir or (
                args.repo_root / "build" / "clang-cl-release" / "deploy"
            )
            benchmark_evidence_dir = args.evidence_dir
            if benchmark_evidence_dir is None:
                package_evidence_dir = benchmark_binary_dir / "evidence"
                sibling_evidence_dir = benchmark_binary_dir.parent / "evidence"
                if package_evidence_dir.is_dir():
                    benchmark_evidence_dir = package_evidence_dir
                elif (
                    benchmark_binary_dir.name.lower() == "deploy"
                    and sibling_evidence_dir.is_dir()
                ):
                    benchmark_evidence_dir = sibling_evidence_dir
            benchmark_result = capture_benchmark_result(
                args.repo_root,
                benchmark_binary_dir,
                hardware_tier=args.record_benchmark,
                system_environment=sys_env or None,
                evidence_dir=benchmark_evidence_dir,
            )

        if args.mode == "CleanSystem":
            code, summary = run_clean_system_mode(
                args.repo_root,
                args.binary_dir,
                evidence_dir=args.evidence_dir,
                system_environment=sys_env or None,
            )
        elif args.mode == "BuildChecks":
            code, summary = run_build_checks_mode(args.repo_root)
        elif args.mode == "ContractsOnly":
            schema_res = validate_all_repository_artifacts(args.repo_root)
            dep_res = verify_all_recorded_ledgers(args.repo_root)
            ledger_res = generate_size_comparison_report(args.repo_root)
            code = 0
            summary = {
                "mode": "ContractsOnly",
                "schemas_validated": schema_res["validated_count"],
                "ledgers_verified": len(dep_res),
                "selected_toolchain": ledger_res["selected_toolchain"],
            }
        else:
            code, summary = run_full_mode(args.repo_root)

        if benchmark_result is not None:
            summary = dict(summary)
            summary["benchmark_capture"] = benchmark_result
            if not benchmark_result.get("benchmark_passed", False):
                capture_execution_failed = any(
                    run.get("execution_status") == "failed"
                    for run in benchmark_result.get("fixture_results", [])
                )
                capture_code = 1 if capture_execution_failed else 2
                if code == 0:
                    code = capture_code
                elif capture_code == 1:
                    code = 1

        print(json.dumps(summary, indent=2))
        if args.summary_json is not None:
            args.summary_json.parent.mkdir(parents=True, exist_ok=True)
            args.summary_json.write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8"
            )
        return code
    except Exception as exc:
        print(f"[NW-VERIFY-ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
