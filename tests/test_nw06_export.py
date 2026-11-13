from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw04_fixture  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw06_export  # noqa: E402
import nw06_procedural as procedural  # noqa: E402


PROCEDURAL = REPO_ROOT / "fixtures" / "nw06" / "procedural_scene.nwgraph"


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
    template = tmp_path / "template.exe"
    template.write_bytes(b"MZ" + b"\x00" * 256)
    settings = nw05_export.ExportSettings(
        byte_ceiling=16 * 1024 * 1024,
        memory_budget=512 * 1024 * 1024,
        player_template=str(template),
    )
    return nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "artifact",
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
