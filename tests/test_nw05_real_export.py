from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw05_export  # noqa: E402
import nw03_compiler  # noqa: E402
import nw05_optimizer  # noqa: E402
import nwp_package as nwp  # noqa: E402


PROCEDURAL = REPO_ROOT / "fixtures" / "nw05" / "procedural_export.nwgraph"


def _hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def test_real_pe_export_is_byte_identical_in_clean_directories(
    tmp_path: Path,
) -> None:
    template_value = os.environ.get("NW05_REAL_PLAYER_TEMPLATE", "")
    if os.name != "nt" or not template_value:
        pytest.skip("real PE export reproducibility runs in Windows CI")
    template = Path(template_value)
    assert template.is_file()
    dxc = shutil.which("dxc") or shutil.which("dxc.exe")
    assert dxc, "pinned DXC must be on PATH for real-PE reproducibility"
    settings = nw05_export.ExportSettings(
        byte_ceiling=64 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        player_template=str(template),
    )
    first = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "clean-a",
        settings=settings,
        dxc_executable=dxc,
    )
    second = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "clean-b",
        settings=settings,
        dxc_executable=dxc,
    )
    assert _hashes(tmp_path / "clean-a") == _hashes(tmp_path / "clean-b")
    common = first.artifacts[nwp.COMMON_PROFILE_ID]
    assert common.executable_bytes is not None
    executable = tmp_path / "clean-a" / common.executable_name
    payload = executable.read_bytes()
    assert payload[:2] == b"MZ"
    pe_offset = int.from_bytes(payload[0x3C:0x40], "little")
    assert payload[pe_offset : pe_offset + 4] == b"PE\x00\x00"
    assert (
        common.executable_bytes
        == second.artifacts[nwp.COMMON_PROFILE_ID].executable_bytes
    )
    assert (
        first.report["provenance"]["player_template_sha256"]
        == hashlib.sha256(template.read_bytes()).hexdigest()
    )
    template_bytes = template.read_bytes()
    assert common.executable_bytes[: len(template_bytes)] != template_bytes
    assert common.manifest["dependencies_source"] == "specialized-pe-imports"
    optimized = nw05_optimizer.optimize_graph(
        nw03_compiler.load_graph(PROCEDURAL)
    )
    materialized = optimized.materialize_document()
    graph_width, graph_height = nw05_export._graph_render_dimensions(
        materialized, optimized
    )
    thread_group = (max(1, min(1024, graph_width)), 1, 1)
    dispatch_groups = (
        (graph_width + thread_group[0] - 1) // thread_group[0],
        max(1, graph_height),
        1,
    )
    generated, diagnostics = nw03_compiler.lower_to_selective_hlsl(
        materialized,
        fused_roots={
            record.root_node for record in optimized.fusions if record.accepted
        },
        thread_group=thread_group,
        dispatch_groups=dispatch_groups,
        image_width=graph_width,
        image_height=graph_height,
    )
    assert not diagnostics
    assert len(generated) == 6
    assert all(shader.unfused for shader in generated)
    expected_hlsl = {shader.hlsl_sha256 for shader in generated}
    manifest_hlsl = {
        item["hlsl_sha256"]
        for item in common.manifest.get("shaders", [])
    }
    assert manifest_hlsl == expected_hlsl
    assert common.manifest["image_oracle_sha256"] == (
        nw05_export._graph_image_oracle_sha256(
            nw03_compiler.load_graph(PROCEDURAL)
        )
    )
    decoded = nwp.decode_package(common.package_bytes)
    manifest_blobs = {
        item["blob_sha256"] for item in common.manifest.get("shaders", [])
    }
    assert {
        hashlib.sha256(shader.blob).hexdigest()
        for shader in decoded.shaders
    } == manifest_blobs
    present = [
        instruction
        for instruction in decoded.frame
        if instruction.opcode == "frame.present"
    ]
    assert present
    present_id = present[-1].operands[0].value
    assert any(
        resource.resource_id == present_id and resource.usage == "uav"
        for resource in decoded.resources
    )
    completed = subprocess.run(
        [
            str(executable),
            "--frames",
            "1",
            "--allow-software-adapter",
            "--emit-image-hash",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, (
        completed.stdout + "\n" + completed.stderr
    )
    assert "NW05_FRAMES=1" in completed.stdout
    assert "NW05_PRESENTS=1" in completed.stdout
    image_bytes = re.search(r"NW05_IMAGE_BYTES=(\d+)", completed.stdout)
    nonzero = re.search(
        r"NW05_IMAGE_NONZERO_BYTES=(\d+)", completed.stdout
    )
    image_hash = re.search(r"NW05_IMAGE_FNV64=([0-9a-f]{16})", completed.stdout)
    assert image_bytes is not None
    assert nonzero is not None
    assert image_hash is not None
    assert int(image_bytes.group(1)) == graph_width * graph_height * 16
    assert int(nonzero.group(1)) > 0
    non_aliased = subprocess.run(
        [
            str(executable),
            "--frames",
            "1",
            "--allow-software-adapter",
            "--force-non-aliased-plan",
            "--emit-image-hash",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert non_aliased.returncode == 0, (
        non_aliased.stdout + "\n" + non_aliased.stderr
    )
    non_aliased_hash = re.search(
        r"NW05_IMAGE_FNV64=([0-9a-f]{16})", non_aliased.stdout
    )
    assert non_aliased_hash is not None
    assert non_aliased_hash.group(1) == image_hash.group(1)

    changed_source = PROCEDURAL.read_text(encoding="utf-8").replace(
        "sin(uv.x * 31.0 + time)", "sin(uv.x * 37.0 + time)"
    )
    changed_graph = tmp_path / "changed.nwgraph"
    changed_graph.write_text(changed_source, encoding="utf-8")
    changed = nw05_export.export_graph(
        changed_graph,
        tmp_path / "changed",
        settings=settings,
        dxc_executable=dxc,
    )
    changed_common = changed.artifacts[nwp.COMMON_PROFILE_ID]
    changed_executable = (
        tmp_path / "changed" / changed_common.executable_name
    )
    changed_run = subprocess.run(
        [
            str(changed_executable),
            "--frames",
            "1",
            "--allow-software-adapter",
            "--emit-image-hash",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert changed_run.returncode == 0, (
        changed_run.stdout + "\n" + changed_run.stderr
    )
    changed_hash = re.search(
        r"NW05_IMAGE_FNV64=([0-9a-f]{16})", changed_run.stdout
    )
    assert changed_hash is not None
    assert changed_hash.group(1) != image_hash.group(1)


def test_unused_nw06_runtime_is_absent_from_legacy_real_pe(
    tmp_path: Path,
) -> None:
    template_value = os.environ.get("NW05_REAL_PLAYER_TEMPLATE", "")
    if os.name != "nt" or not template_value:
        pytest.skip("real PE closure validation runs in Windows CI")
    template = Path(template_value)
    assert template.is_file()
    dxc = shutil.which("dxc") or shutil.which("dxc.exe")
    assert dxc, "pinned DXC must be on PATH for real-PE closure validation"
    settings = nw05_export.ExportSettings(
        byte_ceiling=64 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        player_template=str(template),
    )
    result = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "legacy",
        settings=settings,
        dxc_executable=dxc,
    )
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    assert common.executable_bytes is not None
    package_bytes, manifest_bytes = nw05_export.extract_embedded_package(
        common.executable_bytes
    )
    raw_size = (
        len(common.executable_bytes)
        - len(package_bytes)
        - len(manifest_bytes)
        - nw05_export.EMBED_FOOTER_SIZE
    )
    legacy_pe = common.executable_bytes[:raw_size]
    markers = (
        b"CreateGraphicsPipelineState failed for offline DXIL",
        b"CreateCommandSignature(DRAW_INDEXED) failed",
        b"initialization root signature was not created",
        b"initialization descriptor target has no descriptor",
        b"nw06.params.",
        b"compute.nw06.",
        b"graphics.nw06.",
    )
    for marker in markers:
        assert marker not in legacy_pe

    enabled_manifest = json.loads(json.dumps(common.manifest))
    retained = dict(enabled_manifest.get("retained", {}))
    runtime = set(retained.get("runtime_implementations", []))
    runtime.update(
        {
            "player.nw06.gpu-initialization",
            "player.nw06.indexed-instanced-indirect",
        }
    )
    retained["runtime_implementations"] = sorted(runtime)
    enabled_manifest["retained"] = retained
    nw05_export._refresh_manifest_sha256(enabled_manifest)
    enabled_source = nw05_export.generate_player_source(
        package_sha256=hashlib.sha256(common.package_bytes).hexdigest(),
        manifest_sha256=enabled_manifest["manifest_sha256"],
        manifest=enabled_manifest,
        memory_budget_bytes=settings.memory_budget,
    )
    enabled_pe = nw05_export._build_specialized_player(
        template,
        enabled_source,
        enabled_manifest,
        nwp.COMMON_PROFILE_ID,
    )
    for marker in markers:
        assert marker in enabled_pe
    assert len(legacy_pe) < len(enabled_pe)
    assert nw05_export._pe_runtime_dependencies(legacy_pe) == (
        nw05_export._pe_runtime_dependencies(enabled_pe)
    )
