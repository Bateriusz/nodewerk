"""Validate the actual NW-07 package and NW-11 DXIL bytes in a PE image.

This module is deliberately standard-library only, so the same check can run
inside a clean-host validation bundle without the authoring toolchain.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
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


def embedded_payloads(image: bytes) -> tuple[bytes, bytes, bytes]:
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
    return image[:start], package, manifest


def embedded_image(image: bytes) -> tuple[bytes, bytes, dict[str, Any]]:
    template, package, manifest = embedded_payloads(image)
    overlay = json.loads(manifest)
    if not isinstance(overlay, dict):
        raise ValueError("production specialization overlay must be an object")
    return template, package, overlay


def read_payloads(directory: Path) -> dict[str, bytes]:
    payloads = {name: (directory / name).read_bytes() for name in PAYLOADS}
    if any(not data.startswith(b"DXBC") for data in payloads.values()):
        raise ValueError("invalid offline DXIL container")
    return payloads


def specialize_package(canonical: bytes, specialization: str) -> tuple[bytes, list[dict[str, Any]]]:
    """Remove only the displaced code blob. Keep the canonical stage's routing ID."""
    if specialization == "common":
        return canonical, []
    import nwp_package as nwp

    displaced = {"mesh": (6, "SceneVS", "vs_6_0"),
                 "ray": (7, "ScenePS", "ps_6_0"),
                 "analytic": (7, "ScenePS", "ps_6_0")}[specialization]
    if canonical[:8] != nwp.MAGIC or len(canonical) < nwp.HEADER_SIZE + nwp.CHECKSUM_SIZE:
        raise ValueError("Invalid canonical NWP source for specialization")
    checksum = len(canonical) - nwp.CHECKSUM_SIZE
    if hashlib.sha256(canonical[:checksum]).digest() != canonical[checksum:]:
        raise ValueError("Invalid canonical NWP checksum")
    count, entry_size = struct.unpack_from("<II", canonical, 40)
    if count != 7 or entry_size != nwp.SECTION_ENTRY_SIZE:
        raise ValueError("Canonical specialization requires the stateless production NWP layout")
    cursor = nwp.HEADER_SIZE + count * entry_size
    directory = bytearray(canonical[:cursor])
    source_offset = cursor
    sections = []
    removed = []
    for index in range(count):
        entry = nwp.HEADER_SIZE + index * entry_size
        name, flags, records, offset, size, reserved = struct.unpack_from("<4sIIQQI", canonical, entry)
        if offset != source_offset or offset + size > checksum or flags or reserved:
            raise ValueError("Canonical NWP section directory is invalid")
        source_offset += size
        payload = canonical[offset:offset + size]
        if name == b"SHDR":
            diagnostics: list[dict[str, Any]] = []
            shaders = nwp._decode_shaders(payload, diagnostics)
            if diagnostics or not shaders or len(shaders) != records:
                raise ValueError("Canonical shader section failed structural validation")
            replacement = []
            for shader in shaders:
                if (shader.shader_id, shader.entry_point, shader.target) == displaced:
                    if (shader.family != "graphics.nw06.instanced" or not shader.blob
                            or any(other.shader_id != shader.shader_id and other.blob == shader.blob
                                   for other in shaders)):
                        raise ValueError("Displaced scene stage is not uniquely owned")
                    removed.append(shader.to_dict())
                    shader = replace(shader, blob=b"")
                replacement.append(shader)
            payload = nwp._encode_shaders(replacement)
        struct.pack_into("<QQ", directory, entry + 12, cursor, len(payload))
        sections.append(payload)
        cursor += len(payload)
    if source_offset != checksum or len(removed) != 1:
        raise ValueError("Expected exactly one displaced NW-07 scene shader")
    struct.pack_into("<Q", directory, 32, cursor + nwp.CHECKSUM_SIZE)
    struct.pack_into("<Q", directory, 48, cursor)
    data = bytes(directory) + b"".join(sections)
    return data + hashlib.sha256(data).digest(), removed


def shader_inventory(image: bytes) -> list[dict[str, Any]]:
    """Inventory DXIL containers physically present in the linked image or NWP."""
    shaders = []
    offset = 0
    while (offset := image.find(b"DXBC", offset)) >= 0:
        start = offset
        offset += 4
        if start + 32 > len(image):
            break
        version, size, count = struct.unpack_from("<III", image, start + 20)
        if version != 1 or not 0 < count <= 64 or not 32 + count * 4 <= size <= len(image) - start:
            continue
        parts = struct.unpack_from(f"<{count}I", image, start + 32)
        if any(part < 32 + count * 4 or part + 8 > size or
               part + 8 + struct.unpack_from("<I", image, start + part + 4)[0] > size
               for part in parts):
            continue
        dxil = next((part for part in parts if image[start + part:start + part + 4] == b"DXIL"
                     and part + 12 <= size), None)
        if dxil is None:
            continue
        token = struct.unpack_from("<I", image, start + dxil + 8)[0]
        stage = {0: "ps", 1: "vs", 5: "cs", 13: "ms", 14: "as"}.get(token >> 16)
        target = f"{stage}_{(token >> 4) & 15}_{token & 15}" if stage else "unknown"
        digest = sha256_bytes(image[start:start + size])
        shaders.append({"id": f"dxil:{start}:{digest}", "bytes": size, "target": target})
        offset = start + size
    return shaders


def inspect(image: bytes, *, commit: str, specialization: str,
            package_sha256: str, manifest_sha256: str,
            payloads: dict[str, bytes], canonical_package: bytes) -> dict[str, Any]:
    template, package, overlay = embedded_image(image)
    expected, removed = specialize_package(canonical_package, specialization)
    if (overlay.get("format") != "nw11-production-specialization.v1"
            or overlay.get("tested_commit") != commit
            or overlay.get("specialization") != specialization
            or overlay.get("canonical_package_sha256") != package_sha256
            or sha256_bytes(canonical_package) != package_sha256
            or package != expected
            or overlay.get("specialized_package_sha256") != sha256_bytes(expected)
            or overlay.get("displaced_canonical_shaders") != removed
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
    inventory = shader_inventory(image)
    if {item["blob_sha256"] for item in removed} & {item["id"].split(":")[-1] for item in inventory}:
        raise ValueError("A displaced canonical shader still ships in the native artifact")
    return {"specialization": specialization, "sha256": sha256_bytes(image),
            "package_sha256": sha256_bytes(package), "canonical_package_sha256": package_sha256,
            "displaced_canonical_shaders": removed,
            "template_sha256": sha256_bytes(template),
            "linked_specialization_payloads": linked,
            "shipped_shaders": inventory}


def verify_proof(proof: dict[str, Any], commit: str) -> tuple[str, str]:
    if (proof.get("format") != "nw11-common-regression.v1"
            or proof.get("base_commit") != IMPLEMENTATION_BASE
            or proof.get("candidate_commit") != commit
            or proof.get("status") != "verified" or proof.get("differences") != []
            or proof.get("base") != proof.get("candidate")):
        raise ValueError("canonical NW-07 proof is missing or stale")
    candidate = proof["candidate"]
    return candidate["package_sha256"], candidate["manifest_sha256"]
