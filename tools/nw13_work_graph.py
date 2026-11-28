#!/usr/bin/env python3
"""Stage, verify, or run one immutable NW-13 Work Graphs validation bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile

# The packaged runtime runs isolated; only sibling verified modules are used.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import hardware_evidence_devices as devices
import hardware_evidence_host as host
import nw11_pe_dependencies as pe
import nw11_pack_python_runtime as runtime
import nw13_work_graph_protocol as protocol
import validation_gpu_telemetry as telemetry

EXPERIMENT = "nw13_work_graph.exe"
PROBE = "nw11_capability_probe.exe"
BASELINE = "particle_routing_cs.dxil"
OPTIONAL = "particle_routing_lib.dxil"
NATIVE = (EXPERIMENT, BASELINE, OPTIONAL)
APPLICATIONS = (EXPERIMENT,)
MANIFEST = "bundle-manifest.json"
RUNTIME = "runtime/python-runtime.zip"
WARP_RECORD = "qualification/work-graph-warp.json"
LAUNCHER = "run-work-graphs.ps1"
TRUST_FORMAT = "nw13.work-graph-ci-trust"
FORMAT = "nw13.work-graph-physical-bundle"
TOOLS = (
    "nw13_work_graph.py", "nw13_work_graph_protocol.py", "nw11_pe_dependencies.py",
    "nw11_pack_python_runtime.py", "nw11_contract.py", "hardware_evidence_devices.py",
    "hardware_evidence_host.py", "validation_gpu_telemetry.py",
)
SOURCES = (
    ".github/workflows/nw13-procedural-film.yml",
    "benchmarks/toolchain-manifest.v1.json", "src/nw13/CMakeLists.txt",
    "src/nw13/work_graph.cpp", "shaders/nw13/particle_routing.hlsl",
    "src/nw11/CMakeLists.txt", "src/nw11/capability_probe.cpp",
    "src/nw11/capture_release_frame.h",
)
MEASURED_SOURCES = (
    *(f"tools/{name}" for name in TOOLS), "tools/nw13_run_work_graphs.ps1", *SOURCES,
)
MEMBERS = (
    *(f"bin/{name}" for name in (*NATIVE, PROBE)), *(f"tools/{name}" for name in TOOLS),
    *(f"source/{name.removeprefix('.')}" for name in SOURCES), LAUNCHER, RUNTIME, WARP_RECORD,
)
DIRECTORIES = {parent.as_posix() for name in MEMBERS for parent in PurePosixPath(name).parents} - {"."}
SHA = re.compile(r"[0-9a-f]{64}")
OID = re.compile(r"[0-9a-f]{40}")
LUID = re.compile(r"[0-9a-fA-F]{8}:[0-9a-fA-F]{8}")
MAX_RECORD_BYTES = 1024 * 1024


class EvidenceError(ValueError):
    """Bundle, host, or measured input is not adequately bound."""


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(path: Path) -> dict:
    return {"sha256": sha256(path), "bytes": path.stat().st_size}


def write_json(path: Path, record: dict) -> None:
    path.write_text(json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def _record(raw: bytes) -> dict:
    if not 0 < len(raw) <= MAX_RECORD_BYTES:
        raise EvidenceError("missing or oversized evidence record")

    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EvidenceError("duplicate evidence keys")
            result[key] = value
        return result

    def reject_nonfinite(value):
        raise EvidenceError(f"non-finite JSON constant {value}")

    payload = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_nonfinite)
    if not isinstance(payload, dict):
        raise EvidenceError("evidence must be a JSON object")
    return payload


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, check=True,
                          capture_output=True, timeout=30).stdout.strip()


def _source_identity(repo: Path, revision: str) -> dict:
    if not OID.fullmatch(revision) or _git(repo, "rev-parse", "HEAD") != revision:
        raise EvidenceError("checkout does not match the intended CI revision")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=no"):
        raise EvidenceError("measured checkout has tracked modifications")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    if not OID.fullmatch(tree):
        raise EvidenceError("measured source tree is not an exact Git object ID")
    tracked = {}
    for name in MEASURED_SOURCES:
        blob = _git(repo, "rev-parse", f"{revision}:{name}")
        if not OID.fullmatch(blob):
            raise EvidenceError(f"measured source is not tracked at the exact revision: {name}")
        tracked[name] = {"git_blob": blob, **identity(repo / name)}
    return {"commit": revision, "tree": tree, "tracked": tracked}


def _source_member(name: str) -> str:
    if name == "tools/nw13_run_work_graphs.ps1":
        return LAUNCHER
    return name if name.startswith("tools/") else f"source/{name.removeprefix('.')}"


def _pe_dependencies(root: Path) -> dict:
    result = pe.collect([root / "bin" / EXPERIMENT, root / "bin" / PROBE])
    imports = {item["path"]: item["dlls"] for item in result["executables"]}
    if not all(imports.values()):
        raise EvidenceError("native PE dependency observations are empty")
    return imports


def _paths(root: Path) -> set[str]:
    if (not root.is_dir() or root.is_symlink()
            or getattr(root.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise EvidenceError("bundle directory is missing")
    names = set()
    folded = set()
    def walk_error(exc: OSError) -> None:
        raise exc

    for dirname, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in (*dirs, *files):
            path = Path(dirname) / name
            status = path.lstat()
            if (path.is_symlink() or getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise EvidenceError("bundle may not include links or reparse points")
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in folded:
                raise EvidenceError("bundle contains aliased names")
            folded.add(relative.casefold())
            if name in dirs and relative not in DIRECTORIES:
                raise EvidenceError("bundle contains an unexpected directory")
            if name in files:
                if not stat.S_ISREG(status.st_mode):
                    raise EvidenceError("bundle contains a nonregular file")
                names.add(relative)
    return names


def verify_bundle(root: Path, trust: dict, revision: str) -> dict:
    if (trust.get("format") != TRUST_FORMAT or type(trust.get("version")) is not int or trust["version"] != 1
            or trust.get("commit") != revision or not OID.fullmatch(revision)
            or not OID.fullmatch(str(trust.get("tree", "")))
            or not SHA.fullmatch(str(trust.get("manifest_sha256", "")))):
        raise EvidenceError("missing or mismatched independent CI trust record")
    if _paths(root) != set(MEMBERS) | {MANIFEST}:
        raise EvidenceError("missing or unexpected bundle members")
    path = root / MANIFEST
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != trust["manifest_sha256"]:
        raise EvidenceError("bundle manifest does not match trusted CI SHA-256")
    manifest = _record(raw)
    source, files = manifest.get("source"), manifest.get("files")
    if (manifest.get("format") != FORMAT or type(manifest.get("version")) is not int or manifest["version"] != 1
            or manifest.get("criterion") != protocol.CRITERION or not isinstance(source, dict)
            or source.get("commit") != revision or source.get("tree") != trust["tree"]
            or not isinstance(files, dict) or set(files) != set(MEMBERS)):
        raise EvidenceError("invalid bundle identity")
    for name, entry in files.items():
        if entry != identity(root / name):
            raise EvidenceError(f"bundle member differs from CI bytes: {name}")
    tracked = source.get("tracked")
    if not isinstance(tracked, dict) or set(tracked) != set(MEASURED_SOURCES):
        raise EvidenceError("measured source closure is incomplete")
    for name, entry in tracked.items():
        if not isinstance(entry, dict) or not OID.fullmatch(str(entry.get("git_blob", ""))):
            raise EvidenceError(f"missing Git blob identity for {name}")
        if {key: entry.get(key) for key in ("sha256", "bytes")} != files[_source_member(name)]:
            raise EvidenceError(f"measured source bytes do not match {name}")
    if manifest.get("windows_inbox_imports") != _pe_dependencies(root):
        raise EvidenceError("native inbox dependency closure differs from CI")
    _verify_warp_record(_record((root / WARP_RECORD).read_bytes()), source, files)
    return manifest


def _verify_warp_record(warp: dict, source: dict, members: dict) -> None:
    observation = warp.get("observation")
    if (warp.get("format") != "nw13.work-graph-run" or warp.get("version") != 2
            or warp.get("qualification") != "hosted_warp_conventional_only"
            or warp.get("status") != "functional_only" or warp.get("satisfies_completion") is not False
            or warp.get("revision") != source["commit"] or warp.get("source_tree") != source["tree"]
            or warp.get("inputs") != {name: members[f"bin/{name}"] for name in NATIVE}
            or not isinstance(observation, dict) or observation.get("status") != "warp_baseline_executed"
            or observation.get("adapter", {}).get("software") is not True):
        raise EvidenceError("hosted functional receipt does not match the staged Work Graphs inputs")
    protocol.check_oracle(observation)


def stage(repo: Path, build: Path, probe: Path, python_root: Path, warp_record: Path,
          destination: Path, trust_path: Path, revision: str) -> dict:
    if trust_path.is_relative_to(destination) or destination.exists() or trust_path.exists():
        raise EvidenceError("publish fresh bundle and a separate trusted CI record")
    source = _source_identity(repo, revision)
    destinations = {f"bin/{name}": build / name for name in NATIVE}
    destinations[f"bin/{PROBE}"] = probe
    destinations[WARP_RECORD] = warp_record
    destinations.update({_source_member(name): repo / name for name in MEASURED_SOURCES})
    before = {name: identity(path) for name, path in destinations.items()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".nw13-work-graphs-") as directory:
        staging = Path(directory) / "bundle"
        for name, origin in destinations.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
        runtime_metadata = runtime.pack(python_root, staging / RUNTIME)
        current = {name: identity(staging / name) for name in MEMBERS}
        if (any(current[name] != value for name, value in before.items())
                or any(identity(path) != before[name] for name, path in destinations.items())
                or _source_identity(repo, revision) != source):
            raise EvidenceError("build or measured source inputs changed during staging")
        with zipfile.ZipFile(staging / RUNTIME) as packed:
            if not {"python.exe", "LICENSE.txt", "Lib/json/__init__.py"} <= set(packed.namelist()):
                raise EvidenceError("self-contained Python launcher closure is missing")
        manifest = {
            "format": FORMAT, "version": 1, "source": source, "criterion": protocol.CRITERION,
            "files": current, "windows_inbox_imports": _pe_dependencies(staging), "python_runtime": runtime_metadata,
        }
        write_json(staging / MANIFEST, manifest)
        trust = {"format": TRUST_FORMAT, "version": 1, "commit": revision, "tree": source["tree"],
                 "manifest_sha256": sha256(staging / MANIFEST)}
        verify_bundle(staging, trust, revision)
        staging.rename(destination)
    # Commit the independent trust record only after the archive root is complete.
    write_json(trust_path, trust)
    return trust


def _raw(command: list[str], evidence_dir: Path, name: str, timeout: int) -> tuple[int, dict]:
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=timeout, cwd=evidence_dir)
    except subprocess.TimeoutExpired as exc:
        (evidence_dir / f"{name}.stdout").write_bytes(exc.stdout or b"")
        (evidence_dir / f"{name}.stderr").write_bytes(exc.stderr or b"")
        raise
    (evidence_dir / f"{name}.stdout").write_bytes(result.stdout)
    (evidence_dir / f"{name}.stderr").write_bytes(result.stderr)
    payload = _record(result.stdout) if result.stdout.strip() else {}
    return result.returncode, payload


def _adapter(observed: object, luid: str, *, native: bool) -> dict:
    if not isinstance(observed, dict) or str(observed.get("adapter_luid", "")).lower() != luid.lower():
        raise EvidenceError("explicit LUID differs from observed adapter")
    if native and observed.get("software") is not False:
        raise EvidenceError("native dispatch did not prove a non-software adapter")
    return {"adapter_luid": luid.lower(),
            **{key: protocol.integer(observed.get(key), 0, maximum, key)
               for key, maximum in (("vendor_id", 0xFFFF), ("device_id", 0xFFFF),
                                    ("subsystem_id", 0xFFFFFFFF), ("revision", 0xFF))}}


def _probe(root: Path, output: Path, name: str, luid: str) -> tuple[dict, dict]:
    probe = root / "bin" / PROBE
    code, snapshot = _raw([str(probe), "--adapter-luid", luid], output, name, 45)
    if (code != 0 or snapshot.get("format") != "nw11-capability-snapshot" or snapshot.get("status") != "ok"
            or not snapshot.get("driver_version")):
        raise EvidenceError(f"shared capability query failed ({code})")
    adapter = _adapter(snapshot.get("adapter"), luid, native=False)
    return adapter, snapshot


def _qualify(root: Path, adapter: dict) -> dict:
    observation = telemetry.observe(root / "bin", applications=APPLICATIONS)
    provenance, _reasons = telemetry.qualify_with_diagnostics(observation, adapter, applications=APPLICATIONS)
    if provenance is None:
        raise EvidenceError("shared physical-host, PCI/WDDM or operating-condition qualification unavailable")
    return provenance


def _environment_identity(provenance: dict, snapshot: dict) -> tuple:
    env = provenance["environment"]
    # Ordinary clocks, temperatures and P-state are observations, not equality requirements.
    return (provenance["execution_environment"], provenance["host"], snapshot["driver_version"],
            snapshot.get("runtime"), snapshot.get("features"), snapshot.get("os_build"),
            *(env[name] for name in ("wddm_driver_version", "nvidia_driver_reported_version", "gpu_uuid",
                                     "gpu_pci_bus", "windows_power_plan", "nvidia_power_management_mode",
                                     "pcie_power_management")), env["gpu_power_state"]["enforced_limit_w"])


def _nonmeasurement(status: str, reason: str) -> dict:
    return {"status": status, "decision": "defer", "reason": reason, "satisfies_completion": False,
            "dispatch_median_ticks": None, "dispatch_median_us": None, "measured_ratio": None,
            "backing_bytes": None, "all_output_slots_match_oracle": None}


def _conventional_fallback(native: Path, command: list[str], output: Path, luid: str, before: dict) -> dict:
    fallback = {"status": "failed", "exit_code": None, "observation": None}
    try:
        code, observed = _raw([str(native / EXPERIMENT), "--baseline", *command], output, "fallback", 120)
        fallback.update(exit_code=code, observation=observed)
        if code != 0 or observed.get("status") != "baseline_executed":
            raise EvidenceError(f"native fallback did not complete the conventional workload (exit {code})")
        if _adapter(observed.get("adapter"), luid, native=True) != before:
            raise EvidenceError("native fallback PCI identity differs from independent probe")
        protocol.check_oracle(observed)
    except subprocess.TimeoutExpired:
        fallback["reason"] = "native conventional fallback timed out"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        fallback["reason"] = str(exc)
    else:
        fallback["status"] = "passed"
    return fallback


def physical(root: Path, trust: dict, revision: str, output: Path, luid: str) -> dict:
    manifest = verify_bundle(root, trust, revision)
    if not LUID.fullmatch(luid) or output.exists() or output.is_relative_to(root):
        raise EvidenceError("select an exact adapter LUID and a fresh output directory outside the bundle")
    output.mkdir(parents=True)
    native = root / "bin"
    command = [str(native / BASELINE), str(native / OPTIONAL), "--adapter-luid", luid]
    record = {"format": "nw13.work-graph-run", "version": 2, "qualification": "physical_paired_dispatch",
              "source": manifest["source"], "trusted_ci": trust, "criterion": protocol.CRITERION,
              "inputs": manifest["files"], "adapter": None, "work_graph_capability": None,
              "fallback": None, "post_input_verification": False}
    try:
        before, first_snapshot = _probe(root, output, "capability-before", luid)
        record["adapter"] = before
        record["device_key"] = devices.device_key(before["vendor_id"], before["device_id"])
        record["device_tier"] = devices.physical_device_tier(before["vendor_id"], before["device_id"])
        first_host = _qualify(root, before)
        record["host_before"] = first_host
        # Recheck immediately before the actual dispatch, not just at start of telemetry.
        verify_bundle(root, trust, revision)
        code, raw = _raw([str(native / EXPERIMENT), "--matched", *command], output, "matched", 120)
        record["native_exit_code"], record["observation"] = code, raw
        if raw and _adapter(raw.get("adapter"), luid, native=True) != before:
            raise EvidenceError("native experiment PCI identity differs from independent probe")
        if code == 3 and raw.get("status") == "unsupported":
            tier = protocol.integer(raw.get("work_graph_tier"), 0, 255, "unsupported Work Graphs tier")
            model = protocol.integer(raw.get("shader_model"), 0x51, 0x6F, "unsupported shader model")
            if tier > 0 and model >= 0x68:
                raise EvidenceError("unsupported claim conflicts with queried capability")
            record["work_graph_capability"] = "unsupported"
            verify_bundle(root, trust, revision)
            fallback = _conventional_fallback(native, command, output, luid, before)
            record["fallback"] = fallback
            if fallback["status"] == "passed":
                result = _nonmeasurement("unsupported", "queried adapter does not support the declared Work Graphs arm")
            else:
                result = _nonmeasurement("failed", "required conventional fallback did not pass")
        elif code == 0 and raw.get("status") == "measured":
            result = {"status": "measured", **protocol.recompute(
                raw, conventional_shader_bytes=manifest["files"][f"bin/{BASELINE}"]["bytes"],
                work_graph_shader_bytes=manifest["files"][f"bin/{OPTIONAL}"]["bytes"],
            )}
            record["work_graph_capability"] = "supported"
        elif code == 4 and raw.get("status") == "inconclusive":
            result = _nonmeasurement("inconclusive", "capability query or bounded Work Graphs setup unavailable")
        else:
            result = _nonmeasurement("failed", "native dispatch did not complete a validated workload")
        after, last_snapshot = _probe(root, output, "capability-after", luid)
        last_host = _qualify(root, after)
        record["host_after"] = last_host
        if after != before or _environment_identity(first_host, first_snapshot) != _environment_identity(last_host, last_snapshot):
            raise EvidenceError("physical adapter, runtime or comparison controls changed during measurement")
    except subprocess.TimeoutExpired:
        result = _nonmeasurement("failed", "native procedure or capability probe timed out")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, struct.error) as exc:
        result = _nonmeasurement("inconclusive", str(exc))
    try:
        verify_bundle(root, trust, revision)
        record["post_input_verification"] = True
    except (OSError, ValueError, struct.error) as exc:
        result = _nonmeasurement("inconclusive", f"post-run bundle verification failed: {exc}")
    summary = {**record, **result}
    write_json(output / "work-graph-matched.json", summary)
    return summary


def measure_warp(executable: Path, source: dict, output: Path) -> dict:
    paths = (executable, executable.parent / BASELINE, executable.parent / OPTIONAL)
    before = {path.name: identity(path) for path in paths}
    completed = subprocess.run([str(executable), "--warp", *(str(p) for p in paths[1:])],
                               capture_output=True, check=False, timeout=120)
    observation = _record(completed.stdout)
    if (completed.returncode != 0 or observation.get("status") != "warp_baseline_executed"
            or observation.get("adapter", {}).get("software") is not True):
        raise EvidenceError("hosted WARP conventional work did not pass")
    protocol.check_oracle(observation)
    if before != {path.name: identity(path) for path in paths}:
        raise EvidenceError("hosted Work Graphs inputs changed during execution")
    record = {"format": "nw13.work-graph-run", "version": 2, "revision": source["commit"],
              "source_tree": source["tree"],
              "criterion": protocol.CRITERION, "qualification": "hosted_warp_conventional_only",
              "functional_execution": observation["status"], "observation": observation, "inputs": before,
              **_nonmeasurement("functional_only", "hosted WARP does not measure Work Graphs performance")}
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, record)
    return record


def verify_delivery(archive: Path, trust: dict) -> None:
    # Obtain this whole-archive witness from the independently authenticated
    # successful GitHub workflow/artifact, never derive it from an extracted
    # manifest or a trust file carried inside the archive.
    digest = trust.get("archive_sha256")
    identifiers = (trust.get(key) for key in ("github_artifact_id", "github_workflow_run", "github_workflow_attempt"))
    if (not isinstance(digest, str) or not SHA.fullmatch(digest)
            or not all(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) for value in identifiers)
            or sha256(archive) != digest):
        raise EvidenceError("archive differs from the independently obtained CI delivery witness")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    warp = commands.add_parser("warp")
    warp.add_argument("--executable", type=Path, required=True)
    warp.add_argument("--revision", required=True)
    warp.add_argument("--output", type=Path, required=True)
    pack = commands.add_parser("stage")
    for name in ("repo", "build", "probe", "python-root", "warp-record", "bundle", "trust"):
        pack.add_argument(f"--{name}", type=Path, required=True)
    pack.add_argument("--revision", required=True)
    for action in ("verify", "physical"):
        cmd = commands.add_parser(action)
        cmd.add_argument("--bundle", type=Path, required=True)
        cmd.add_argument("--trust", type=Path, required=True)
        cmd.add_argument("--archive", type=Path, required=True)
        cmd.add_argument("--revision", required=True)
        if action == "physical":
            cmd.add_argument("--output", type=Path, required=True)
            cmd.add_argument("--adapter-luid", required=True)
    args = parser.parse_args()
    try:
        if args.command == "warp":
            source = _source_identity(Path.cwd(), args.revision)
            measure_warp(args.executable.resolve(), source, args.output.resolve())
        elif args.command == "stage":
            stage(args.repo.resolve(), args.build.resolve(), args.probe.resolve(), args.python_root.resolve(),
                  args.warp_record.resolve(), args.bundle.resolve(), args.trust.resolve(), args.revision)
        else:
            root, trust_path = args.bundle.resolve(), args.trust.resolve()
            archive = args.archive.resolve()
            if trust_path.is_relative_to(root) or archive.is_relative_to(root):
                raise EvidenceError("trust record must come independently from CI, outside the bundle")
            trust = _record(trust_path.read_bytes())
            verify_delivery(archive, trust)
            if args.command == "verify":
                verify_bundle(root, trust, args.revision)
            else:
                if os.name != "nt":
                    raise EvidenceError("physical qualification requires a Windows desktop host")
                result = physical(root, trust, args.revision, args.output.resolve(), args.adapter_luid)
                print(json.dumps({key: result[key] for key in ("status", "decision", "satisfies_completion")}))
                if result["status"] in {"failed", "inconclusive"}:
                    return 2
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, struct.error, zipfile.BadZipFile) as exc:
        print(f"NW-13 Work Graphs: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
