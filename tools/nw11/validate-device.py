#!/usr/bin/env python3
"""Launch the bundle on one physical Windows device and preserve its raw evidence."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nw11_bundle as bundle
import nw11_contract as contract
import nw11_measurements as measurements
import nw11_verify as verifier


class ValidationError(RuntimeError):
    def __init__(self, message: str, code: int = 4):
        super().__init__(message)
        self.code = code


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def filetime() -> int:
    return 116444736000000000 + time.time_ns() // 100


def qpc_ms() -> float:
    return time.perf_counter_ns() / 1_000_000


def checked_path(root: Path, manifest: dict, relative: str) -> Path:
    path = root / relative
    entry = next(item for item in manifest["artifacts"] if item["path"] == relative)
    if contract.sha256_file(path) != entry["sha256"]:
        raise ValidationError(f"Artifact changed before execution: {relative}", 6)
    return path


@contextlib.contextmanager
def presentmon(exe: Path, target: Path, output: Path, launch_id: str):
    command = [
        str(exe), "--process_name", target.name, "--session_name", f"NW11-{launch_id}",
        "--output_file", str(output / "presentmon.csv"), "--qpc_time_ms",
        "--v2_metrics", "--no_console_stats", "--terminate_on_proc_exit",
        "--timed", "100", "--terminate_after_timed",
    ]
    with (output / "presentmon.log").open("wb") as log:
        trace = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            time.sleep(1)
            if trace.poll() is not None:
                raise ValidationError("PresentMon ended before the measured process started")
            yield
            if trace.wait(timeout=105) != 0:
                raise ValidationError("PresentMon failed during collection")
        finally:
            if trace.poll() is None:
                trace.terminate()
                trace.wait(timeout=10)


def launch_renderer(root: Path, manifest: dict, settings: dict, decision: dict,
                    folder: Path, role: str, relative: str, *,
                    mode: str, capture: bool = False) -> dict:
    folder.mkdir(parents=True)
    executable = checked_path(root, manifest, relative)
    artifact = next(item for item in manifest["artifacts"] if item["path"] == relative)
    launch_id = uuid.uuid4().hex
    decision_path = folder / "capability-decision.json"
    write_json(decision_path, decision)
    capture_manifest = folder / "captures.json"
    timing = settings["protocol"]
    measured = mode == "warm"
    command = [
        str(executable), "--runs", "1",
        "--benchmark-json", str(folder / "report.json"),
        "--warmup-seconds", str(timing["warmup_seconds"] if measured else 0),
        "--run-seconds", str(timing["run_seconds"] if measured else 1),
        "--fixed-quality", settings["workload"]["quality_profile_id"],
        "--role", role, "--artifact-path", relative,
        "--launch-id", launch_id, "--commit", manifest["commit"],
        "--device-tier", decision["physical_device_tier"],
        "--device-key", decision["device_key"], "--environment-key", decision["environment_key"],
        "--adapter-luid", decision["adapter"]["adapter_luid"],
        "--capability-decision", str(decision_path),
        "--distribution-bytes", str(manifest["accounting"].get(f"{role}_distribution_bytes", 0)),
        "--shader-ids", ";".join(manifest["shader_inventory"]["roles"].get(role, [])),
    ]
    if capture:
        command += ["--capture-dir", str(folder), "--capture-manifest", str(capture_manifest)]
    if mode == "canonical_reference":
        command.append("--capture-display")
    monitor = (
        presentmon(checked_path(root, manifest, "runtime/presentmon.exe"), executable, folder, launch_id)
        if measured else contextlib.nullcontext()
    )
    with monitor, (folder / "renderer.log").open("wb") as log:
        start = filetime()
        start_qpc = qpc_ms()
        with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as player:
            timed_out = False
            try:
                code = player.wait(timeout=105 if measured else 40)
            except subprocess.TimeoutExpired:
                player.kill()
                code = player.wait(timeout=10)
                timed_out = True
            launch = {
                "launch_id": launch_id, "mode": mode, "role": role,
                "commit": manifest["commit"], "bundle_id": manifest["bundle_id"],
                "artifact_path": relative, "executable_sha256": artifact["sha256"],
                "process_id": player.pid, "start_filetime": start, "end_filetime": filetime(),
                "start_qpc_ms": start_qpc, "end_qpc_ms": qpc_ms(), "exit_code": code,
                "command": command, "timed_out": timed_out,
            }
            write_json(folder / "launch.json", launch)
            if timed_out or code != 0:
                raise ValidationError(f"{role} {mode} exited {code}; see {folder / 'renderer.log'}")
    return launch


def capture_release(root: Path, manifest: dict, output: Path) -> None:
    relative = "bin/canonical_release.exe"
    release = checked_path(root, manifest, relative)
    probe = checked_path(root, manifest, "bin/nw11_capability_probe.exe")
    output.mkdir()
    ticks = [*contract.REFERENCE_FIXED_TICKS, contract.LOOP_TICKS]
    images = []
    for tick in ticks:
        path = output / f"{tick}.rgba"
        command = [str(release), "--tick", str(tick), "--frames", "1"]
        start = filetime()
        with (output / f"{tick}.log").open("wb") as log, subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT
        ) as player:
            try:
                time.sleep(0.65)
                capture = subprocess.run([str(probe), "--capture-release-pid", str(player.pid), str(path)],
                                         check=False, timeout=8)
                exit_code = player.wait(timeout=10)
            except BaseException:
                if player.poll() is None:
                    player.kill()
                raise
        execution = {
            "commit": manifest["commit"], "bundle_id": manifest["bundle_id"],
            "executable_sha256": contract.sha256_file(release),
            "process_id": player.pid, "tick": tick, "command": command,
            "start_filetime": start, "end_filetime": filetime(),
            "player_exit_code": exit_code, "capture_exit_code": capture.returncode,
            "player_log_sha256": contract.sha256_file(output / f"{tick}.log"),
        }
        write_json(output / f"{tick}-execution.json", execution)
        if capture.returncode != 0 or exit_code != 0:
            raise ValidationError(f"Canonical release capture failed at tick {tick}")
        images.append({"tick": tick, "path": path.name, "sha256": contract.sha256_file(path),
                       "width": contract.OUTPUT_WIDTH, "height": contract.OUTPUT_HEIGHT,
                       "format": "rgba32f"})
    write_json(output / "captures.json", {"format": "nw11-capture-set", "version": 1,
                                        "fixed_ticks": ticks, "images": images})


def validate_device(root: Path, manifest: dict, output: Path, args: argparse.Namespace) -> int:
    probe = checked_path(root, manifest, "bin/nw11_capability_probe.exe")
    observation_path = output / "capability-snapshot.json"
    probe_command = [str(probe), "--json", str(observation_path)]
    if adapter := os.environ.get("NW11_ADAPTER_LUID"):
        probe_command += ["--adapter-luid", adapter]
    probe_start = filetime()
    with subprocess.Popen(probe_command) as process:
        try:
            probe_code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        probe_pid = process.pid
    write_json(output / "probe-execution.json", {
        "commit": manifest["commit"], "bundle_id": manifest["bundle_id"],
        "probe_sha256": contract.sha256_file(probe), "snapshot_sha256": contract.sha256_file(observation_path),
        "process_id": probe_pid, "start_filetime": probe_start, "end_filetime": filetime(),
        "exit_code": probe_code,
    })
    if probe_code != 0:
        result = measurements.load_json(observation_path)
        raise ValidationError(f"Capability probe failed: {result.get('initialization')}")
    observation = measurements.load_json(observation_path)
    common, optional = measurements.evaluate_profiles(root, manifest, observation)
    tier = common["physical_device_tier"]
    if not tier or (args.expected_device_tier and tier != args.expected_device_tier):
        raise ValidationError(f"Physical board identity {common['device_key']} does not match the target")
    write_json(output / "common-eligibility.json", common)
    write_json(output / "eligibility.json", optional)
    if args.verify_only:
        write_summary(output, manifest, "verification_only", 0, device_tier=tier)
        return 0
    if common["status"] != "eligible":
        raise ValidationError(f"Required common runtime/capability is {common['status']}: {common['diagnostics']}",
                              11 if common["status"] == "pending" else 4)
    if optional["status"] not in {"eligible", "pending", "unsupported"}:
        raise ValidationError(f"Optional capability evaluation failed: {optional['diagnostics']}")
    measurements.validate_release_adapter(observation)
    settings = measurements.load_json(root / manifest["experiment_definition"]["path"])
    roles = measurements.role_paths(manifest, optional)
    for role, relative in roles.items():
        directory = output / "raw" / role
        directory.mkdir(parents=True)
        decision = optional if role == "variant" else common
        cache_record = directory / "cache-preparation.json"
        reset_started = filetime()
        reset = subprocess.run([str(probe), "--json", str(cache_record), "--reset-driver-cache",
                                "--adapter-luid", decision["adapter"]["adapter_luid"]],
                               check=False, timeout=60)
        write_json(directory / "cache-execution.json",
                   {"probe_sha256": contract.sha256_file(probe), "start_filetime": reset_started,
                    "end_filetime": filetime(), "exit_code": reset.returncode,
                    "result_sha256": contract.sha256_file(cache_record)})
        launch_renderer(root, manifest, settings, decision, directory / "startup",
                        role, relative, mode="startup")
        for index in range(1, settings["protocol"]["runs"] + 1):
            launch_renderer(root, manifest, settings, decision,
                            directory / f"run-{index}", role, relative, mode="warm",
                            capture=index == settings["protocol"]["runs"])
    launch_renderer(root, manifest, settings, common, output / "canonical-reference",
                    "canonical_reference", "bin/canonical_reference.exe",
                    mode="canonical_reference", capture=True)
    capture_release(root, manifest, output / "canonical-release")

    record = measurements.collect(root, manifest, output, "evidence.json")
    write_json(output / "evidence.json", record)
    code = verifier.main(["evidence", "--bundle-root", str(root), "--evidence", str(output / "evidence.json"),
                          "--expected-commit", manifest["commit"], "--require-accepted",
                          "--json-out", str(output / "decisions.json")])
    result = {0: "verified", 3: "unsupported", 10: "rejected", 11: "pending"}.get(code, "failed")
    write_summary(output, manifest, result, code, device_tier=tier,
                  device_key=common["device_key"], environment_key=common["environment_key"],
                  evidence="evidence.json", decisions="decisions.json")
    return code


def write_summary(output: Path, manifest: dict, status: str, exit_code: int, **details) -> None:
    result = {
        **{key: manifest[key] for key in ("bundle_id", "commit", "experiment_id", "profile_id") if key in manifest},
        "status": status, "exit_code": exit_code, "completion_satisfied": False, **details,
    }
    write_json(output / "summary.json", result)
    (output / "summary.txt").write_text(
        "\n".join(f"{key}: {value}" for key, value in result.items()) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--expected-commit")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-device-tier")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root, output = args.bundle_root.resolve(), args.output.resolve()
    manifest: dict = {}
    try:
        if output == root or root in output.parents:
            raise ValidationError("Evidence must be written outside the bundle", 6)
        manifest = bundle.verify_bundle(root, expected_commit=args.expected_commit)["manifest"]
        if args.dry_run:
            write_summary(output, manifest, "dry_run", 0)
            return 0
        return validate_device(root, manifest, output, args)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, KeyError) as exc:
        code = getattr(exc, "code", 4)
        write_summary(output, manifest, "pending" if code == 11 else "failed", code, reason=str(exc))
        print(f"[NW11-VALIDATION-ERR] {exc}", file=sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
