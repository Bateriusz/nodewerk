from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile

import pytest

import nw03_compiler
import nw05_export
import nw09_export
import nw13_artifact_measure as measure
import nw13_evidence as film
import nw13_physical_film as handoff
import nw13_production
import nwp_package as nwp
from test_dependency_closure_and_accounting import build_synthetic_pe64


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = "fixtures/nw13/hotel_corridor.production.json"


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    dir = tmp_path_factory.mktemp("nw13-physical-film")
    repo = dir / "repo"
    repo.mkdir()
    production = nw13_production.load_production(ROOT, PRODUCTION)
    files = [PRODUCTION, production["graph"], production["song"], "tools/nw13/run-film.ps1"]
    files += [f"tools/{name}" for name in handoff.HELPERS]
    for relative in files:
        to = repo / relative
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, to)
    def git(*command):
        return subprocess.run(["git", *command], check=True, capture_output=True, text=True, cwd=repo).stdout.strip()
    git("init")
    git("config", "core.autocrlf", "false")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "exact sources")
    revision = git("rev-parse", "HEAD")
    production["revision"] = revision
    graph = nw03_compiler.load_graph(ROOT / production["graph"])
    song = nw13_production.playback_song(ROOT, production, graph=graph)
    pe = build_synthetic_pe64(["kernel32.dll", "d3d12.dll"])
    template, probe = dir / "template.exe", dir / "probe.exe"
    template.write_bytes(pe)
    probe.write_bytes(pe)
    emitted = dir / "hosted"
    dxc = nw09_export._test_dxc(dir / "dxc")
    caps = {"highest_shader_model": "6_5", "raytracing_tier": 1.1}
    records = {}
    size = [production["rendering"][key] for key in ("width", "height")]
    # Exercise the real package codec, real PE trailer and real hosted record
    # without pretending the fixture ran a real renderer or needed an SDK.
    exported = nw05_export.export_graph(ROOT / production["graph"], dir / "codec",
        allow_test_payloads=True, dxc_executable=dxc)
    package = nwp.encode_package(replace(exported.artifacts[nwp.COMMON_PROFILE_ID].package, audio_song=song))
    for name, path in handoff.ARMS.items():
        link = {"package_sha256": handoff.digest(package), "present_geometry": dict(zip(("width", "height"), size))}
        if name == "dxr":
            link["film_lowering"] = {**film.select_lowering("dxr_inline_ray_query", caps), "revision": revision}
        nw05_export._refresh_manifest_sha256(link)
        manifest = nw05_export.optimizer.manifest_bytes(link)
        executable = nw05_export.embed_package(pe, package, manifest)
        arm = emitted / path
        arm.mkdir(parents=True)
        for file, data in ((handoff.PLAYER, executable), ("common.nwp", package), ("link-manifest.json", manifest)):
            (arm / file).write_bytes(data)
        records[name] = {
            "distribution_bytes": sum(map(len, [executable, package, manifest])),
            "shader_bytes": 16, "dependency_identities": [], "package_sha256": handoff.digest(package),
            "audio_song_sha256": handoff.digest(song), "executable_sha256": handoff.digest(executable),
            "manifest_sha256": link["manifest_sha256"], "manifest_file_sha256": handoff.digest(manifest),
            "film_lowering": link.get("film_lowering"),
            "smoke": {"status": "pass", "executed": True, "returncode": 0, "headless": True},
        }
    reference = struct.pack("<ffff", .125, .25, .5, 1.0) * (size[0] * size[1])
    (emitted / "common-fixed-ticks").mkdir()
    frames = []
    for tick in sorted(production["fixed_ticks"]):
        file = f"tick-{tick:06d}.rgba32f"
        (emitted / "common-fixed-ticks" / file).write_bytes(reference)
        frames.append({"tick": tick, "file": file, "sha256": handoff.digest(reference),
                       "reference": "graph_cpu_oracle", "image_within_tolerance": True})
    sources = {name: {"path": path, "sha256": handoff.sha(repo / path)}
               for name, path in (("production", PRODUCTION), ("graph", production["graph"]), ("song", production["song"]))}
    sources["song"]["nwas_sha256"] = handoff.digest(song)
    record = film.export_artifacts(production, capabilities=caps, common_observation=records["common"],
        fallback_observation=records["fallback"], requested_features=["dxr_inline_ray_query"],
        optional_observations={"dxr_inline_ray_query": records["dxr"]}, independent_builds=True)
    record = film.seal_bundle({**record, "independent_observations": {"a": records["common"], "b": records["common"]},
        "optional_rebuilds": {"dxr_inline_ray_query": {"a": records["dxr"], "b": records["dxr"]}},
        "measured_sources": sources, "native_fixed_ticks": {"common": {"status": "measured_native", "image_within_tolerance": True,
           "width": size[0], "height": size[1], "frames": frames}}})
    handoff.write_json(emitted / "artifact-bundle.v1.json", record)
    runtime = dir / "runtime"
    for relative in ("python.exe", "LICENSE.txt", "Lib/json/__init__.py", "Lib/encodings/__init__.py"):
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    output = dir / "film-physical.zip"
    receipt = handoff.stage(repo=repo, emitted=emitted, probe=probe, python_root=runtime, output=output, revision=revision)
    return output, receipt


def unpack(tmp_path, prepared):
    archive, receipt = prepared
    root = tmp_path / "bundle"
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        z.extractall(root)
    ids = dict(index_sha=receipt["index_sha256"], revision=receipt["revision"], tree=receipt["tree"])
    return root, ids


def test_stage_and_verify_exact_native_bundle(tmp_path, prepared):
    root, ids = unpack(tmp_path, prepared)
    result = handoff.verify(root, **ids)
    assert set(result["arms"]) == set(handoff.ARMS)
    assert set(result["native_dependency_closure"]) == {handoff.PROBE, *(arm["executable"] for arm in result["arms"].values())}
    assert (root / "run-film.ps1").is_file() and (root / "python/python.exe").is_file()
    assert result["render_size"] == [48, 27]
    for arg in ("index_sha", "tree", "revision"):
        with pytest.raises(handoff.HandoffError):
            handoff.verify(root, **{**ids, arg: "0" * len(ids[arg])})


@pytest.mark.parametrize("change", ["missing", "player", "package", "manifest", "song", "stale", "extra"])
def test_prelaunch_integrity_is_fail_closed(tmp_path, prepared, change):
    root, ids = unpack(tmp_path, prepared)
    if change == "missing":
        (root / handoff.PROBE).unlink()
    elif change == "extra":
        (root / "not-an-artifact").write_bytes(b"extra")
    else:
        path = {"player": f"arms/common/{handoff.PLAYER}", "package": "arms/dxr/common.nwp",
                "manifest": "arms/fallback/link-manifest.json", "song": "source/fixtures/nw13/hotel_corridor.song.json",
                "stale": "source/fixtures/nw13/hotel_corridor.nwgraph"}[change]
        (root / path).write_bytes(b"mismatch")
    with pytest.raises(handoff.HandoffError):
        handoff.run(root=root, output=tmp_path / "out", **ids, adapter_luid="00000000:00000001",
                    windows=True, execute=lambda *_, **__: pytest.fail("launched unverified artifact"))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(("tier", "expected", "normalized"), [
    (0, "unsupported", 0.0), (10, "unsupported", 1.0), (11, "pass", 1.1), (12, "pass", 1.2),
    (None, "unavailable", None), (13, "unavailable", None), ("12", "unavailable", None), (True, "unavailable", None),
])
def test_simulated_run_records_independent_axes(tmp_path, prepared, monkeypatch, tier, expected, normalized):
    root, ids = unpack(tmp_path, prepared)
    monkeypatch.setattr(handoff.gpu, "observe", lambda *_args, **_kwargs: {"virtualization_probe": {}})
    monkeypatch.setattr(handoff.host, "evaluate_physical_virtualization_probe", lambda _: {"valid": True})
    monkeypatch.setattr(handoff.gpu, "qualify_with_diagnostics", lambda *_args, **_kwargs: (None, []))
    luid = "00000000:00123456"
    adapter = {"adapter_luid": luid, "vendor_id": 0x10DE, "device_id": 0x2803, "adapter_name": "Fixture", "software": False}
    features = {"raytracing_tier": tier, "highest_shader_model": "D3D12_SHADER_MODEL_6_8",
                "d3d_feature_level": "D3D_FEATURE_LEVEL_12_2", "mesh_shader_tier": "D3D12_MESH_SHADER_TIER_1"}
    calls = []
    def execute(command, details, **kw):
        details.mkdir()
        stdout = "NW05_VENDOR_ID=4318\nNW05_DEVICE_ID=10243\nNW05_PRESENTS=1\nNW05_PRESENT_SYNC_INTERVAL=1\n"
        if "--json" in command:
            handoff.write_json(command[-1], {"status": "ok", "adapter": adapter, "adapters": [adapter],
                                              "driver_version": "fixture", "features": features})
        else:
            calls.append(command[0].relative_to(root).as_posix())
            assert "--maximized" in command and "--allow-software-adapter" not in command
            assert kw["env"]["NODEWERK_ADAPTER_LUID"] == luid
            if "--tick" in command:
                tick = int(command[command.index("--tick") + 1])
                data = (root / handoff.reference_name(tick)).read_bytes()
                command[-1].write_bytes(data)
                stdout += f"NW05_EXPLICIT_TICK={tick}\nNW05_IMAGE_BYTES={len(data)}\n"
        (details / "stdout.bin").write_text(stdout, encoding="utf-8")
        (details / "stderr.bin").write_text(f"NODEWERK_SELECTED_ADAPTER_LUID={luid}\n", encoding="utf-8")
        return {"returncode": 0, "timed_out": False, "presentmon": None}
    report = handoff.run(root=root, output=tmp_path / "out", **ids, adapter_luid=luid, windows=True, execute=execute)
    assert report["bundle_unchanged"] and report["adoption"]["status"] == "not_decided"
    assert set(report["arms"]) == set(handoff.ARMS)
    shipped = handoff.verify(root, **ids)["arms"]
    assert shipped["fallback"]["executable"] in calls
    assert (shipped["dxr"]["executable"] in calls) is (expected == "pass")
    assert report["host"]["dxr_tier"] == (tier if normalized is not None else None)
    assert report["host"]["dxr_tier_status"] == (
        "unknown" if normalized is None else "unsupported" if tier == 0 else "reported")
    if expected != "pass":
        assert report["arms"]["dxr"]["status"] == expected
    for name, arm in report["arms"].items():
        if name == "dxr" and expected != "pass":
            continue
        assert arm["functional"]["status"] == arm["correctness"]["status"] == "pass"
        assert arm["performance"]["status"] == "unavailable"
        assert arm["correctness"]["display_pixels"]["status"] == "unavailable"
    public = json.dumps(report)
    assert luid not in public and str(root) not in public
    def query(command, **_kwargs):
        handoff.write_json(Path(command[-1]), {"features": features, "adapter": adapter})
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(measure.subprocess, "run", query)
    capabilities, queried_luid = measure._query_capabilities(root / handoff.PROBE, tmp_path)
    assert queried_luid == luid and capabilities["raytracing_tier"] == normalized
    assert "adapter_luid" not in capabilities


def packaged_cli(tmp_path, bundle, name, *args):
    env = {key: value for key, value in os.environ.items() if key.upper() != "PYTHONPATH"}
    completed = subprocess.run([sys.executable, "-I", "-B", bundle / "tools" / name, *args],
                               cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def verify_packaged_cli(tmp_path, root, ids):
    stdout = packaged_cli(tmp_path, root, "nw13_physical_film.py", "verify",
                          "--root", root, "--index-sha", ids["index_sha"], "--revision", ids["revision"],
                          "--tree", ids["tree"])
    return json.loads(stdout)


def test_staged_isolated_tools_verify_without_checkout_or_pythonpath(tmp_path, prepared):
    root, ids = unpack(tmp_path, prepared)
    assert verify_packaged_cli(tmp_path, root, ids)["revision"] == ids["revision"]
    assert "--help" in packaged_cli(tmp_path, root, "audio_physical_validation.py", "--help")


def test_physical_preflight_never_runs_renderer_on_virtual_host(tmp_path, prepared, monkeypatch):
    root, ids = unpack(tmp_path, prepared)
    monkeypatch.setattr(handoff.gpu, "observe", lambda *_args, **_kwargs: {"virtualization_probe": {}})
    monkeypatch.setattr(handoff.host, "evaluate_physical_virtualization_probe", lambda _: {"valid": False})
    luid = "00000000:00123456"
    adapter = {"adapter_luid": luid, "vendor_id": 0x10DE, "device_id": 0x2803, "adapter_name": "Fixture", "software": False}
    def execute(command, *_args, **_kwargs):
        assert command[0] == root / handoff.PROBE
        handoff.write_json(command[-1], {"status": "ok", "adapter": adapter, "adapters": [adapter]})
        return {"returncode": 0, "timed_out": False}
    out = tmp_path / "unqualified"
    with pytest.raises(handoff.HandoffError, match="physical/virtualization"):
        handoff.run(root=root, output=out, **ids, adapter_luid=luid, windows=True, execute=execute)
    report = json.loads((out / "summary.json").read_text())
    assert report["status"] == "failed" and report["arms"] == {} and not (out / "private/runs").exists()


def fixture_csv(clock="CPUStartQPCTimeInMs", change=None):
    v2 = clock == "CPUStartQPCTime"
    prefix = ["Application", "ProcessID", "SwapChainAddress", "PresentRuntime", "SyncInterval",
              "PresentFlags", "AllowsTearing", "PresentMode"]
    # PresentMon v2.6.0 CsvOutput.cpp uses mutually exclusive column modes.
    # GPU and display are optional but both enabled in this fixture.
    if v2:
        header = prefix + [clock, "FrameTime", "CPUBusy", "CPUWait", "GPULatency", "GPUTime",
                           "GPUBusy", "GPUWait", "DisplayLatency", "DisplayedTime",
                           "AnimationError", "AnimationTime", "MsFlipDelay"]
    else:
        header = prefix + ["TimeInSeconds", "MsBetweenSimulationStart", "MsBetweenPresents",
                           "MsBetweenDisplayChange", "MsInPresentAPI", "MsRenderPresentLatency",
                           "MsUntilDisplayed", clock, "MsBetweenAppStart", "MsCPUBusy", "MsCPUWait",
                           "MsGPULatency", "MsGPUTime", "MsGPUBusy", "MsGPUWait",
                           "MsAnimationError", "AnimationTime", "MsFlipDelay"]
    extra_clock = {"ambiguous_clock": "CPUStartQPCTime", "competing_unit": "CPUStartQPC"}.get(change)
    if extra_clock:
        header.append(extra_clock)
    if change == "hybrid_present_api":
        header.append("MsInPresentAPI")
    rows = []
    for index in range(6):
        early = index == 5
        stamp = ("1100" if early else "400100" if change == "wrong_epoch"
                 else "NaN" if change == "invalid_stamp" else str(2001 + index))
        displayed = "150" if early else "NA" if change == "missing_display" else "16.5"
        values = {
            "Application": handoff.PLAYER, "ProcessID": "501" if change == "foreign_process" and not early else "500",
            "SwapChainAddress": "0x20" if early else "0x0B" if change == "mixed_swapchains" and index == 4 else "0x01",
            "PresentRuntime": "DXGI", "SyncInterval": "0" if change == "mismatched_sync" else "1",
            "PresentFlags": "0", "AllowsTearing": "0", "PresentMode": "Composed: Flip",
            clock: stamp, "TimeInSeconds": "1.002", "FrameTime": "16.5",
            "GPUTime": "20" if early else "2.0", "MsGPUTime": "20" if early else "2.0",
            "CPUBusy": "12" if early else "0.5", "MsCPUBusy": "12" if early else "0.5",
            "MsInPresentAPI": "3" if early else "0.25",
            "DisplayedTime": displayed, "MsBetweenDisplayChange": displayed,
        }
        if extra_clock:
            values[extra_clock] = stamp
        rows.append([values.get(field, "NA") for field in header])
    return "\n".join(map(",".join, [header, *rows])).encode()


def fixture_collector(binary, csv_path, file_hash=None):
    record = {"format": "nw07-presentmon-collector", "version": 1, "process_id": 500, "exit_code": 0,
              "presentmon_path": str(binary), "presentmon_sha256": handoff.PRESENTMON_HASH,
              "command": [str(binary), "--process_id", "500", "--timed", "5", "--terminate_after_timed",
                          "--terminate_on_proc_exit", "--output_file", str(csv_path), "--no_console_stats",
                          "--qpc_time_ms", "--session_name", "NW07-500-common"]}
    if file_hash is not None:
        record.update(clock="absolute_qpc_ms", csv_sha256=file_hash)
    return record


@pytest.mark.parametrize(("clock", "change", "reason"), [
    ("CPUStartQPCTimeInMs", None, None), ("CPUStartQPCTime", None, None),
    ("CPUStartQPCTimeInMs", "missing_display", None),
    ("CPUStartQPCTime", "hybrid_present_api", "etw_schema_or_clock_unavailable"),
    ("CPUStartQPCTime", "wrong_column_mode", "etw_schema_or_clock_unavailable"),
    ("CPUStartQPCTimeInMs", "wrong_column_mode", "etw_schema_or_clock_unavailable"),
    ("CPUStartQPC", None, "etw_schema_or_clock_unavailable"),
    ("CPUStartQPCTimeInMs", "ambiguous_clock", "etw_schema_or_clock_unavailable"),
    ("CPUStartQPCTimeInMs", "competing_unit", "etw_schema_or_clock_unavailable"),
    ("CPUStartQPCTimeInMs", "mismatched_sync", "unmatched_sync_interval_or_swapchain"),
    ("CPUStartQPCTimeInMs", "mixed_swapchains", "unmatched_sync_interval_or_swapchain"),
    ("CPUStartQPCTimeInMs", "foreign_process", "etw_process_window_unavailable"),
    ("CPUStartQPCTimeInMs", "wrong_epoch", "etw_process_window_unavailable"),
    ("CPUStartQPCTimeInMs", "invalid_stamp", "invalid_etw_samples"),
    ("CPUStartQPCTimeInMs", "tampered_csv", "etw_csv_identity_mismatch"),
    ("CPUStartQPCTimeInMs", "wrong_collector_identity", "etw_collector_identity_mismatch"),
])
def test_timing_uses_only_current_process_clock_and_swapchain(tmp_path, clock, change, reason):
    path = tmp_path / handoff.timing.PRESENTMON_CSV
    path.write_bytes(fixture_csv(clock, change))
    meta = fixture_collector(tmp_path / "PresentMon.exe", path, handoff.sha(path))
    v2 = clock == "CPUStartQPCTime"
    if v2 != (change == "wrong_column_mode"):
        meta["command"].append("--v2_metrics")
    if change == "tampered_csv":
        path.write_bytes(path.read_bytes() + b"\n")
    if change == "wrong_collector_identity":
        meta["process_id"] = 501
    handoff.write_json(tmp_path / handoff.timing.PRESENTMON_METADATA, meta)
    report = handoff.performance(tmp_path, {"presentmon": path.name, "pid": 500,
        "start_qpc_ms": 1000, "end_qpc_ms": 4000}, 1)
    if reason is None:
        assert report["status"] == ("partial" if v2 else "observed")
        assert report["gpu_time"] == {"status": "observed", "count": 5, "median_ms": 2}
        if v2:
            assert report["cpu_submit"] == {"status": "unavailable", "reason": "present_api_time_not_reported_by_schema"}
        else:
            assert report["cpu_submit"]["median_ms"] == 0.75
        assert report["clock"] == "absolute_qpc_ms"
        if change == "missing_display":
            assert report["display_intervals"]["status"] == "unavailable"
        else:
            assert report["display_intervals"]["median_ms"] == 16.5
    else:
        assert report["status"] == "unavailable" and report["reason"] == reason


@pytest.mark.parametrize(("stderr", "reason"), [
    (b"error: failed to start trace session: access denied.\r\nprivate log detail", "etw_access_denied"),
    (b"error: failed to start trace session: invalid --session_name.", "collector_failed"),
])
def test_optional_collector_classifies_actual_permission_without_disclosing_private_logs(tmp_path, monkeypatch, stderr, reason):
    presentmon = tmp_path / "PresentMon.exe"
    presentmon.write_bytes(b"fixture")
    output = tmp_path / "visible"
    first = subprocess.Popen
    monkeypatch.setattr(handoff.timing.subprocess, "run",
                        lambda args, **kwargs: subprocess.CompletedProcess(args, 6, b"", stderr))
    launch = handoff.capture_process([sys.executable, "-B", "-c", "print('functional')"], output,
                                     cwd=tmp_path, presentmon=presentmon)
    assert subprocess.Popen is first and launch["returncode"] == 0
    assert (output / "presentmon-stderr.bin").read_bytes() == stderr
    assert json.loads((output / "presentmon-metadata.json").read_text())["exit_code"] == 6
    report = handoff.performance(output, launch, 1)
    assert report["reason"] == reason and report["collector_exit_code"] == 6
    assert "private log detail" not in json.dumps(report)
    assert ("administrator" in report.get("action", "")) is (reason == "etw_access_denied")


def test_real_inspection_source_builds_bound_distinct_arms_without_claiming_hosted_execution(tmp_path, monkeypatch):
    from nw13_physical_inspection import build

    relative = "fixtures/nw13/hotel_corridor.inspection.production.json"
    if not (ROOT / relative).is_file():
        pytest.skip("the separately authored native inspection profile is not in this checkout")
    repo = tmp_path / "repo"
    repo.mkdir()
    d = nw13_production.load_production(ROOT, relative)
    assert [d["rendering"][key] for key in ("width", "height")] == [320, 180]
    sources = [relative, d["graph"], d["song"], "tools/nw13/run-film.ps1",
               *(f"tools/{name}" for name in handoff.HELPERS)]
    for source in sources:
        dst = repo / source
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, dst)
    def git(*command):
        return subprocess.run(["git", *command], check=True, capture_output=True, text=True, cwd=repo).stdout.strip()
    git("init")
    git("config", "core.autocrlf", "false")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "inspection sources")
    revision = git("rev-parse", "HEAD")
    player, probe, emitted = tmp_path / "player.exe", tmp_path / "probe.exe", tmp_path / "emitted"
    pe = build_synthetic_pe64(["kernel32.dll", "d3d12.dll"])
    player.write_bytes(pe)
    probe.write_bytes(pe)
    dxc = Path(nw09_export._test_dxc(tmp_path / "dxc"))
    calls = []
    def fake_export(graph_path, delivered, build_path, template, dxc, song, *, selection, revision):
        assert graph_path.read_bytes() == (repo / d["graph"]).read_bytes()
        assert template == player and dxc.is_file()
        assert revision == git("rev-parse", "HEAD")
        calls.append((delivered.relative_to(emitted).as_posix(), build_path))
        if selection:
            assert selection["capability_provenance"] == "compile_target_only_not_observed"
            selection = {**selection, "revision": revision}
        # Exercise the real container/manifest/PE readers only. Reuse the
        # previous known-good package fixture and overwrite its NWAS entirely.
        raw = nwp.encode_package(replace(base_artifact, audio_song=song))
        link = {"package_sha256": handoff.digest(raw), "present_geometry": {"width": 320, "height": 180}}
        if selection:
            link["film_lowering"] = selection
        nw05_export._refresh_manifest_sha256(link)
        manifest = nw05_export.optimizer.manifest_bytes(link)
        executable = nw05_export.embed_package(pe, raw, manifest)
        delivered.mkdir(parents=True)
        for filename, data in ((handoff.PLAYER, executable), ("common.nwp", raw), ("link-manifest.json", manifest)):
            (delivered / filename).write_bytes(data)
        return {"executable_sha256": handoff.digest(executable), "package_sha256": handoff.digest(raw),
                "manifest_file_sha256": handoff.digest(manifest), "manifest_sha256": link["manifest_sha256"],
                "audio_song_sha256": handoff.digest(song)}

    # Reuse the regular graph's real NWP codec fixture without pretending
    # Python could produce a native high-resolution Windows shader/PE here.
    base_result = nw05_export.export_graph(ROOT / nw13_production.load_production(ROOT, PRODUCTION)["graph"],
        tmp_path / "base-codec", allow_test_payloads=True, dxc_executable=str(dxc))
    base_artifact = base_result.artifacts[nwp.COMMON_PROFILE_ID].package
    build(repo, Path(relative), emitted, player, dxc, revision, windows=True, export=fake_export,
          describe=lambda result: result)
    assert [lane for lane, _ in calls] == list(handoff.ARMS.values())
    assert len({build_path for _, build_path in calls}) == 3
    observed = json.loads((emitted / handoff.INSPECTION_INPUT).read_text())
    assert observed["hosted_execution"] == "not_run"
    assert set(observed["ticks"]) >= set(d["fixed_ticks"])
    runtime = tmp_path / "runtime"
    for relative in ("python.exe", "LICENSE.txt", "Lib/json/__init__.py", "Lib/encodings/__init__.py"):
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    archive = tmp_path / "inspection-physical.zip"
    receipt = handoff.stage(repo=repo, emitted=emitted, probe=probe, python_root=runtime, output=archive,
                            revision=revision, inspection=True)
    root, ids = unpack(tmp_path, (archive, receipt))
    result = handoff.verify(root, **ids)
    assert result["image_reference"] == handoff.PEER
    assert result["render_size"] == verify_packaged_cli(tmp_path, root, ids)["render_size"] == [320, 180]
    assert not (root / "reference").exists()
    assert result["graph_sha256"] == handoff.sha(repo / d["graph"])


def test_reanalyze_entrypoint_binds_legacy_sidecars_without_rewriting_them(tmp_path, prepared, monkeypatch, capsys):
    root, ids = unpack(tmp_path, prepared)
    # The synthetic bundle substitutes a test-only PresentMon digest. No
    # Windows binary, collector launch or elevation is needed for replay.
    tool = root / handoff.PRESENTMON
    tool.parent.mkdir(exist_ok=True)
    tool.write_bytes(b"synthetic pinned collector")
    tool_sha = handoff.sha(tool)
    monkeypatch.setattr(handoff, "PRESENTMON_HASH", tool_sha)
    monkeypatch.setattr(handoff.timing, "PRESENTMON_260_SHA256", tool_sha)
    idx = root / handoff.INDEX
    values = json.loads(idx.read_text())
    values["files"][handoff.PRESENTMON] = tool_sha
    handoff.write_json(idx, values)
    ids["index_sha"] = handoff.sha(idx)
    identity = {key: values["arms"]["common"][key]
                for key in ("executable_sha256", "package_sha256", "manifest_sha256", "song_sha256")}
    evidence = tmp_path / "retained"
    lane = evidence / "private/runs/common/visible"
    lane.mkdir(parents=True)
    luid = "00000000:12345678"
    adapter = {"adapter_luid": luid, "vendor_id": 0x10DE, "device_id": 0x2803,
               "adapter_name": "Fixture", "software": False}
    summary = evidence / "summary.json"
    capability = evidence / "private/capability.json"
    handoff.write_json(summary, {
        "format": "nw13.physical-film-evidence", "version": 1, "status": "completed", "bundle_unchanged": True,
        "revision": ids["revision"], "tree": ids["tree"], "bundle_index_sha256": ids["index_sha"],
        "render_size": values["render_size"], "window_mode": "maximized",
        "host": {"status": "physical", "device": "pci:10de:2803"},
        "arms": {"common": {"identity": identity, "functional": {"status": "pass", "queried_adapter_bound": True},
                           "metrics": {"successful_present_calls": 7, "present_sync_interval": 1}}},
    })
    handoff.write_json(capability, {"status": "ok", "adapter": adapter, "adapters": [adapter], "driver_version": "fixture"})
    stdout, stderr = lane / "stdout.bin", lane / "stderr.bin"
    stdout.write_text("NW05_VENDOR_ID=4318\nNW05_DEVICE_ID=10243\nNW05_PRESENTS=7\nNW05_PRESENT_SYNC_INTERVAL=1\n")
    stderr.write_text(f"NODEWERK_SELECTED_ADAPTER_LUID={luid}\n")
    csv_path = lane / handoff.timing.PRESENTMON_CSV
    csv_path.write_bytes(fixture_csv())
    launch = lane / "launch.json"
    from pathlib import PureWindowsPath
    old_bundle = PureWindowsPath(r"C:\nw13\old-bundle")
    old_csv = PureWindowsPath(r"C:\nw13\old-evidence\private\runs\common\visible") / csv_path.name
    original = {"argv": [str(old_bundle / values["arms"]["common"]["executable"]), "--maximized", "--frames", "420"],
                "pid": 500, "start_qpc_ms": 1000, "end_qpc_ms": 4000, "returncode": 0, "timed_out": False,
                "presentmon": csv_path.name, "presentmon_error": None,
                "stdout_sha256": handoff.sha(stdout), "stderr_sha256": handoff.sha(stderr)}
    handoff.write_json(launch, original)
    collector = lane / handoff.timing.PRESENTMON_METADATA
    handoff.write_json(collector, fixture_collector(old_bundle / handoff.PRESENTMON, old_csv))

    def cli():
        return ["reanalyze", "--root", str(root), "--evidence", str(evidence), "--arm", "common",
                "--index-sha", ids["index_sha"], "--revision", ids["revision"], "--tree", ids["tree"],
                "--summary-sha", handoff.sha(summary), "--capability-sha", handoff.sha(capability),
                "--launch-sha", handoff.sha(launch), "--collector-sha", handoff.sha(collector),
                "--csv-sha", handoff.sha(csv_path)]
    before = {str(file): handoff.sha(file) for parent in (root, evidence) for file in parent.rglob("*") if file.is_file()}
    arguments = cli()
    assert handoff.main(arguments) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["scope"] == "historical_only" and report["performance"]["status"] == "observed"
    assert report["performance"]["raw_csv_sha256"] == handoff.sha(csv_path)
    assert luid not in output and str(old_bundle) not in output
    assert before == {str(file): handoff.sha(file) for parent in (root, evidence) for file in parent.rglob("*") if file.is_file()}
    old_csv_bytes = csv_path.read_bytes()
    csv_path.write_bytes(old_csv_bytes + b"\n")
    assert handoff.main(arguments) == 2
    assert "historical presentmon.csv hash differs" in capsys.readouterr().err
    csv_path.write_bytes(old_csv_bytes)
    original["argv"][0] = str(old_bundle / values["arms"]["fallback"]["executable"])
    handoff.write_json(launch, original)
    assert handoff.main(cli()) == 2
    assert "does not name the selected verified arm" in capsys.readouterr().err
