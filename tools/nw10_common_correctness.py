#!/usr/bin/env python3
"""NW-10 representative-hardware common-artifact correctness capture.

This tool deliberately does not claim NW-07 release-capture certification.
NW-10 acceptance requires common-artifact correctness coverage on representative
desktop Ada and Blackwell.  The diagnostic sibling is a separate executable
with capture controls enabled.  Its canonical package/profile identity must
match the final common artifact exactly; its diagnostic-only link manifest and
dependency closure are intentionally separate from the frozen release EXE.

Raw RGBA32F files stay outside tracked evidence.  A later cross-hardware
comparison can compare Blackwell captures against the retained Ada reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw05_export  # noqa: E402
import nw07_contract  # noqa: E402
import nw07_evidence  # noqa: E402
import nw07_export  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw10_compact as compact  # noqa: E402
import nw10_contract as contract  # noqa: E402
import nw10_intro  # noqa: E402

FORMAT = "nw10-common-artifact-correctness-capture"
VERSION = 1
PACKAGING_MODES = ("unpacked", "packed")
DIAGNOSTIC_SOURCES = ("nw07", "nw10_intro")
ALL_TICKS = tuple(nw07_contract.REQUIRED_FIXED_TICKS) + (
    nw07_contract.LOOP_BOUNDARY_TICKS[1],
)
EXPECTED_RGBA32F_BYTES = (
    nw07_contract.OUTPUT_WIDTH
    * nw07_contract.OUTPUT_HEIGHT
    * nw07_contract.CAPTURE_BYTES_PER_PIXEL
)

_DESKTOP_CHASSIS_TYPES = {3, 4, 5, 6, 7, 15, 16, 35, 36}
_LAPTOP_CHASSIS_TYPES = {8, 9, 10, 11, 14, 30, 31, 32}


def _windows_device_form_factor_probe() -> dict[str, Any]:
    """Measure chassis form factor instead of asserting desktop coverage."""
    if platform.system() != "Windows":
        return {
            "device_form_factor": "unknown",
            "device_form_factor_source": "unsupported_host",
            "chassis_types": [],
            "battery_present": None,
        }
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$e=Get-CimInstance Win32_SystemEnclosure;"
            "$bt=$null;try{$bt=Get-CimInstance Win32_Battery -ErrorAction Stop}catch{};"
            "[pscustomobject]@{ChassisTypes=@($e.ChassisTypes);"
            "BatteryPresent=($null -ne $bt)}|ConvertTo-Json -Compress"
        ),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "device_form_factor": "unknown",
            "device_form_factor_source": f"probe_error:{exc}",
            "chassis_types": [],
            "battery_present": None,
        }
    if completed.returncode != 0:
        return {
            "device_form_factor": "unknown",
            "device_form_factor_source": "probe_failed",
            "chassis_types": [],
            "battery_present": None,
        }
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "device_form_factor": "unknown",
            "device_form_factor_source": "probe_malformed",
            "chassis_types": [],
            "battery_present": None,
        }
    raw_types = value.get("ChassisTypes") or []
    if not isinstance(raw_types, list):
        raw_types = [raw_types]
    chassis_types: list[int] = []
    for item in raw_types:
        try:
            chassis_types.append(int(item))
        except (TypeError, ValueError):
            continue
    battery = value.get("BatteryPresent")
    if chassis_types and all(t in _DESKTOP_CHASSIS_TYPES for t in chassis_types):
        factor = "desktop"
    elif battery is True or any(t in _LAPTOP_CHASSIS_TYPES for t in chassis_types):
        factor = "laptop"
    else:
        factor = "unknown"
    return {
        "device_form_factor": factor,
        "device_form_factor_source": (
            "win32_system_enclosure_and_battery"
            if chassis_types
            else "no_chassis_measurement"
        ),
        "chassis_types": chassis_types,
        "battery_present": battery,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 else ""


def _identity(path: Path) -> dict[str, Any]:
    return nw07_export.inspect_artifact_directory(path)


def _diagnostic_identity(
    path: Path, *, packaging_mode: str = "unpacked"
) -> dict[str, Any]:
    """Inspect a dev diagnostic sibling without applying release-only rules.

    NW-07's inspect_artifact_directory intentionally expects the frozen release
    executable to embed only the SHA-256 binding of its published manifest.
    A capture-enabled dev sibling instead embeds its complete diagnostic link
    manifest and has a different dependency closure (debug CRT/PDB).  That
    difference is expected and must not be mistaken for a common-package
    mismatch.
    """
    if packaging_mode not in PACKAGING_MODES:
        raise ValueError(
            f"diagnostic packaging mode must be one of {PACKAGING_MODES}"
        )
    stem = nw05_export._profile_file_stem(nw07_contract.PROFILE_ID)
    package_path = path / f"{stem}.nwp"
    manifest_path = path / f"{stem}.manifest.json"
    executable_path = path / f"{stem}.exe"
    for label, candidate in (
        ("package", package_path),
        ("manifest", manifest_path),
        ("executable", executable_path),
    ):
        if not candidate.is_file():
            raise ValueError(f"diagnostic sibling {label} is unavailable: {candidate}")

    package_bytes = package_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    executable_bytes = executable_path.read_bytes()
    inspection = nwp.inspect_package_bytes(package_bytes, allow_dev=False)
    if not inspection.valid or inspection.package is None:
        raise ValueError("diagnostic sibling package is not a valid release package")
    package = inspection.package
    if package.capability.profile_id != nw07_contract.PROFILE_ID:
        raise ValueError("diagnostic sibling changed the common profile")
    if package.capability.requirements_digest != nw07_contract.REQUIREMENTS_DIGEST:
        raise ValueError("diagnostic sibling changed the requirements digest")

    embedded_payload, embedded_manifest = nw05_export.extract_embedded_package(
        executable_bytes
    )
    encoded_sha256: str | None = None
    encoded_bytes: int | None = None
    codec_id: str | None = None
    layout_id: str | None = None
    if packaging_mode == "unpacked":
        if embedded_payload != package_bytes:
            raise ValueError(
                "diagnostic executable does not embed the published package"
            )
    else:
        if not compact.is_compact_container(embedded_payload):
            raise ValueError(
                "packed diagnostic executable does not embed a compact container"
            )
        decoded = compact.decode_compact(embedded_payload)
        if decoded != package_bytes:
            raise ValueError(
                "packed diagnostic executable does not decode to the common package"
            )
        encoded_sha256 = hashlib.sha256(embedded_payload).hexdigest()
        encoded_bytes = len(embedded_payload)
        compact_header = compact.parse_header(embedded_payload)
        codec_id = str(compact_header["codec_id"])
        layout_id = str(compact_header["layout_id"])
    if embedded_manifest != manifest_bytes:
        raise ValueError(
            "diagnostic executable manifest binding does not match its published "
            "diagnostic manifest"
        )

    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("profile_id") != nw07_contract.PROFILE_ID:
        raise ValueError("diagnostic manifest changed the common profile")
    if manifest.get("requirements_digest") != nw07_contract.REQUIREMENTS_DIGEST:
        raise ValueError("diagnostic manifest changed the requirements digest")
    declared_manifest_sha = str(manifest.get("manifest_sha256", ""))
    manifest_without_digest = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    computed_manifest_sha = "sha256:v1:" + hashlib.sha256(
        nwp.canonical_json_bytes(manifest_without_digest)
    ).hexdigest()
    if declared_manifest_sha != computed_manifest_sha:
        raise ValueError("diagnostic manifest self-digest is invalid")

    declared_dependencies = sorted(
        str(item).lower()
        for item in manifest.get("retained", {}).get("dependencies", [])
    )
    actual_dependencies = sorted(
        str(item).lower()
        for item in nw05_export._pe_runtime_dependencies(executable_bytes)
    )
    if declared_dependencies != actual_dependencies:
        raise ValueError(
            "diagnostic manifest dependency closure does not match the executable"
        )

    return {
        "artifact_key": str(manifest.get("artifact_identity", "")),
        "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_declared_sha256": declared_manifest_sha,
        "executable_sha256": hashlib.sha256(executable_bytes).hexdigest(),
        "executable_bytes": len(executable_bytes),
        "profile_id": package.capability.profile_id,
        "requirements_digest": package.capability.requirements_digest,
        "dependencies": actual_dependencies,
        "packaging_mode": packaging_mode,
        "encoded_container_sha256": encoded_sha256,
        "encoded_container_bytes": encoded_bytes,
        "codec_id": codec_id,
        "layout_id": layout_id,
    }


def _require_same_common_artifact(
    expected: Mapping[str, Any], observed: Mapping[str, Any]
) -> None:
    for field in (
        "artifact_key",
        "package_sha256",
        "profile_id",
        "requirements_digest",
    ):
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
    packaging_mode: str = "unpacked",
    codec_id: str = "auto",
    layout_id: str = "auto",
    source_kind: str = "nw07",
) -> dict[str, Any]:
    if packaging_mode not in PACKAGING_MODES:
        raise ValueError(
            f"diagnostic packaging mode must be one of {PACKAGING_MODES}"
        )
    if source_kind not in DIAGNOSTIC_SOURCES:
        raise ValueError(
            f"diagnostic source must be one of {DIAGNOSTIC_SOURCES}"
        )
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"diagnostic output directory is not empty: {output}")
    expected = _identity(expected_package)
    generated_shaders_override = None
    frozen_embedded_specialization = False
    if source_kind == "nw10_intro":
        graph_path = REPO_ROOT / contract.INTRO_GRAPH
        nw10_intro.validate_intro_graph(graph_path)
        document = nw03.load_graph(graph_path)
        generated_shaders_override = (
            nw10_intro._intro_generated_shader(document),
        )
        frozen_embedded_specialization = True
    else:
        config = nw07_export.load_canonical_config()
        pinned = nw07_export.verify_pinned_inputs(config)
        if pinned.get("matches_frozen_digest") is not True:
            raise ValueError("frozen NW-07 pinned build-input digest does not match")
        graph_path = REPO_ROOT / str(config["graph"])
    template_configuration = nw05_export._template_build_configuration(
        Path(player_template)
    )
    template_compact = str(
        template_configuration.get("NW10_COMPACT_PACKAGE", "")
    ).strip().upper() in {"ON", "1", "TRUE"}
    required_compact = packaging_mode == "packed"
    if template_compact is not required_compact:
        raise ValueError(
            "diagnostic packaging mode requires the template to be built with "
            f"-DNW10_COMPACT_PACKAGE={'ON' if required_compact else 'OFF'}"
        )

    settings = nw05_export.ExportSettings(
        byte_ceiling=64 * 1024 * 1024,
        memory_budget=nw07_contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
        target_profile=nw07_contract.PROFILE_ID,
        requirements_digest=nw07_contract.REQUIREMENTS_DIGEST,
        frames_in_flight=2,
        player_template=str(player_template),
        specialized_build_root=str(specialized_build_root),
        present_sync_interval=0,
        frozen_embedded_specialization=frozen_embedded_specialization,
        include_optional_artifacts=False,
        allow_optional_profiles=False,
    )
    export_kwargs: dict[str, Any] = {}
    if generated_shaders_override is not None:
        export_kwargs["generated_shaders_override"] = generated_shaders_override
    nw05_export.export_graph(
        graph_path,
        output,
        settings=settings,
        dxc_executable=dxc_executable,
        optimization="O2",
        **export_kwargs,
    )

    stem = nw05_export._profile_file_stem(nw07_contract.PROFILE_ID)
    package_path = output / f"{stem}.nwp"
    manifest_path = output / f"{stem}.manifest.json"
    executable = output / f"{stem}.exe"
    if not executable.is_file():
        raise ValueError("diagnostic sibling executable was not emitted")

    encoded: compact.CompactEncoding | None = None
    if packaging_mode == "packed":
        package_bytes = package_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        executable_bytes = executable.read_bytes()
        embedded_package, embedded_manifest = nw05_export.extract_embedded_package(
            executable_bytes
        )
        if embedded_package != package_bytes:
            raise ValueError(
                "packed diagnostic base executable does not embed the canonical package"
            )
        if embedded_manifest != manifest_bytes:
            raise ValueError(
                "packed diagnostic base executable manifest binding changed"
            )
        total = (
            len(package_bytes)
            + len(manifest_bytes)
            + nw05_export.EMBED_FOOTER_SIZE
        )
        template_prefix = executable_bytes[: len(executable_bytes) - total]
        resolved_layout = layout_id
        if resolved_layout == "auto":
            resolved_layout = compact.measure_layouts(package_bytes)["selected"]
        resolved_codec = codec_id
        if resolved_codec == "auto":
            resolved_codec = compact.select_player_codec(
                package_bytes,
                layout_id=resolved_layout,
                delta_references=True,
            )["selected_codec_id"]
        encoded = compact.encode_compact(
            package_bytes,
            codec_id=resolved_codec,
            layout_id=resolved_layout,
            delta_references=True,
        )
        rebuilt = nw05_export.embed_package(
            template_prefix, encoded.bytes, manifest_bytes
        )
        if compact.decode_compact(
            nw05_export.extract_embedded_package(rebuilt)[0]
        ) != package_bytes:
            raise ValueError("packed diagnostic sibling did not round-trip")
        executable.write_bytes(rebuilt)
        (output / f"{stem}.compact.nwc").write_bytes(encoded.bytes)

    observed = _diagnostic_identity(output, packaging_mode=packaging_mode)
    _require_same_common_artifact(expected, observed)
    if observed["executable_sha256"] == expected["executable_sha256"]:
        raise ValueError("diagnostic sibling unexpectedly equals final executable")

    return {
        "format": "nw10-common-artifact-diagnostic-build",
        "version": 1,
        "status": "built",
        "common_artifact": {
            "artifact_key": expected["artifact_key"],
            "package_sha256": expected["package_sha256"],
            "manifest_sha256": expected["manifest_sha256"],
            "profile_id": expected["profile_id"],
            "requirements_digest": expected["requirements_digest"],
        },
        "final_executable_sha256": expected["executable_sha256"],
        "diagnostic_executable_sha256": observed["executable_sha256"],
        "diagnostic_executable_bytes": observed["executable_bytes"],
        "diagnostic_manifest_sha256": observed["manifest_sha256"],
        "diagnostic_dependencies": observed["dependencies"],
        "packaging_mode": packaging_mode,
        "encoded_container_sha256": observed["encoded_container_sha256"],
        "encoded_container_bytes": observed["encoded_container_bytes"],
        "codec_id": encoded.codec_id if encoded is not None else None,
        "layout_id": encoded.layout_id if encoded is not None else None,
        "capture_controls_required": True,
        "capture_protocol": nw07_contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "source_kind": source_kind,
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


def _write_result_bundle_manifest(
    root: Path,
    *,
    tier: str,
    record_path: Path,
    capture_dir: Path,
    verification_file: str | None,
) -> None:
    """Bind a packed capture directory to a comparable result bundle.

    The compare command requires the capture record, its raw captures, and the
    retained packed diagnostic executable to be covered by a
    ``bundle-manifest.json`` in the record directory.  Generate that manifest
    as part of the direct capture path so callers do not have to remember a
    separate bundling step.  ``New-NwBundle`` may still re-bundle the same
    material for transfer.
    """
    files: dict[str, Path] = {record_path.name: record_path}
    if verification_file:
        files[verification_file] = root / verification_file
    for path in sorted(capture_dir.glob("*")):
        if path.is_file():
            files[f"captures/{path.name}"] = path
    manifest = {
        "format": "nw10-correctness-result-bundle",
        "version": 1,
        "task": "NW-10",
        "tier": tier,
        "files": [
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for relative, path in sorted(files.items())
        ],
    }
    (root / "bundle-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def capture(
    package: Path,
    *,
    expected_package: Path,
    diagnostic_executable: Path,
    capture_dir: Path,
    tier: str,
    output: Path,
    packaging_mode: str = "unpacked",
) -> dict[str, Any]:
    if packaging_mode not in PACKAGING_MODES:
        raise ValueError(
            f"capture packaging mode must be one of {PACKAGING_MODES}"
        )
    if packaging_mode == "packed":
        if output.name != "capture.json":
            raise ValueError(
                "packed correctness capture must be written as capture.json so "
                "its result bundle manifest can be verified"
            )
        if capture_dir.resolve() != (output.parent / "captures").resolve():
            raise ValueError(
                "packed correctness capture directory must be the record "
                "directory's captures subdirectory"
            )
    expected = _identity(expected_package)
    observed = _diagnostic_identity(package, packaging_mode=packaging_mode)
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
    form_factor = _windows_device_form_factor_probe()
    if form_factor["device_form_factor"] != "desktop":
        raise ValueError(
            "representative desktop correctness requires a measured desktop "
            f"chassis, observed {form_factor['device_form_factor']!r}"
        )
    observed_tier = nw07_evidence.classify_hardware_tier(
        hardware["adapter_description"],
        device_form_factor=form_factor["device_form_factor"],
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

    verification_file: str | None = None
    output.parent.mkdir(parents=True, exist_ok=True)
    verification_path = output.parent / f"{packaging_mode}-diagnostic.exe"
    if diagnostic_executable.resolve() != verification_path.resolve():
        shutil.copy2(diagnostic_executable, verification_path)
    if _sha256(verification_path) != observed["executable_sha256"]:
        raise ValueError(
            f"retained {packaging_mode} diagnostic executable hash changed"
        )
    verification_file = verification_path.name

    record = {
        "format": FORMAT,
        "version": VERSION,
        "task": "NW-10",
        "coverage_role": "common_artifact_correctness",
        "status": "captured_reference" if tier == "ada" else "captured_candidate",
        "tier": tier,
        "execution_kind": "physical",
        "packaging_mode": packaging_mode,
        "capture_protocol": nw07_contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "common_artifact": {
            "artifact_key": expected["artifact_key"],
            "package_sha256": expected["package_sha256"],
            "manifest_sha256": expected["manifest_sha256"],
            "profile_id": expected["profile_id"],
            "requirements_digest": expected["requirements_digest"],
            "final_executable_sha256": expected["executable_sha256"],
        },
        "diagnostic_sibling": {
            "executable_sha256": observed["executable_sha256"],
            "executable_bytes": observed["executable_bytes"],
            "manifest_sha256": observed["manifest_sha256"],
            "dependencies": observed["dependencies"],
            "separate_from_final_executable": True,
            "same_common_package": True,
            "release_manifest_reused": False,
            "packaging_mode": packaging_mode,
            "encoded_container_sha256": observed["encoded_container_sha256"],
            "encoded_container_bytes": observed["encoded_container_bytes"],
            "codec_id": observed["codec_id"],
            "layout_id": observed["layout_id"],
            "verification_file": verification_file,
        },
        "hardware": {
            **hardware,
            "device_form_factor": form_factor["device_form_factor"],
            "device_form_factor_source": form_factor[
                "device_form_factor_source"
            ],
            "chassis_types": form_factor["chassis_types"],
            "battery_present": form_factor["battery_present"],
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
        "runtime": {
            "status": "passed",
            "capture_exit_code": 0,
            "frames_per_tick": 1,
            "present_sync_interval": hardware["present_sync_interval"],
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
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if packaging_mode == "packed":
        _write_result_bundle_manifest(
            output.parent,
            tier=tier,
            record_path=output,
            capture_dir=capture_dir,
            verification_file=verification_file,
        )
        _verify_result_bundle_manifest(
            record,
            record_path=output,
            capture_dir=capture_dir,
        )
    return record


def _verify_capture_manifest(
    record: Mapping[str, Any], capture_dir: Path
) -> None:
    rows = record.get("captures")
    if not isinstance(rows, list) or len(rows) != len(ALL_TICKS):
        raise ValueError("correctness record has an incomplete capture manifest")
    expected_ticks = list(ALL_TICKS)
    observed_ticks = [int(row.get("tick", -1)) for row in rows if isinstance(row, Mapping)]
    if observed_ticks != expected_ticks:
        raise ValueError(
            f"correctness capture ticks changed: expected={expected_ticks}, "
            f"observed={observed_ticks}"
        )
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("correctness capture manifest row is invalid")
        tick = int(row["tick"])
        expected_name = f"tick_{tick}.rgba32f"
        if row.get("file") != expected_name:
            raise ValueError(f"correctness capture filename changed for tick {tick}")
        path = capture_dir / expected_name
        if not path.is_file():
            raise ValueError(f"correctness raw capture is missing for tick {tick}")
        if path.stat().st_size != int(row.get("bytes", -1)):
            raise ValueError(f"correctness raw capture size mismatch for tick {tick}")
        if _sha256(path) != str(row.get("sha256", "")):
            raise ValueError(f"correctness raw capture SHA-256 mismatch for tick {tick}")


def _verify_result_bundle_manifest(
    record: Mapping[str, Any],
    *,
    record_path: Path,
    capture_dir: Path,
) -> None:
    root = record_path.parent.resolve()
    if record_path.name != "capture.json":
        raise ValueError("packed capture record must be the bundle capture.json")
    if capture_dir.resolve() != (root / "captures").resolve():
        raise ValueError("packed capture directory is not the bundle captures directory")
    manifest_path = root / "bundle-manifest.json"
    if not manifest_path.is_file():
        raise ValueError("packed correctness result bundle manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise ValueError("packed correctness result bundle manifest is not an object")
    if manifest.get("format") not in {
        "nw10-correctness-result-bundle",
        "nw10-correctness-transfer-bundle",
    }:
        raise ValueError("packed correctness result bundle format is invalid")
    if manifest.get("version") != 1 or manifest.get("task") != "NW-10":
        raise ValueError("packed correctness result bundle identity is invalid")
    if manifest.get("tier") != record.get("tier"):
        raise ValueError("packed correctness result bundle tier does not match capture")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise ValueError("packed correctness result bundle has no files ledger")
    file_hashes: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("packed correctness result bundle file row is invalid")
        relative = row.get("path")
        digest = str(row.get("sha256", "")).lower()
        if not isinstance(relative, str):
            raise ValueError("packed correctness result bundle path is invalid")
        normalized = relative.replace("\\", "/")
        if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
            raise ValueError("packed correctness result bundle path is absolute")
        raw_parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise ValueError("packed correctness result bundle path escapes its root")
        path = PurePosixPath(normalized)
        if path.is_absolute():
            raise ValueError("packed correctness result bundle path is absolute")
        if normalized in file_hashes:
            raise ValueError("packed correctness result bundle contains duplicate paths")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("packed correctness result bundle SHA-256 is malformed")
        try:
            expected_bytes = int(row.get("bytes", -1))
        except (TypeError, ValueError) as exc:
            raise ValueError("packed correctness result bundle byte count is invalid") from exc
        if expected_bytes < 0:
            raise ValueError("packed correctness result bundle byte count is invalid")
        candidate = root.joinpath(*path.parts)
        try:
            candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError("packed correctness result bundle path escapes its root") from exc
        if not candidate.is_file():
            raise ValueError(f"packed correctness result bundle file is missing: {normalized}")
        if candidate.stat().st_size != expected_bytes:
            raise ValueError(f"packed correctness result bundle size mismatch: {normalized}")
        actual = _sha256(candidate)
        if actual != digest:
            raise ValueError(f"packed correctness result bundle SHA-256 mismatch: {normalized}")
        file_hashes[normalized] = actual
    diagnostic = record.get("diagnostic_sibling")
    if not isinstance(diagnostic, Mapping):
        raise ValueError("packed capture is missing diagnostic identity")
    verification_file = diagnostic.get("verification_file")
    if not isinstance(verification_file, str) or not verification_file:
        raise ValueError("packed capture does not retain its diagnostic executable")
    required = {"capture.json", verification_file}
    captures = record.get("captures")
    if not isinstance(captures, list):
        raise ValueError("packed capture has no capture manifest")
    for row in captures:
        if not isinstance(row, Mapping) or not isinstance(row.get("file"), str):
            raise ValueError("packed capture manifest row is invalid")
        required.add("captures/" + str(row["file"]))
    missing = sorted(required - set(file_hashes))
    if missing:
        raise ValueError(
            "packed correctness result bundle manifest omits required files: "
            + ", ".join(missing)
        )
    if file_hashes["capture.json"] != _sha256(record_path):
        raise ValueError("packed capture.json is not bound by the result bundle manifest")
    expected_executable_sha = str(diagnostic.get("executable_sha256", "")).lower()
    if file_hashes[verification_file] != expected_executable_sha:
        raise ValueError("packed diagnostic executable is not bound by the result bundle manifest")


def _verify_packed_executable(
    record: Mapping[str, Any], *, record_path: Path
) -> None:
    diagnostic = record.get("diagnostic_sibling")
    common = record.get("common_artifact")
    if not isinstance(diagnostic, Mapping) or not isinstance(common, Mapping):
        raise ValueError("packed capture is missing artifact identity")
    relative = diagnostic.get("verification_file")
    if not isinstance(relative, str) or not relative:
        raise ValueError("packed capture does not retain its diagnostic executable")
    relative_path = Path(relative)
    if relative_path.is_absolute() or relative_path.name != relative:
        raise ValueError("packed diagnostic verification file must be a local filename")
    executable = record_path.parent / relative_path
    if not executable.is_file():
        raise ValueError("packed diagnostic verification executable is missing")
    executable_bytes = executable.read_bytes()
    executable_sha = hashlib.sha256(executable_bytes).hexdigest()
    if executable_sha != str(diagnostic.get("executable_sha256", "")).lower():
        raise ValueError("packed diagnostic executable SHA-256 mismatch")
    payload, _manifest = nw05_export.extract_embedded_package(executable_bytes)
    if not compact.is_compact_container(payload):
        raise ValueError("packed diagnostic executable does not contain compact bytes")
    encoded_sha = hashlib.sha256(payload).hexdigest()
    if encoded_sha != str(diagnostic.get("encoded_container_sha256", "")).lower():
        raise ValueError("packed encoded-container SHA-256 does not match executable")
    if len(payload) != int(diagnostic.get("encoded_container_bytes", -1)):
        raise ValueError("packed encoded-container size does not match executable")
    header = compact.parse_header(payload)
    if str(header.get("codec_id")) != str(diagnostic.get("codec_id")):
        raise ValueError("packed codec identity does not match executable")
    if str(header.get("layout_id")) != str(diagnostic.get("layout_id")):
        raise ValueError("packed layout identity does not match executable")
    decoded = compact.decode_compact(payload)
    decoded_sha = hashlib.sha256(decoded).hexdigest()
    if decoded_sha != str(common.get("package_sha256", "")).lower():
        raise ValueError("packed executable does not decode to the common package")


def _verify_unpacked_executable(
    record: Mapping[str, Any], *, record_path: Path
) -> None:
    """Reopen and hash the retained unpacked diagnostic executable.

    The unpacked capture path must be bound to the actual bytes that produced
    the compared images, not merely to a non-empty hash string.
    """
    diagnostic = record.get("diagnostic_sibling")
    common = record.get("common_artifact")
    if not isinstance(diagnostic, Mapping) or not isinstance(common, Mapping):
        raise ValueError("unpacked capture is missing artifact identity")
    relative = diagnostic.get("verification_file")
    if not isinstance(relative, str) or not relative:
        raise ValueError(
            "unpacked capture does not retain its diagnostic executable"
        )
    relative_path = Path(relative)
    if relative_path.is_absolute() or relative_path.name != relative:
        raise ValueError(
            "unpacked diagnostic verification file must be a local filename"
        )
    executable = record_path.parent / relative_path
    if not executable.is_file():
        raise ValueError("unpacked diagnostic verification executable is missing")
    executable_bytes = executable.read_bytes()
    executable_sha = hashlib.sha256(executable_bytes).hexdigest()
    if executable_sha != str(diagnostic.get("executable_sha256", "")).lower():
        raise ValueError("unpacked diagnostic executable SHA-256 mismatch")
    payload, _manifest = nw05_export.extract_embedded_package(executable_bytes)
    if compact.is_compact_container(payload):
        raise ValueError(
            "unpacked diagnostic executable unexpectedly contains compact bytes"
        )
    if hashlib.sha256(payload).hexdigest() != str(
        common.get("package_sha256", "")
    ).lower():
        raise ValueError(
            "unpacked executable does not embed the common package"
        )


def _verify_capture_record(
    record: Mapping[str, Any],
    *,
    tier: str,
    status: str,
    packaging_mode: str,
    capture_dir: Path,
    record_path: Path,
) -> None:
    if record.get("format") != FORMAT:
        raise ValueError(f"{tier} {packaging_mode} capture has an unexpected format")
    if record.get("task") != "NW-10":
        raise ValueError(f"{tier} {packaging_mode} capture is not NW-10 evidence")
    if record.get("tier") != tier:
        raise ValueError(f"{tier} {packaging_mode} capture changed its hardware tier")
    if record.get("status") != status:
        raise ValueError(f"{tier} {packaging_mode} capture has the wrong status")
    if record.get("execution_kind") != "physical":
        raise ValueError(f"{tier} {packaging_mode} capture is not physical")
    if record.get("capture_protocol") != nw07_contract.CORRECTNESS_CAPTURE_PROTOCOL:
        raise ValueError(f"{tier} {packaging_mode} capture changed the protocol")
    if record.get("packaging_mode") != packaging_mode:
        raise ValueError(
            f"{tier} capture is not the declared {packaging_mode} artifact"
        )
    diagnostic = record.get("diagnostic_sibling")
    if not isinstance(diagnostic, Mapping):
        raise ValueError(f"{tier} {packaging_mode} diagnostic identity is missing")
    if diagnostic.get("separate_from_final_executable") is not True:
        raise ValueError(f"{tier} capture did not use a separate diagnostic sibling")
    if diagnostic.get("same_common_package") is not True:
        raise ValueError(f"{tier} capture diagnostic package identity is not bound")
    if diagnostic.get("release_manifest_reused") is not False:
        raise ValueError(f"{tier} capture reused the release manifest")
    if str(diagnostic.get("executable_sha256", "")) == "":
        raise ValueError(f"{tier} capture does not name its diagnostic executable")
    if packaging_mode == "packed":
        if not diagnostic.get("encoded_container_sha256"):
            raise ValueError(f"{tier} packed capture does not name its encoded container")
        if diagnostic.get("codec_id") not in contract.PLAYER_SUPPORTED_CODEC_IDS:
            raise ValueError(f"{tier} packed capture has no supported codec identity")
        if diagnostic.get("layout_id") not in contract.KNOWN_LAYOUT_IDS:
            raise ValueError(f"{tier} packed capture has no supported layout identity")
        _verify_result_bundle_manifest(
            record,
            record_path=record_path,
            capture_dir=capture_dir,
        )
        _verify_packed_executable(record, record_path=record_path)
    else:
        _verify_unpacked_executable(record, record_path=record_path)
    hardware = record.get("hardware")
    if not isinstance(hardware, Mapping):
        raise ValueError(f"{tier} {packaging_mode} hardware identity is missing")
    if hardware.get("device_form_factor") != "desktop":
        raise ValueError(f"{tier} {packaging_mode} capture is not a measured desktop")
    form_factor_source = str(hardware.get("device_form_factor_source", ""))
    if (
        not form_factor_source
        or form_factor_source in {"asserted", "pending_physical_probe", "unsupported_host"}
        or "reused" in form_factor_source.casefold()
        or "inventory" in form_factor_source.casefold()
    ):
        raise ValueError(
            f"{tier} {packaging_mode} form factor was not measured by a chassis probe"
        )
    runtime = record.get("runtime")
    if not isinstance(runtime, Mapping) or runtime.get("status") != "passed":
        raise ValueError(f"{tier} {packaging_mode} runtime capture did not pass")
    if int(runtime.get("capture_exit_code", -1)) != 0:
        raise ValueError(f"{tier} {packaging_mode} capture process did not exit zero")
    if int(runtime.get("frames_per_tick", -1)) != 1:
        raise ValueError(f"{tier} {packaging_mode} capture changed frames-per-tick")
    if int(runtime.get("present_sync_interval", -1)) != 0:
        raise ValueError(f"{tier} {packaging_mode} capture is not uncapped")
    _verify_capture_manifest(record, capture_dir)


def compare(
    ada_record_path: Path,
    blackwell_record_path: Path,
    *,
    ada_capture_dir: Path,
    blackwell_capture_dir: Path,
    output: Path,
    packed_ada_capture_dir: Path | None = None,
    packed_blackwell_capture_dir: Path | None = None,
    packed_ada_record: Path | None = None,
    packed_blackwell_record: Path | None = None,
    packed_package_sha256: str | None = None,
    unpacked_package_sha256: str | None = None,
    implementation_head: str | None = None,
) -> dict[str, Any]:
    ada = json.loads(ada_record_path.read_text(encoding="utf-8"))
    blackwell = json.loads(blackwell_record_path.read_text(encoding="utf-8"))
    _verify_capture_record(
        ada,
        tier="ada",
        status="captured_reference",
        packaging_mode="unpacked",
        capture_dir=ada_capture_dir,
        record_path=ada_record_path,
    )
    _verify_capture_record(
        blackwell,
        tier="blackwell",
        status="captured_candidate",
        packaging_mode="unpacked",
        capture_dir=blackwell_capture_dir,
        record_path=blackwell_record_path,
    )
    if ada.get("common_artifact") != blackwell.get("common_artifact"):
        raise ValueError("Ada and Blackwell captures do not name the same common artifact")
    for label, record, expected_tier in (
        ("ada", ada, "ada"),
        ("blackwell", blackwell, "blackwell"),
    ):
        hardware = record.get("hardware")
        if not isinstance(hardware, Mapping):
            raise ValueError(f"{label} correctness hardware is missing")
        if hardware.get("classified_tier") != expected_tier:
            raise ValueError(f"{label} correctness hardware tier is wrong")
        if hardware.get("device_form_factor") != "desktop":
            raise ValueError(
                f"{label} correctness hardware is not a measured desktop"
            )
        if hardware.get("device_form_factor_source") in (
            None,
            "",
            "asserted",
            "unsupported_host",
        ):
            raise ValueError(
                f"{label} correctness form factor was not measured"
            )
        form_factor_source = str(
            hardware.get("device_form_factor_source", "")
        ).casefold()
        if "reused" in form_factor_source or "inventory" in form_factor_source:
            raise ValueError(
                f"{label} correctness form factor came from inventory rather "
                "than a chassis probe"
            )
    ada_loop = _compare_rgba32f(
        ada_capture_dir / f"tick_{nw07_contract.LOOP_BOUNDARY_TICKS[0]}.rgba32f",
        ada_capture_dir / f"tick_{nw07_contract.LOOP_BOUNDARY_TICKS[1]}.rgba32f",
    )
    blackwell_loop = _compare_rgba32f(
        blackwell_capture_dir / f"tick_{nw07_contract.LOOP_BOUNDARY_TICKS[0]}.rgba32f",
        blackwell_capture_dir / f"tick_{nw07_contract.LOOP_BOUNDARY_TICKS[1]}.rgba32f",
    )
    ada_loop["status"] = (
        "passed"
        if ada_loop["maximum_error"] <= nw07_contract.IMAGE_ABSOLUTE_TOLERANCE
        else "failed"
    )
    blackwell_loop["status"] = (
        "passed"
        if blackwell_loop["maximum_error"] <= nw07_contract.IMAGE_ABSOLUTE_TOLERANCE
        else "failed"
    )
    for loop_result in (ada_loop, blackwell_loop):
        loop_result["absolute_tolerance"] = nw07_contract.IMAGE_ABSOLUTE_TOLERANCE
        loop_result["relative_tolerance"] = nw07_contract.IMAGE_RELATIVE_TOLERANCE

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
        if (
            all(row["status"] == "passed" for row in per_tick)
            and ada_loop["status"] == "passed"
            and blackwell_loop["status"] == "passed"
        )
        else "failed"
    )
    packed_block: dict[str, Any]
    packed_inputs = (
        packed_ada_capture_dir,
        packed_blackwell_capture_dir,
        packed_ada_record,
        packed_blackwell_record,
    )
    if any(item is not None for item in packed_inputs):
        if not all(item is not None for item in packed_inputs):
            raise ValueError(
                "packed correctness comparison requires both hardware capture "
                "directories and both capture manifests"
            )
        if packed_ada_record is None or packed_blackwell_record is None:
            raise ValueError(
                "packed capture directories require their capture manifests"
            )
        packed_records = {
            "ada": json.loads(packed_ada_record.read_text(encoding="utf-8")),
            "blackwell": json.loads(
                packed_blackwell_record.read_text(encoding="utf-8")
            ),
        }
        for label, record, capture_dir, expected_tier, expected_status in (
            (
                "ada",
                packed_records["ada"],
                packed_ada_capture_dir,
                "ada",
                "captured_reference",
            ),
            (
                "blackwell",
                packed_records["blackwell"],
                packed_blackwell_capture_dir,
                "blackwell",
                "captured_candidate",
            ),
        ):
            _verify_capture_record(
                record,
                tier=expected_tier,
                status=expected_status,
                packaging_mode="packed",
                capture_dir=capture_dir,
                record_path=(
                    packed_ada_record if label == "ada" else packed_blackwell_record
                ),
            )
            if record.get("common_artifact") != ada.get("common_artifact"):
                raise ValueError(
                    f"packed {label} captures are not bound to the common artifact"
                )
            unpacked_hardware = (
                ada if label == "ada" else blackwell
            ).get("hardware", {})
            unpacked_record = ada if label == "ada" else blackwell
            packed_hardware = record.get("hardware", {})
            for field in (
                "adapter_description",
                "driver_version",
                "vendor_id",
                "device_id",
                "classified_tier",
                "device_form_factor",
                "device_form_factor_source",
            ):
                if packed_hardware.get(field) != unpacked_hardware.get(field):
                    raise ValueError(
                        f"packed {label} capture hardware changed {field}"
                    )
            if (
                str(
                    packed_records[label]
                    .get("diagnostic_sibling", {})
                    .get("executable_sha256", "")
                )
                == str(
                    unpacked_record.get("diagnostic_sibling", {}).get(
                        "executable_sha256", ""
                    )
                )
            ):
                raise ValueError(
                    f"packed {label} capture reused the unpacked executable"
                )
        for label, record_path, capture_dir in (
            ("ada", packed_ada_record, packed_ada_capture_dir),
            ("blackwell", packed_blackwell_record, packed_blackwell_capture_dir),
        ):
            if not record_path.is_file():
                raise ValueError(f"packed {label} capture manifest is missing")
            if not capture_dir.is_dir():
                raise ValueError(f"packed {label} capture directory is missing")
        packed_codec_ids = {
            str(
                packed_records[label]
                .get("diagnostic_sibling", {})
                .get("codec_id", "")
            )
            for label in ("ada", "blackwell")
        }
        packed_layout_ids = {
            str(
                packed_records[label]
                .get("diagnostic_sibling", {})
                .get("layout_id", "")
            )
            for label in ("ada", "blackwell")
        }
        if len(packed_codec_ids) != 1 or "" in packed_codec_ids:
            raise ValueError("packed captures do not share one explicit codec identity")
        if len(packed_layout_ids) != 1 or "" in packed_layout_ids:
            raise ValueError("packed captures do not share one explicit layout identity")
        packed_codec_id = next(iter(packed_codec_ids))
        packed_layout_id = next(iter(packed_layout_ids))
        common_package_sha = str(
            ada["common_artifact"].get("package_sha256", "")
        ).lower()
        for label in ("ada", "blackwell"):
            packed_sha = str(
                packed_records[label]
                .get("diagnostic_sibling", {})
                .get("encoded_container_sha256", "")
            ).lower()
            if not packed_sha:
                raise ValueError(
                    f"packed {label} capture does not name its encoded container"
                )
        packed_worst = 0.0
        packed_runtime: dict[str, Any] = {}
        for label, unpacked_dir, packed_dir in (
            ("ada", ada_capture_dir, packed_ada_capture_dir),
            ("blackwell", blackwell_capture_dir, packed_blackwell_capture_dir),
        ):
            tier_worst = 0.0
            for tick in ALL_TICKS:
                result = _compare_rgba32f(
                    packed_dir / f"tick_{tick}.rgba32f",
                    unpacked_dir / f"tick_{tick}.rgba32f",
                )
                tier_worst = max(tier_worst, result["maximum_error"])
            passed = tier_worst <= nw07_contract.IMAGE_ABSOLUTE_TOLERANCE
            packed_runtime[label] = {
                "status": "passed" if passed else "failed",
                "worst_absolute_error": tier_worst,
                "runtime_status": "passed",
                "unpacked_executable_sha256": (
                    (ada if label == "ada" else blackwell)
                    .get("diagnostic_sibling", {})
                    .get("executable_sha256")
                ),
                "packed_executable_sha256": (
                    packed_records[label]
                    .get("diagnostic_sibling", {})
                    .get("executable_sha256")
                ),
                "encoded_container_sha256": (
                    packed_records[label]
                    .get("diagnostic_sibling", {})
                    .get("encoded_container_sha256")
                ),
                "encoded_container_bytes": (
                    packed_records[label]
                    .get("diagnostic_sibling", {})
                    .get("encoded_container_bytes")
                ),
                "unpacked_manifest_sha256": (
                    (ada if label == "ada" else blackwell)
                    .get("diagnostic_sibling", {})
                    .get("manifest_sha256")
                ),
                "packed_manifest_sha256": (
                    packed_records[label]
                    .get("diagnostic_sibling", {})
                    .get("manifest_sha256")
                ),
                "codec_id": packed_codec_id,
                "layout_id": packed_layout_id,
                "device_form_factor_source": packed_records[label]
                    .get("hardware", {})
                    .get("device_form_factor_source"),
                "frames_per_tick": packed_records[label]
                    .get("runtime", {})
                    .get("frames_per_tick"),
                "present_sync_interval": packed_records[label]
                    .get("runtime", {})
                    .get("present_sync_interval"),
            }
            packed_worst = max(packed_worst, tier_worst)
        packed_ok = (
            all(item["status"] == "passed" for item in packed_runtime.values())
            and packed_package_sha256 is not None
            and packed_package_sha256 == unpacked_package_sha256
            and packed_package_sha256 == common_package_sha
            and all(
                item.get("runtime_status") == "passed"
                for item in packed_runtime.values()
            )
        )
        packed_block = {
            "status": "verified" if packed_ok else "failed",
            "direct_separate_packed_gpu_capture_recorded": True,
            "codec_id": packed_codec_id,
            "layout_id": packed_layout_id,
            "packed_package_sha256": packed_package_sha256,
            "unpacked_package_sha256": unpacked_package_sha256,
            "worst_absolute_error": packed_worst,
            "runtime": packed_runtime,
        }
        if not packed_ok:
            status = "failed"
    else:
        packed_block = {
            "status": "pending_physical",
            "direct_separate_packed_gpu_capture_recorded": False,
            "codec_id": None,
            "layout_id": None,
            "packed_package_sha256": None,
            "unpacked_package_sha256": None,
            "worst_absolute_error": None,
            "runtime": {
                "ada": {"status": "pending_physical"},
                "blackwell": {"status": "pending_physical"},
            },
        }
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
            "loop_boundary": ada_loop,
        },
        "blackwell": {
            "hardware": blackwell["hardware"],
            "capture_manifest": blackwell["captures"],
            "loop_boundary": blackwell_loop,
        },
        "comparison": {
            "per_tick": per_tick,
            "fixed_ticks": list(nw07_contract.REQUIRED_FIXED_TICKS),
            "fixed_tick_status": (
                "passed"
                if all(row["status"] == "passed" for row in per_tick)
                else "failed"
            ),
            "loop_boundary_ticks": list(nw07_contract.LOOP_BOUNDARY_TICKS),
            "ada_loop_boundary_status": ada_loop["status"],
            "blackwell_loop_boundary_status": blackwell_loop["status"],
            "worst_absolute_error": worst_absolute,
            "worst_relative_error": worst_relative,
            "absolute_tolerance": nw07_contract.IMAGE_ABSOLUTE_TOLERANCE,
            "relative_tolerance": nw07_contract.IMAGE_RELATIVE_TOLERANCE,
            "absolute_required": True,
        },
        "packed_unpacked_comparison": packed_block,
        "source": {
            "mode": "compare",
            "execution_kind": "physical_cross_hardware",
            "tool": "tools/nw10/run-correctness-evidence.ps1",
            "implementation_head": (
                implementation_head or _git_head() or "0" * 40
            ),
            "raw_capture_manifests_tracked": False,
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
    build.add_argument(
        "--mode", choices=PACKAGING_MODES, default="unpacked"
    )
    build.add_argument(
        "--source", choices=DIAGNOSTIC_SOURCES, default="nw07"
    )
    build.add_argument(
        "--codec", default="auto", choices=("auto", *contract.PLAYER_SUPPORTED_CODEC_IDS)
    )
    build.add_argument(
        "--layout", default="auto", choices=("auto", *contract.KNOWN_LAYOUT_IDS)
    )

    cap = sub.add_parser("capture")
    cap.add_argument("--package", type=Path, required=True)
    cap.add_argument("--expected-package", type=Path, required=True)
    cap.add_argument("--diagnostic-executable", type=Path, required=True)
    cap.add_argument("--capture-dir", type=Path, required=True)
    cap.add_argument("--tier", choices=("ada", "blackwell"), required=True)
    cap.add_argument("--output", type=Path, required=True)
    cap.add_argument(
        "--mode", choices=PACKAGING_MODES, default="unpacked"
    )

    comp = sub.add_parser("compare")
    comp.add_argument("--ada-record", type=Path, required=True)
    comp.add_argument("--blackwell-record", type=Path, required=True)
    comp.add_argument("--ada-captures", type=Path, required=True)
    comp.add_argument("--blackwell-captures", type=Path, required=True)
    comp.add_argument("--packed-ada-captures", type=Path)
    comp.add_argument("--packed-blackwell-captures", type=Path)
    comp.add_argument("--packed-ada-record", type=Path)
    comp.add_argument("--packed-blackwell-record", type=Path)
    comp.add_argument("--packed-package-sha256")
    comp.add_argument("--unpacked-package-sha256")
    comp.add_argument("--implementation-head", default="")
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
                packaging_mode=args.mode,
                codec_id=args.codec,
                layout_id=args.layout,
                source_kind=args.source,
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
                packaging_mode=args.mode,
            )
        else:
            summary = compare(
                args.ada_record,
                args.blackwell_record,
                ada_capture_dir=args.ada_captures,
                blackwell_capture_dir=args.blackwell_captures,
                output=args.output,
                packed_ada_capture_dir=args.packed_ada_captures,
                packed_blackwell_capture_dir=args.packed_blackwell_captures,
                packed_ada_record=args.packed_ada_record,
                packed_blackwell_record=args.packed_blackwell_record,
                packed_package_sha256=args.packed_package_sha256,
                unpacked_package_sha256=args.unpacked_package_sha256,
                implementation_head=args.implementation_head or None,
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
