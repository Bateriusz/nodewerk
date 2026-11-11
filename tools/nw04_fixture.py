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
    return compose_compute_display_package(package)


def compose_compute_display_package(
    package: nwp.NwpPackage,
) -> nwp.NwpPackage:
    """Append the NW-04 display instruction stream to a valid NW-03 package.

    The container format, profile identity, shader blobs, and root signatures
    are unchanged.  Only the ordered initialization/frame stream gains the
    uploads and ``frame.present`` needed for a visible compute/display run.
    """
    initialization = list(package.initialization)
    uploaded = {
        instruction.operands[0].value
        for instruction in initialization
        if instruction.opcode == "init.upload_resource"
        and instruction.operands
        and instruction.operands[0].type == "resource_ref"
    }
    for resource in package.resources:
        if resource.usage not in {"cbv", "uav"}:
            continue
        if resource.resource_id in uploaded:
            continue
        initialization.append(
            nwp.Instruction(
                "init.upload_resource",
                (nwp.InstructionOperand("resource_ref", resource.resource_id),),
            )
        )

    frame = list(package.frame)
    if (
        package.parameters
        and not any(
            instruction.opcode == "frame.set_timeline"
            for instruction in frame
        )
    ):
        frame.insert(
            0,
            nwp.Instruction(
                "frame.set_timeline",
                (
                    nwp.InstructionOperand(
                        "u32", package.parameters[0].parameter_id
                    ),
                    nwp.InstructionOperand("u64", 0),
                ),
            ),
        )
    output = next(
        (
            resource
            for resource in package.resources
            if resource.usage == "uav" and resource.kind == "buffer"
        ),
        None,
    )
    if output is None:
        raise ValueError("package has no UAV buffer output for display")
    if not any(
        instruction.opcode == "frame.barrier" for instruction in frame
    ):
        barrier_index = next(
            (
                index
                for index, instruction in enumerate(frame)
                if instruction.opcode == "frame.set_descriptor_table"
            ),
            len(frame) - 1,
        )
        frame.insert(
            barrier_index + 1,
            nwp.Instruction(
                "frame.barrier",
                (
                    nwp.InstructionOperand("u32", 4),
                    nwp.InstructionOperand(
                        "resource_ref", output.resource_id
                    ),
                ),
            ),
        )
    if not any(
        instruction.opcode == "frame.present" for instruction in frame
    ):
        frame.append(
            nwp.Instruction(
                "frame.present",
                (
                    nwp.InstructionOperand(
                        "resource_ref", output.resource_id
                    ),
                ),
            )
        )
    # Exercise dependent accesses that remain in UAV state: the second
    # dispatch reads/writes the same output and must be preceded by a UAV
    # barrier even though no transition is needed.
    if not any(
        instruction.opcode == "frame.dispatch"
        and index > 0
        and frame[index - 1].opcode == "frame.dispatch"
        for index, instruction in enumerate(frame)
    ):
        dispatch_index = next(
            (
                index
                for index in range(len(frame) - 1, -1, -1)
                if frame[index].opcode == "frame.dispatch"
            ),
            None,
        )
        if dispatch_index is not None:
            frame.insert(dispatch_index + 1, frame[dispatch_index])
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
    parser.add_argument(
        "--from-package",
        type=Path,
        default=None,
        help=(
            "Compose the display stream onto an existing real offline-compiled "
            "NW-03 package instead of the synthetic contract fixture."
        ),
    )
    args = parser.parse_args()
    if args.from_package is not None:
        package = nwp.decode_package(args.from_package.read_bytes())
        data = nwp.encode_package(compose_compute_display_package(package))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(data)
        if args.print_sha256:
            print(hashlib.sha256(data).hexdigest())
        return 0
    digest = write_fixture(args.output)
    if args.print_sha256:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
