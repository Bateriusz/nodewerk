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
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from fractions import Fraction
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import hardware_evidence_host  # noqa: E402
import nwp_package as nwp  # noqa: E402
from audio_song import compile_song, decode_song  # noqa: E402

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
        "AUDIO_PLAYBACK_EVIDENCE_V1",
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
    "display": "real windowed Present on the unambiguous monitor at its recorded refresh",
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
    reason: str, *, verified: Mapping[str, Any] | None = None,
    playback_artifact_matches: bool = False,
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
        reference["status"] = ("verified" if playback_artifact_matches
                               else "verified_production_reference")
        reference["evidence"] = dict(verified)
    return reference


@dataclass(frozen=True)
class ArchiveFiles:
    """Original inputs alongside the report, or explicitly selected by the operator."""
    run: Path
    observation: Path
    audio: Path
    video: Path

    def _paths(self) -> dict[str, Path]:
        return {"run": self.run, "observation": self.observation,
                "audio": self.audio, "video": self.video}

    def complete(self) -> bool:
        return all(path.is_file() for path in self._paths().values())

    def relative_to(self, report: Path) -> dict[str, str] | None:
        root = report.parent.resolve()
        try:
            return {name: path.resolve().relative_to(root).as_posix()
                    for name, path in self._paths().items()}
        except ValueError:
            return None

    @classmethod
    def from_report(cls, report: Path, evidence: Mapping[str, Any]) -> "ArchiveFiles | None":
        files = evidence.get("archive_files")
        if files is None:
            return None
        if not isinstance(files, Mapping) or set(files) != {"run", "observation", "audio", "video"}:
            raise ValueError("physical evidence requires four archive file references")
        root = report.parent.resolve()
        paths = {}
        for name, relative in files.items():
            if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
                raise ValueError("archive file references must remain relative to the report")
            path = PurePosixPath(relative)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise ValueError("archive file references must remain relative to the report")
            resolved = (root / Path(*path.parts)).resolve()
            if not resolved.is_relative_to(root):
                raise ValueError("archive file references must remain relative to the report")
            paths[name] = resolved
        return cls(**paths)


@dataclass(frozen=True)
class _CaptureArchive:
    run: Mapping[str, Any]
    run_sha256: str
    observation: Mapping[str, Any]
    observation_sha256: str

    def bound_observation(self) -> dict[str, Any]:
        capture = dict(self.observation["capture"], run_id=self.run["run_id"],
                       run_sha256=self.run_sha256, observation_sha256=self.observation_sha256)
        return dict(self.observation, capture=capture)


def _read_capture_archive(files: ArchiveFiles) -> _CaptureArchive:
    raw_run = files.run.read_bytes()
    raw_observation = files.observation.read_bytes()
    run, observation = json.loads(raw_run), json.loads(raw_observation)
    validate_capture_binding(run, observation, files.audio, files.video)
    return _CaptureArchive(run, sha256_bytes(raw_run), observation, sha256_bytes(raw_observation))


def load_verified_evidence(
    path: str | os.PathLike[str] | None = None,
    *,
    package_bytes: bytes | None = None,
    song_bytes: bytes | None = None,
    executable_bytes: bytes | None = None,
    source_head_sha: str | None = None,
) -> dict[str, Any] | None:
    """Load a verified physical record, matching the selected artifact if given."""
    candidate = path or os.environ.get("NW12_PHYSICAL_EVIDENCE", "")
    if not candidate:
        return None
    evidence_path = Path(candidate)
    if not evidence_path.is_file():
        return None
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence = verify_evidence(
        evidence, package_bytes=package_bytes, song_bytes=song_bytes,
        executable_bytes=executable_bytes, evidence_path=evidence_path,
    )
    if (evidence.get("verification_status") != "verified_physical"
            or (source_head_sha is not None and evidence["source_head_sha"] != source_head_sha)):
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
    seen = set()
    for line in stdout.splitlines():
        name, separator, value = line.partition("=")
        if not separator:
            continue
        name = name.strip()
        value = value.strip()
        if name in _INT_TELEMETRY:
            raw[_INT_TELEMETRY[name]] = (
                int(value) if name not in seen and re.fullmatch(r"[0-9]{1,20}", value)
                and int(value) < 1 << 64 else None
            )
        elif name in _FLAG_TELEMETRY:
            raw[_FLAG_TELEMETRY[name]] = name not in seen and value == "1"
        elif name == "AUDIO_PHYSICAL_UNDERRUNS":
            raw["physical_underruns"] = value
        seen.add(name)
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
    display_frame_rate: int | Fraction = DEFAULT_DISPLAY_FRAME_RATE,
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
    rate = Fraction(display_frame_rate)
    tolerance = Fraction(TICKS_PER_SECOND, 1) / rate
    deltas: list[int] = []
    normalized = []
    for index, (event, impulse) in enumerate(zip(events, impulses)):
        if not isinstance(impulse, Mapping):
            raise ValueError("each impulse must contain its measured onsets")
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
        normalized.append({"event_tick": event_tick, "audio_tick": audio_tick,
                           "display_tick": display_tick})
        deltas.append(display_tick - audio_tick)
    maximum = max((abs(delta) for delta in deltas), default=0)
    return {
        "display_refresh_hz": float(rate),
        "tolerance_ticks": float(tolerance),
        "sample_rate": TICKS_PER_SECOND,
        "impulses": normalized,
        "deltas_ticks": deltas,
        "max_abs_delta_ticks": maximum,
        "within_one_displayed_frame": maximum <= tolerance,
        "impulse_count": len(deltas),
        "capture": dict(observation.get("capture", {})),
    }


def _valid_hash(value: Any, length: int = 64) -> bool:
    return (isinstance(value, str) and len(value) == length
            and re.fullmatch(r"[0-9a-f]+", value) is not None)


def _positive_int(value: Any, maximum: int = (1 << 64) - 1) -> bool:
    return type(value) is int and 1 <= value <= maximum


def _refresh(display: Mapping[str, Any]) -> Fraction:
    numerator, denominator = display.get("refresh_numerator"), display.get("refresh_denominator")
    if not _positive_int(numerator, (1 << 32) - 1) or not _positive_int(denominator, (1 << 32) - 1):
        raise ValueError("invalid display refresh ratio")
    rate = Fraction(numerator, denominator)
    if not 1 <= rate <= 480:
        raise ValueError("display refresh must be in 1..480 Hz")
    return rate


def _windows_11(os_info: Any) -> bool:
    return (isinstance(os_info, Mapping)
            and re.fullmatch(r"[0-9]{5,7}", str(os_info.get("build", ""))) is not None
            and int(os_info["build"]) >= 22000
            and type(os_info.get("product_type")) is int and os_info["product_type"] == 1)


def probe_environment() -> dict[str, Any]:
    """Query host identity only. Playback determines the endpoint and display."""
    result: dict[str, Any] = {
        "status": "unavailable",
        "reason": "physical WASAPI/display evidence requires a Windows 11 desktop session",
        "os": None, "audio_endpoint": None, "display": None,
    }
    if os.name != "nt":
        return result
    script = (
        "$ErrorActionPreference='Stop';"
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "[pscustomobject]@{version=$os.Version;build=$os.BuildNumber;"
        "product_type=$os.ProductType}|ConvertTo-Json -Compress"
    )
    try:
        completed = hardware_evidence_host.run_windows_text(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=30,
        )
        if completed.returncode == 0:
            os_info = json.loads(completed.stdout)
            if _windows_11(os_info):
                result["os"] = os_info
                result["reason"] = "selected outputs are available only from the playback process"
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return result


def playback_environment(host: Mapping[str, Any], stdout: str,
                         returncode: int | None) -> dict[str, Any]:
    result = dict(host)
    prefix = "AUDIO_PLAYBACK_EVIDENCE_V1="
    rows = [line[len(prefix):] for line in stdout.splitlines() if line.startswith(prefix)]
    result.update(status="unavailable", audio_endpoint=None, display=None,
                  reason="successful full playback did not identify one stable display and endpoint")
    if type(returncode) is not int or returncode != 0 or not _windows_11(host.get("os")) or len(rows) != 1:
        return result
    cols = rows[0].split(",")
    if (len(cols) != 6 or not all(_valid_hash(item) for item in cols[:2])
            or not all(re.fullmatch(r"[0-9]{1,20}", item) for item in cols[2:])):
        return result
    numerator, denominator, presents, final_tick = map(int, cols[2:])
    display = {"id_sha256": cols[1], "refresh_numerator": numerator,
               "refresh_denominator": denominator, "present_count": presents}
    try:
        _refresh(display)
    except ValueError:
        return result
    if not _positive_int(presents) or not _positive_int(final_tick):
        return result
    result.update(status="available", reason="", display=display,
                  audio_endpoint={"id_sha256": cols[0], "data_flow": "eRender", "role": "eConsole",
                                  "clock": "wasapi_endpoint", "final_tick": final_tick})
    return result


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
    provenance: Mapping[str, Any] | None = None,
    build_identity: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    run_sha256: str | None = None,
    player_returncode: int | None = None,
    _archive: _CaptureArchive | None = None,
    _archive_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a physical record; unarchived findings remain diagnostic only."""
    if _archive is not None:
        observation = _archive.bound_observation()
        run_id, run_sha256 = _archive.run["run_id"], _archive.run_sha256
    package = nwp.decode_package(package_bytes)
    payload = package.audio_song
    if payload is None or payload != song_bytes:
        raise ValueError("package AUDI payload does not match the selected song")
    song = decode_song(song_bytes)
    unavailable: list[dict[str, str]] = []
    unavailable_telemetry: list[dict[str, str]] = []
    environment = playback_environment(environment or probe_environment(), telemetry_text,
                                       player_returncode)
    artifact = {
        "package_sha256": sha256_bytes(package_bytes),
        "executable_sha256": sha256_bytes(executable_bytes) if executable_bytes else None,
        "song_sha256": sha256_bytes(song_bytes), "song_bytes": len(song_bytes),
        "duration_ticks": song["duration_ticks"],
    }
    provenance = dict(provenance or provenance_snapshot(include_source=False))
    if build_identity is None:
        unavailable.append({"field": "build_identity",
                            "reason": "no independently recorded artifact build identity was supplied"})
    else:
        validate_physical_inputs(build_identity, _identity_hashes(artifact), provenance)
    if _archive is None or _archive_paths is None:
        unavailable.append({"field": "archive_files",
                            "reason": "the original run, observation JSON and both media must be retained beside the report"})
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
    elif environment["status"] == "available":
        alignment = evaluate_alignment(observation, song, _refresh(environment["display"]))
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
    evidence = {
        "format": FORMAT,
        "version": VERSION,
        "task": "NW-12",
        "implementation_base": IMPLEMENTATION_BASE,
        "nw11_incorporated": True,
        "provenance": provenance,
        "source_head_sha": source_head_sha if verified else None,
        "build_identity": build_identity,
        "run_id": run_id,
        "run_sha256": run_sha256,
        "archive_files": _archive_paths,
        "observation_sha256": _archive.observation_sha256 if _archive else None,
        "song": song,
        "verification_status": "verified_physical" if verified else "pending_physical",
        "physical_evidence": "verified" if verified else "pending",
        "contract_evidence": "verified_by_pytest",
        "satisfies_completion": verified,
        "artifact": artifact,
        "environment": dict(environment),
        "required_environment": dict(REQUIRED_ENVIRONMENT),
        "protocol": dict(PROTOCOL),
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

    if player_returncode is not None:
        mark_player_result(evidence, player_returncode)
    _validate_report(evidence)
    if verified:
        _verify_archive(evidence, _archive)
    return evidence


def _identity_hashes(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {key: artifact.get(key) for key in
            ("executable_sha256", "package_sha256", "song_sha256")}


def _verify_measured_evidence(evidence: Mapping[str, Any]) -> None:
    artifact = evidence["artifact"]
    provenance = evidence.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("verified evidence requires build provenance")
    source = evidence.get("source_head_sha")
    if (not _valid_hash(source, 40) or source != provenance.get("source_revision")
            or provenance.get("tracked_files_clean") is not True
            or provenance.get("nw11_incorporated") is not True
            or provenance.get("implementation_base") != IMPLEMENTATION_BASE
            or provenance.get("nw11_merge_revision") != IMPLEMENTATION_BASE
            or provenance.get("implementation_base_tree") != IMPLEMENTATION_BASE_TREE):
        raise ValueError("verified evidence requires a clean source head binding")
    digest_fields = _identity_hashes(artifact)
    if not all(_valid_hash(value) for value in digest_fields.values()):
        raise ValueError("verified evidence requires executable, package and song hashes")
    identity = evidence.get("build_identity")
    if not isinstance(identity, Mapping):
        raise ValueError("verified evidence requires the artifact build identity")
    validate_physical_inputs(identity, digest_fields, provenance)
    if (type(evidence.get("player_returncode")) is not int or evidence["player_returncode"] != 0
            or evidence.get("physical_evidence") != "verified"
            or evidence.get("satisfies_completion") is not True):
        raise ValueError("verified evidence requires successful playback and matching status")

    song = evidence.get("song")
    compiled = compile_song(song)
    if (digest_fields["song_sha256"] != sha256_bytes(compiled)
            or artifact.get("song_bytes") != len(compiled)
            or artifact.get("duration_ticks") != song["duration_ticks"]):
        raise ValueError("measured song schedule does not match artifact identity")

    environment = evidence.get("environment")
    if not isinstance(environment, Mapping):
        raise ValueError("verified evidence requires the active playback environment")
    endpoint = environment.get("audio_endpoint")
    display = environment.get("display")
    if (environment.get("status") != "available" or not _windows_11(environment.get("os"))
            or not isinstance(endpoint, Mapping) or not isinstance(display, Mapping)
            or not _valid_hash(endpoint.get("id_sha256"))
            or not _valid_hash(display.get("id_sha256"))
            or endpoint.get("data_flow") != "eRender" or endpoint.get("role") != "eConsole"
            or endpoint.get("clock") != "wasapi_endpoint"
            or not _positive_int(endpoint.get("final_tick"))
            or endpoint["final_tick"] < song["duration_ticks"]
            or not _positive_int(display.get("present_count"))):
        raise ValueError("verified evidence requires the active playback endpoint and display")
    protocol = evidence.get("protocol") or {}
    if (not isinstance(protocol, Mapping) or protocol.get("clock") != "wasapi_endpoint"
            or protocol.get("sample_rate") != TICKS_PER_SECOND):
        raise ValueError("verified evidence requires the fixed WASAPI timebase")

    alignment = evidence.get("alignment")
    if not isinstance(alignment, Mapping):
        raise ValueError("verified evidence requires measured alignment")
    capture = alignment.get("capture")
    run_id, run_sha256 = evidence.get("run_id"), evidence.get("run_sha256")
    observation_sha256 = evidence.get("observation_sha256")
    if (not _valid_hash(run_id, 32) or not _valid_hash(run_sha256)
            or (observation_sha256 is not None and not _valid_hash(observation_sha256))
            or not isinstance(capture, Mapping)
            or capture.get("run_id") != run_id or capture.get("run_sha256") != run_sha256
            or capture.get("observation_sha256") != observation_sha256
            or not all(_valid_hash(capture.get(f"{kind}_sha256")) for kind in ("audio", "video"))):
        raise ValueError("verified evidence requires the same run, original observation and both raw capture hashes")
    recomputed = evaluate_alignment(
        {"format": OBSERVATION_FORMAT, "version": 1, **alignment}, song, _refresh(display)
    )
    if alignment != recomputed or recomputed["within_one_displayed_frame"] is not True:
        raise ValueError("verified evidence requires measured in-frame alignment")

    m = evidence.get("measurements") or {}
    if (not isinstance(m, Mapping) or
            any(type(m.get(key)) is not int or not 0 <= m[key] < 1 << 64
                for key in _INT_TELEMETRY.values())):
        raise ValueError("verified evidence requires the actual non-negative integer telemetry")
    if m.get("physical_underruns") != "unavailable":
        raise ValueError("exact audible glitch telemetry must remain unavailable")
    if (m["synthesis_cpu_100ns"] > m["worker_cpu_100ns"]
            or m["observed_underruns"] > m["starvation_observations"]
            or m["observed_underruns"] > m["clock_discontinuities"]
            or m["observed_underruns"] > m["missing_samples_lower_bound"]
            or m.get("synthesis_cpu_ms") != _convert_cpu_ms(m["synthesis_cpu_100ns"])
            or m.get("worker_cpu_ms") != _convert_cpu_ms(m["worker_cpu_100ns"])
            or m.get("observed_underrun_rate_per_second") !=
            m["observed_underruns"] / (song["duration_ticks"] / TICKS_PER_SECOND)):
        raise ValueError("reported CPU or underrun summary contradicts run telemetry")


def _validate_report(
    evidence: Mapping[str, Any],
    *,
    package_bytes: bytes | None = None,
    song_bytes: bytes | None = None,
    executable_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Validate the persisted report, but do not certify its external inputs."""
    if (not isinstance(evidence, Mapping) or evidence.get("format") != FORMAT
            or type(evidence.get("version")) is not int or evidence["version"] != VERSION):
        raise ValueError("unexpected NW-12 physical evidence format")
    if (evidence.get("task") != "NW-12" or evidence.get("nw11_incorporated") is not True
            or evidence.get("implementation_base") != IMPLEMENTATION_BASE):
        raise ValueError("evidence must identify NW-12 on the NW-11 implementation base")
    status = evidence.get("verification_status")
    if status not in {"pending_physical", "verified_physical", "failed"}:
        raise ValueError("invalid verification status")
    artifact = evidence.get("artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("evidence requires artifact identities")
    for field, value in _identity_hashes(artifact).items():
        if value is not None and not _valid_hash(value):
            raise ValueError(f"invalid artifact.{field}")
    unavailable = evidence.get("unavailable") or []
    if status == "verified_physical":
        if unavailable:
            raise ValueError("verified evidence cannot contain unavailable fields")
        _verify_measured_evidence(evidence)
    else:
        if evidence.get("source_head_sha") is not None:
            raise ValueError("pending evidence cannot bind a source head")
        if status == "pending_physical" and not unavailable:
            raise ValueError("pending evidence must list unavailable observations")
        if evidence.get("satisfies_completion") is not False:
            raise ValueError("pending/failed evidence cannot satisfy completion")
        if evidence.get("physical_evidence") != ("pending" if status == "pending_physical" else "failed"):
            raise ValueError("physical evidence status fields disagree")
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


def verify_evidence(
    evidence: Mapping[str, Any], *,
    package_bytes: bytes | None = None, song_bytes: bytes | None = None,
    executable_bytes: bytes | None = None, evidence_path: Path | None = None,
    archive_files: ArchiveFiles | None = None,
) -> dict[str, Any]:
    """Validate stored facts and certify only an independently rechecked archive."""
    result = _validate_report(evidence, package_bytes=package_bytes, song_bytes=song_bytes,
                              executable_bytes=executable_bytes)
    if result["verification_status"] != "verified_physical":
        return result
    if archive_files is None and evidence_path is not None:
        archive_files = ArchiveFiles.from_report(evidence_path, result)
    if result.get("observation_sha256") is not None and archive_files is not None and archive_files.complete():
        try:
            snapshot = _read_capture_archive(archive_files)
        except FileNotFoundError:
            pass  # The archive became unavailable between discovery and reading.
        else:
            _verify_archive(result, snapshot)
            return result
    # Do not mutate the historical file or let a detached report certify itself.
    return dict(result, verification_status="pending_physical", physical_evidence="pending",
                satisfies_completion=False, source_head_sha=None, unavailable=[
                    {"field": "archive_files",
                     "reason": "original run, observation JSON or capture media are unavailable"},
                ])


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
    if (not isinstance(identity, Mapping)
            or identity.get("format") != "nodewerk-audio-physical-inputs"
            or type(identity.get("version")) is not int or identity["version"] != 1):
        raise ValueError("invalid physical input manifest")
    if (not all(_valid_hash(value) for value in _identity_hashes(artifact).values())
            or identity.get("artifact") != artifact):
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
    identity = (json.loads(identity_path.read_text(encoding="utf-8"))
                if identity_path is not None else None)
    if identity is not None:
        validate_physical_inputs(identity, artifact, provenance)
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", IMPLEMENTATION_BASE, "HEAD"],
            cwd=ROOT, capture_output=True,
        )
        if ancestry.returncode != 0:
            raise ValueError("build source must contain the NW-11 implementation base")
    host = probe_environment()
    run_id = uuid.uuid4().hex
    print(f"Audio capture run ID: {run_id}", flush=True)
    completed = subprocess.run([str(player), "--audio-evidence"], capture_output=True,
                               timeout=timeout)
    if _artifact_identity(player, package, song) != artifact:
        raise ValueError("playback artifacts changed during execution")
    if provenance_snapshot() != provenance:
        raise ValueError("source checkout changed during execution")
    result = {"format": "nodewerk-audio-physical-run", "version": 1, "run_id": run_id,
              "artifact": artifact, "provenance": provenance, "build_identity": identity,
              "environment": host,
              "stdout": hardware_evidence_host.decode_windows_output(completed.stdout),
              "stderr": hardware_evidence_host.decode_windows_output(completed.stderr),
              "returncode": completed.returncode}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
    return result


def validate_capture_binding(run: Mapping[str, Any], observation: Mapping[str, Any],
                             audio_capture: Path, video_capture: Path) -> None:
    if (not isinstance(run, Mapping) or run.get("format") != "nodewerk-audio-physical-run"
            or type(run.get("version")) is not int or run["version"] != 1):
        raise ValueError("invalid physical run format")
    run_id = run.get("run_id")
    if not _valid_hash(run_id, 32) or not isinstance(observation, Mapping) or observation.get("run_id") != run_id:
        raise ValueError("observation does not identify the recorded playback run")
    capture = observation.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("observation is missing raw capture hashes")
    audio_hash = sha256_file(audio_capture)
    if capture.get("audio_sha256") != audio_hash:
        raise ValueError("audio capture SHA-256 mismatch")
    video_hash = audio_hash if audio_capture.resolve() == video_capture.resolve() else sha256_file(video_capture)
    if capture.get("video_sha256") != video_hash:
        raise ValueError("video capture SHA-256 mismatch")


def record(*, player: Path, package: Path, song: Path, output: Path,
           run_path: Path, observation_path: Path, audio_capture: Path,
           video_capture: Path
           ) -> dict[str, Any]:
    """Bind a capture to the saved telemetry; never launch a second playback."""
    files = ArchiveFiles(run_path, observation_path, audio_capture, video_capture)
    archive = _read_capture_archive(files)
    run = archive.run
    if run.get("artifact") != _artifact_identity(player, package, song):
        raise ValueError("physical run artifact identity mismatch")
    provenance = run["provenance"]
    if not provenance.get("tracked_files_clean"):
        raise ValueError("physical run source checkout was not clean")
    relative_paths = files.relative_to(output)
    if relative_paths is not None:
        ArchiveFiles.from_report(output, {"archive_files": relative_paths})
    evidence = build_evidence(
        package_bytes=package.read_bytes(), song_bytes=song.read_bytes(),
        source_head_sha=provenance.get("source_revision"),
        executable_bytes=player.read_bytes(), telemetry_text=run["stdout"],
        environment=run["environment"], provenance=provenance,
        build_identity=run.get("build_identity"), player_returncode=run["returncode"],
        _archive=archive, _archive_paths=relative_paths,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return evidence


def _verify_archive(evidence: Mapping[str, Any], archive: _CaptureArchive) -> None:
    run = archive.run
    if (evidence.get("run_sha256") != archive.run_sha256
            or evidence.get("run_id") != run.get("run_id")
            or evidence.get("build_identity") != run.get("build_identity")
            or evidence.get("provenance") != run.get("provenance")
            or _identity_hashes(evidence["artifact"]) != run.get("artifact")):
        raise ValueError("archived run does not match the verified evidence")
    if evidence.get("observation_sha256") != archive.observation_sha256:
        raise ValueError("original observation JSON SHA-256 mismatch")
    environment = playback_environment(run["environment"], run["stdout"], run["returncode"])
    if environment != evidence["environment"]:
        raise ValueError("archived run does not match the active playback environment")
    measured = evaluate_alignment(archive.bound_observation(), evidence["song"],
                                  _refresh(environment["display"]))
    if measured != evidence["alignment"]:
        raise ValueError("recorded alignment does not match the original measured onsets")
    telemetry = parse_player_telemetry(run["stdout"])
    if any(telemetry[key] != evidence["measurements"][key] for key in _INT_TELEMETRY.values()):
        raise ValueError("archived run does not match the measured audio telemetry")


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
    for name in ("run", "observation", "capture-audio", "capture-video"):
        verify_parser.add_argument("--" + name, type=Path)
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
        )
        print(json.dumps({"verification_status": evidence["verification_status"]}))
        return 0
    archive = (args.run, args.observation, args.capture_audio, args.capture_video)
    if any(archive) and not all(archive):
        raise ValueError("verify requires --run, --observation, --capture-audio and --capture-video together")
    evidence = json.loads(args.evidence.read_bytes())
    verified = verify_evidence(
        evidence,
        package_bytes=args.package.read_bytes() if args.package else None,
        song_bytes=args.song.read_bytes() if args.song else None,
        executable_bytes=args.executable.read_bytes() if args.executable else None,
        evidence_path=args.evidence, archive_files=ArchiveFiles(*archive) if all(archive) else None,
    )
    print(json.dumps({"valid": True, "verification_status": verified["verification_status"],
                      "archive_rechecked": verified["verification_status"] == "verified_physical"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
