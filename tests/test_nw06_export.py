from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw04_fixture  # noqa: E402
import nw04_player_contract as nw04_contract  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw06_export  # noqa: E402
import nw06_procedural as procedural  # noqa: E402


PROCEDURAL = REPO_ROOT / "fixtures" / "nw06" / "procedural_scene.nwgraph"
SHADER_ROOT = REPO_ROOT / "shaders" / "nw06"


@pytest.fixture(autouse=True)
def _test_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")


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


def _export(tmp_path: Path) -> nw05_export.ExportResult:
    return _export_graph(tmp_path, PROCEDURAL, "artifact")


def _export_graph(
    tmp_path: Path, graph_path: Path, artifact_name: str
) -> nw05_export.ExportResult:
    template = tmp_path / "template.exe"
    template.write_bytes(b"MZ" + b"\x00" * 256)
    settings = nw05_export.ExportSettings(
        byte_ceiling=16 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        player_template=str(template),
    )
    return nw05_export.export_graph(
        graph_path,
        tmp_path / artifact_name,
        settings=settings,
        dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
        allow_test_payloads=True,
    )


def test_nw06_graph_is_reachable_partitioned_and_typed() -> None:
    document = nw03.load_graph(PROCEDURAL)
    optimized = optimizer.optimize_graph(document)
    assert nw06_export.uses_nw06(optimized)
    assert set(("texture", "torus", "instances")) <= set(
        optimized.partitions["initialization"]
    )
    assert "render" in optimized.partitions["frame"]
    assert "render" not in optimized.partitions["initialization"]
    assert optimized.outputs[0].node_id == "screen"
    node_map = optimized.node_map()
    assert node_map["render"].frequency == "frame"
    assert any(item.port == "texture" for item in node_map["render"].inputs)


def test_export_generates_gpu_initialization_and_one_indirect_raster_draw(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path)
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    package = common.package
    init_ops = [item.opcode for item in package.initialization]
    frame_ops = [item.opcode for item in package.frame]
    assert init_ops.count("init.dispatch") == 5
    assert "init.upload_resource" not in init_ops
    assert frame_ops.count("frame.execute_indirect") == 1
    assert "frame.draw" not in frame_ops
    assert frame_ops.count("frame.dispatch") == 2
    assert frame_ops[-1] == "frame.present"
    assert all(
        item.opcode != "frame.copy_buffer" for item in package.frame
    )
    assert all(resource.usage != "readback" for resource in package.resources)


def test_export_resources_and_capabilities_are_exact(tmp_path: Path) -> None:
    result = _export(tmp_path)
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    texture = next(
        resource
        for resource in package.resources
        if resource.kind == "texture2d" and resource.width == 256
    )
    assert texture.size_bytes == 256 * 256 * 4
    assert texture.format == "DXGI_FORMAT_R8G8B8A8_UNORM"
    assert next(
        resource for resource in package.resources if resource.name == "vertices"
    ).size_bytes == 2048 * 32
    assert next(
        resource for resource in package.resources if resource.name == "indices"
    ).size_bytes == 12_288 * 4
    assert next(
        resource for resource in package.resources if resource.name == "instances"
    ).size_bytes == 256 * 80
    assert package.capability.profile_id == nwp.COMMON_PROFILE_ID
    assert package.capability.requirements_digest == nwp.COMMON_REQUIREMENTS_DIGEST
    assert set(package.capability.emitted_shader_targets) == {
        "cs_6_0",
        "vs_6_0",
        "ps_6_0",
    }
    assert package.capability.required_frame_instructions == tuple(
        sorted({item.opcode for item in package.frame})
    )
    assert package.capability.required_init_instructions == tuple(
        sorted({item.opcode for item in package.initialization})
    )
    assert not any(
        binding.usage == "sampler"
        for root in package.root_signatures
        for binding in root.bindings
    )
    assert sum(resource.size_bytes for resource in package.resources) == (
        procedural.estimate_resources()["resident_payload_bytes"]
    )


def test_export_identity_is_deterministic_and_manifest_links_used_features(
    tmp_path: Path,
) -> None:
    first = _export(tmp_path)
    second = _export(tmp_path)
    common_a = first.artifacts[nwp.COMMON_PROFILE_ID]
    common_b = second.artifacts[nwp.COMMON_PROFILE_ID]
    assert common_a.package_bytes == common_b.package_bytes
    assert common_a.manifest["manifest_sha256"] == common_b.manifest["manifest_sha256"]
    assert common_a.manifest["artifact_identity"] == common_b.manifest["artifact_identity"]
    assert "nw.render.instanced" in common_a.manifest["retained"]["code"]
    assert (
        "player.nw06.indexed-instanced-indirect"
        in common_a.manifest["retained"]["runtime_implementations"]
    )
    assert common_a.manifest["feature_requirements"]["mesh_shaders"] is False
    assert common_a.manifest["feature_requirements"]["ray_tracing"] is False
    assert common_a.manifest["feature_requirements"]["fp16_shader_ops"] is False
    assert common_a.manifest["excluded"]["gpu_name_predicates"] == []


def test_baseline_package_carries_parameters_not_baked_payloads(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path)
    package_bytes = result.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    # The package remains far smaller than the generated resident content
    # (texture + mesh + instances + render targets) and contains no readback.
    assert len(package_bytes) < 512 * 1024
    assert not any(
        instruction.opcode.startswith("frame.copy")
        for instruction in package.frame
    )
    seeds = [
        parameter
        for parameter in package.parameters
        if parameter.name.endswith(".texture_seed")
        or parameter.name.endswith(".seed")
    ]
    assert len(seeds) == 2
    assert all(parameter.value for parameter in seeds)


def test_failed_validation_preserves_previous_preview(tmp_path: Path) -> None:
    first = _export(tmp_path)
    common = first.artifacts[nwp.COMMON_PROFILE_ID]
    output = tmp_path / "artifact"
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.iterdir()
        if path.is_file()
    }
    invalid = tmp_path / "invalid.nwgraph"
    invalid.write_text(
        PROCEDURAL.read_text(encoding="utf-8")
        .replace("param minor_radius 0.3", "param minor_radius 1.5"),
        encoding="utf-8",
    )
    with pytest.raises(nw03.CompilerError):
        nw05_export.export_graph(
            invalid,
            output,
            settings=nw05_export.ExportSettings(
                byte_ceiling=16 * 1024 * 1024,
                memory_budget=512 * 1024 * 1024,
                player_template=str(tmp_path / "template.exe"),
            ),
            dxc_executable=str(_mock_dxc(tmp_path / "toolchain")),
            allow_test_payloads=True,
        )
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.iterdir()
        if path.is_file()
    }
    assert after == before


def test_frame_tick_is_explicit_uint_and_packs_at_nonzero_tick(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path)
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    tick_parameter = next(
        parameter
        for parameter in package.parameters
        if parameter.name == "nw06.params.14.tick"
    )
    assert tick_parameter.type == "uint"
    assert tick_parameter.component_count == 1
    assert tick_parameter.value == struct.pack("<I", 0)
    timeline_instruction = next(
        instruction
        for instruction in package.frame
        if instruction.opcode == "frame.set_timeline"
    )
    assert timeline_instruction.operands[0].value == tick_parameter.parameter_id
    packed = nw06_export.pack_frame_root_constants(package, 14, 24_000)
    assert len(packed) == 8 * 4
    assert struct.unpack_from("<I", packed, 0)[0] == 24_000
    # A numeric float conversion at this offset would not equal 24,000.
    assert struct.unpack_from("<f", packed, 0)[0] != 24_000.0


def test_camera_curve_quantization_is_bounded_recorded_and_recoverable(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path)
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    curve = common.manifest["timeline"]["camera_curve"]
    assert curve["interpolation"] == "linear"
    assert curve["within_tolerance"] is True
    assert (
        curve["maximum_absolute_error"]
        <= curve["declared_tolerance"]
    )
    assert curve["value_min"] == 0.0
    assert curve["value_max"] == pytest.approx(6.2831853)
    assert curve["recoverable"] is True
    camera_interpolation = next(
        parameter
        for parameter in common.package.parameters
        if parameter.name == "nw06.params.10.curve_interpolation"
    )
    assert camera_interpolation.type == "uint"
    assert struct.unpack("<I", camera_interpolation.value)[0] == 0


def test_schedule_models_render_targets_indirect_draw_and_lifetimes(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path)
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    logical = common.logical_schedule
    assert "frame.set_render_targets" in logical.frame_order
    assert "frame.execute_indirect" in logical.frame_order
    assert "frame.set_graphics_pipeline" in logical.frame_order
    assert any(
        access.opcode == "frame.execute_indirect"
        and access.state == "VERTEX_AND_CONSTANT_BUFFER"
        for access in logical.resource(3).access
    )
    assert any(
        access.opcode == "frame.execute_indirect"
        and access.state == "INDEX_BUFFER"
        for access in logical.resource(4).access
    )
    assert any(
        access.opcode == "frame.execute_indirect"
        and access.state == "ALL_SHADER_RESOURCE"
        for access in logical.resource(6).access
    )
    assert any(
        access.opcode == "frame.execute_indirect"
        and access.state == "INDIRECT_ARGUMENT"
        for access in logical.resource(7).access
    )
    assert all(
        access.access != "write"
        for resource_id in (6, 9, 11, 13, 17, 18)
        for access in logical.resource(resource_id).access
        if access.opcode == "frame.set_graphics_descriptor_table"
    )
    assert any(
        access.state == "RENDER_TARGET" for access in logical.resource(17).access
    )
    assert any(
        access.state == "DEPTH_WRITE" for access in logical.resource(15).access
    )
    assert logical.resource(17).alias_eligible
    assert logical.resource(18).alias_eligible
    assert logical.resource(19).alias_eligible
    assert logical.resource(17).lifetime.last_frame >= (
        logical.resource(17).lifetime.first_frame
    )


def test_step_camera_interpolation_is_encoded_not_silently_ignored(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "step.nwgraph"
    graph.write_text(
        PROCEDURAL.read_text(encoding="utf-8").replace(
            "param interpolation linear", "param interpolation step"
        ),
        encoding="utf-8",
    )
    result = _export_graph(tmp_path, graph, "step-artifact")
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    assert (
        common.manifest["timeline"]["camera_curve"]["interpolation"] == "step"
    )
    interpolation = next(
        parameter
        for parameter in common.package.parameters
        if parameter.name == "nw06.params.10.curve_interpolation"
    )
    assert struct.unpack("<I", interpolation.value)[0] == 1


def test_camera_curve_full_precision_escape_hatch(tmp_path: Path) -> None:
    graph = tmp_path / "full-precision.nwgraph"
    graph.write_text(
        PROCEDURAL.read_text(encoding="utf-8").replace(
            "  param curve_quantization_bits 16\n",
            "  param curve_quantization_bits 0\n",
        ),
        encoding="utf-8",
    )
    result = _export_graph(tmp_path, graph, "full-precision-artifact")
    curve = result.artifacts[nwp.COMMON_PROFILE_ID].manifest["timeline"][
        "camera_curve"
    ]
    assert curve["full_precision_escape_hatch"] is True
    assert curve["maximum_absolute_error"] == 0.0
    assert curve["recoverable"] is True


def test_static_generators_must_remain_initialization_only(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "frame-generation.nwgraph"
    graph.write_text(
        PROCEDURAL.read_text(encoding="utf-8").replace(
            "node texture_gpu nw.residency.generate_texture@1.0.0 "
            "frequency=initialization",
            "node texture_gpu nw.residency.generate_texture@1.0.0 "
            "frequency=frame",
        ),
        encoding="utf-8",
    )
    with pytest.raises(nw03.CompilerError, match="initialization-only"):
        _export_graph(tmp_path, graph, "frame-generation-artifact")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("param near 0.1", "param near 100"),
        ("param radius_min 3.2", "param radius_min 5.0"),
        ("param radius_max 4.8", "param radius_max 1.0"),
        ("param interpolation linear", "param interpolation nearest"),
        ("param curve_value_min 0", "param curve_value_min 7"),
        (
            "param curve_value_min 0\n  param curve_value_max 6.2831853",
            "param curve_value_min 5\n  param curve_value_max 1",
        ),
    ],
)
def test_invalid_camera_ranges_and_interpolation_fail_before_dispatch(
    tmp_path: Path, old: str, new: str
) -> None:
    graph = tmp_path / "invalid-camera.nwgraph"
    graph.write_text(
        PROCEDURAL.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )
    with pytest.raises(nw03.CompilerError):
        _export_graph(tmp_path, graph, "invalid-artifact")


def test_nw06_shader_contracts_preserve_bits_eye_and_tick_units() -> None:
    light = (SHADER_ROOT / "nw06_scene_data_cs.hlsl").read_text(
        encoding="utf-8"
    )
    assert "uint4(Param0, Param1, Param2, Param3)" in light
    assert "asuint(float4(Param0" not in light

    material = (SHADER_ROOT / "nw06_material_cs.hlsl").read_text(
        encoding="utf-8"
    )
    assert "float(TextureIndex)" not in material
    assert "TextureIndex))" in material

    vertex = (SHADER_ROOT / "nw06_scene_vs.hlsl").read_text(encoding="utf-8")
    assert "uint Tick;" in vertex
    assert "output.camera_position = eye;" in vertex
    assert "SampleCurve(uint tick, uint interpolation)" in vertex

    pixel = (SHADER_ROOT / "nw06_scene_ps.hlsl").read_text(encoding="utf-8")
    assert "float3 camera_position : TEXCOORD4;" in pixel
    assert "normalize(input.camera_position - input.world_position)" in pixel
    assert "float3(0.0, 0.0, 0.0) - input.world_position" not in pixel


def test_render_target_binding_clears_color_and_depth() -> None:
    player = (
        REPO_ROOT / "src" / "player" / "package_player.cpp"
    ).read_text(encoding="utf-8")
    assert "ClearRenderTargetView(rtv" in player
    assert "ClearDepthStencilView(" in player
    assert "D3D12_CLEAR_FLAG_DEPTH" in player


def test_package_player_uses_dedicated_rtv_and_dsv_heaps() -> None:
    source = (
        REPO_ROOT / "src" / "player" / "package_player.cpp"
    ).read_text(encoding="utf-8")
    assert "package_rtv_heap" in source
    assert "package_dsv_heap" in source
    assert source.count("D3D12_DESCRIPTOR_HEAP_TYPE_RTV") >= 2
    assert source.count("D3D12_DESCRIPTOR_HEAP_TYPE_DSV") >= 2
    count_body = source[
        source.index("std::size_t CountCbvSrvUavDescriptors") : source.index(
            "std::size_t CountSamplerDescriptors"
        )
    ]
    # Package RTV/DSV resources do not consume CBV/SRV/UAV descriptors;
    # rtv_srv consumes exactly one SRV descriptor.
    assert 'resource.usage == "rtv"' not in count_body
    assert 'resource.usage == "dsv"' not in count_body
    assert 'resource.usage == "rtv_srv"' in count_body


def test_descriptor_srv_flag_is_set_after_binding_resolution() -> None:
    source = (
        REPO_ROOT / "src" / "player" / "package_player.cpp"
    ).read_text(encoding="utf-8")
    resolution = source.index(
        'op.descriptor_is_srv =\n                binding != nullptr'
    )
    propagation = source.index(
        "active_descriptor_is_srv[op.root_parameter] ="
    )
    assert resolution < propagation


def test_resource_plan_is_binding_aware_for_init_and_indirect_draw() -> None:
    source = (
        REPO_ROOT / "src" / "player" / "resource_plan.cpp"
    ).read_text(encoding="utf-8")
    assert "binding_usage_for" in source
    assert 'instruction.opcode == "init.dispatch"' in source
    assert "it->second.initialized = true;" in source
    assert 'instruction.opcode == "frame.set_root_signature"' in source
    assert "bound_by_root.clear();" in source
    assert 'instruction.opcode == "frame.execute_indirect"' in source
    assert 'resource.name == "vertices"' in source
    assert 'resource.name == "indices"' in source
    assert source.count("root_bindings.clear();") >= 2


def _model_native_binding_accesses(package: nwp.NwpPackage) -> dict:
    """Reference trace of resource_plan.cpp's per-root binding replacement."""
    roots = {root.root_signature_id: root for root in package.root_signatures}
    by_id = {resource.resource_id: resource for resource in package.resources}

    def binding_usage(root_id: int, root_parameter: int) -> str:
        root = roots.get(root_id)
        if root is None:
            return ""
        for binding in root.bindings:
            if binding.root_parameter == root_parameter:
                return binding.usage
        return ""

    accesses: dict[int, list[tuple[int, str]]] = {
        resource.resource_id: [] for resource in package.resources
    }
    bound_by_root: dict[int, dict[int, str]] = {}
    active_root = 0
    for index, instruction in enumerate(package.frame):
        refs = [
            operand.value
            for operand in instruction.operands
            if operand.type == "resource_ref"
        ]
        if instruction.opcode in {
            "frame.set_root_signature",
            "frame.set_graphics_root_signature",
        }:
            root_id = instruction.operands[0].value
            if root_id != active_root:
                active_root = root_id
                bound_by_root.clear()
        elif instruction.opcode in {
            "frame.set_descriptor_table",
            "frame.set_root_constants",
            "frame.set_graphics_descriptor_table",
            "frame.set_graphics_root_constants",
        }:
            root_parameter = instruction.operands[0].value
            root_constants = instruction.opcode in {
                "frame.set_root_constants",
                "frame.set_graphics_root_constants",
            }
            binding = (
                "cbv"
                if root_constants
                else binding_usage(active_root, root_parameter)
            )
            # The native planner replaces, rather than merges, a root's
            # previous binding set when the root parameter is rebound.
            bound_by_root[root_parameter] = {
                resource_id: binding or by_id[resource_id].usage
                for resource_id in refs
            }
        elif instruction.opcode == "frame.dispatch":
            for root_bindings in bound_by_root.values():
                for resource_id, usage in root_bindings.items():
                    accesses[resource_id].append(
                        (index, "write" if usage == "uav" else "read")
                    )
        elif instruction.opcode == "frame.execute_indirect":
            for resource_id in refs:
                accesses[resource_id].append((index, "read"))
            for root_bindings in bound_by_root.values():
                for resource_id in root_bindings:
                    accesses[resource_id].append((index, "read"))
            for resource in package.resources:
                if resource.name in {"vertices", "indices"}:
                    accesses[resource.resource_id].append((index, "read"))
    return accesses


def test_native_resource_plan_rebinds_tonemap_root_without_writing_bloom(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path)
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    accesses = _model_native_binding_accesses(package)
    dispatch_indices = [
        index
        for index, instruction in enumerate(package.frame)
        if instruction.opcode == "frame.dispatch"
    ]
    bloom_index, tonemap_index = dispatch_indices
    assert ("write" in [
        access for index, access in accesses[18] if index == bloom_index
    ])
    # Root 3 is rebound from the bloom UAV (18) to the tonemap output (19).
    # Resource 18 must be read through root 2, never written by TonemapCS.
    tonemap_18 = [
        access for index, access in accesses[18] if index == tonemap_index
    ]
    tonemap_17 = [
        access for index, access in accesses[17] if index == tonemap_index
    ]
    tonemap_19 = [
        access for index, access in accesses[19] if index == tonemap_index
    ]
    assert tonemap_18 == ["read"]
    assert tonemap_17 == ["read"]
    assert tonemap_19 == ["write"]
    assert accesses[18][-1] == (tonemap_index, "read")
    assert accesses[19][-1] == (tonemap_index, "write")


def test_player_packs_uint_tick_root_constant_at_nonzero_tick() -> None:
    # Unconditional contract assertion against the actual C++ implementation.
    # The player lowers a `uint` timeline parameter with tick_kind 1 and writes
    # the absolute integer tick, not a numeric float conversion.
    player_source = (
        REPO_ROOT / "src" / "player" / "package_player.cpp"
    ).read_text(encoding="utf-8")
    assert '} else if (parameter.type == "uint") {' in player_source
    assert "*tick_kind = 1u;" in player_source
    assert "if (tick_kind == 1u) {" in player_source
    assert "static_cast<std::uint32_t>(tick)" in player_source

    candidates = []
    override = os.environ.get("NW04_FIXTURE")
    if override:
        candidates.append(Path(override))
    candidates.extend(
        sorted((REPO_ROOT / "build").glob("**/nw_package_player_fixture*"))
    )
    fixture = next(
        (
            candidate
            for candidate in candidates
            if candidate.is_file()
            and candidate.suffix.lower() in ("", ".exe")
            and "CMakeFiles" not in candidate.parts
        ),
        None,
    )
    if fixture is None:
        # The source-level contract above is always checked.  When CI has built
        # the native fixture, additionally execute the real PackagePlayer path.
        return
    package_path = (
        REPO_ROOT / "fixtures" / "nw04" / "compute_display.nwp"
    )
    completed = subprocess.run(
        [
            str(fixture),
            "--package",
            str(package_path),
            "--frames",
            "2",
            "--tick-start",
            "0",
            "--tick-step",
            "24000",
            "--planner-self-test",
            "--planner-mutation",
            "timeline-uint",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "NW_PLAYER_PLANNER_SELF_TEST=1" in completed.stdout
    # 24,000 decimal = 0x00005DC0, proving the player path wrote an integer
    # tick rather than float seconds (which would be 0x46BB8000).
    assert "NW_PLAYER_LAST_ROOT_CONSTANT0=0x00005DC0" in completed.stdout


def test_player_contract_mirror_uses_uint_tick_representation() -> None:
    package = nw04_fixture.build_compute_display_package()
    tick_parameter = replace(
        package.parameters[0],
        type="uint",
        component_count=1,
        value=struct.pack("<I", 0),
    )
    package = replace(
        package, parameters=[tick_parameter, *package.parameters[1:]]
    )
    last_constants, timeline = nw04_contract.apply_frame_constants(
        package, 24_000
    )
    assert timeline == 24_000
    assert last_constants is not None
    assert struct.unpack_from("<I", last_constants, 0)[0] == 24_000
