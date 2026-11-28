"""Offline tests for exact Work Graphs delivery and replayable paired decisions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import nw13_evidence as evidence
import nw13_work_graph as runner
import nw13_work_graph_protocol as contract
from test_dependency_closure_and_accounting import build_synthetic_pe64


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "Synthetic Work Graph Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "config", "core.autocrlf", "false")
    for name in runner.MEASURED_SOURCES:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    git(source, "add", ".")
    git(source, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Synthetic source")
    revision = git(source, "rev-parse", "HEAD")
    build = tmp_path / "native"
    build.mkdir()
    for name in (runner.EXPERIMENT, runner.PROBE):
        (build / name).write_bytes(build_synthetic_pe64(["kernel32.dll", "dxgi.dll", "d3d12.dll"]))
    (build / runner.BASELINE).write_bytes(b"fixture baseline shader")
    (build / runner.OPTIONAL).write_bytes(b"fixture optional shader")
    python = tmp_path / "python"
    for name in ("python.exe", "LICENSE.txt", "Lib/json/__init__.py", "Lib/encodings/__init__.py"):
        path = python / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic runtime member")
    warp = tmp_path / "ci-warp.json"
    runner.write_json(warp, {
        "format": "nw13.work-graph-run", "version": 2, "qualification": "hosted_warp_conventional_only",
        "status": "functional_only", "satisfies_completion": False, "revision": revision,
        "source_tree": git(source, "rev-parse", "HEAD^{tree}"),
        "inputs": {name: runner.identity(build / name) for name in runner.NATIVE},
        "observation": {"status": "warp_baseline_executed", "adapter": {"software": True},
                        "oracle_slots_checked": contract.CANDIDATES, "output_hash": 1188270820},
    })
    args = (source, build, build / runner.PROBE, python, warp)
    return args, revision


@pytest.fixture
def bundle(tmp_path, prepared):
    args, revision = prepared
    root, trust_path = tmp_path / "bundle", tmp_path / "external-trust.json"
    trust = runner.stage(*args, root, trust_path, revision)
    return root, trust, revision


def test_exact_bundle_files_and_extracted_normal_cli(tmp_path, bundle):
    root, trust, revision = bundle
    record = runner.verify_bundle(root, trust, revision)
    assert set(record["files"]) == set(runner.MEMBERS)
    assert all(not part.startswith(".") for name in record["files"] for part in name.split("/"))
    assert set(record["windows_inbox_imports"]) == {runner.PROBE, runner.EXPERIMENT}
    assert record["source"]["tree"] == trust["tree"]
    with zipfile.ZipFile(root / runner.RUNTIME) as runtime:
        assert {"LICENSE.txt", "python.exe", "Lib/encodings/__init__.py"} <= set(runtime.namelist())
    # An independent ZIP witness and a standalone isolated tool tree, no PYTHONPATH.
    archive, trust_path = tmp_path / "ci.zip", tmp_path / "witness.json"
    with zipfile.ZipFile(archive, "w") as zipped:
        for name in [runner.MANIFEST, *runner.MEMBERS]:
            zipped.write(root / name, name)
    trust |= {"archive_sha256": runner.sha256(archive), "github_artifact_id": "123",
              "github_workflow_run": "456", "github_workflow_attempt": "1"}
    runner.write_json(trust_path, trust)
    command = [sys.executable, "-I", "-B", "-S", str(root / "tools/nw13_work_graph.py"), "verify",
               "--bundle", str(root), "--revision", revision, "--archive", str(archive),
               "--trust", str(trust_path)]
    subprocess.run(command, check=True)
    archive.write_bytes(archive.read_bytes() + b"changed")
    invalid = subprocess.run(command, capture_output=True, text=True)
    assert invalid.returncode == 2 and "delivery witness" in invalid.stderr


@pytest.mark.parametrize("mutation", ["shader", "probe", "runner", "missing", "extra", "source", "manifest", "trust"])
def test_inventory_refuses_mixed_handoffs(bundle, mutation):
    root, trust, revision = bundle
    if mutation == "trust":
        trust["tree"] = "0" * 40
    elif mutation == "extra":
        (root / "bin/side-load.dll").write_bytes(b"not expected")
    elif mutation == "missing":
        (root / "bin" / runner.EXPERIMENT).unlink()
    else:
        filename = {"shader": f"bin/{runner.OPTIONAL}", "probe": f"bin/{runner.PROBE}",
                    "runner": "tools/nw13_work_graph.py", "source": "source/src/nw13/work_graph.cpp",
                    "manifest": runner.MANIFEST}[mutation]
        p = root / filename
        p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(runner.EvidenceError):
        runner.verify_bundle(root, trust, revision)


@pytest.mark.parametrize("failure", ["dirty_source", "wrong_checkout", "runtime_dll", "inconsistent_warp"])
def test_stage_refuses_untrusted_build_inputs_without_publication(tmp_path, prepared, failure):
    args, revision = prepared
    source, build, _probe, _python, warp = args
    if failure == "dirty_source":
        (source / "src/nw13/work_graph.cpp").write_text("not the built checkout")
    elif failure == "wrong_checkout":
        revision = "0" * 40
    elif failure == "inconsistent_warp":
        record = json.loads(warp.read_bytes())
        record["inputs"][runner.EXPERIMENT]["sha256"] = "0" * 64
        runner.write_json(warp, record)
    else:
        (build / runner.EXPERIMENT).write_bytes(build_synthetic_pe64(["vcruntime140.dll"]))
    root, trust = tmp_path / "fresh", tmp_path / "new-trust.json"
    with pytest.raises(ValueError):
        runner.stage(*args, root, trust, revision)
    assert not root.exists() and not trust.exists()


ADAPTER = {"adapter_luid": "00000000:00000006", "vendor_id": 0x10DE, "device_id": 0x2803,
           "subsystem_id": 22, "revision": 3, "software": False}


def samples(manifest, *, variant=94):
    ends = 0
    oracle_hash = contract.oracle_hash()

    def pair(order, durations=(100, variant)):
        nonlocal ends
        raw = {}
        for arm in order:
            begin = ends + 7
            ends = begin + durations[arm == "work_graph"]
            raw[arm] = {"begin_ticks": begin, "end_ticks": ends, "output_hash": oracle_hash,
                        "oracle_slots_checked": contract.CANDIDATES}
        return raw

    arms = ("conventional", "work_graph")
    result = {"status": "measured", "adapter": dict(ADAPTER), "candidates": contract.CANDIDATES,
              "tick": contract.TICK, "samples_per_arm": contract.SAMPLES_PER_ARM, "timestamp_frequency_hz": 1000,
              "work_graph_tier": 10, "shader_model": 0x68, "backing_bytes": 65536,
              "backing_required_bytes": 100,
              "conventional_shader_bytes": manifest["files"][f"bin/{runner.BASELINE}"]["bytes"],
              "work_graph_shader_bytes": manifest["files"][f"bin/{runner.OPTIONAL}"]["bytes"],
              "warmup": pair(arms, (4, 8)), "pairs": []}
    for index in range(contract.SAMPLES_PER_ARM):
        order = arms if index % 2 == 0 else arms[::-1]
        result["pairs"].append({"pair": index, "first": order[0], **pair(order)})
    return result


@pytest.mark.parametrize("variant,expected", [(94, "adopt"), (95, "reject"), (120, "reject")])
def test_score_uses_raw_same_queue_ticks_and_strict_95_percent(bundle, variant, expected):
    root, trust, revision = bundle
    observation = samples(runner.verify_bundle(root, trust, revision), variant=variant)
    observation["work_graph_gpu_median_us"] = 1  # Never trust the native summary field.
    result = contract.recompute(observation,
                                conventional_shader_bytes=observation["conventional_shader_bytes"],
                                work_graph_shader_bytes=observation["work_graph_shader_bytes"])
    assert result["decision"] == expected
    assert result["dispatch_median_ticks"] == {"conventional": 100, "work_graph": variant}
    assert result["measured_ratio"] == variant / 100


@pytest.mark.parametrize("fault", ["no-pairs", "nonpositive", "wrong-order", "oracle", "missing-warmup", "backing"])
def test_invalid_or_absent_raw_proof_is_not_a_measured_rejection(bundle, fault):
    root, trust, revision = bundle
    sample = samples(runner.verify_bundle(root, trust, revision))
    arm = sample["pairs"][0]["conventional"]
    if fault == "no-pairs":
        sample["pairs"].pop()
    elif fault == "nonpositive":
        arm["end_ticks"] = arm["begin_ticks"]
    elif fault == "wrong-order":
        sample["pairs"][0]["first"] = "work_graph"
    elif fault == "oracle":
        arm["output_hash"] ^= 1
    elif fault == "missing-warmup":
        sample.pop("warmup")
    else:
        sample["backing_bytes"] = contract.MAX_BACKING_BYTES + 1
    with pytest.raises(contract.ObservationError):
        contract.recompute(sample, conventional_shader_bytes=sample["conventional_shader_bytes"],
                           work_graph_shader_bytes=sample["work_graph_shader_bytes"])


def physical_host():
    return {"execution_environment": {"kind": "physical"}, "host": {"model": "synthetic physical host"},
            "environment": {"wddm_driver_version": "WDDM 3.2", "nvidia_driver_reported_version": "610.88",
                            "gpu_uuid": "synthetic-physical", "gpu_pci_bus": "pci-fixture",
                            "windows_power_plan": "synthetic power", "nvidia_power_management_mode": {},
                            "pcie_power_management": {}, "gpu_power_state": {"enforced_limit_w": 150}}}


@pytest.mark.parametrize("case,status,decision", [
    ("accept", "measured", "adopt"), ("reject", "measured", "reject"),
    ("no-capability", "unsupported", "defer"), ("moved-adapter", "inconclusive", "defer"),
    ("moved-file", "inconclusive", "defer"), ("failed", "failed", "defer"),
    ("missing-host", "inconclusive", "defer"),
])
def test_physical_driver_fences_explicit_luid_and_preserves_raw_decision(
    tmp_path, bundle, monkeypatch, case, status, decision,
):
    root, trust, revision = bundle
    manifest = runner.verify_bundle(root, trust, revision)
    obs = samples(manifest, variant=95 if case == "reject" else 94)
    calls = []

    def _scripted_tool(argv, output, name, _timeout):
        calls.append(name)
        assert argv[-2:] == ["--adapter-luid", ADAPTER["adapter_luid"]]
        if name.startswith("capability"):
            return 0, {"format": "nw11-capability-snapshot", "status": "ok", "adapter": dict(ADAPTER),
                       "driver_version": "fixture"}
        if name == "fallback":
            assert argv[1] == "--baseline"
            return 0, {"status": "baseline_executed", "adapter": dict(ADAPTER),
                       "oracle_slots_checked": contract.CANDIDATES, "output_hash": contract.oracle_hash()}
        assert argv[1] == "--matched"
        if case == "no-capability":
            return 3, {"status": "unsupported", "work_graph_tier": 0, "shader_model": 0x68,
                       "adapter": dict(ADAPTER)}
        if case == "moved-adapter":
            obs["adapter"]["subsystem_id"] += 1
        elif case == "moved-file":
            (root / "bin" / runner.BASELINE).write_bytes(b"not the measured shader")
        elif case == "failed":
            return 5, {}
        return 0, obs

    monkeypatch.setattr(runner, "_raw", _scripted_tool)
    def qualify(*_args):
        if case == "missing-host":
            raise runner.EvidenceError("physical provenance unavailable")
        return physical_host()
    monkeypatch.setattr(runner, "_qualify", qualify)
    output = tmp_path / "evidence outside"
    report = runner.physical(root, trust, revision, output, ADAPTER["adapter_luid"])
    assert (report["status"], report["decision"]) == (status, decision)
    assert report == json.loads((output / "work-graph-matched.json").read_bytes())
    if case == "missing-host":
        assert calls == ["capability-before"]
    if case == "no-capability":
        assert "fallback" in calls and report["fallback"]["status"] == "passed"
    assert report["post_input_verification"] is (case != "moved-file")
    assert report["satisfies_completion"] is (status == "measured")


@pytest.mark.parametrize("fallback_case,exit_code,reason", [
    ("passed", 0, None),
    ("nonzero", 2, "exit 5"),
    ("wrong-device", 2, "PCI identity"),
    ("bad-oracle", 2, "oracle output hash"),
    ("timeout", 2, "timed out"),
    ("bad-record", 2, "Expecting value"),
])
def test_physical_cli_reports_fallback_failure_independent_of_unsupported_capability(
    tmp_path, bundle, monkeypatch, capsys, fallback_case, exit_code, reason,
):
    root, trust, revision = bundle
    archive, witness, output = tmp_path / "ci.zip", tmp_path / "witness.json", tmp_path / "output"
    with zipfile.ZipFile(archive, "w") as zipped:
        for name in [runner.MANIFEST, *runner.MEMBERS]:
            zipped.write(root / name, name)
    trust |= {"archive_sha256": runner.sha256(archive), "github_artifact_id": "123",
              "github_workflow_run": "456", "github_workflow_attempt": "1"}
    runner.write_json(witness, trust)
    luid = ADAPTER["adapter_luid"]
    calls = []

    def native(command, *, cwd, **kwargs):
        assert cwd == output and command[-2:] == ["--adapter-luid", luid]
        if Path(command[0]).name == runner.PROBE:
            calls.append("capability")
            code, payload = 0, {"format": "nw11-capability-snapshot", "status": "ok",
                                "adapter": dict(ADAPTER), "driver_version": "fixture"}
        elif command[1] == "--matched":
            calls.append("matched")
            code, payload = 3, {"status": "unsupported", "work_graph_tier": 0,
                                "shader_model": 0x68, "adapter": dict(ADAPTER)}
        else:
            assert command[1] == "--baseline"
            calls.append("fallback")
            if fallback_case == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"partial output")
            if fallback_case == "bad-record":
                return subprocess.CompletedProcess(command, 0, b"unreadable fixture output", b"")
            code = 5 if fallback_case == "nonzero" else 0
            payload = {"status": "baseline_executed", "adapter": dict(ADAPTER),
                       "oracle_slots_checked": contract.CANDIDATES, "output_hash": contract.oracle_hash()}
            if fallback_case == "wrong-device":
                payload["adapter"]["subsystem_id"] += 1
            if fallback_case == "bad-oracle":
                payload["output_hash"] ^= 1
        return subprocess.CompletedProcess(command, code, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt", walk=os.walk))
    monkeypatch.setattr(runner, "_qualify", lambda *_: physical_host())
    monkeypatch.setattr(runner.subprocess, "run", native)
    monkeypatch.setattr(sys, "argv", [
        "nw13_work_graph.py", "physical", "--bundle", str(root), "--trust", str(witness),
        "--archive", str(archive), "--revision", revision, "--output", str(output), "--adapter-luid", luid,
    ])
    assert runner.main() == exit_code
    status = "unsupported" if fallback_case == "passed" else "failed"
    assert json.loads(capsys.readouterr().out) == {
        "status": status, "decision": "defer", "satisfies_completion": False,
    }
    report = json.loads((output / "work-graph-matched.json").read_bytes())
    assert report["status"] == status and report["work_graph_capability"] == "unsupported"
    assert report["post_input_verification"] is True
    assert all(report[name] is None for name in ("dispatch_median_ticks", "measured_ratio", "backing_bytes"))
    assert report["native_exit_code"] == 3 and report["observation"]["status"] == "unsupported"
    assert report["fallback"]["status"] == ("passed" if reason is None else "failed")
    if reason is not None:
        assert reason in report["fallback"]["reason"]
    assert (output / "fallback.stdout").is_file()
    assert calls == ["capability", "matched", "fallback", "capability"]


def test_matrix_matches_export_selection_and_the_active_nw11_record():
    tracked = json.loads((ROOT / "benchmarks/nw13/optional-lowering.v1.json").read_bytes())
    generated = evidence.optional_lowering_matrix()
    assert {key: tracked[key] for key in generated} == generated
    assert generated["common_surface"]["surface_realization"] == evidence.select_lowering("common", {})["lowering"]
    dxr = evidence.select_lowering("dxr_inline_ray_query", {"raytracing_tier": 1.1, "highest_shader_model": "6_5"})
    for name in ("profile_id", "fallback_artifact_id", "requirements_digest", "lowering"):
        assert generated["dxr_inline_ray_query"][name] == dxr[name]
    assert generated["dxr_inline_ray_query"]["matched_physical_acceptance"] is None
    mesh = json.loads((ROOT / generated["mesh_shader"]["decision_record"]).read_bytes())
    assert mesh["aggregate_status"] == "pending" and not mesh["satisfies_completion"]
    assert mesh["fallback_artifact_id"] == generated["mesh_shader"]["fallback_artifact_id"]
    experiment = json.loads((ROOT / "benchmarks/nw13/work-graph-experiment.v1.json").read_bytes())
    assert generated["work_graphs"]["criterion"]["accept_if"] == contract.CRITERION == experiment["criterion"]
    assert experiment["decision"] == "defer" and experiment["satisfies_completion"] is False
    assert experiment["workload"]["samples_per_arm"] == contract.SAMPLES_PER_ARM
    assert experiment["workload"]["max_backing_bytes"] == contract.MAX_BACKING_BYTES


def test_workflow_uploads_independently_anchored_bundle_not_just_warp_report():
    workflow = (ROOT / ".github/workflows/nw13-procedural-film.yml").read_text()
    stage = workflow.index("tools/nw13_work_graph.py stage")
    upload = workflow.index("name: nw13-work-graph-physical-", stage)
    assert workflow.index("tools/nw13_work_graph.py warp") < stage < upload
    assert "path: build/nw13-work-graph-physical/" in workflow[upload:]
    assert "steps.work-graph-upload.outputs.artifact-digest" in workflow[upload:]
    assert "Add-Content $env:GITHUB_STEP_SUMMARY" in workflow[upload:]


def test_hosted_warp_keeps_physical_metrics_unavailable(tmp_path, prepared, monkeypatch):
    args, revision = prepared
    source, build, _probe, _python, _warp = args
    executable = build / runner.EXPERIMENT
    observed = {"status": "warp_baseline_executed", "adapter": {"software": True},
                "output_hash": 1188270820, "oracle_slots_checked": contract.CANDIDATES}

    def run(command, **_kwargs):
        assert command == [str(executable), "--warp", str(build / runner.BASELINE),
                           str(build / runner.OPTIONAL)]
        return subprocess.CompletedProcess(command, 0, json.dumps(observed).encode(), b"")

    source = runner._source_identity(source, revision)
    monkeypatch.setattr(runner.subprocess, "run", run)
    output = tmp_path / "hosted.json"
    result = runner.measure_warp(executable, source, output)
    assert result == json.loads(output.read_bytes())
    assert result.get("native_exit_code") is None
    assert result["status"] == "functional_only" and result["decision"] == "defer"
    assert not result["satisfies_completion"]
    assert all(result[key] is None for key in ("dispatch_median_ticks", "backing_bytes", "measured_ratio"))
    (build / runner.OPTIONAL).write_bytes(b"same name, different bytes")

    def altered(command, **_kwargs):
        (build / runner.BASELINE).write_bytes(b"changed during WARP")
        return run(command)

    monkeypatch.setattr(runner.subprocess, "run", altered)
    with pytest.raises(runner.EvidenceError, match="inputs changed"):
        runner.measure_warp(executable, source, output)
