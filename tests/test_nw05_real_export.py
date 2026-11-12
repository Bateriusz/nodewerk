from __future__ import annotations

import hashlib
import os
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


PROCEDURAL = REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph"


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
    optimized = nw05_optimizer.optimize_graph(
        nw03_compiler.load_graph(PROCEDURAL)
    )
    generated, diagnostics = nw03_compiler.lower_to_fused_hlsl(
        optimized.materialize_document()
    )
    assert not diagnostics
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
        [str(executable), "--frames", "1", "--allow-software-adapter"],
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
