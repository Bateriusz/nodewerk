#!/usr/bin/env python3
"""Build the deterministic NW-04 package-only compute/display fixture.

The fixture is a real NW-03 `.nwp` v1 container assembled through the
established `tools/nwp_package.py` writer.  It adds the documented
`frame.present` instruction to the sample compute package and recomputes the
exact capability requirements and profile fingerprint.  The synthetic DXIL
payload used for the repository fixture is structurally valid and accepted by
the NW-03 test-container path; hardware qualification compiles the real fixture
through `tools/nodewerkc.py` and records its actual hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(REPO_ROOT / "tools"))

import nwp_package as nwp  # noqa: E402


def make_dxil_container(*, shader_type: int = 5, major: int = 6, minor: int = 0) -> bytes:
    """Structurally valid cs_6_0 DXIL container used by the package fixtures."""
    sfi0 = b"SFI0" + struct.pack("<I", 8) + struct.pack("<Q", 0)
    psv0_info = bytearray(36)
    struct.pack_into("<III", psv0_info, 12, 64, 1, 1)
    psv0_payload = (
        struct.pack("<I", len(psv0_info)) + bytes(psv0_info) + struct.pack("<II", 0, 1)
    )
    psv0 = b"PSV0" + struct.pack("<I", len(psv0_payload)) + psv0_payload
    words = [0x00000021, 0x00000002, 0x00000021, 0x0000000C] + [0] * 16
    bitcode = b"BC\xc0\xde" + struct.pack(f"<{len(words)}I", *words)
    dxil_payload = (
        struct.pack(
            "<II4sIII",
            (shader_type << 16) | (major << 4) | minor,
            (24 + len(bitcode)) // 4,
            b"DXIL",
            0x00010100,
            16,
            len(bitcode),
        )
        + bitcode
    )
    dxil = b"DXIL" + struct.pack("<I", len(dxil_payload)) + dxil_payload
    chunks = [sfi0, psv0, dxil]
    header_size = 32 + len(chunks) * 4
    offsets = []
    cursor = header_size
    for chunk in chunks:
        offsets.append(cursor)
        cursor += len(chunk)
    joined = b"".join(chunks)
    digest = hashlib.blake2s(b"dxc-retail-sig:" + joined, digest_size=16).digest()
    return (
        b"DXBC"
        + digest
        + struct.pack("<HHII", 1, 0, cursor, len(chunks))
        + struct.pack(f"<{len(chunks)}I", *offsets)
        + joined
    )


def build_compute_display_package(*, specialization_count: int = 1) -> nwp.NwpPackage:
    package = nwp.make_test_package(
        shader_blob=make_dxil_container(),
        specialization_count=specialization_count,
    )
    initialization = list(package.initialization)
    initialization.append(
        nwp.Instruction(
            "init.upload_resource",
            (nwp.InstructionOperand("resource_ref", 1),),
        )
    )
    frame = list(package.frame)
    frame.insert(
        0,
        nwp.Instruction(
            "frame.set_timeline",
            (
                nwp.InstructionOperand("u32", 1),
                nwp.InstructionOperand("u64", 0),
            ),
        ),
    )
    barrier_index = next(
        index
        for index, instruction in enumerate(frame)
        if instruction.opcode == "frame.set_descriptor_table"
    )
    frame.insert(
        barrier_index + 1,
        nwp.Instruction(
            "frame.barrier",
            (
                nwp.InstructionOperand("u32", 4),
                nwp.InstructionOperand("resource_ref", 2),
            ),
        ),
    )
    frame.append(
        nwp.Instruction(
            "frame.present",
            (nwp.InstructionOperand("resource_ref", 2),),
        )
    )
    derived = nwp.derive_capability_requirements(
        package.resources,
        initialization,
        frame,
        package.shaders,
        package.root_signatures,
    )
    capability = replace(package.capability, **derived)
    capability = replace(
        capability,
        required_init_instructions=tuple(
            sorted({instruction.opcode for instruction in initialization})
        ),
        required_frame_instructions=tuple(
            sorted({instruction.opcode for instruction in frame})
        ),
    )
    capability = replace(
        capability,
        profile_fingerprint=capability.computed_profile_fingerprint(),
    )
    return replace(
        package,
        capability=capability,
        initialization=initialization,
        frame=frame,
        flags=1,
    )


def package_bytes() -> bytes:
    return nwp.encode_package(build_compute_display_package())


def write_fixture(path: Path) -> str:
    data = package_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "fixtures" / "nw04" / "compute_display.nwp",
    )
    parser.add_argument("--print-sha256", action="store_true")
    args = parser.parse_args()
    digest = write_fixture(args.output)
    if args.print_sha256:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
