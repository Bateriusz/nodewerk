from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from offline_shader_pack import (  # noqa: E402
    build_deterministic_dxil_container,
    check_hlsl_source_constraints,
    format_cpp_dxil_header,
    validate_dxil_container,
)


def test_offline_dxil_container_and_embedded_header_reproducibility() -> None:
    hlsl_path = REPO_ROOT / "shaders" / "compute_fixture.hlsl"
    dxil_path = REPO_ROOT / "shaders" / "compute_fixture.dxil"
    header_path = REPO_ROOT / "shaders" / "generated" / "compute_fixture_dxil.h"
    manifest_path = REPO_ROOT / "shaders" / "manifest.json"

    hlsl_bytes = hlsl_path.read_bytes()
    assert check_hlsl_source_constraints(hlsl_bytes.decode("utf-8")) == []

    rebuilt_blob = build_deterministic_dxil_container(hlsl_bytes)
    committed_blob = dxil_path.read_bytes()
    assert rebuilt_blob == committed_blob, "Committed DXIL container must match deterministic offline pack output"

    info = validate_dxil_container(committed_blob)
    assert info["valid"] is True
    assert info["shader_model"] == "cs_6_0"
    assert info["size_bytes"] == 392
    assert info["requires_sm_6_6_heap_indexing"] is False
    assert info["requires_enhanced_barriers"] is False
    assert info["requires_mesh_shaders"] is False
    assert info["requires_ray_tracing"] is False

    expected_header = format_cpp_dxil_header(committed_blob, info["sha256"])
    assert header_path.read_text(encoding="utf-8") == expected_header

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shader_entry = manifest["shaders"][0]
    assert shader_entry["target_profile"] == "cs_6_0"
    assert shader_entry["offline_dxil_bytes"] == len(committed_blob)
    assert shader_entry["offline_dxil_sha256"] == info["sha256"]
    assert shader_entry["runtime_compilation_used"] is False
    assert shader_entry["agility_sdk_required"] is False


def test_hlsl_source_constraint_checker_rejects_non_baseline_features() -> None:
    for forbidden_snippet in (
        "RWByteAddressBuffer buf = ResourceDescriptorHeap[0];",
        "SamplerState s = SamplerDescriptorHeap[1];",
        "RaytracingAccelerationStructure scene : register(t0);",
        "SetMeshOutputCounts(64, 32);",
        "DispatchMesh(1, 1, 1, payload);",
    ):
        violations = check_hlsl_source_constraints(forbidden_snippet)
        assert len(violations) == 1, f"Expected violation for: {forbidden_snippet}"


def test_dxil_container_validator_rejects_corrupt_or_wrong_shader_model() -> None:
    with pytest.raises(ValueError, match="too small"):
        validate_dxil_container(b"DXBCshort")

    hlsl_bytes = (REPO_ROOT / "shaders" / "compute_fixture.hlsl").read_bytes()
    valid_blob = bytearray(build_deterministic_dxil_container(hlsl_bytes))

    # Corrupt magic
    bad_magic = bytearray(valid_blob)
    bad_magic[:4] = b"BAD!"
    with pytest.raises(ValueError, match="magic"):
        validate_dxil_container(bytes(bad_magic))

    # Corrupt shader model from cs_6_0 (0x60) to cs_6_6 (0x66)
    sm66_blob = bytearray(valid_blob)
    dxil_offset = struct.unpack_from("<I", sm66_blob, 36)[0]
    prog_ver_cs_6_6 = (5 << 16) | (6 << 4) | 6
    struct.pack_into("<I", sm66_blob, dxil_offset + 8, prog_ver_cs_6_6)
    with pytest.raises(ValueError, match="cs_6_6"):
        validate_dxil_container(bytes(sm66_blob))


def test_compute_fixture_expected_output_matches_reference_formula() -> None:
    expected_path = REPO_ROOT / "fixtures" / "compute_shader" / "expected_output.json"
    data = json.loads(expected_path.read_text(encoding="utf-8"))
    assert data["element_count"] == 256
    assert data["thread_group_size"] == [64, 1, 1]
    assert data["dispatch_groups"] == [4, 1, 1]

    seed_mask = int(data["seed_mask_hex"], 16)
    recomputed = [((tid * 1664525 + 1013904223) & 0xFFFFFFFF) ^ seed_mask for tid in range(256)]
    assert data["elements_u32"] == recomputed
    raw_bytes = struct.pack("<256I", *recomputed)
    assert data["output_sha256"] == hashlib.sha256(raw_bytes).hexdigest()
