#!/usr/bin/env python3
"""Record NW-12 physical WASAPI/display audio evidence.

This is the physical-validation harness for the bounded procedural audio
fixture.  It deliberately separates three observations that must never be
fabricated or inferred:

* real WASAPI playback telemetry (synthesis/worker CPU, observed underruns,
  missing-sample lower bound and clock discontinuities) emitted by the
  exact audio-enabled artifact;
* the real displayed impulse/flash alignment, supplied by an external A/V
  observation record produced while the same artifact played on the desktop;
* the display/audio environment the observation executed in.

A record only becomes ``verified_physical`` when all three are present and
bound to the same artifact identity.  Otherwise it stays ``pending_physical``
with explicit ``unavailable`` reasons; numeric telemetry is never invented.

The harness reuses the shared Windows text decoder from
``hardware_evidence_host`` so it does not introduce locale-dependent parsing
or task-local host/VM classification.  It is not executed by hosted CI; CI
only verifies the deterministic binding logic below.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import hardware_evidence_host  # noqa: E402
import nwp_package as nwp  # noqa: E402
from audio_song import decode_song  # noqa: E402

FORMAT = "nodewerk-nw12-audio-physical-evidence"
VERSION = 1
TICKS_PER_SECOND = 48_000
DEFAULT_DISPLAY_FRAME_RATE = 60
OBSERVATION_FORMAT = "nodewerk-audio-display-observation"

_INT_TELEMETRY = {
    "AUDIO_STARVATION_OBSERVATIONS": "starvation_observations",
    "AUDIO_CLOCK_DISCONTINUITIES": "clock_discontinuities",
    "AUDIO_SYNTHESIS_CPU_100NS": "synthesis_cpu_100ns",
    "AUDIO_OBSERVED_UNDERRUNS": "observed_underruns",
    "AUDIO_MISSING_SAMPLES_LOWER_BOUND": "missing_samples_lower_bound",
    "AUDIO_WORKER_CPU_100NS": "worker_cpu_100ns",
}
_FLAG_TELEMETRY = {
    "AUDIO_SYNTHESIS_CPU_AVAILABLE": "synthesis_cpu_available",
    "AUDIO_WORKER_CPU_AVAILABLE": "worker_cpu_available",
}

PROTOCOL = {
    "tool": "tools/audio_physical_validation.py",
    "graph": "fixtures/audio/sync_flash.nwgraph",
    "song": "fixtures/audio/sync_song.json",
    "sample_rate": TICKS_PER_SECOND,
    "clock": "wasapi_endpoint",
    "display_frame_rate": DEFAULT_DISPLAY_FRAME_RATE,
    "alignment_definition": (
        "abs(observed_flash_onset_display_tick - authoritative_impulse_onset_tick) "
        "<= 48000 / display_refresh_hz"
    ),
    "measurement_steps": [
        "export fixtures/audio/sync_flash.nwgraph with fixtures/audio/sync_song.json",
        "launch the generated audio-enabled player on a Windows 11 desktop with a real WASAPI endpoint",
        "record the displayed flash onsets and the audible impulse onsets with one A/V capture",
        "record the player AUDIO_* telemetry from the same run",
        "run `python tools/audio_physical_validation.py record` with the capture and telemetry",
    ],
    "telemetry_fields": [
        "AUDIO_SYNTHESIS_CPU_100NS",
        "AUDIO_SYNTHESIS_CPU_AVAILABLE",
        "AUDIO_WORKER_CPU_100NS",
        "AUDIO_WORKER_CPU_AVAILABLE",
        "AUDIO_STARVATION_OBSERVATIONS",
        "AUDIO_OBSERVED_UNDERRUNS",
        "AUDIO_MISSING_SAMPLES_LOWER_BOUND",
        "AUDIO_CLOCK_DISCONTINUITIES",
        "AUDIO_PHYSICAL_UNDERRUNS",
    ],
}

EVIDENCE_RECORD = "benchmarks/nw12/audio-physical-evidence.v1.json"
IMPLEMENTATION_BASE = "8ade0b74c78113c18c977afbde0f3e2b98c4fcf2"
IMPLEMENTATION_BASE_TREE = "6bf077705a406e2e1cea83cf27723b0c51c5ed9d"
BUILD_INPUTS = (
    "CMakeLists.txt",
    "CMakePresets.json",
    "benchmarks/toolchain-manifest.v1.json",
    "cmake/DependencyCheck.cmake",
)
REQUIRED_ENVIRONMENT = {
    "platform": "Windows 11 x64 desktop session on the ordinary validation host",
    "audio": "real WASAPI render endpoint queried and active during playback",
    "display": "real windowed Present at the recorded refresh rate (60 Hz fixture tolerance)",
    "gpu": "no audio-specific GPU predicate; existing common-renderer representative coverage only",
}


def _git_revision(reference: str, root: Path = ROOT) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", reference],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def provenance_snapshot(
    root: Path = ROOT, *, include_source: bool = True
) -> dict[str, Any]:
    """Record the implementation base, source tree and build-input identity."""
    import build_attestation

    snapshot: dict[str, Any] = {
        "main_ref_revision": _git_revision("main", root) or IMPLEMENTATION_BASE,
        "implementation_base": IMPLEMENTATION_BASE,
        "implementation_base_tree": IMPLEMENTATION_BASE_TREE,
        "nw11_merge_revision": IMPLEMENTATION_BASE,
        "nw11_incorporated": True,
        "build_inputs": {
            relative: sha256_file(root / relative)
            for relative in BUILD_INPUTS
            if (root / relative).is_file()
        },
    }
    if include_source:
        revision = build_attestation.source_revision(root)
        snapshot["source_revision"] = revision["commit"]
        snapshot["source_tree"] = revision["tree"]
        snapshot["tracked_files_clean"] = revision["tracked_files_clean"]
    return snapshot


def evidence_reference(
    reason: str, *, verified: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Describe the physical observation an offline/headless check cannot make."""
    reference: dict[str, Any] = {
        "status": "unavailable",
        "reason": reason,
        "tool": PROTOCOL["tool"],
        "evidence_record": EVIDENCE_RECORD,
        "required_observation": (
            "real WASAPI playback plus a real displayed impulse/flash capture on a "
            "Windows 11 desktop session"
        ),
        "alignment_definition": PROTOCOL["alignment_definition"],
    }
    if verified is not None:
        reference["status"] = "verified"
        reference["evidence"] = dict(verified)
    return reference


def load_verified_evidence(
    path: str | os.PathLike[str] | None = None,
    *,
    package_bytes: bytes | None = None,
    song_bytes: bytes | None = None,
    executable_bytes: bytes | None = None,
) -> dict[str, Any] | None:
    """Load a verified physical record, matching the selected artifact if given."""
    candidate = path or os.environ.get("NW12_PHYSICAL_EVIDENCE", "")
    if not candidate:
        return None
    evidence_path = Path(candidate)
    if not evidence_path.is_file():
        return None
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    verify_evidence(
        evidence, package_bytes=package_bytes, song_bytes=song_bytes,
        executable_bytes=executable_bytes,
    )
    if evidence.get("verification_status") != "verified_physical":
        return None
    return evidence


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_player_telemetry(stdout: str) -> dict[str, Any]:
    """Parse the exact ``AUDIO_*`` lines printed by the generated player."""
    raw: dict[str, Any] = {}
    for line in stdout.splitlines():
        name, separator, value = line.partition("=")
        if not separator:
            continue
        name = name.strip()
        value = value.strip()
        if name in _INT_TELEMETRY:
            try:
                raw[_INT_TELEMETRY[name]] = int(value)
            except ValueError:
                raw[_INT_TELEMETRY[name]] = None
        elif name in _FLAG_TELEMETRY:
            raw[_FLAG_TELEMETRY[name]] = value == "1"
        elif name == "AUDIO_PHYSICAL_UNDERRUNS":
            raw["physical_underruns"] = value
    result: dict[str, Any] = {
        "synthesis_cpu_100ns": None,
        "synthesis_cpu_available": raw.get("synthesis_cpu_available") is True,
        "worker_cpu_100ns": None,
        "worker_cpu_available": raw.get("worker_cpu_available") is True,
        "starvation_observations": raw.get("starvation_observations"),
        "observed_underruns": raw.get("observed_underruns"),
        "missing_samples_lower_bound": raw.get("missing_samples_lower_bound"),
        "clock_discontinuities": raw.get("clock_discontinuities"),
        # Exact audible glitch telemetry is unavailable by contract.  Never
        # substitute observed stream-coordinate gaps for physical underruns.
        "physical_underruns": "unavailable",
    }
    if result["synthesis_cpu_available"]:
        result["synthesis_cpu_100ns"] = raw.get("synthesis_cpu_100ns")
    if result["worker_cpu_available"]:
        result["worker_cpu_100ns"] = raw.get("worker_cpu_100ns")
    return result


def _as_ticks(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"observation field {field} must be a non-negative integer")
    return value


def evaluate_alignment(
    observation: Mapping[str, Any],
    song: Mapping[str, Any],
    display_frame_rate: int = DEFAULT_DISPLAY_FRAME_RATE,
) -> dict[str, Any]:
    """Compute absolute impulse/flash alignment from a real A/V observation."""
    if display_frame_rate <= 0 or display_frame_rate > 480:
        raise ValueError("display_frame_rate must be in 1..480")
    if observation.get("format") != OBSERVATION_FORMAT or observation.get(
        "version"
    ) != 1:
        raise ValueError("unexpected display observation format")
    if observation.get("sample_rate") != TICKS_PER_SECOND:
        raise ValueError("display observation must use the 48000 Hz timebase")
    events = song.get("events", [])
    impulses = observation.get("impulses")
    if not isinstance(impulses, list) or len(impulses) != len(events):
        raise ValueError("observation must contain one impulse per song event")
    tolerance = TICKS_PER_SECOND / display_frame_rate
    deltas: list[int] = []
    for index, (event, impulse) in enumerate(zip(events, impulses)):
        event_tick = _as_ticks(impulse.get("event_tick"), f"impulses[{index}].event_tick")
        audio_tick = _as_ticks(impulse.get("audio_tick"), f"impulses[{index}].audio_tick")
        display_tick = _as_ticks(
            impulse.get("display_tick"), f"impulses[{index}].display_tick"
        )
        if event_tick != int(event["tick"]):
            raise ValueError(
                f"impulse {index} event tick {event_tick} does not match song onset "
                f"{event['tick']}"
            )
        if index and (audio_tick <= previous_audio or display_tick <= previous_display):
            raise ValueError("observed audio and display onsets must be strictly increasing")
        previous_audio, previous_display = audio_tick, display_tick
        deltas.append(display_tick - audio_tick)
    maximum = max((abs(delta) for delta in deltas), default=0)
    return {
        "display_refresh_hz": display_frame_rate,
        "tolerance_ticks": tolerance,
        "deltas_ticks": deltas,
        "max_abs_delta_ticks": maximum,
        "within_one_displayed_frame": maximum <= tolerance,
        "impulse_count": len(deltas),
        "capture": dict(observation.get("capture", {})),
    }


def probe_environment() -> dict[str, Any]:
    """Record the real Windows audio/display environment, or say unavailable."""
    if os.name != "nt":
        return {
            "status": "unavailable",
            "reason": (
                "physical WASAPI/display evidence requires a Windows 11 desktop "
                f"session; this host reports os.name={os.name!r}"
            ),
            "os": None,
            "audio_endpoint": None,
            "display": None,
        }
    script = (
        "$ErrorActionPreference='Stop';"
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$audio=@(Get-CimInstance Win32_SoundDevice | "
        "Select-Object Name,Status,DeviceID);"
        "$video=@(Get-CimInstance Win32_VideoController | "
        "Select-Object Name,DriverVersion,CurrentHorizontalResolution,"
        "CurrentVerticalResolution,CurrentRefreshRate);"
        "[pscustomobject]@{"
        "Caption=$os.Caption;Version=$os.Version;BuildNumber=$os.BuildNumber;"
        "Audio=$audio;Video=$video}|ConvertTo-Json -Depth 5 -Compress"
    )
    try:
        completed = hardware_evidence_host.run_windows_text(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return {
            "status": "unavailable",
            "reason": f"environment probe failed: {exc}",
            "os": None,
            "audio_endpoint": None,
            "display": None,
        }
    if completed.returncode != 0:
        return {
            "status": "unavailable",
            "reason": f"environment probe failed: {completed.stderr.strip()}",
            "os": None,
            "audio_endpoint": None,
            "display": None,
        }
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "status": "unavailable",
            "reason": f"environment probe returned invalid JSON: {exc}",
            "os": None,
            "audio_endpoint": None,
            "display": None,
        }
    audio = [
        {
            "name": str(item.get("Name") or ""),
            "status": str(item.get("Status") or ""),
            "device_id": str(item.get("DeviceID") or ""),
        }
        for item in value.get("Audio", [])
        if isinstance(item, Mapping)
    ]
    video = [
        {
            "name": str(item.get("Name") or ""),
            "driver_version": str(item.get("DriverVersion") or ""),
            "width": item.get("CurrentHorizontalResolution"),
            "height": item.get("CurrentVerticalResolution"),
            "refresh_hz": item.get("CurrentRefreshRate"),
        }
        for item in value.get("Video", [])
        if isinstance(item, Mapping)
    ]
    active_audio = [
        item for item in audio if item["status"].lower() in {"ok", "enabled"}
    ]
    if not active_audio or not video:
        return {
            "status": "unavailable",
            "reason": "no active WASAPI endpoint or display adapter was queried",
            "os": {
                "caption": str(value.get("Caption") or ""),
                "version": str(value.get("Version") or ""),
                "build": str(value.get("BuildNumber") or ""),
            },
            "audio_endpoint": None,
            "display": None,
        }
    return {
        "status": "available",
        "reason": "",
        "os": {
            "caption": str(value.get("Caption") or ""),
            "version": str(value.get("Version") or ""),
            "build": str(value.get("BuildNumber") or ""),
        },
        "audio_endpoint": active_audio,
        "display": video,
    }


def _convert_cpu_ms(value: int | None) -> float | None:
    if value is None:
        return None
    return value / 10_000.0


def build_evidence(
    *,
    package_bytes: bytes,
    song_bytes: bytes,
    source_head_sha: str | None = None,
    executable_bytes: bytes | None = None,
    telemetry_text: str = "",
    observation: Mapping[str, Any] | None = None,
    environment: Mapping[str, Any] | None = None,
    display_frame_rate: int = DEFAULT_DISPLAY_FRAME_RATE,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a physical evidence record without inventing any observation."""
    package = nwp.decode_package(package_bytes)
    payload = package.audio_song
    if payload is None or payload != song_bytes:
        raise ValueError("package AUDI payload does not match the selected song")
    song = decode_song(song_bytes)
    unavailable: list[dict[str, str]] = []
    unavailable_telemetry: list[dict[str, str]] = []
    environment = environment or probe_environment()
    if environment.get("status") != "available":
        unavailable.append(
            {
                "field": "environment",
                "reason": str(environment.get("reason") or "environment unavailable"),
            }
        )

    telemetry = parse_player_telemetry(telemetry_text)
    measurements: dict[str, Any] = {}
    for key in (
        "starvation_observations",
        "observed_underruns",
        "missing_samples_lower_bound",
        "clock_discontinuities",
    ):
        value = telemetry.get(key)
        if value is None:
            unavailable.append(
                {"field": f"measurements.{key}", "reason": "not emitted by player telemetry"}
            )
        else:
            measurements[key] = value
    if telemetry["synthesis_cpu_available"] and telemetry["synthesis_cpu_100ns"] is not None:
        measurements["synthesis_cpu_100ns"] = telemetry["synthesis_cpu_100ns"]
        measurements["synthesis_cpu_ms"] = _convert_cpu_ms(
            telemetry["synthesis_cpu_100ns"]
        )
    else:
        unavailable.append(
            {
                "field": "measurements.synthesis_cpu_100ns",
                "reason": "GetThreadTimes synthesis CPU telemetry unavailable",
            }
        )
    if telemetry["worker_cpu_available"] and telemetry["worker_cpu_100ns"] is not None:
        measurements["worker_cpu_100ns"] = telemetry["worker_cpu_100ns"]
        measurements["worker_cpu_ms"] = _convert_cpu_ms(telemetry["worker_cpu_100ns"])
    else:
        unavailable.append(
            {
                "field": "measurements.worker_cpu_100ns",
                "reason": "GetThreadTimes worker CPU telemetry unavailable",
            }
        )
    measurements["physical_underruns"] = "unavailable"
    # Exact audible glitch telemetry has no OS source. It is declared
    # unavailable rather than inferred from observed stream-coordinate gaps,
    # and it does not block the required alignment/CPU/underrun coverage.
    unavailable_telemetry.append(
        {
            "field": "measurements.physical_underruns",
            "reason": "exact audible glitch telemetry is unavailable; observed gaps are a lower bound",
        }
    )
    duration_seconds = song["duration_ticks"] / TICKS_PER_SECOND
    if "observed_underruns" in measurements and duration_seconds > 0:
        measurements["observed_underrun_rate_per_second"] = (
            measurements["observed_underruns"] / duration_seconds
        )

    alignment = None
    if observation is None:
        unavailable.append(
            {
                "field": "alignment",
                "reason": "no real displayed impulse/flash observation was supplied",
            }
        )
    else:
        alignment = evaluate_alignment(observation, song, display_frame_rate)
        if not alignment["within_one_displayed_frame"]:
            unavailable.append(
                {
                    "field": "alignment.within_one_displayed_frame",
                    "reason": (
                        "measured max |flash-audio| delta "
                        f"{alignment['max_abs_delta_ticks']} ticks exceeds one displayed "
                        f"frame ({alignment['tolerance_ticks']} ticks)"
                    ),
                }
            )

    verified = (
        environment.get("status") == "available"
        and alignment is not None
        and alignment["within_one_displayed_frame"]
        and not unavailable
    )
    if verified:
        if not source_head_sha or len(source_head_sha) != 40:
            raise ValueError("verified physical evidence requires a 40-hex source head")
    else:
        # A pending record must not claim to certify a particular source head.
        source_head_sha = None
    return {
        "format": FORMAT,
        "version": VERSION,
        "task": "NW-12",
        "implementation_base": IMPLEMENTATION_BASE,
        "nw11_incorporated": True,
        "provenance": dict(provenance or provenance_snapshot(include_source=False)),
        "source_head_sha": source_head_sha,
        "verification_status": "verified_physical" if verified else "pending_physical",
        "physical_evidence": "verified" if verified else "pending",
        "contract_evidence": "verified_by_pytest",
        "satisfies_completion": verified,
        "artifact": {
            "package_sha256": sha256_bytes(package_bytes),
            "executable_sha256": (
                sha256_bytes(executable_bytes) if executable_bytes else None
            ),
            "song_sha256": sha256_bytes(song_bytes),
            "song_bytes": len(song_bytes),
            "duration_ticks": song["duration_ticks"],
        },
        "environment": dict(environment),
        "required_environment": dict(REQUIRED_ENVIRONMENT),
        "protocol": dict(PROTOCOL, display_frame_rate=display_frame_rate),
        "measurements": measurements,
        "alignment": alignment,
        "unavailable": unavailable,
        "unavailable_telemetry": unavailable_telemetry,
        "policy": (
            "Physical WASAPI/display evidence binds only to the exact immutable "
            "artifact, song and source head recorded here. Missing observations stay "
            "pending; they are never inferred from offline or headless checks. "
            "Numeric telemetry is recorded only when emitted by the measured run."
        ),
    }


def verify_evidence(
    evidence: Mapping[str, Any],
    *,
    package_bytes: bytes | None = None,
    song_bytes: bytes | None = None,
    executable_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Validate evidence structure and artifact binding without hardware."""
    if evidence.get("format") != FORMAT or evidence.get("version") != VERSION:
        raise ValueError("unexpected NW-12 physical evidence format")
    if evidence.get("task") != "NW-12" or evidence.get("nw11_incorporated") is not True:
        raise ValueError("evidence must identify NW-12 on the NW-11 implementation base")
    status = evidence.get("verification_status")
    if status not in {"pending_physical", "verified_physical", "failed"}:
        raise ValueError("invalid verification status")
    artifact = evidence.get("artifact") or {}
    for field in ("package_sha256", "song_sha256"):
        value = artifact.get(field)
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError(f"invalid artifact.{field}")
    unavailable = evidence.get("unavailable") or []
    alignment = evidence.get("alignment")
    measurements = evidence.get("measurements") or {}
    if status == "verified_physical":
        if not isinstance(evidence.get("source_head_sha"), str) or len(
            evidence["source_head_sha"]
        ) != 40:
            raise ValueError("verified evidence requires a source head binding")
        if artifact.get("package_sha256") is None or artifact.get("song_sha256") is None:
            raise ValueError("verified evidence requires artifact hashes")
        if unavailable:
            raise ValueError("verified evidence cannot contain unavailable fields")
        if not alignment or alignment.get("within_one_displayed_frame") is not True:
            raise ValueError("verified evidence requires in-frame alignment")
        for field in (
            "synthesis_cpu_100ns",
            "worker_cpu_100ns",
            "observed_underruns",
            "clock_discontinuities",
        ):
            if measurements.get(field) is None:
                raise ValueError(f"verified evidence requires measurements.{field}")
        provenance = evidence.get("provenance") or {}
        bound_source = provenance.get("source_revision")
        if bound_source is not None and bound_source != evidence["source_head_sha"]:
            raise ValueError(
                "verified evidence source head does not match its provenance"
            )
    else:
        # Pending/failed evidence must not certify a source head and must say
        # exactly which observations are missing.
        if evidence.get("source_head_sha") is not None:
            raise ValueError("pending evidence cannot bind a source head")
        if status == "pending_physical" and not unavailable:
            raise ValueError("pending evidence must list unavailable observations")
        if evidence.get("satisfies_completion") is not False:
            raise ValueError("pending/failed evidence cannot satisfy completion")
    if package_bytes is not None:
        if artifact.get("package_sha256") != sha256_bytes(package_bytes):
            raise ValueError("package bytes do not match the evidence binding")
    if song_bytes is not None:
        if artifact.get("song_sha256") != sha256_bytes(song_bytes):
            raise ValueError("song bytes do not match the evidence binding")
        if artifact.get("song_bytes") != len(song_bytes):
            raise ValueError("song length does not match the evidence binding")
    if executable_bytes is not None:
        if artifact.get("executable_sha256") != sha256_bytes(executable_bytes):
            raise ValueError("executable bytes do not match the evidence binding")
    return dict(evidence)


def mark_player_result(evidence: dict[str, Any], returncode: int) -> dict[str, Any]:
    """Attach the player exit and downgrade failed runs without inventing data."""
    evidence["player_returncode"] = returncode
    if returncode == 0:
        return evidence
    evidence["verification_status"] = "failed"
    evidence["physical_evidence"] = "failed"
    evidence["satisfies_completion"] = False
    evidence["source_head_sha"] = None
    evidence.setdefault("unavailable", []).append(
        {
            "field": "player.exit",
            "reason": f"audio-enabled player exited with code {returncode}",
        }
    )
    return evidence


def _artifact_identity(player: Path, package: Path, song: Path) -> dict[str, str]:
    return {"executable_sha256": sha256_file(player),
            "package_sha256": sha256_file(package), "song_sha256": sha256_file(song)}


def validate_physical_inputs(identity: Mapping[str, Any], artifact: Mapping[str, str],
                             provenance: Mapping[str, Any]) -> None:
    """Bind downloaded CI artifacts to their actual build checkout and bytes."""
    if identity.get("format") != "nodewerk-audio-physical-inputs" or identity.get("version") != 1:
        raise ValueError("invalid physical input manifest")
    if identity.get("artifact") != artifact:
        raise ValueError("physical input artifact SHA-256 mismatch")
    for field, key in (("source_commit", "source_revision"), ("source_tree", "source_tree")):
        value = identity.get(field)
        if (not isinstance(value, str) or len(value) != 40
                or any(c not in "0123456789abcdef" for c in value)
                or value != provenance.get(key)):
            raise ValueError(f"physical input {field} does not match the checked-out source")


def run_playback(*, player: Path, package: Path, song: Path, output: Path,
                 timeout: int = 120, identity_path: Path | None = None) -> dict[str, Any]:
    """Launch once while the operator records a simultaneous external A/V capture."""
    if os.name != "nt":
        raise RuntimeError("physical WASAPI/display evidence requires Windows")
    player, package, song = (p.resolve(strict=True) for p in (player, package, song))
    if output.exists():
        raise FileExistsError(output)
    import nw05_export
    payload, manifest = nw05_export.extract_embedded_package(player.read_bytes())
    if payload != package.read_bytes() or nwp.decode_package(payload).audio_song != song.read_bytes():
        raise ValueError("player embedded package/song does not match supplied artifacts")
    audio = json.loads(manifest).get("audio") or {}
    if audio.get("song_sha256") != sha256_file(song):
        raise ValueError("player audio manifest does not match supplied song")
    artifact = _artifact_identity(player, package, song)
    provenance = provenance_snapshot()
    if not provenance.get("tracked_files_clean") or not provenance.get("source_revision"):
        raise ValueError("physical playback requires a clean tracked source checkout")
    if identity_path is not None:
        validate_physical_inputs(json.loads(identity_path.read_text(encoding="utf-8")),
                                 artifact, provenance)
    environment = probe_environment()
    run_id = uuid.uuid4().hex
    print(f"Audio capture run ID: {run_id}", flush=True)
    completed = subprocess.run([str(player)], capture_output=True, text=True, timeout=timeout)
    if _artifact_identity(player, package, song) != artifact:
        raise ValueError("playback artifacts changed during execution")
    if provenance_snapshot() != provenance:
        raise ValueError("source checkout changed during execution")
    result = {"format": "nodewerk-audio-physical-run", "version": 1, "run_id": run_id,
              "artifact": artifact, "provenance": provenance, "environment": environment,
              "stdout": completed.stdout, "stderr": completed.stderr,
              "returncode": completed.returncode}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
    return result


def validate_capture_binding(run: Mapping[str, Any], observation: Mapping[str, Any],
                             audio_capture: Path, video_capture: Path) -> None:
    if run.get("format") != "nodewerk-audio-physical-run" or run.get("version") != 1:
        raise ValueError("invalid physical run format")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or len(run_id) != 32 or observation.get("run_id") != run_id:
        raise ValueError("observation does not identify the recorded playback run")
    capture = observation.get("capture") or {}
    if capture.get("audio_sha256") != sha256_file(audio_capture):
        raise ValueError("audio capture SHA-256 mismatch")
    if capture.get("video_sha256") != sha256_file(video_capture):
        raise ValueError("video capture SHA-256 mismatch")


def record(*, player: Path, package: Path, song: Path, output: Path,
           run_path: Path, observation_path: Path, audio_capture: Path,
           video_capture: Path, display_frame_rate: int = DEFAULT_DISPLAY_FRAME_RATE
           ) -> dict[str, Any]:
    """Bind a capture to the saved telemetry; never launch a second playback."""
    run = json.loads(run_path.read_text(encoding="utf-8"))
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    validate_capture_binding(run, observation, audio_capture, video_capture)
    if run.get("artifact") != _artifact_identity(player, package, song):
        raise ValueError("physical run artifact identity mismatch")
    provenance = run["provenance"]
    if not provenance.get("tracked_files_clean"):
        raise ValueError("physical run source checkout was not clean")
    environment = run["environment"]
    if not any(row.get("refresh_hz") == display_frame_rate
               for row in (environment.get("display") or [])):
        raise ValueError("declared refresh rate does not match the recorded display")
    observation["capture"] = dict(observation["capture"], run_id=run["run_id"],
                                  run_sha256=sha256_file(run_path))
    evidence = build_evidence(
        package_bytes=package.read_bytes(), song_bytes=song.read_bytes(),
        source_head_sha=provenance.get("source_revision"),
        executable_bytes=player.read_bytes(), telemetry_text=run["stdout"],
        observation=observation, environment=environment,
        display_frame_rate=display_frame_rate, provenance=provenance,
    )
    mark_player_result(evidence, run["returncode"])
    verify_evidence(evidence)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record_parser = sub.add_parser("record", help="record a physical observation")
    record_parser.add_argument("--player", type=Path, required=True)
    record_parser.add_argument("--package", type=Path, required=True)
    record_parser.add_argument("--song", type=Path, required=True)
    record_parser.add_argument("--output", type=Path, required=True)
    record_parser.add_argument("--run", type=Path, required=True)
    record_parser.add_argument("--observation", type=Path, required=True)
    record_parser.add_argument("--capture-audio", type=Path, required=True)
    record_parser.add_argument("--capture-video", type=Path, required=True)
    record_parser.add_argument("--display-frame-rate", type=int, default=DEFAULT_DISPLAY_FRAME_RATE)
    run_parser = sub.add_parser("run", help="launch a single physical playback while capturing A/V")
    for name in ("player", "package", "song", "output"):
        run_parser.add_argument("--" + name, type=Path, required=True)
    run_parser.add_argument("--timeout", type=int, default=120)
    run_parser.add_argument("--identity", type=Path, help="identity.json from the CI physical inputs artifact")
    verify_parser = sub.add_parser("verify", help="verify a recorded observation profile")
    verify_parser.add_argument("--evidence", type=Path, required=True)
    verify_parser.add_argument("--package", type=Path)
    verify_parser.add_argument("--song", type=Path)
    verify_parser.add_argument("--executable", type=Path)
    probe_parser = sub.add_parser("environment", help="print the detected environment")
    probe_parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "environment":
        environment = probe_environment()
        text = json.dumps(environment, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    if args.command == "run":
        result = run_playback(player=args.player, package=args.package, song=args.song,
                              output=args.output, timeout=args.timeout, identity_path=args.identity)
        return 0 if result["returncode"] == 0 else 1
    if args.command == "record":
        evidence = record(
            player=args.player,
            package=args.package,
            song=args.song,
            output=args.output,
            run_path=args.run, observation_path=args.observation,
            audio_capture=args.capture_audio, video_capture=args.capture_video,
            display_frame_rate=args.display_frame_rate,
        )
        print(json.dumps({"verification_status": evidence["verification_status"]}))
        return 0
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    verified = verify_evidence(
        evidence,
        package_bytes=args.package.read_bytes() if args.package else None,
        song_bytes=args.song.read_bytes() if args.song else None,
        executable_bytes=args.executable.read_bytes() if args.executable else None,
    )
    print(json.dumps({"valid": True, "verification_status": verified["verification_status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
