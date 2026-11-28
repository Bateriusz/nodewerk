#!/usr/bin/env python3
"""Stage, verify and run an immutable NW-13 physical film handoff."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any
import zipfile

# The verified portable-runtime invocation uses Python's isolated mode.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hardware_evidence_devices as devices
import hardware_evidence_host as host
import nw11_pe_dependencies as pe
from nw11_production_binding import embedded_payloads
import nw13_evidence as film
import nwp_package as nwp
import presentation_metrics as timing
import validation_gpu_telemetry as gpu

FORMAT = "nw13.physical-film-bundle"
INDEX = "film-physical.json"
INSPECTION_INPUT = "inspection-build.v1.json"
INSPECTION_PROFILE = "inspection-v1"
ORACLE = "hosted_cpu_oracle_checked_readback"
PEER = "matched_physical_common_only"
PROBE = "bin/nw11_capability_probe.exe"
PRESENTMON = "bin/PresentMon.exe"
PRESENTMON_HASH = "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"
PLAYER = "nw_common_d3d12_fl12_0_sm6_0_v1.exe"
ARMS = {"common": "independent-a", "fallback": "fallback", "dxr": "inline-ray/independent-a"}
HELPERS = (
    "audio_capture_media.py", "audio_physical_validation.py", "audio_presentation_cues.py", "audio_song.py",
    "hardware_evidence_devices.py", "hardware_evidence_host.py", "nw11_contract.py",
    "nw11_pe_dependencies.py", "nw11_production_binding.py", "nw13_evidence.py",
    "nw13_physical_film.py", "nw13_work_graph_protocol.py", "nwp_package.py",
    "offline_shader_pack.py", "presentation_metrics.py", "validation_gpu_telemetry.py",
)
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
LUID = re.compile(r"[0-9a-fA-F]{8}:[0-9a-fA-F]{8}")


class HandoffError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_object(payload: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HandoffError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def reject(constant):
        raise HandoffError(f"nonfinite JSON number: {constant}")
    value = json.loads(payload, object_pairs_hook=unique, parse_constant=reject)
    if not isinstance(value, dict):
        raise HandoffError("expected a JSON object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def hash_value(value: Any) -> str:
    text = value.removeprefix("sha256:v1:").removeprefix("sha256:") if isinstance(value, str) else ""
    if not HEX64.fullmatch(text):
        raise HandoffError("invalid SHA-256")
    return text


def relative_name(name: str) -> str:
    if not isinstance(name, str) or "\\" in name or ":" in name or not name:
        raise HandoffError("unsafe archive name")
    parts = name.split("/")
    if any(not item or item in {".", ".."} or item.rstrip(" .") != item
           or not re.fullmatch(r"[a-zA-Z0-9_.+-]+", item)
           or item.split(".", 1)[0].casefold() in {"con", "prn", "nul", "aux", *[f"com{i}" for i in range(1, 10)],
                                                    *[f"lpt{i}" for i in range(1, 10)]}
           for item in parts):
        raise HandoffError(f"unsafe archive name: {name}")
    return str(PurePosixPath(name))


def member(root: Path, name: str) -> Path:
    path = root.joinpath(*relative_name(name).split("/"))
    if (path.resolve() != path or not path.is_file()
            or any(node.is_symlink() or getattr(node, "is_junction", lambda: False)()
                   for node in (path, *path.parents) if node != root and node.is_relative_to(root))):
        raise HandoffError(f"not an ordinary immutable bundle member: {name}")
    return path


def inventory(root: Path) -> dict[str, str]:
    files = {}
    folded = set()
    for path in sorted(root.rglob("*")):
        name = relative_name(path.relative_to(root).as_posix())
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise HandoffError(f"archive link is not allowed: {name}")
        if path.is_dir():
            continue
        if name == INDEX:
            continue
        if name.casefold() in folded:
            raise HandoffError("case-aliased bundle paths")
        folded.add(name.casefold())
        files[name] = sha(member(root, name))
    return files


def source_bytes(root: Path, revision: str, relative: str) -> bytes:
    relative = relative_name(relative)
    data = subprocess.run(["git", "show", f"{revision}:{relative}"], cwd=root,
                          check=True, capture_output=True).stdout
    return data


def copy_member(root: Path, name: str, data: bytes) -> None:
    path = root / relative_name(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise HandoffError(f"duplicate staged file: {name}")
    path.write_bytes(data)


def arm_bytes(root: Path, name: str, record: dict, song_sha: str, size: list[int], revision: str) -> None:
    names = {role: f"arms/{name}/{file}" for role, file in (
        ("executable", PLAYER), ("package", "common.nwp"), ("manifest", "link-manifest.json"))}
    for role, path in names.items():
        if record.get(role) != path or sha(member(root, path)) != hash_value(record.get(role + "_sha256")):
            raise HandoffError(f"{name}: {role} identity differs")
    executable, package, manifest = (member(root, names[role]).read_bytes() for role in names)
    _, enclosed_package, enclosed_manifest = embedded_payloads(executable)
    if (package, manifest) != (enclosed_package, enclosed_manifest):
        raise HandoffError(f"{name}: PE does not embed the delivered package and manifest")
    link = json_object(manifest)
    if link.get("package_sha256") != digest(package) or link.get("present_geometry") != dict(zip(("width", "height"), size)):
        raise HandoffError(f"{name}: manifest package/extent identity differs")
    manifest_digest = hash_value(link.get("manifest_sha256"))
    unsigned = dict(link)
    unsigned.pop("manifest_sha256")
    canonical = nwp.canonical_json_bytes(unsigned)
    if manifest_digest != digest(canonical) or manifest_digest != hash_value(record.get("link_manifest_sha256")):
        raise HandoffError(f"{name}: semantic link manifest hash differs")
    history = nwp.decode_package(package)
    if history.capability.profile_id != nwp.COMMON_PROFILE_ID or not history.audio_song:
        raise HandoffError(f"{name}: expected the portable film package with embedded song")
    if digest(history.audio_song) != song_sha or hash_value(record.get("song_sha256")) != song_sha:
        raise HandoffError(f"{name}: embedded NWAS song differs")
    optional = link.get("film_lowering")
    if name == "dxr":
        if (not isinstance(optional, dict) or optional.get("profile_id") != film.RAY_PROFILE_ID
                or optional.get("requirements_digest") != film.RAY_REQUIREMENTS_DIGEST
                or optional.get("revision") != revision):
            raise HandoffError("DXR executable lacks admitted inline-ray specialization identity")
    elif optional:
        raise HandoffError(f"{name}: portable arm unexpectedly has optional lowering")


def verify(root: Path, *, index_sha: str, revision: str, tree: str) -> dict:
    root = root.resolve(strict=True)
    index = member(root, INDEX).read_bytes()
    if digest(index) != hash_value(index_sha):
        raise HandoffError("bundle inventory does not match the trusted CI hash")
    values = json_object(index)
    if (values.get("format") != FORMAT or values.get("version") != 1
            or not HEX40.fullmatch(revision) or not HEX40.fullmatch(tree)
            or values.get("revision") != revision or values.get("tree") != tree):
        raise HandoffError("unexpected measured source commit/tree or inventory format")
    if values.get("files") != inventory(root):
        raise HandoffError("missing, additional, or modified bundle files")
    sources = values["sources"]
    if set(sources) != {"production", "graph", "song"}:
        raise HandoffError("source identity set is incomplete")
    for source in sources.values():
        if sha(member(root, "source/" + source["path"])) != hash_value(source["sha256"]):
            raise HandoffError("stale measured source snapshot")
    if hash_value(values["graph_sha256"]) != hash_value(sources["graph"]["sha256"]):
        raise HandoffError("compiled graph identity differs")
    definition = json_object(member(root, "source/" + sources["production"]["path"]).read_bytes())
    rendering = definition["rendering"]
    size = [rendering["width"], rendering["height"]]
    for key in ("graph", "song"):
        if definition[key] != sources[key]["path"]:
            raise HandoffError("production references a different graph or song")
    if (values["render_size"] != size or any(type(n) is not int or not 0 < n <= 4096 for n in size)
            or values["tolerances"] != rendering["tolerances"]):
        raise HandoffError("inconsistent native dimensions or image tolerances")
    for threshold in values["tolerances"].values():
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or threshold < 0:
            raise HandoffError("invalid image tolerance")
    ticks = values["ticks"]
    if (not isinstance(ticks, list) or not 1 <= len(ticks) <= 32
            or any(type(tick) is not int or not 0 <= tick <= 0xffffffff for tick in ticks)
            or sorted(set(ticks)) != ticks or not set(definition["fixed_ticks"]) <= set(ticks)):
        raise HandoffError("invalid matched authored ticks")
    reference = values.get("image_reference")
    if reference == ORACLE:
        for tick in ticks:
            read_frame(member(root, reference_name(tick)), size)
    elif (reference != PEER or rendering.get("work_profile") != INSPECTION_PROFILE
          or any(name.startswith("reference/") for name in values["files"])):
        raise HandoffError("incorrectly labeled inspection reference or source profile")
    if set(values["arms"]) not in ({"common", "fallback"}, set(ARMS)):
        raise HandoffError("film bundle lacks both portable arms")
    for name, record in values["arms"].items():
        arm_bytes(root, name, record, hash_value(values["song_sha256"]), size, revision)
    probe = member(root, PROBE)
    if not probe.read_bytes().startswith(b"MZ"):
        raise HandoffError("capability probe is not a Windows executable")
    for relative in [PROBE, *(row["executable"] for row in values["arms"].values())]:
        actual_imports = pe.imports(member(root, relative))
        if pe.forbidden(actual_imports) or values["native_dependency_closure"].get(relative) != actual_imports:
            raise HandoffError("delivered native dependency closure differs")
    if PRESENTMON in values["files"] and sha(member(root, PRESENTMON)) != PRESENTMON_HASH:
        raise HandoffError("unexpected PresentMon identity")
    return values


def reference_name(tick: int) -> str:
    return f"reference/tick-{tick:010d}.rgba32f"


def read_frame(path: Path, size: list[int]) -> tuple[float, ...]:
    data = path.read_bytes()
    if len(data) != size[0] * size[1] * 16:
        raise HandoffError("incorrect image dimensions")
    values = struct.unpack(f"<{len(data) // 4}f", data)
    if not all(math.isfinite(value) for value in values) or any(alpha != 1.0 for alpha in values[3::4]):
        raise HandoffError("nonfinite or non-opaque RGBA32F image")
    return values


def comparison(actual: tuple[float, ...], expected: tuple[float, ...], thresholds: dict) -> dict:
    if not actual or len(actual) != len(expected):
        raise HandoffError("different capture extents")
    maximum = max(abs(a - b) for a, b in zip(actual, expected, strict=True))
    rmse = math.sqrt(math.fsum((a - b) ** 2 for a, b in zip(actual, expected, strict=True)) / len(actual))
    return {"max_abs_channel_error": maximum, "rmse": rmse,
            "within_tolerance": maximum <= thresholds["max_abs_channel_error"] and rmse <= thresholds["rmse"]}


def stage(*, repo: Path, emitted: Path, probe: Path, python_root: Path, output: Path,
          revision: str, presentmon: Path | None = None, presentmon_license: Path | None = None,
          inspection: bool = False) -> dict:
    from nw11_pack_python_runtime import _files as runtime_files

    repo = repo.resolve()
    emitted, output = emitted.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(emitted):
        raise HandoffError("handoff output must be new and outside hosted evidence")
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    if revision != git("rev-parse", "HEAD") or git("status", "--porcelain", "--untracked-files=normal"):
        raise HandoffError("physical package requires the exact clean CI checkout")
    tree = git("rev-parse", "HEAD^{tree}")
    hosted_data = member(emitted, INSPECTION_INPUT if inspection else "artifact-bundle.v1.json").read_bytes()
    hosted = json_object(hosted_data)
    if hosted.get("revision") != revision or film.seal_bundle(hosted) != hosted:
        raise HandoffError("CI export record is stale or unverified")
    sources = hosted["measured_sources"]
    song_sha = hash_value(sources["song"]["nwas_sha256"])
    if inspection:
        if (hosted.get("format") != "nw13.inspection-build" or hosted.get("tree") != tree
                or hosted.get("hosted_execution") != "not_run" or hosted.get("image_reference") != PEER):
            raise HandoffError("inspection must be an exact build-only CI input")
        records = hosted["arms"]
        frames, rebuilt = None, {}
        size, ticks = hosted["render_size"], hosted["ticks"]
    else:
        if (hosted.get("source_state") != "clean_revision" or film.validate_bundle(hosted)
                or not hosted.get("independent_builds")):
            raise HandoffError("CI export record is incomplete")
        frames = hosted["native_fixed_ticks"]["common"]
        if frames["status"] != "measured_native" or frames["image_within_tolerance"] is not True:
            raise HandoffError("CI fixed tick captures did not pass their CPU oracle check")
        size = [frames["width"], frames["height"]]
        records = {"common": hosted["common"], "fallback": hosted["fallback"]}
        rebuilt = {"common": hosted["independent_observations"]["a"]}
        for optional in hosted["optional"]:
            if optional.get("profile_id") != film.RAY_PROFILE_ID or "dxr" in records:
                raise HandoffError("unexpected or duplicate optional film lowering")
            pair = hosted["optional_rebuilds"]["dxr_inline_ray_query"]
            identity = ("executable_sha256", "package_sha256", "manifest_file_sha256", "dependency_identities")
            if any(pair["a"][key] != pair["b"][key] for key in identity):
                raise HandoffError("independent DXR builds disagree")
            rebuilt["dxr"] = pair["a"]
            records["dxr"] = optional
        ticks = [frame["tick"] for frame in frames["frames"]]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nw13-film-stage-", dir=output.parent) as temp:
        root = Path(temp).resolve()
        for name in HELPERS:
            copy_member(root, f"tools/{name}", source_bytes(repo, revision, f"tools/{name}"))
        copy_member(root, "run-film.ps1", source_bytes(repo, revision, "tools/nw13/run-film.ps1"))
        for file in runtime_files(python_root):
            copy_member(root, f"python/{file.relative_to(python_root).as_posix()}", file.read_bytes())
        copy_member(root, PROBE, probe.read_bytes())
        if (presentmon is None) != (presentmon_license is None):
            raise HandoffError("PresentMon and its license must be staged together")
        if presentmon is not None:
            copy_member(root, PRESENTMON, presentmon.read_bytes())
            copy_member(root, "licenses/PresentMon.txt", presentmon_license.read_bytes())
        for source in sources.values():
            copy_member(root, "source/" + source["path"], source_bytes(repo, revision, source["path"]))
        copy_member(root, "provenance/hosted-artifact-bundle.json", hosted_data)
        arms = {}
        for name, observed in records.items():
            if (observed.get("revision") != revision
                    or observed.get("measurement_status") != ("built_only" if inspection else "measured")):
                raise HandoffError(f"{name}: unexpected CI evidence class or revision")
            arm = {"link_manifest_sha256": hash_value(observed["manifest_sha256"]), "song_sha256": song_sha,
                   "budgets": observed["budgets"]}
            for role, file, key in (("executable", PLAYER, "executable_sha256"),
                                    ("package", "common.nwp", "package_sha256"),
                                    ("manifest", "link-manifest.json", "manifest_file_sha256")):
                identity = hash_value(observed.get(key) or (rebuilt[name].get(key) if name in rebuilt else None) or
                                      ("sha256:" + digest(member(emitted, f"{ARMS[name]}/{file}").read_bytes())
                                       if role == "manifest" else None))
                data = member(emitted, f"{ARMS[name]}/{file}").read_bytes()
                if (digest(data) != identity or
                        (name in rebuilt and hash_value(rebuilt[name][key]) != identity)):
                    raise HandoffError(f"{name}: exported {role} differs from the measured record")
                arm[role] = f"arms/{name}/{file}"
                arm[role + "_sha256"] = identity
                copy_member(root, arm[role], data)
            if hash_value(observed["audio_song_sha256"]) != song_sha:
                raise HandoffError(f"{name}: CI song differs")
            arms[name] = arm
        for frame in frames["frames"] if frames else ():
            if frame.get("reference") != "graph_cpu_oracle" or frame.get("image_within_tolerance") is not True:
                raise HandoffError("CI frame is not an accepted CPU-oracle reference")
            tick = frame["tick"]
            data = member(emitted, "common-fixed-ticks/" + frame["file"]).read_bytes()
            if digest(data) != hash_value(frame["sha256"]):
                raise HandoffError("CI readback hash differs")
            copy_member(root, reference_name(tick), data)
        compiled = json_object(member(root, "source/" + sources["production"]["path"]).read_bytes())
        closure = {path: pe.imports(root / path) for path in [PROBE, *(row["executable"] for row in arms.values())]}
        for path, imports in closure.items():
            if pe.forbidden(imports):
                raise HandoffError(f"{path}: non-inbox player/probe dependency")
        values = {"format": FORMAT, "version": 1, "revision": revision, "tree": tree, "sources": sources,
                  "graph_sha256": hash_value(sources["graph"]["sha256"]), "song_sha256": song_sha,
                  "render_size": size, "tolerances": compiled["rendering"]["tolerances"],
                  "image_reference": PEER if inspection else ORACLE,
                  "ticks": ticks, "arms": arms, "native_dependency_closure": closure, "files": inventory(root)}
        write_json(root / INDEX, values)
        index_sha = sha(root / INDEX)
        # Prove the shipped entry points can import from the actual staging
        # tree, not from this process's populated sys.modules or checkout.
        env = {key: value for key, value in os.environ.items() if key.upper() != "PYTHONPATH"}
        for tool, argv in (
            ("nw13_physical_film.py", ["verify", "--root", str(root), "--index-sha", index_sha,
                                      "--revision", revision, "--tree", tree]),
            ("audio_physical_validation.py", ["--help"]),
        ):
            checked = subprocess.run([sys.executable, "-I", "-B", str(root / "tools" / tool), *argv],
                                     cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                     timeout=60, check=False)
            if checked.returncode:
                expected = checked.stderr.decode("utf-8", errors="replace").strip()
                raise HandoffError(f"isolated staged {tool} failed: {expected}")
        temporary = output.with_name(output.name + ".part")
        if temporary.exists():
            raise HandoffError("temporary output already exists")
        try:
            with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name in sorted([INDEX, *values["files"]]):
                    archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), (root / name).read_bytes(),
                                     compress_type=zipfile.ZIP_DEFLATED)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    return {"revision": revision, "tree": tree, "archive_sha256": sha(output), "index_sha256": index_sha,
            "arms": sorted(arms), "render_size": size, "image_reference": values["image_reference"]}


def capture_process(command, output: Path, *, cwd: Path, env=None, timeout=60, presentmon=None):
    output.mkdir()
    timed_out = False
    with (output / "stdout.bin").open("wb") as stdout, (output / "stderr.bin").open("wb") as stderr:
        start = time.perf_counter()
        with subprocess.Popen([str(arg) for arg in command], cwd=cwd, env=env, stdout=stdout, stderr=stderr) as proc:
            capture = None
            capture_failure = None
            try:
                if presentmon:
                    try:
                        capture = timing.run_presentmon(presentmon, process_id=proc.pid, run_seconds=5,
                                                        run_dir=output, run_index=output.parent.name)
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
                        capture_failure = type(exc).__name__
                code = proc.wait(timeout=max(0.1, start + timeout - time.perf_counter()))
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                code = proc.wait(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
            launch = {"argv": [str(arg) for arg in command], "pid": proc.pid, "elapsed_ms": (time.perf_counter() - start) * 1000,
                      "start_qpc_ms": start * 1000, "returncode": code, "timed_out": timed_out,
                      "presentmon": capture.name if capture else None, "presentmon_error": capture_failure}
    launch["end_qpc_ms"] = time.perf_counter() * 1000
    launch["stdout_sha256"] = sha(output / "stdout.bin")
    launch["stderr_sha256"] = sha(output / "stderr.bin")
    write_json(output / "launch.json", launch)
    return launch


def counter(source: str, key: str) -> int | None:
    values = re.findall(rf"^{re.escape(key)}=([0-9]+)\r?$", source, flags=re.MULTILINE)
    return int(values[0]) if len(values) == 1 else None


def device_report(output: Path, launch: dict, adapter: dict, driver: str) -> dict:
    stdout = host.decode_windows_output((output / "stdout.bin").read_bytes())
    stderr = host.decode_windows_output((output / "stderr.bin").read_bytes())
    matches = re.findall(r"^NODEWERK_SELECTED_ADAPTER_LUID=([a-fA-F0-9:]+)\r?$", stderr, flags=re.MULTILINE)
    # The explicitly selected DXGI LUID is emitted by the actual child, not inferred.
    driver_rows = re.findall(r"^NW05_DRIVER=([^\r\n]*)", stdout, flags=re.MULTILINE)
    observed_driver = driver_rows[0] if len(driver_rows) == 1 else None
    return {
        "same_adapter": len(matches) == 1 and matches[0].upper() == adapter["adapter_luid"].upper()
                        and counter(stdout, "NW05_VENDOR_ID") == adapter["vendor_id"]
                        and counter(stdout, "NW05_DEVICE_ID") == adapter["device_id"],
        # The probe and the player can use different driver string conventions.
        # The LUID and numeric PCI values bind the selected process.
        "driver_observed": observed_driver,
        "probe_driver": driver,
        "native_startup_us": counter(stdout, "NW13_STARTUP_US"),
        "managed_committed_bytes": counter(stdout, "NW13_MANAGED_COMMITTED_BYTES"),
        "acceleration_structure_bytes": counter(stdout, "NW13_AS_COMMITTED_BYTES"),
        "successful_present_calls": counter(stdout, "NW05_PRESENTS"),
        "frames_rendered": counter(stdout, "NW05_FRAMES"),
        "present_sync_interval": counter(stdout, "NW05_PRESENT_SYNC_INTERVAL"),
        "tick": counter(stdout, "NW05_EXPLICIT_TICK"),
        "image_bytes": counter(stdout, "NW05_IMAGE_BYTES"),
        "returncode": launch["returncode"], "timed_out": launch["timed_out"],
    }


def performance(output: Path, launch: dict, sync_interval: int | None) -> dict:
    if not launch["presentmon"] or sync_interval is None:
        return unavailable("no_process_bound_etw")
    try:
        with (output / launch["presentmon"]).open(encoding="utf-8-sig", newline="") as stream:
            rows = csv.DictReader(stream, strict=True)
            fields = set(rows.fieldnames or ())
            if not {"ProcessID", "CPUStartQPCTime", "SyncInterval", "SwapChainAddress"} <= fields:
                return unavailable("etw_schema_or_clock_unavailable")
            selected = []
            for row in rows:
                if row.get("ProcessID") != str(launch["pid"]):
                    continue
                stamp = float(row["CPUStartQPCTime"])
                if launch["start_qpc_ms"] + 1000 <= stamp <= launch["end_qpc_ms"]:
                    if row["SyncInterval"] != str(sync_interval) or not row["SwapChainAddress"]:
                        return unavailable("unmatched_sync_interval_or_swapchain")
                    selected.append(row)
            if len({row["SwapChainAddress"] for row in selected}) != 1:
                return unavailable("missing_or_multiple_swapchains")
            gpu_samples, cpu_samples, display = timing.extract_samples(selected, process_id=launch["pid"])
    except (OSError, ValueError, TypeError, csv.Error, KeyError):
        return unavailable("invalid_etw_samples")
    def metric(samples):
        return ({"status": "observed", "count": len(samples), "median_ms": statistics.median(samples)}
                if len(samples) >= 5 and all(value > 0 for value in samples)
                else unavailable("insufficient_process_bound_samples"))
    gpu_values, cpu_values, display_values = map(metric, (gpu_samples, cpu_samples, display))
    return {"status": "observed" if all(obs["status"] == "observed" for obs in (gpu_values, cpu_values))
            else "partial" if any(obs["status"] == "observed" for obs in (gpu_values, cpu_values, display_values))
            else "unavailable",
            "gpu_time": gpu_values, "cpu_submit": cpu_values, "display_intervals": display_values,
            "raw_csv_sha256": sha(output / launch["presentmon"])}


def unavailable(code: str) -> dict:
    return {"status": "unavailable", "reason": code}


def run(*, root: Path, output: Path, index_sha: str, revision: str, tree: str, adapter_luid: str,
        collect_timing: bool = False, windows=os.name == "nt", execute=capture_process) -> dict:
    root = root.resolve(strict=True)
    output = output.resolve()
    if output == root or output.is_relative_to(root) or output.exists():
        raise HandoffError("evidence output must be new and outside the immutable bundle")
    values = verify(root, index_sha=index_sha, revision=revision, tree=tree)
    if not windows or not LUID.fullmatch(adapter_luid):
        raise HandoffError("physical execution requires Windows and a selected DXGI LUID")
    output.mkdir(parents=True)
    raw = output / "private"
    raw.mkdir()
    report = {
        "format": "nw13.physical-film-evidence", "version": 1, "revision": revision, "tree": tree,
        "bundle_index_sha256": hash_value(index_sha), "source_graph_sha256": values["graph_sha256"],
        "song_sha256": values["song_sha256"], "render_size": values["render_size"],
        "image_reference": values["image_reference"],
        "window_mode": "maximized", "window_extent": unavailable("not_emitted_by_native_player"),
        "host": unavailable("physical_preflight_pending"), "arms": {},
        "adoption": {"status": "not_decided", "reason": "functional_image_performance_are_independent"},
    }
    def checked(name):
        path = member(root, name)
        if sha(path) != values["files"][name]:
            raise HandoffError(f"bundle member changed before use: {name}")
        return path
    def record():
        write_json(output / "summary.json", report)
    record()
    try:
        probe = checked(PROBE)
        probe_out = raw / "capability.json"
        observation = execute([probe, "--adapter-luid", adapter_luid, "--json", probe_out],
                              raw / "probe", cwd=raw, timeout=45)
        if observation["returncode"] != 0 or observation["timed_out"]:
            raise HandoffError("explicit physical capability query failed")
        snapshot = json_object(probe_out.read_bytes())
        adapter = snapshot["adapter"]
        if (snapshot.get("status") != "ok" or adapter["adapter_luid"].upper() != adapter_luid.upper()
                or not any(item.get("software") is False and all(item.get(key) == adapter.get(key)
                    for key in ("vendor_id", "device_id", "adapter_name")) for item in snapshot["adapters"])):
            raise HandoffError("queried adapter is not the selected physical hardware")
        # Study the same binary basename used in every arm; the individual
        # launch profiles are best-effort and are not a performance match proof.
        try:
            raw_host = gpu.observe(root / "arms/common", applications=(PLAYER,))
            exact_telemetry = True
        except TypeError:
            # Base revisions without shared application selection still provide
            # the physical observation, not film-specific power qualification.
            raw_host = gpu.observe(root / "arms/common")
            exact_telemetry = False
        write_json(raw / "host.json", raw_host)
        physical = host.evaluate_physical_virtualization_probe(raw_host.get("virtualization_probe"))
        if physical["valid"] is not True:
            raise HandoffError("shared physical/virtualization preflight rejected the host")
        qualified, reasons = (gpu.qualify_with_diagnostics(raw_host, adapter, applications=(PLAYER,))
                              if exact_telemetry else (None, ["film_application_selection_unavailable"]))
        write_json(raw / "host-qualification.json", {"qualification": qualified, "reasons": reasons})
        # Only explicit allowlisted observations enter the shareable summary.
        report["host"] = {"status": "physical", "device": devices.device_key(adapter["vendor_id"], adapter["device_id"]),
                          "representative_tier": devices.physical_device_tier(adapter["vendor_id"], adapter["device_id"]),
                          "gpu_power_telemetry": "observed" if qualified else "unavailable"}
        if qualified:
            env = qualified["environment"]
            report["host"]["wddm"] = env["wddm_driver_version"]
            report["host"]["clocks"] = env["gpu_clocks"]
            report["host"]["temperature_c"] = env["temperature_c"]
            report["host"]["enforced_limit_w"] = env["gpu_power_state"]["enforced_limit_w"]
        features = snapshot["features"]
        report["host"]["dxr_tier"] = features.get("raytracing_tier")
        report["host"]["shader_model"] = features.get("highest_shader_model")
        selection = dict(highest_shader_model=str(features.get("highest_shader_model")).removeprefix("D3D12_SHADER_MODEL_"),
                         raytracing_tier={10: 1.0, 11: 1.1}.get(features.get("raytracing_tier")))
        env = {**os.environ, "NODEWERK_ADAPTER_LUID": adapter["adapter_luid"]}
        size, ticks, thresholds = values["render_size"], values["ticks"], values["tolerances"]
        run_root = raw / "runs"
        run_root.mkdir()
        for name in ARMS:
            if name not in values["arms"]:
                report["arms"][name] = unavailable("not_built_by_hosted_capability")
                continue
            if name == "dxr":
                try:
                    film.select_lowering("dxr_inline_ray_query", selection)
                except film.EvidenceError:
                    report["arms"][name] = {"status": "unsupported", "reason": "physical_d3d12_feature_query"}
                    continue
            arm = values["arms"][name]
            lane = run_root / name
            lane.mkdir()
            def check_arm():
                exe = checked(arm["executable"])
                checked(arm["package"])
                checked(arm["manifest"])
                return exe
            use_etw = collect_timing and PRESENTMON in values["files"]
            presentmon = checked(PRESENTMON) if use_etw else None
            # A single visible run per arm, with equal frame counts in timing mode.
            pilot = execute([check_arm(), "--maximized", "--frames", "420" if collect_timing else "120"],
                            lane / "visible", cwd=lane, env=env, timeout=45, presentmon=presentmon)
            native = device_report(lane / "visible", pilot, adapter, snapshot["driver_version"])
            shown = (pilot["returncode"] == 0 and not pilot["timed_out"] and native["same_adapter"]
                     and (native["successful_present_calls"] or 0) > 0)
            metrics = {key: native[key] if native[key] is not None else unavailable("not_emitted_by_native_player")
                       for key in ("native_startup_us", "managed_committed_bytes", "acceleration_structure_bytes",
                                   "successful_present_calls", "frames_rendered", "present_sync_interval")}
            metrics["vram_bytes"] = unavailable("no_process_scoped_video_memory_observation")
            budget = {}
            for slot, key in (("managed_committed_bytes", "max_memory_bytes"),
                              ("acceleration_structure_bytes", "max_as_bytes")):
                if key in arm["budgets"]:
                    observed, limit = native[slot], arm["budgets"][key]
                    budget[slot] = "unavailable" if observed is None else "pass" if observed <= limit else "exceeded"
            lane_reports = []
            for tick in ticks if shown else ():
                target = lane / f"tick-{tick}.rgba32f"
                run_folder = lane / f"tick-{tick}"
                single = execute([check_arm(), "--maximized", "--tick", str(tick), "--film-capture", target],
                                 run_folder, cwd=lane, env=env, timeout=45)
                telemetry = device_report(run_folder, single, adapter, snapshot["driver_version"])
                try:
                    if (single["returncode"] != 0 or single["timed_out"] or not telemetry["same_adapter"]
                            or telemetry["tick"] != tick or not telemetry["successful_present_calls"]
                            or not target.is_file() or telemetry["image_bytes"] != target.stat().st_size):
                        raise HandoffError("native capture did not prove the bound tick/adapter/Present")
                    actual = read_frame(target, size)
                    row = {"tick": tick, "status": "observed",
                           "successful_present_calls": telemetry["successful_present_calls"],
                           "capture_sha256": sha(target)}
                    if values["image_reference"] == ORACLE:
                        expected = read_frame(checked(reference_name(tick)), size)
                        compared = comparison(actual, expected, thresholds)
                        row["oracle_checked_hosted_reference"] = compared
                        row["status"] = "pass" if compared["within_tolerance"] else "fail"
                    if name != "common":
                        baseline = next((item for item in report["arms"]["common"]["correctness"]["ticks"]
                                         if item["tick"] == tick and item["status"] in {"pass", "observed"}), None)
                        if baseline:
                            common_capture = run_root / "common" / f"tick-{tick}.rgba32f"
                            if sha(common_capture) != baseline["capture_sha256"]:
                                raise HandoffError("physical common capture identity changed")
                            matched = comparison(actual, read_frame(common_capture, size), thresholds)
                            row["matched_physical_common"] = matched
                            if not matched["within_tolerance"]:
                                row["status"] = "fail"
                            elif values["image_reference"] == PEER:
                                row["status"] = "matched"
                except (OSError, HandoffError):
                    row = {"tick": tick, "status": "fail", "reason": "invalid_native_capture"}
                lane_reports.append(row)
            states = {row["status"] for row in lane_reports}
            correctness = ("fail" if "fail" in states else next(iter(states)) if len(states) == 1
                           else "partial" if states else "unavailable")
            arm_report = {
                "functional": {"status": "pass" if shown else "fail", "returncode": pilot["returncode"],
                               "queried_adapter_bound": native["same_adapter"]},
                "correctness": {"status": correctness, "ticks": lane_reports,
                                "absolute_oracle_image_comparison": values["image_reference"] == ORACLE,
                                "matched_physical_common": name != "common" and bool(lane_reports) and
                                    all("matched_physical_common" in row for row in lane_reports),
                                "display_pixels": unavailable("GPU_readback_is_not_a_display_capture")},
                "metrics": metrics, "budgets": budget,
                "performance": performance(lane / "visible", pilot, native["present_sync_interval"])
                               if shown else unavailable("functional_run_failed"),
                "identity": {key: arm[key] for key in ("executable_sha256", "package_sha256", "manifest_sha256", "song_sha256")},
            }
            report["arms"][name] = arm_report
            record()
        report["performance_comparison"] = {"status": "observations_only" if collect_timing else "unavailable",
                                              "reason": "no_declared_timing_adoption_criterion"}
        verify(root, index_sha=index_sha, revision=revision, tree=tree)
        report["bundle_unchanged"] = True
        report["status"] = "completed"
    except (HandoffError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        (raw / "failure.txt").write_text(str(exc) + "\n", encoding="utf-8")
        report["status"] = "failed"
        report["failure"] = type(exc).__name__
        raise
    finally:
        # Never publish raw host IDs, Windows paths, LUIDs, text logs or commands here.
        record()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    make = actions.add_parser("stage")
    for name in ("repo", "emitted", "probe", "python-root", "output"):
        make.add_argument("--" + name, required=True, type=Path)
    make.add_argument("--revision", required=True)
    make.add_argument("--presentmon", type=Path)
    make.add_argument("--presentmon-license", type=Path)
    make.add_argument("--receipt", type=Path)
    make.add_argument("--inspection-production", type=Path)
    make.add_argument("--template", type=Path)
    make.add_argument("--dxc", type=Path)
    for mode in ("verify", "run"):
        command = actions.add_parser(mode)
        command.add_argument("--root", type=Path, required=True)
        for name in ("index-sha", "revision", "tree"):
            command.add_argument("--" + name, required=True)
        if mode == "run":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--adapter-luid", required=True)
            command.add_argument("--collect-timing", action="store_true")
    args = vars(parser.parse_args(argv))
    action = args.pop("action")
    try:
        if action == "stage":
            receipt = args.pop("receipt")
            production_path, template, dxc = (args.pop(name) for name in ("inspection_production", "template", "dxc"))
            if production_path:
                if not template or not dxc:
                    raise HandoffError("inspection staging requires a Windows template and the pinned offline DXC")
                from nw13_physical_inspection import build
                build(args["repo"], production_path, args["emitted"], template, dxc, args["revision"])
            elif template or dxc:
                raise HandoffError("template and DXC inputs are only for the build-only inspection profile")
            result = stage(**args, inspection=production_path is not None)
            if receipt is not None:
                write_json(receipt, result)
            print(json.dumps(result, sort_keys=True))
        elif action == "verify":
            result = verify(**args)
            print(json.dumps({key: result[key] for key in ("revision", "tree", "render_size", "ticks")}))
        else:
            result = run(**args)
            required = [row for row in result["arms"].values() if "functional" in row]
            return int(any(row["functional"]["status"] != "pass" or row["correctness"]["status"] == "fail"
                           or "exceeded" in row["budgets"].values() for row in required))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"NW-13 physical handoff: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
