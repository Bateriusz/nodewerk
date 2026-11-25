#!/usr/bin/env python3
"""Bind NW-11 measurement executables to the byte-verified NW-07 package."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import nw05_export
import nw11_contract as contract
from nw11_common_regression import FILES, identity
import nw11_production_binding as binding

VARIANTS = {
    "nw11_common_baseline": "common",
    "nw11_fallback_common": "common",
    "nw11_common_analytic": "analytic",
    "nw11_fallback_analytic": "analytic",
    "nw11_optional_mesh": "mesh",
    "nw11_optional_ray": "ray",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def embed(proof_dir: Path, executable_dir: Path, commit: str) -> None:
    proof = json.loads((proof_dir / "common-regression.json").read_text(encoding="utf-8"))
    if (proof.get("status") != "verified" or proof.get("differences") != []
            or proof.get("base_commit") != contract.IMPLEMENTATION_BASE
            or proof.get("candidate_commit") != commit):
        raise ValueError("common production comparison is missing or stale")
    for arm in ("base", "candidate"):
        folder = proof_dir / arm
        report = json.loads((folder / "export-report.json").read_text(encoding="utf-8"))
        if identity(report, folder) != proof[arm]:
            raise ValueError("actual production export does not match regression proof")
    candidate = proof_dir / "candidate"
    shutil.copyfile(candidate / FILES[0], executable_dir / "nw11-canonical-release.exe")
    package = (candidate / FILES[1]).read_bytes()
    manifest = (candidate / FILES[2]).read_bytes()
    link = json.loads(manifest)
    package_hash, manifest_hash = binding.verify_proof(proof, commit)
    payloads = binding.read_payloads(executable_dir / "compiled-specializations")
    bindings = []
    for name, specialization in VARIANTS.items():
        path = executable_dir / (name + ".exe")
        template = path.read_bytes()
        overlay = {
            "format": "nw11-production-specialization.v1",
            "tested_commit": commit,
            "specialization": specialization,
            "canonical_package_sha256": sha(package),
            "canonical_link_manifest_sha256": sha(manifest),
            "template_sha256": sha(template),
            "link_metadata": link["link_metadata"],
        }
        payload = json.dumps(overlay, sort_keys=True, separators=(",", ":")).encode()
        image = nw05_export.embed_package(template, package, payload)
        if nw05_export.extract_embedded_package(image) != (package, payload):
            raise ValueError("embedded image round trip failed")
        path.write_bytes(image)
        record = binding.inspect(image, commit=commit, specialization=specialization,
                                 package_sha256=package_hash, manifest_sha256=manifest_hash,
                                 payloads=payloads)
        bindings.append({"file": path.name, **record})
    (executable_dir / "nw11-common-regression.json").write_bytes(
        (proof_dir / "common-regression.json").read_bytes())
    (executable_dir / "nw11-production-binding.json").write_text(
        json.dumps({"commit": commit, "artifacts": bindings}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--executables", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    embed(args.proof, args.executables, args.commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
