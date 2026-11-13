#!/usr/bin/env python3
"""NW-06 ContractsOnly and capture verification entry point.

Usage:
    python tools/verify_nw06.py --mode ContractsOnly [--summary-json out.json]
    python tools/verify_nw06.py --mode Full --fixture <player.exe> \
        --package <export-dir> --evidence <evidence.json>

ContractsOnly is GPU-less and never claims hardware acceptance.  Full/Hardware
require matching evidence for the identical common artifact; missing coverage
is reported as pending/unverified, not success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw04_fixture  # noqa: E402
import nw04_player_contract as nw04_contract  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw05_export as nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw06_evidence  # noqa: E402
import nw06_export  # noqa: E402
import nw06_procedural as procedural  # noqa: E402
import nw06_timeline as timeline  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402


PROCEDURAL_FIXTURE = REPO_ROOT / "fixtures" / "nw06" / "procedural_scene.nwgraph"
NOISE_FIXTURE = REPO_ROOT / "fixtures" / "nw06" / "noise_torus_instances.nwgraph"
EVIDENCE_FIXTURE = REPO_ROOT / "benchmarks" / "nw06-hardware-evidence.json"
EVIDENCE_SCHEMA = REPO_ROOT / "schemas" / "nw06-hardware-evidence.schema.json"


@dataclass
class CheckResult:
    name: str
    required: bool = True
    passed: bool = False
    status: str = "failed"
    detail: str = ""
    unavailable: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "passed": self.passed,
            "status": self.status,
            "detail": self.detail,
            "unavailable": self.unavailable,
            "evidence": self.evidence,
        }


def _ok(name: str, detail: str = "", evidence: dict[str, Any] | None = None) -> CheckResult:
    return CheckResult(name=name, passed=True, status="passed", detail=detail, evidence=evidence or {})


def _fail(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, passed=False, status="failed", detail=detail)


def _pending(name: str, detail: str) -> CheckResult:
    return CheckResult(
        name=name,
        required=True,
        passed=False,
        status="pending_unverified",
        detail=detail,
        unavailable=True,
    )


def _mock_dxc(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dxc = directory / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
        "fo.parent.mkdir(parents=True, exist_ok=True)\n"
        f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\n",
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    return dxc


def check_graph() -> CheckResult:
    try:
        document = nw03.load_graph(PROCEDURAL_FIXTURE)
        alternate = nw03.load_graph(NOISE_FIXTURE)
        if set(document.nodes) != set(alternate.nodes):
            return _fail("nw06_graph_contract", "alternate fixture has different nodes")
        optimized = optimizer.optimize_graph(document)
        alternate_optimized = optimizer.optimize_graph(alternate)
        if alternate_optimized.ir_sha256() != optimized.ir_sha256():
            return _fail(
                "nw06_graph_contract",
                "alternate fixture has a different semantic identity",
            )
        required_ops = {
            "nw.texture.noise2d",
            "nw.mesh.torus",
            "nw.scene.instances",
            "nw.material.standard",
            "nw.camera.curve",
            "nw.animation.twist",
            "nw.render.instanced",
            "nw.post.bloom",
            "nw.post.tonemap",
        }
        used = {node.operator_id for node in optimized.nodes}
        missing = sorted(required_ops - used)
        if missing:
            return _fail("nw06_graph_contract", f"missing operators {missing}")
        for node_id in ("texture", "torus", "instances"):
            if node_id not in optimized.partitions.get("initialization", ()):
                return _fail("nw06_graph_contract", f"{node_id} is not initialization-frequency")
        if "render" not in optimized.partitions.get("frame", ()):
            return _fail("nw06_graph_contract", "raster pass is not frame-frequency")
        return _ok(
            "nw06_graph_contract",
            evidence={
                "reachable": list(optimized.reachable_node_ids),
                "partitions": {key: list(value) for key, value in optimized.partitions.items()},
                "ir_sha256": optimized.ir_sha256(),
            },
        )
    except Exception as exc:
        return _fail("nw06_graph_contract", str(exc))


def check_procedural_reference() -> CheckResult:
    try:
        params = procedural.Noise2DParameters()
        params.validate()
        samples = procedural.generate_noise2d(8, 8, seed=params.seed)
        if len(samples) != 64 or any(not 0.0 <= value <= 1.0 for value in samples):
            return _fail("nw06_procedural_reference", "noise reference is outside [0,1]")
        exact = procedural.hash_coord_u32(17, 29, 0x4E573036)
        if not 0 <= exact <= 0xFFFFFFFF:
            return _fail("nw06_procedural_reference", "integer seed/control hash is invalid")
        estimate = procedural.estimate_resources()
        if estimate["baked_asset_bytes"] != 0 or not estimate["procedural_parameters_only"]:
            return _fail("nw06_procedural_reference", "baseline estimate contains baked assets")
        digest = procedural.procedural_content_digest()
        if not digest.startswith("sha256:v1:") or len(digest) != 74:
            return _fail("nw06_procedural_reference", "content digest is malformed")
        return _ok(
            "nw06_procedural_reference",
            evidence={
                "noise_sample": samples[0],
                "integer_hash": exact,
                "content_digest": digest,
                "resource_estimate": estimate,
            },
        )
    except Exception as exc:
        return _fail("nw06_procedural_reference", str(exc))


def check_mesh_and_encodings() -> CheckResult:
    try:
        mesh = procedural.generate_torus_mesh()
        mesh.validate()
        if mesh.vertex_count != 64 * 32 or mesh.index_count != 64 * 32 * 6:
            return _fail("nw06_mesh_and_encoding", "unexpected torus dimensions")
        if procedural.VERTEX_BYTES != 32 or procedural.INSTANCE_BYTES != 80:
            return _fail("nw06_mesh_and_encoding", "vertex/instance stride changed")
        if procedural.MATERIAL_BYTES != 32 or procedural.INDIRECT_ARGS_BYTES != 20:
            return _fail("nw06_mesh_and_encoding", "material/indirect stride changed")
        if min(mesh.indices) < 0 or max(mesh.indices) >= mesh.vertex_count:
            return _fail("nw06_mesh_and_encoding", "index outside vertex bounds")
        instances = procedural.generate_instances()
        if len(instances) != 256:
            return _fail("nw06_mesh_and_encoding", "instance count is not 256")
        if any(len(record.transform) != 16 for record in instances):
            return _fail("nw06_mesh_and_encoding", "instance transform width is not 16")
        material = procedural.generate_materials()[0]
        encoded_instance = instances[0].encode()
        encoded_material = material.encode()
        if len(encoded_instance) != 80 or len(encoded_material) != 32:
            return _fail("nw06_mesh_and_encoding", "record encoding length mismatch")
        args = procedural.encode_indirect_args(mesh.index_count, len(instances))
        if len(args) != 20:
            return _fail("nw06_mesh_and_encoding", "indirect argument length mismatch")
        return _ok(
            "nw06_mesh_and_encoding",
            evidence={
                "vertex_count": mesh.vertex_count,
                "index_count": mesh.index_count,
                "instance_count": len(instances),
                "vertex_layout": list(procedural.VERTEX_LAYOUT),
                "instance_layout": list(procedural.INSTANCE_LAYOUT),
                "material_layout": list(procedural.MATERIAL_LAYOUT),
            },
        )
    except Exception as exc:
        return _fail("nw06_mesh_and_encoding", str(exc))


def check_timeline() -> CheckResult:
    try:
        if timeline.TICKS_PER_SECOND != 48_000:
            return _fail("nw06_timeline", "tick rate is not 48,000")
        if timeline.checked_seconds_to_ticks("1.5") != 72_000:
            return _fail("nw06_timeline", "seconds conversion is wrong")
        if timeline.checked_ticks_from_monotonic_ns(1_000_000_000) != 48_000:
            return _fail("nw06_timeline", "monotonic conversion is wrong")
        overflow = False
        try:
            timeline.checked_add_ticks(timeline.MAX_TICKS, 1)
        except timeline.TimelineError:
            overflow = True
        if not overflow:
            return _fail("nw06_timeline", "tick overflow was not rejected")
        step = timeline.RationalFrameStep(1001, 30000)
        if step.tick_for_frame(30_000) != 1001:
            return _fail("nw06_timeline", "rational frame step is wrong")
        curve = timeline.Curve(
            (
                timeline.CurveKey(0, 0.0),
                timeline.CurveKey(24_000, 0.5),
                timeline.CurveKey(48_000, 1.0),
            ),
            value_min=0.0,
            value_max=1.0,
        )
        quantized = timeline.quantize_curve(curve, bits=10, tolerance=0.001)
        if not quantized.record.within_tolerance:
            return _fail("nw06_timeline", "bounded curve quantization failed")
        escape = timeline.quantize_curve(curve, full_precision=True)
        if escape.maximum_error != 0.0:
            return _fail("nw06_timeline", "full-precision escape introduced error")
        invalid = False
        try:
            timeline.Curve((timeline.CurveKey(0, 2.0),), value_min=0.0, value_max=1.0)
        except timeline.TimelineError:
            invalid = True
        if not invalid:
            return _fail("nw06_timeline", "invalid curve range was accepted")
        events = [timeline.Event(10, 1), timeline.Event(20, 2), timeline.Event(30, 3)]
        cursor = timeline.EventCursor(events)
        first = cursor.advance(10)
        second = cursor.advance(20)
        if [event.event_id for event in first] != [1] or [event.event_id for event in second] != [2]:
            return _fail("nw06_timeline", "event interval is not (previous,current]")
        cursor.seek(25)
        if [event.event_id for event in cursor.advance(30)] != [3]:
            return _fail("nw06_timeline", "seek did not reset event cursor")
        cursor.seek(0, replay_from_start=True)
        if [event.event_id for event in cursor.advance(20)] != [1, 2]:
            return _fail("nw06_timeline", "replay semantics are wrong")
        camera = timeline.CameraCurve(
            angle_curve=curve,
            radius_curve=timeline.Curve((timeline.CurveKey(0, 3.0),), value_min=0.0, value_max=10.0),
            height_curve=timeline.Curve((timeline.CurveKey(0, 1.0),), value_min=0.0, value_max=10.0),
        )
        at_a = camera.sample(24_000)
        at_b = camera.sample(24_000)
        if at_a != at_b:
            return _fail("nw06_timeline", "absolute-tick sampling is not stateless")
        return _ok(
            "nw06_timeline",
            evidence={
                "ticks_per_second": timeline.TICKS_PER_SECOND,
                "curve_quantization": quantized.record.to_dict(),
                "events": [event.to_dict() for event in events],
                "camera_sample": at_a,
            },
        )
    except Exception as exc:
        return _fail("nw06_timeline", str(exc))


def _export(temp: Path) -> nw05_export.ExportResult:
    os.environ["NODEWERK_NW03_TEST_DIGEST"] = "1"
    dxc = _mock_dxc(temp / "toolchain")
    template = temp / "template.exe"
    template.write_bytes(b"MZ" + b"\x00" * 256)
    settings = nw05_export.ExportSettings(
        byte_ceiling=16 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        player_template=str(template),
    )
    return nw05_export.export_graph(
        PROCEDURAL_FIXTURE,
        temp / "artifact",
        settings=settings,
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )


def check_export_and_determinism() -> CheckResult:
    try:
        with tempfile.TemporaryDirectory(prefix="nw06-verify-") as temp_name:
            temp = Path(temp_name)
            result = _export(temp)
            common = result.artifacts[nwp.COMMON_PROFILE_ID]
            package = common.package
            init_ops = [item.opcode for item in package.initialization]
            frame_ops = [item.opcode for item in package.frame]
            if init_ops.count("init.dispatch") != 5:
                return _fail(
                    "nw06_export",
                    "expected five initialization-only GPU generators",
                )
            if any(item.opcode == "init.upload_resource" for item in package.initialization):
                return _fail("nw06_export", "procedural content is uploaded from the CPU")
            if frame_ops.count("frame.execute_indirect") != 1:
                return _fail("nw06_export", "expected exactly one indirect draw")
            if "frame.draw" in frame_ops or "frame.dispatch" not in frame_ops:
                return _fail("nw06_export", "post chain or rasterization stream is wrong")
            if any(item.opcode == "frame.copy_buffer" for item in package.frame):
                return _fail("nw06_export", "frame stream performs a CPU-observable copy")
            if any(resource.usage == "readback" for resource in package.resources):
                return _fail("nw06_export", "playback package contains a readback resource")
            texture = next(resource for resource in package.resources if resource.kind == "texture2d" and resource.width == 256)
            if texture.size_bytes != 256 * 256 * 4:
                return _fail("nw06_export", "Noise2D texture resource size is incorrect")
            vertices = next(resource for resource in package.resources if resource.name == "vertices")
            indices = next(resource for resource in package.resources if resource.name == "indices")
            instances = next(resource for resource in package.resources if resource.name == "instances")
            if (
                vertices.size_bytes != 2048 * 32
                or indices.size_bytes != 12288 * 4
                or instances.size_bytes != 256 * 80
            ):
                return _fail("nw06_export", "procedural buffer sizes are incorrect")
            if package.capability.profile_id != nwp.COMMON_PROFILE_ID:
                return _fail("nw06_export", "common profile identity changed")
            if package.capability.requirements_digest != nwp.COMMON_REQUIREMENTS_DIGEST:
                return _fail("nw06_export", "requirements digest changed")
            derived = nwp.derive_capability_requirements(
                package.resources,
                package.initialization,
                package.frame,
                package.shaders,
                package.root_signatures,
            )
            if (
                derived["max_cbv_srv_uav_descriptors_per_frame"] != 17
                or package.capability.max_cbv_srv_uav_descriptors_per_frame
                != 17
            ):
                return _fail(
                    "nw06_export",
                    "native/exporter descriptor requirements disagree for "
                    "combined usages",
                )
            if nw04_contract.validate_package_requirements(package):
                return _fail(
                    "nw06_export",
                    "NW-04 package requirement validation rejected the NW-06 "
                    "common artifact",
                )
            for root in package.root_signatures:
                flags: list[int] = []
                decoded = nwp._decode_root_signature_blob(
                    root.blob, [], flags
                )
                if decoded is None:
                    return _fail(
                        "nw06_export",
                        f"root signature {root.root_signature_id} is malformed",
                    )
                expected_flags = (
                    1 if root.family.startswith("graphics.") else 0
                )
                if flags != [expected_flags]:
                    return _fail(
                        "nw06_export",
                        "root signature flags do not match the pass family",
                    )
            tick_parameter = next(
                parameter
                for parameter in package.parameters
                if parameter.name == "nw06.params.14.tick"
            )
            if tick_parameter.type != "uint":
                return _fail(
                    "nw06_export",
                    "graphics tick is not an explicit integer tick parameter",
                )
            packed = nw06_export.pack_frame_root_constants(package, 14, 24_000)
            if struct.unpack_from("<I", packed, 0)[0] != 24_000:
                return _fail(
                    "nw06_export",
                    "frame root constants do not pack an absolute integer tick",
                )
            camera_curve = common.manifest.get("timeline", {}).get(
                "camera_curve", {}
            )
            if (
                not camera_curve
                or not camera_curve.get("within_tolerance")
                or camera_curve.get("maximum_absolute_error", 1.0)
                > camera_curve.get("declared_tolerance", 0.0)
            ):
                return _fail(
                    "nw06_export",
                    "camera curve quantization was not bounded and recorded",
                )
            logical = common.logical_schedule
            if (
                "frame.set_render_targets" not in logical.frame_order
                or "frame.execute_indirect" not in logical.frame_order
                or not logical.resource(17).alias_eligible
                or not logical.resource(18).alias_eligible
                or not logical.resource(19).alias_eligible
                or any(
                    logical.resource(resource_id).alias_eligible
                    for resource_id in (2, 3, 4, 6, 7, 9, 11, 13)
                )
            ):
                return _fail(
                    "nw06_export",
                    "lifetime/alias planning did not include the raster/post resources",
                )
            targets = set(package.capability.emitted_shader_targets)
            if targets != {"cs_6_0", "vs_6_0", "ps_6_0"}:
                return _fail("nw06_export", f"unexpected shader targets {sorted(targets)}")
            if any(
                root.dword_cost > nwp.MAX_ROOT_SIGNATURE_DWORD_COST
                for root in package.root_signatures
            ):
                return _fail(
                    "nw06_export",
                    "root signature exceeds the declared common-profile limit",
                )
            if not common.manifest.get("retained", {}).get("runtime_implementations"):
                return _fail("nw06_export", "generated link manifest has no used features")
            if not any(
                item == "player.nw06.indexed-instanced-indirect"
                for item in common.manifest["retained"]["runtime_implementations"]
            ):
                return _fail("nw06_export", "generated link closure omitted raster implementation")
            instrumentation = result.report.get("instrumentation", {})
            if (
                instrumentation.get("initialization_time", {}).get("status")
                != "unavailable"
                or instrumentation.get("per_pass_time", {}).get("status")
                != "unavailable"
                or instrumentation.get("shader_bytes", {}).get("status")
                != "measured"
            ):
                return _fail(
                    "nw06_export",
                    "instrumentation did not distinguish unavailable and measured costs",
                )
            second = _export(temp)
            second_common = second.artifacts[nwp.COMMON_PROFILE_ID]
            if second_common.package_bytes != common.package_bytes:
                return _fail("nw06_export", "common package identity is not deterministic")
            if second_common.manifest["manifest_sha256"] != common.manifest["manifest_sha256"]:
                return _fail("nw06_export", "generated link manifest is not deterministic")
            return _ok(
                "nw06_export",
                evidence={
                    "artifact_key": result.common_artifact_key,
                    "package_sha256": hashlib.sha256(common.package_bytes).hexdigest(),
                    "init_ops": init_ops,
                    "frame_ops": frame_ops,
                    "shader_targets": sorted(targets),
                    "camera_curve": camera_curve,
                    "alias_eligible_resources": [
                        resource.resource_id
                        for resource in logical.resources
                        if resource.alias_eligible
                    ],
                },
            )
    except Exception as exc:
        return _fail("nw06_export", str(exc))


def check_shader_contracts() -> CheckResult:
    try:
        shader_root = REPO_ROOT / "shaders" / "nw06"
        light = (shader_root / "nw06_scene_data_cs.hlsl").read_text(
            encoding="utf-8"
        )
        material = (shader_root / "nw06_material_cs.hlsl").read_text(
            encoding="utf-8"
        )
        vertex = (shader_root / "nw06_scene_vs.hlsl").read_text(
            encoding="utf-8"
        )
        pixel = (shader_root / "nw06_scene_ps.hlsl").read_text(
            encoding="utf-8"
        )
        player = (
            REPO_ROOT / "src" / "player" / "package_player.cpp"
        ).read_text(encoding="utf-8")
        resource_plan = (
            REPO_ROOT / "src" / "player" / "resource_plan.cpp"
        ).read_text(encoding="utf-8")
        if (
            "uint4(Param0, Param1, Param2, Param3)" not in light
            or "asuint(float4(Param0" in light
        ):
            return _fail(
                "nw06_shader_contract",
                "light record does not preserve float bit patterns",
            )
        if "float(TextureIndex)" in material or "TextureIndex))" not in material:
            return _fail(
                "nw06_shader_contract",
                "material texture index is not encoded as uint",
            )
        if "uint Tick;" not in vertex:
            return _fail(
                "nw06_shader_contract", "vertex tick is not an integer tick"
            )
        if (
            "output.camera_position = eye;" not in vertex
            or "normalize(input.camera_position - input.world_position)"
            not in pixel
        ):
            return _fail(
                "nw06_shader_contract",
                "pixel lighting does not use the animated camera eye",
            )
        if (
            "ClearRenderTargetView(rtv" not in player
            or "ClearDepthStencilView(" not in player
        ):
            return _fail(
                "nw06_shader_contract",
                "render-target binding does not clear colour/depth",
            )
        if (
            '} else if (parameter.type == "uint") {' not in player
            or "*tick_kind = 1u;" not in player
            or "if (tick_kind == 1u) {" not in player
            or "static_cast<std::uint32_t>(tick)" not in player
        ):
            return _fail(
                "nw06_shader_contract",
                "PackagePlayer does not lower uint timeline ticks as integers",
            )
        descriptor_resolution = player.find(
            "op.descriptor_is_srv =\n                binding != nullptr"
        )
        descriptor_propagation = player.find(
            "active_descriptor_is_srv[op.root_parameter] ="
        )
        if (
            "package_rtv_heap" not in player
            or "package_dsv_heap" not in player
            or "D3D12_DESCRIPTOR_HEAP_TYPE_DSV" not in player
            or descriptor_resolution < 0
            or descriptor_propagation < 0
            or descriptor_resolution >= descriptor_propagation
        ):
            return _fail(
                "nw06_shader_contract",
                "PackagePlayer descriptor-heap or SRV-binding invariants "
                "are not satisfied",
            )
        if (
            "binding_usage_for" not in resource_plan
            or 'instruction.opcode == "init.dispatch"' not in resource_plan
            or "it->second.initialized = true;" not in resource_plan
            or 'instruction.opcode == "frame.set_root_signature"' not in resource_plan
            or "bound_by_root.clear();" not in resource_plan
            or 'instruction.opcode == "frame.execute_indirect"' not in resource_plan
            or 'resource.name == "vertices"' not in resource_plan
            or 'resource.name == "indices"' not in resource_plan
            or resource_plan.count("root_bindings.clear();") < 2
        ):
            return _fail(
                "nw06_shader_contract",
                "native resource lifetime analysis is not binding-aware",
            )
        return _ok(
            "nw06_shader_contract",
            evidence={
                "light_bit_preserving": True,
                "material_uint_texture_index": True,
                "integer_tick": True,
                "camera_eye_specular": True,
                "explicit_render_target_clears": True,
                "package_player_uint_tick_lowering": True,
                "dedicated_rtv_dsv_heaps": True,
                "descriptor_srv_binding_order": True,
                "native_resource_plan_binding_aware": True,
            },
        )
    except Exception as exc:
        return _fail("nw06_shader_contract", str(exc))


def check_evidence_contract() -> CheckResult:
    try:
        record = json.loads(EVIDENCE_FIXTURE.read_text(encoding="utf-8"))
        validate_file_against_schema(EVIDENCE_FIXTURE, EVIDENCE_SCHEMA)
        validation = nw06_evidence.validate_evidence_record(record)
        if validation["status"] == "verified":
            return _fail("nw06_evidence_contract", "empty coverage was reported verified")
        if validation["status"] not in {"pending_unverified", "failed"}:
            return _fail("nw06_evidence_contract", "unknown evidence status")
        missing = nw06_evidence.validate_capture(
            {
                "status": "passed",
                "profile_id": nwp.COMMON_PROFILE_ID,
                "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
            }
        )
        if missing["status"] == "passed":
            return _fail("nw06_evidence_contract", "missing telemetry was accepted")
        return _ok(
            "nw06_evidence_contract",
            evidence={
                "status": validation["status"],
                "required_metadata": list(nw06_evidence.REQUIRED_CAPTURE_METADATA),
                "missing_telemetry_rejected": True,
            },
        )
    except Exception as exc:
        return _fail("nw06_evidence_contract", str(exc))


def contracts_only() -> list[CheckResult]:
    return [
        check_graph(),
        check_procedural_reference(),
        check_mesh_and_encodings(),
        check_timeline(),
        check_export_and_determinism(),
        check_shader_contracts(),
        check_evidence_contract(),
    ]


def _load_evidence(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def full_or_hardware(args: argparse.Namespace) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if not args.fixture or not args.package or not args.evidence:
        return [
            _pending(
                "nw06_hardware_capture",
                "--fixture, --package, and --evidence are required for hardware "
                "verification; coverage remains pending/unverified",
            )
        ]
    package_dir = Path(args.package)
    executable_path = Path(args.fixture)
    if not executable_path.is_file():
        return [
            _fail(
                "nw06_artifact_identity",
                f"capture executable is unavailable: {executable_path}",
            )
        ]
    try:
        _, package_bytes, artifact_key, manifest_sha256 = (
            nw06_evidence._artifact_identity(package_dir)
        )
    except Exception as exc:
        return [_fail("nw06_artifact_identity", str(exc))]
    package_sha256 = hashlib.sha256(package_bytes).hexdigest()
    executable_sha256 = hashlib.sha256(
        executable_path.read_bytes()
    ).hexdigest()
    record = _load_evidence(Path(args.evidence))
    try:
        validate_file_against_schema(Path(args.evidence), EVIDENCE_SCHEMA)
    except Exception as exc:
        return [_fail("nw06_evidence_schema", str(exc))]
    captures = record.get("captures") or record.get("executions") or []
    mismatched: list[dict[str, Any]] = []
    for capture in captures:
        expected = {
            "artifact_key": artifact_key,
            "package_sha256": package_sha256,
            "manifest_sha256": manifest_sha256,
            "executable_sha256": executable_sha256,
        }
        differences = {
            name: {
                "expected": value,
                "recorded": capture.get(name),
            }
            for name, value in expected.items()
            if capture.get(name) != value
        }
        if differences:
            mismatched.append(
                {
                    "capture_id": capture.get("capture_id", ""),
                    "differences": differences,
                }
            )
    if mismatched:
        return [
            _fail(
                "nw06_artifact_identity",
                "supplied artifact/executable do not match the capture record",
            ),
            _fail(
                "nw06_hardware_capture",
                json.dumps(mismatched, sort_keys=True),
            ),
        ]
    status = nw06_evidence.validate_evidence_record(
        record, artifact_root=Path(args.evidence).parent
    )
    checks.append(
        _ok(
            "nw06_artifact_identity",
            evidence={
                "artifact_key": artifact_key,
                "package_sha256": package_sha256,
                "manifest_sha256": manifest_sha256,
                "executable_sha256": executable_sha256,
            },
        )
    )
    if status["status"] == "verified":
        checks.append(_ok("nw06_hardware_capture", evidence=status))
    elif status["status"] == "failed":
        checks.append(_fail("nw06_hardware_capture", json.dumps(status, sort_keys=True)))
    else:
        checks.append(
            _pending(
                "nw06_hardware_capture",
                "required Ada/Blackwell fixed-tick coverage is missing or incomplete",
            )
        )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("ContractsOnly", "Full", "Hardware"),
        default="ContractsOnly",
    )
    parser.add_argument("--fixture")
    parser.add_argument("--package")
    parser.add_argument("--evidence")
    parser.add_argument("--summary-json")
    args = parser.parse_args(argv)
    checks = contracts_only() if args.mode == "ContractsOnly" else full_or_hardware(args)
    required_failures = [
        check for check in checks if check.required and check.status == "failed"
    ]
    pending = [
        check for check in checks if check.status == "pending_unverified"
    ]
    payload = {
        "format": "nw06-verification-summary",
        "version": 1,
        "mode": args.mode,
        "success": not required_failures,
        "contracts_only_success_does_not_imply_hardware_acceptance": True,
        "checks": [check.to_dict() for check in checks],
        "required_failures": len(required_failures),
        "pending_checks": len(pending),
    }
    if args.summary_json:
        path = Path(args.summary_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    if required_failures:
        return 1
    if args.mode != "ContractsOnly" and pending:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
