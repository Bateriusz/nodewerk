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


def build_sampler_dispatch_package() -> nwp.NwpPackage:
    """Compute/display package whose dispatch binds a sampler descriptor table.

    This is the runtime regression fixture for the player dispatch path:
    sampler resources intentionally have no ``ID3D12Resource`` and only own a
    descriptor created by ``CreateSampler``.  Binding one before dispatch must
    validate that descriptor without treating the null resource pointer as a
    missing bound resource or emitting a resource-state transition.
    """
    base = build_compute_display_package()
    family = "compute.math_noise"
    sampler = nwp.ResourceDescriptor(
        resource_id=3,
        name="sampler",
        kind="buffer",
        format="DXGI_FORMAT_R32_UINT",
        usage="sampler",
        pass_family=family,
        binding_slot=0,
        binding_space=0,
        size_bytes=4,
    )
    bindings = (
        nwp.RootBinding("params", "cbv", 0, 0, 0),
        nwp.RootBinding("output", "uav", 0, 0, 1),
        nwp.RootBinding("sampler", "sampler", 0, 0, 2),
    )
    root = nwp.RootSignatureRecord(
        1,
        family,
        family + ".v1",
        6,
        nwp.make_root_signature_blob(family, bindings),
        bindings,
    )
    operand = nwp.InstructionOperand
    initialization = list(base.initialization)
    initialization.insert(
        2,
        nwp.Instruction(
            "init.create_resource", (operand("resource_ref", 3),)
        ),
    )
    frame = []
    for instruction in base.frame:
        frame.append(instruction)
        if instruction.opcode == "frame.set_root_constants":
            frame.append(
                nwp.Instruction(
                    "frame.set_descriptor_table",
                    (
                        operand("u32", 2),
                        operand("string", "sampler"),
                        operand("resource_ref", 3),
                    ),
                )
            )
    derived = nwp.derive_capability_requirements(
        list(base.resources) + [sampler],
        initialization,
        frame,
        base.shaders,
        [root],
    )
    capability = replace(base.capability, **derived)
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
        base,
        resources=list(base.resources) + [sampler],
        initialization=initialization,
        frame=frame,
        root_signatures=[root],
        capability=capability,
    )


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


def build_signature_switch_package(
    base: nwp.NwpPackage | None = None,
) -> nwp.NwpPackage:
    """Package whose present happens after switching root-signature layouts.

    Signature 1 has a third descriptor-table parameter (optional input).
    Signature 2 has only parameters 0 and 1.  Presenting while signature 2 is
    active must not replay signature 1's cached parameter 2 binding.
    """
    base = base or build_compute_display_package()
    family = "compute.math_noise"
    params = next(
        resource
        for resource in base.resources
        if resource.usage == "cbv" and resource.name == "params"
    )
    output = next(
        resource
        for resource in base.resources
        if resource.usage == "uav" and resource.kind == "buffer"
    )
    shader_blob = base.shaders[0].blob
    input_id = max(resource.resource_id for resource in base.resources) + 1
    bindings_a = (
        nwp.RootBinding("params", "cbv", 0, 0, 0),
        nwp.RootBinding("output", "uav", 0, 0, 1),
        nwp.RootBinding("input", "srv", 0, 0, 2),
    )
    bindings_b = (
        nwp.RootBinding("params", "cbv", 0, 0, 0),
        nwp.RootBinding("output", "uav", 0, 0, 1),
    )
    root_a = nwp.RootSignatureRecord(
        1,
        family,
        family + ".v1",
        6,
        nwp.make_root_signature_blob(family, bindings_a),
        bindings_a,
    )
    root_b = nwp.RootSignatureRecord(
        2,
        family,
        family + ".v1",
        5,
        nwp.make_root_signature_blob(family, bindings_b),
        bindings_b,
    )
    resources = [
        params,
        output,
        nwp.ResourceDescriptor(
            input_id, "input", "buffer", "DXGI_FORMAT_R32_UINT", "srv", family,
            0, 0, 0, 0, 0, 1, 1, 256, 0,
        ),
    ]
    parameters = list(base.parameters)
    if not parameters:
        parameters = [
            nwp.ParameterRecord(
                1, "time", "float", "frame", 1, struct.pack("<f", 0.0)
            )
        ]
    shader = nwp.ShaderRecord(
        1, "CSMain", "cs_6_0", shader_blob, 2, family, 64, 1, 1, 0
    )
    operand = nwp.InstructionOperand
    init = [
        nwp.Instruction(
            "init.create_resource", (operand("resource_ref", params.resource_id),)
        ),
        nwp.Instruction(
            "init.create_resource", (operand("resource_ref", output.resource_id),)
        ),
        nwp.Instruction(
            "init.create_resource", (operand("resource_ref", input_id),)
        ),
        nwp.Instruction(
            "init.create_root_signature", (operand("root_signature_ref", 1),)
        ),
        nwp.Instruction(
            "init.create_root_signature", (operand("root_signature_ref", 2),)
        ),
        nwp.Instruction(
            "init.upload_resource", (operand("resource_ref", params.resource_id),)
        ),
        nwp.Instruction(
            "init.upload_resource", (operand("resource_ref", output.resource_id),)
        ),
        nwp.Instruction(
            "init.create_pipeline",
            (operand("shader_ref", 1), operand("root_signature_ref", 2)),
        ),
    ]
    timeline_parameter_id = parameters[0].parameter_id
    frame = [
        nwp.Instruction(
            "frame.set_timeline",
            (operand("u32", timeline_parameter_id), operand("u64", 0)),
        ),
        nwp.Instruction(
            "frame.set_root_signature", (operand("root_signature_ref", 1),)
        ),
        nwp.Instruction(
            "frame.set_root_constants",
            (
                operand("u32", 0),
                operand("resource_ref", params.resource_id),
                operand("u32", 4),
            ),
        ),
        nwp.Instruction(
            "frame.set_descriptor_table",
            (
                operand("u32", 1),
                operand("string", "cbv_srv_uav"),
                operand("resource_ref", output.resource_id),
            ),
        ),
        nwp.Instruction(
            "frame.set_descriptor_table",
            (
                operand("u32", 2),
                operand("string", "cbv_srv_uav"),
                operand("resource_ref", input_id),
            ),
        ),
        nwp.Instruction(
            "frame.set_root_signature", (operand("root_signature_ref", 2),)
        ),
        nwp.Instruction(
            "frame.set_root_constants",
            (
                operand("u32", 0),
                operand("resource_ref", params.resource_id),
                operand("u32", 4),
            ),
        ),
        nwp.Instruction(
            "frame.set_descriptor_table",
            (
                operand("u32", 1),
                operand("string", "cbv_srv_uav"),
                operand("resource_ref", output.resource_id),
            ),
        ),
        nwp.Instruction(
            "frame.dispatch",
            (
                operand("u32", 4),
                operand("u32", 1),
                operand("u32", 1),
                operand("shader_ref", 1),
            ),
        ),
        nwp.Instruction(
            "frame.present", (operand("resource_ref", output.resource_id),)
        ),
    ]
    derived = nwp.derive_capability_requirements(
        resources, init, frame, [shader], [root_a, root_b]
    )
    capability = replace(
        base.capability,
        **derived,
        emitted_shader_targets=("cs_6_0",),
        required_init_instructions=tuple(
            sorted({instruction.opcode for instruction in init})
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
        base,
        capability=capability,
        resources=list(resources),
        initialization=list(init),
        frame=list(frame),
        parameters=list(parameters),
        shaders=[shader],
        root_signatures=[root_a, root_b],
    )


def build_redundant_signature_selection_package(
    base: nwp.NwpPackage | None = None,
) -> nwp.NwpPackage:
    """Dispatch, redundantly select the same signature, present, dispatch.

    D3D12 preserves root arguments when the same root signature is selected
    again. Present must restore those cached bindings for the final dispatch.
    """
    base = base or build_compute_display_package()
    params = next(
        resource
        for resource in base.resources
        if resource.usage == "cbv" and resource.name == "params"
    )
    output = next(
        resource
        for resource in base.resources
        if resource.usage == "uav" and resource.kind == "buffer"
    )
    shader = base.shaders[0]
    root = next(
        record
        for record in base.root_signatures
        if record.root_signature_id == shader.root_signature_id
    )
    parameter_id = (
        base.parameters[0].parameter_id if base.parameters else 1
    )
    operand = nwp.InstructionOperand
    initialization = [
        instruction
        for instruction in base.initialization
        if not (
            instruction.opcode == "init.create_root_signature"
            and instruction.operands[0].value != root.root_signature_id
        )
        and not (
            instruction.opcode == "init.create_pipeline"
            and instruction.operands[0].value != shader.shader_id
        )
    ]
    frame = [
        nwp.Instruction(
            "frame.set_timeline",
            (operand("u32", parameter_id), operand("u64", 0)),
        ),
        nwp.Instruction(
            "frame.set_root_signature",
            (operand("root_signature_ref", root.root_signature_id),),
        ),
        nwp.Instruction(
            "frame.set_root_constants",
            (
                operand("u32", 0),
                operand("resource_ref", params.resource_id),
                operand("u32", 4),
            ),
        ),
        nwp.Instruction(
            "frame.set_descriptor_table",
            (
                operand("u32", 1),
                operand("string", "cbv_srv_uav"),
                operand("resource_ref", output.resource_id),
            ),
        ),
        nwp.Instruction(
            "frame.dispatch",
            (
                operand("u32", 4),
                operand("u32", 1),
                operand("u32", 1),
                operand("shader_ref", shader.shader_id),
            ),
        ),
        nwp.Instruction(
            "frame.set_root_signature",
            (operand("root_signature_ref", root.root_signature_id),),
        ),
        nwp.Instruction(
            "frame.present", (operand("resource_ref", output.resource_id),)
        ),
        nwp.Instruction(
            "frame.dispatch",
            (
                operand("u32", 4),
                operand("u32", 1),
                operand("u32", 1),
                operand("shader_ref", shader.shader_id),
            ),
        ),
    ]
    derived = nwp.derive_capability_requirements(
        base.resources,
        initialization,
        frame,
        [shader],
        [root],
    )
    capability = replace(
        base.capability,
        **derived,
        required_init_instructions=tuple(
            sorted(
                {
                    instruction.opcode
                    for instruction in initialization
                }
            )
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
        base,
        capability=capability,
        initialization=initialization,
        frame=frame,
        shaders=[shader],
        root_signatures=[root],
    )


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
    parser.add_argument(
        "--signature-switch-output",
        type=Path,
        default=None,
        help="Write the root-signature-layout switch regression package.",
    )
    args = parser.parse_args()
    if args.signature_switch_output is not None:
        data = nwp.encode_package(build_signature_switch_package())
        args.signature_switch_output.parent.mkdir(parents=True, exist_ok=True)
        args.signature_switch_output.write_bytes(data)
        if args.print_sha256:
            print(hashlib.sha256(data).hexdigest())
        return 0
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
