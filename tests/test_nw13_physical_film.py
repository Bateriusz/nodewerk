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


def test_simulated_run_records_independent_axes(tmp_path, prepared, monkeypatch):
    root, ids = unpack(tmp_path, prepared)
    monkeypatch.setattr(handoff.gpu, "observe", lambda *_args, **_kwargs: {"virtualization_probe": {}})
    monkeypatch.setattr(handoff.host, "evaluate_physical_virtualization_probe", lambda _: {"valid": True})
    monkeypatch.setattr(handoff.gpu, "qualify_with_diagnostics", lambda *_args, **_kwargs: (None, []))
    luid = "00000000:00123456"
    adapter = {"adapter_luid": luid, "vendor_id": 0x10DE, "device_id": 0x2803, "adapter_name": "Fixture", "software": False}
    features = {"raytracing_tier": 11, "highest_shader_model": "D3D12_SHADER_MODEL_6_5"}
    def execute(command, details, **kw):
        details.mkdir()
        stdout = "NW05_VENDOR_ID=4318\nNW05_DEVICE_ID=10243\nNW05_PRESENTS=1\nNW05_PRESENT_SYNC_INTERVAL=1\n"
        if "--json" in command:
            handoff.write_json(command[-1], {"status": "ok", "adapter": adapter, "adapters": [adapter],
                                              "driver_version": "fixture", "features": features})
        else:
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
    for arm in report["arms"].values():
        assert arm["functional"]["status"] == arm["correctness"]["status"] == "pass"
        assert arm["performance"]["status"] == "unavailable"
        assert arm["correctness"]["display_pixels"]["status"] == "unavailable"
    public = json.dumps(report)
    assert luid not in public and str(root) not in public


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


@pytest.mark.parametrize("change", [None, "mismatched_sync", "mixed_swapchains", "foreign_process"])
def test_timing_uses_only_current_process_clock_and_swapchain(tmp_path, change):
    filename = "capture.csv"
    headers = ["ProcessID", "CPUStartQPCTime", "SyncInterval", "SwapChainAddress",
               "GPUTime", "CPUBusy", "MsInPresentAPI", "DisplayedTime"]
    rows = []
    for i in range(5):
        rows.append(["501" if change == "foreign_process" else "500", str(2001 + i),
                     "0" if change == "mismatched_sync" else "1",
                     "different" if change == "mixed_swapchains" and i == 4 else "primary",
                     "2.0", "0.5", "0.25", "16.5"])
    # A genuine process row outside the allowed window is never included.
    rows.append(["500", "1100", "1", "early", "20", "12", "3", "150"])
    (tmp_path / filename).write_text("\n".join(map(",".join, [headers, *rows])), encoding="utf-8")
    report = handoff.performance(tmp_path, {"presentmon": filename, "pid": 500,
        "start_qpc_ms": 1000, "end_qpc_ms": 4000}, 1)
    if change is None:
        assert report["status"] == "observed"
        assert report["gpu_time"] == {"status": "observed", "count": 5, "median_ms": 2}
        assert report["display_intervals"]["median_ms"] == 16.5
    else:
        assert report["status"] == "unavailable"


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
