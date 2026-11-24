"""Validate the actual NW-07 package and NW-11 DXIL bytes in a PE image.

This module is deliberately standard-library only, so the same check can run
inside a clean-host validation bundle without the authoring toolchain.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from nw11_contract import IMPLEMENTATION_BASE, sha256_bytes

FOOTER = struct.Struct("<8sIQQ32s32s")
MAGIC = b"NWEMB1\x00\x00"
PAYLOADS = {
    "nw11_torus_amplification_as_dxil.dxil": "mesh",
    "nw11_torus_meshlet_ms_dxil.dxil": "mesh",
    "nw11_scene_rayquery_ps_dxil.dxil": "ray",
    "nw11_deform_cs_dxil.dxil": "ray",
    "nw11_analytic_reflection_ps_dxil.dxil": "analytic",
}


def embedded_image(image: bytes) -> tuple[bytes, bytes, dict[str, Any]]:
    if len(image) < FOOTER.size:
        raise ValueError("missing production package footer")
    magic, version, package_size, manifest_size, package_hash, manifest_hash = FOOTER.unpack_from(
        image, len(image) - FOOTER.size
    )
    start = len(image) - FOOTER.size - package_size - manifest_size
    if (magic != MAGIC or version != 1 or start < 2 or image[:2] != b"MZ"
            or package_size == 0 or manifest_size == 0):
        raise ValueError("invalid production package footer")
    package = image[start:start + package_size]
    manifest = image[start + package_size:len(image) - FOOTER.size]
    if (sha256_bytes(package) != package_hash.hex()
            or sha256_bytes(manifest) != manifest_hash.hex()):
        raise ValueError("embedded production package checksum mismatch")
    overlay = json.loads(manifest)
    if not isinstance(overlay, dict):
        raise ValueError("production specialization overlay must be an object")
    return image[:start], package, overlay


def read_payloads(directory: Path) -> dict[str, bytes]:
    payloads = {name: (directory / name).read_bytes() for name in PAYLOADS}
    if any(not data.startswith(b"DXBC") for data in payloads.values()):
        raise ValueError("invalid offline DXIL container")
    return payloads


def inspect(image: bytes, *, commit: str, specialization: str,
            package_sha256: str, manifest_sha256: str,
            payloads: dict[str, bytes]) -> dict[str, Any]:
    template, package, overlay = embedded_image(image)
    if (overlay.get("format") != "nw11-production-specialization.v1"
            or overlay.get("tested_commit") != commit
            or overlay.get("specialization") != specialization
            or overlay.get("canonical_package_sha256") != package_sha256
            or sha256_bytes(package) != package_sha256
            or overlay.get("canonical_link_manifest_sha256") != manifest_sha256
            or overlay.get("template_sha256") != sha256_bytes(template)):
        raise ValueError("production specialization identity mismatch")
    if set(payloads) != set(PAYLOADS):
        raise ValueError("incomplete offline DXIL inventory")
    linked = []
    for name, owner in PAYLOADS.items():
        data = payloads[name]
        offset = template.find(data)
        if (offset >= 0) != (owner == specialization):
            raise ValueError(f"{specialization}: unexpected presence/absence of {name}")
        if offset >= 0:
            linked.append({"file": name, "offset": offset,
                           "sha256": sha256_bytes(data), "bytes": len(data)})
    return {"specialization": specialization, "sha256": sha256_bytes(image),
            "package_sha256": package_sha256,
            "template_sha256": sha256_bytes(template),
            "linked_specialization_payloads": linked}


def verify_proof(proof: dict[str, Any], commit: str) -> tuple[str, str]:
    if (proof.get("format") != "nw11-common-regression.v1"
            or proof.get("base_commit") != IMPLEMENTATION_BASE
            or proof.get("candidate_commit") != commit
            or proof.get("status") != "verified" or proof.get("differences") != []
            or proof.get("base") != proof.get("candidate")):
        raise ValueError("canonical NW-07 proof is missing or stale")
    candidate = proof["candidate"]
    return candidate["package_sha256"], candidate["manifest_sha256"]
