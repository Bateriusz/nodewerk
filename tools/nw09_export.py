#!/usr/bin/env python3
"""NW-09 stateful export extension over the established `.nwp` package path.

Stateful content uses an explicitly versioned additive extension of the
existing container: package minor version 1 plus optional ``HIST`` and
``CKPT`` sections.  History-free exports never enter this module and retain the
exact NW-08A baseline bytes and linked closure.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import nw03_compiler as nw03
import nw04_fixture
import nwp_package as nwp
import nw09_temporal as temporal


NW09_EXPORT_FORMAT = "nw09-stateful-export-report"
NW09_EXPORT_VERSION = 1


class StatefulExportError(RuntimeError):
    def __init__(
        self,
        message: str,
        diagnostics: Sequence[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def present_geometry(document: nw03.GraphDocument) -> tuple[int, int] | None:
    """Return the explicit NW-05 rectangular presentation geometry."""

    width = document.settings.get("nw05.present.width")
    height = document.settings.get("nw05.present.height")
    if width is None or height is None:
        return None
    try:
        parsed = (int(float(width)), int(float(height)))
    except (TypeError, ValueError):
        return None
    if parsed[0] <= 0 or parsed[1] <= 0:
        return None
    return parsed


def _base_package(document: nw03.GraphDocument, *, allow_test_payloads: bool) -> nwp.NwpPackage:
    if not allow_test_payloads:
        # A production stateful export uses the same offline DXC path as the
        # established exporter.  Contract builds and tests use the structurally
        # valid synthetic container explicitly.
        raise StatefulExportError(
            "production stateful export requires the offline-DXC package path; "
            "use allow_test_payloads only for contract verification",
            [
                nwp.package_diagnostic(
                    "export_requires_offline_dxc",
                    "offline shader compilation is unavailable in this build",
                )
            ],
        )
    return nw04_fixture.build_compute_display_package()


def build_stateful_package(
    document: nw03.GraphDocument,
    *,
    allow_test_payloads: bool,
    packaged_checkpoint_ticks: Sequence[int] = (),
    frames_in_flight: int = 2,
) -> tuple[nwp.NwpPackage, dict[str, Any]]:
    """Return the existing package model with the explicit NW-09 extension."""

    if frames_in_flight < 2:
        raise StatefulExportError(
            "stateful packages require at least two frames in flight"
        )
    specs = temporal.history_specs(document)
    if not specs:
        raise StatefulExportError("document has no History declarations")
    package = _base_package(document, allow_test_payloads=allow_test_payloads)
    next_resource = max(
        (resource.resource_id for resource in package.resources), default=0
    ) + 1
    resources = list(package.resources)
    initialization = list(package.initialization)
    frame = list(package.frame)
    parameters = list(package.parameters)
    next_parameter_id = max(
        (parameter.parameter_id for parameter in parameters), default=0
    ) + 1
    geometry = present_geometry(document)
    if geometry is not None:
        for name, value in (
            ("nw05.present.width", geometry[0]),
            ("nw05.present.height", geometry[1]),
        ):
            parameters.append(
                nwp.ParameterRecord(
                    parameter_id=next_parameter_id,
                    name=name,
                    type="uint",
                    phase="initialization",
                    component_count=1,
                    value=struct.pack("<I", int(value) & 0xFFFFFFFF),
                    timeline_start_frame=0,
                    timeline_end_frame=0,
                    timeline_step=0,
                    timeline_interpolation="none",
                )
            )
            next_parameter_id += 1
    history_records: list[nwp.HistoryRecord] = []
    reports: list[dict[str, Any]] = []
    state_resource_ids: list[int] = []
    for index, spec in enumerate(specs, start=1):
        current_id = next_resource
        next_resource += 1
        previous_id = next_resource
        next_resource += 1
        state_bytes = spec.state_bytes
        for resource_id, name, family in (
            (current_id, f"{spec.node_id}.current", "compute.nw06.camera"),
            (previous_id, f"{spec.node_id}.previous", "compute.nw06.light"),
        ):
            resources.append(
                nwp.ResourceDescriptor(
                    resource_id=resource_id,
                    name=name,
                    kind="buffer",
                    format="DXGI_FORMAT_R32G32B32A32_FLOAT",
                    usage="uav_srv",
                    pass_family=family,
                    binding_slot=0,
                    binding_space=0,
                    size_bytes=max(16, state_bytes),
                )
            )
            state_resource_ids.append(resource_id)
            initialization.append(
                nwp.Instruction(
                    "init.create_resource",
                    (nwp.InstructionOperand("resource_ref", resource_id),),
                )
            )
        # Explicit ping-pong boundaries: previous is sampled, current is
        # written, and the barrier makes the write visible only for the next
        # simulation tick.  The two resource ids are never identical.
        frame.extend(
            [
                nwp.Instruction(
                    "frame.barrier",
                    (
                        nwp.InstructionOperand("u32", 4),
                        nwp.InstructionOperand("resource_ref", current_id),
                    ),
                ),
                nwp.Instruction(
                    "frame.copy_buffer",
                    (
                        nwp.InstructionOperand("resource_ref", previous_id),
                        nwp.InstructionOperand("resource_ref", current_id),
                        nwp.InstructionOperand("u64", state_bytes),
                    ),
                ),
            ]
        )
        history_records.append(
            nwp.HistoryRecord(
                state_id=index,
                node_id=spec.node_id,
                operator_id=spec.operator_id,
                state_type=spec.state_type,
                initial_value=spec.initial_value,
                reset_policy=spec.reset_policy,
                step_rate_hz=spec.step_rate_hz,
                state_layout_hash=spec.state_layout_hash,
                values_per_element=spec.values_per_element,
                element_count=spec.element_count,
                current_resource_id=current_id,
                previous_resource_id=previous_id,
                frames_in_flight=frames_in_flight,
            )
        )
        reports.append(
            {
                **spec.to_dict(),
                "current_resource_id": current_id,
                "previous_resource_id": previous_id,
                "ping_pong_distinct": current_id != previous_id,
                "state_bytes": state_bytes,
                "frames_in_flight": frames_in_flight,
            }
        )
    checkpoints: list[nwp.ReleaseCheckpointRecord] = []
    checkpoint_reports: list[dict[str, Any]] = []
    for checkpoint_id, tick in enumerate(packaged_checkpoint_ticks, start=1):
        simulation_tick = tick // temporal.TIMELINE_TICKS_PER_SIMULATION_TICK
        # Packaged/disk checkpoints are explicitly host-resident.  They make no
        # portable GPU-state replay claim.
        payload = nwp.canonical_json_bytes(
            {
                "format": "nw09-release-checkpoint.v1",
                "tick": int(tick),
                "simulation_tick": int(simulation_tick),
                "semantic_digest": "deferred-to-runtime-semantic-digest",
                "state_layout_hash": nwp.sha256_hex(
                    nwp.canonical_json_bytes(
                        sorted(
                            spec.state_layout_hash for spec in specs
                        )
                    )
                ),
                "states": [
                    {"node_id": spec.node_id, "state_bytes": spec.state_bytes}
                    for spec in specs
                ],
                "residency": "host",
                "portable_gpu_state_claim": False,
            }
        )
        record = nwp.ReleaseCheckpointRecord(
            checkpoint_id=checkpoint_id,
            label=f"release-tick-{tick}",
            tick=int(tick),
            simulation_tick=int(simulation_tick),
            semantic_digest="deferred-to-runtime-semantic-digest",
            state_layout_hash="sha256:v1:"
            + nwp.sha256_hex(
                nwp.canonical_json_bytes(
                    sorted(spec.state_layout_hash for spec in specs)
                )
            ),
            capability_profile_id=document.profile_id,
            capability_requirements_digest=document.requirements_digest,
            payload=payload,
            state_count=len(specs),
        )
        checkpoints.append(record)
        checkpoint_reports.append(record.to_dict())

    derived = nwp.derive_capability_requirements(
        resources,
        initialization,
        frame,
        package.shaders,
        package.root_signatures,
    )
    capability = replace(
        package.capability,
        **derived,
        required_init_instructions=tuple(
            sorted({instruction.opcode for instruction in initialization})
        ),
        required_frame_instructions=tuple(
            sorted({instruction.opcode for instruction in frame})
        ),
    )
    capability = replace(
        capability, profile_fingerprint=capability.computed_profile_fingerprint()
    )
    stateful = replace(
        package,
        capability=capability,
        resources=resources,
        initialization=initialization,
        frame=frame,
        parameters=parameters,
        history=tuple(history_records),
        release_checkpoints=tuple(checkpoints),
        flags=1,
    )
    diagnostics = nwp.validate_package_model(stateful)
    if diagnostics:
        raise StatefulExportError(
            "stateful package extension is invalid", diagnostics
        )
    report = {
        "format": NW09_EXPORT_FORMAT,
        "version": NW09_EXPORT_VERSION,
        "stateful": True,
        "package_version": {
            "major": nwp.VERSION_MAJOR,
            "minor": nwp.VERSION_MINOR_STATEFUL,
        },
        "capability_profile_id": document.profile_id,
        "capability_requirements_digest": document.requirements_digest,
        "fixed_step": {
            "simulation_hz": temporal.FIXED_STEP_HZ,
            "timeline_ticks_per_second": temporal.TIMELINE_TICKS_PER_SECOND,
            "timeline_ticks_per_simulation_tick": (
                temporal.TIMELINE_TICKS_PER_SIMULATION_TICK
            ),
        },
        "history": reports,
        "state_resources": state_resource_ids,
        "frames_in_flight": frames_in_flight,
        "pending_retirement_bytes": sum(
            report["state_bytes"] * frames_in_flight for report in reports
        ),
        "packaged_checkpoints": checkpoint_reports,
        "packaged_checkpoint_byte_cost": sum(
            item["byte_cost"] for item in checkpoint_reports
        ),
        "present_geometry": (
            {"width": present_geometry(document)[0], "height": present_geometry(document)[1]}
            if present_geometry(document)
            else None
        ),
        "portable_gpu_state_replay": False,
    }
    return stateful, report


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".nw09-export-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            Path(name).unlink()
        except FileNotFoundError:
            pass


def export_stateful_graph(
    graph_path: str | os.PathLike[str],
    output_directory: str | os.PathLike[str],
    *,
    report_path: str | os.PathLike[str] | None = None,
    allow_test_payloads: bool = False,
    packaged_checkpoint_ticks: Sequence[int] = (),
    frames_in_flight: int = 2,
) -> dict[str, Any]:
    graph_path = Path(graph_path)
    output_directory = Path(output_directory)
    document = nw03.load_graph(graph_path)
    package, report = build_stateful_package(
        document,
        allow_test_payloads=allow_test_payloads,
        packaged_checkpoint_ticks=packaged_checkpoint_ticks,
        frames_in_flight=frames_in_flight,
    )
    package_bytes = nwp.encode_package(package)
    valid, diagnostics = nwp.validate_package_bytes(package_bytes)
    if not valid:
        raise StatefulExportError(
            "stateful package bytes did not round-trip", diagnostics
        )
    output_directory.mkdir(parents=True, exist_ok=True)
    package_path = output_directory / "stateful.nwp"
    _write_atomic(package_path, package_bytes)
    report.update(
        {
            "success": True,
            "source_graph": {
                "path": str(graph_path),
                "sha256": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
            },
            "package_path": str(package_path),
            "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
            "package_bytes": len(package_bytes),
            "checkpoint_byte_cost": report["packaged_checkpoint_byte_cost"],
        }
    )
    if report_path is not None:
        _write_atomic(
            Path(report_path),
            json.dumps(report, indent=2, sort_keys=True).encode("utf-8"),
        )
    return report

