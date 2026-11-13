from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw03_oracle  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw05_export  # noqa: E402
import nw05_schedule as schedule  # noqa: E402


PROCEDURAL = REPO_ROOT / "fixtures" / "nw05" / "procedural_export.nwgraph"
ORIGINAL_PROCEDURAL = REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph"


def _make_dxc(directory: Path, *, fail: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dxc = directory / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        + (
            "sys.stderr.write('shader compilation injected failure\\n')\nsys.exit(1)\n"
            if fail
            else (
                "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
                "fo.parent.mkdir(parents=True, exist_ok=True)\n"
                f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\n"
            )
        ),
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    return dxc


@pytest.fixture(autouse=True)
def _test_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")


def _settings(tmp_path: Path, **overrides: object) -> nw05_export.ExportSettings:
    template = tmp_path / "template.exe"
    if not template.exists():
        template.write_bytes(b"MZ" + b"\x00" * 256)
    values = {
        "byte_ceiling": 1024 * 1024,
        "memory_budget": 256 * 1024 * 1024,
        "player_template": str(template),
    }
    values.update(overrides)
    return nw05_export.ExportSettings(**values)  # type: ignore[arg-type]


def _export(
    tmp_path: Path,
    *,
    name: str = "out",
    dxc: Path | None = None,
    settings: nw05_export.ExportSettings | None = None,
    prepare_staging=None,
) -> nw05_export.ExportResult:
    dxc = dxc or _make_dxc(tmp_path / "toolchain")
    return nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / name,
        settings=settings or _settings(tmp_path),
        report_path=tmp_path / f"{name}-report.json",
        dxc_executable=str(dxc),
        allow_test_payloads=True,
        prepare_staging=prepare_staging,
    )


def _hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def test_export_produces_embedded_executable_and_display_stream(tmp_path: Path) -> None:
    result = _export(tmp_path)
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    frame = [instruction.opcode for instruction in common.package.frame]
    assert "frame.dispatch" in frame
    assert "frame.present" in frame
    assert common.package.capability.profile_id == nwp.COMMON_PROFILE_ID
    assert common.package.capability.requirements_digest == nwp.COMMON_REQUIREMENTS_DIGEST
    assert common.executable_bytes is not None
    executable_path = tmp_path / "out" / common.executable_name
    embedded_package, embedded_manifest = nw05_export.extract_embedded_package(
        executable_path.read_bytes()
    )
    assert embedded_package == common.package_bytes
    assert (
        embedded_manifest
        == (tmp_path / "out" / f"{nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)}.manifest.json").read_bytes()
    )
    player_source = (
        tmp_path / "out" / f"{nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)}.player.cpp"
    ).read_text(encoding="utf-8")
    assert "PackagePlayer" in player_source
    assert "graph" in player_source.lower()
    assert player_source.index("nw::player::PlayerConfig config;") < (
        player_source.index("config.allow_software_adapter")
    )
    assert common.package_bytes not in player_source.encode("utf-8")


def test_original_procedural_image_graph_exports_texture_render_path(
    tmp_path: Path,
) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    result = nw05_export.export_graph(
        ORIGINAL_PROCEDURAL,
        tmp_path / "original",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    assert any(
        resource.usage == "uav" for resource in common.package.resources
    )
    assert any(
        instruction.opcode == "frame.present"
        for instruction in common.package.frame
    )
    assert any(
        binding.name == "input" and binding.usage == "srv"
        for binding in common.package.root_signatures[0].bindings
    )
    input_resource = next(
        resource
        for resource in common.package.resources
        if resource.name == "input"
    )
    assert input_resource.kind == "buffer"
    assert any(
        instruction.opcode == "init.upload_resource"
        and instruction.operands[0].type == "resource_ref"
        and instruction.operands[0].value == input_resource.resource_id
        for instruction in common.package.initialization
    )
    assert any(
        instruction.opcode == "frame.set_descriptor_table"
        and instruction.operands[0].value == 2
        and any(
            operand.type == "resource_ref"
            and operand.value == input_resource.resource_id
            for operand in instruction.operands
        )
        for instruction in common.package.frame
    )


def test_shader_execution_plan_maps_roots_by_blob_not_index() -> None:
    first_blob = nw04_fixture.make_dxil_container() + b"\x01"
    second_blob = nw04_fixture.make_dxil_container() + b"\x02"

    def record(shader_id: int, blob: bytes) -> nwp.ShaderRecord:
        return nwp.ShaderRecord(
            shader_id=shader_id,
            entry_point="CSMain",
            target="cs_6_0",
            blob=blob,
            root_signature_id=1,
            family="compute.math_noise",
        )

    z_record = record(1, first_blob)
    a_record = record(2, second_blob)
    rows = [
        {
            "specialization_id": "z_root",
            "blob_sha256": hashlib.sha256(first_blob).hexdigest(),
        },
        {
            "specialization_id": "a_root",
            "blob_sha256": hashlib.sha256(second_blob).hexdigest(),
        },
    ]
    _, execution = nw05_export._shader_execution_plan(
        rows, [z_record, a_record]
    )
    by_root = {item["root"]: item["shader_id"] for item in execution}
    assert by_root == {"a_root": 2, "z_root": 1}

    shared_blob = nw04_fixture.make_dxil_container()
    shared_rows = [
        {
            "specialization_id": "z_root",
            "shader_id": 1,
            "blob_sha256": hashlib.sha256(shared_blob).hexdigest(),
        },
        {
            "specialization_id": "a_root",
            "shader_id": 2,
            "blob_sha256": hashlib.sha256(shared_blob).hexdigest(),
        },
    ]
    _, shared_execution = nw05_export._shader_execution_plan(
        shared_rows,
        [record(1, shared_blob), record(2, shared_blob)],
    )
    by_root = {
        item["root"]: item["shader_id"] for item in shared_execution
    }
    assert by_root == {"a_root": 2, "z_root": 1}


def test_duplicate_transient_bindings_must_be_bound_one_at_a_time() -> None:
    package = nw04_fixture.build_compute_display_package()
    duplicate = nwp.replace(
        package.resources[1], resource_id=3, name="second_output"
    )
    simultaneous_frame = list(package.frame) + [
        nwp.Instruction(
            "frame.set_descriptor_table",
            (
                nwp.InstructionOperand("u32", 1),
                nwp.InstructionOperand("string", "cbv_srv_uav"),
                nwp.InstructionOperand("resource_ref", 2),
                nwp.InstructionOperand("resource_ref", 3),
            ),
        )
    ]
    simultaneous = nwp.replace(
        package,
        resources=list(package.resources) + [duplicate],
        frame=simultaneous_frame,
    )
    diagnostics = nwp.validate_package_model(simultaneous)
    assert any(
        item["code"] == "package_resource_binding_collision"
        for item in diagnostics
    )

    separate_frame = list(package.frame) + [
        nwp.Instruction(
            "frame.set_descriptor_table",
            (
                nwp.InstructionOperand("u32", 1),
                nwp.InstructionOperand("string", "cbv_srv_uav"),
                nwp.InstructionOperand("resource_ref", 2),
            ),
        ),
        nwp.Instruction(
            "frame.set_descriptor_table",
            (
                nwp.InstructionOperand("u32", 1),
                nwp.InstructionOperand("string", "cbv_srv_uav"),
                nwp.InstructionOperand("resource_ref", 3),
            ),
        ),
    ]
    separate = nwp.replace(
        package,
        resources=list(package.resources) + [duplicate],
        frame=separate_frame,
    )
    assert not any(
        item["code"] == "package_resource_binding_collision"
        for item in nwp.validate_package_model(separate)
    )


def test_export_is_byte_identical_in_two_clean_directories(tmp_path: Path) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    first = _export(tmp_path, name="clean-a", dxc=dxc)
    second = _export(tmp_path, name="clean-b", dxc=dxc)
    assert _hashes(first_path := tmp_path / "clean-a") == _hashes(tmp_path / "clean-b")
    common = first.artifacts[nwp.COMMON_PROFILE_ID]
    other = second.artifacts[nwp.COMMON_PROFILE_ID]
    assert common.package_bytes == other.package_bytes
    assert common.manifest == other.manifest
    assert common.executable_bytes == other.executable_bytes
    assert common.artifact_key == other.artifact_key


def test_artifact_identity_depends_on_profile_digest_and_emitted_requirements() -> None:
    package = nw04_fixture.build_compute_display_package()
    requirements = nw05_export.emitted_requirements(package)
    first = nw05_export.artifact_key(
        nwp.COMMON_PROFILE_ID, nwp.COMMON_REQUIREMENTS_DIGEST, requirements
    )
    second = nw05_export.artifact_key(
        nwp.COMMON_PROFILE_ID, nwp.COMMON_REQUIREMENTS_DIGEST, requirements
    )
    assert first == second
    changed = dict(requirements)
    changed["limit_max_compute_threads_per_group"] = 1024
    assert (
        nw05_export.artifact_key(
            nwp.COMMON_PROFILE_ID, nwp.COMMON_REQUIREMENTS_DIGEST, changed
        )
        != first
    )


def test_unknown_profile_fails_with_dependency_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path, target_profile="nw.unknown.profile.v1")
    with pytest.raises(nw05_export.ExportError) as excinfo:
        _export(tmp_path, settings=settings)
    assert excinfo.value.dependency_path
    assert excinfo.value.dependency_path[0] == "capability_profile"


def test_optional_profile_is_a_separate_budgeted_artifact(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        optional_profiles=(nwp.OPTIONAL_PROFILE_WAVE_OPS,),
        allow_optional_profiles=True,
    )
    result = _export(tmp_path, settings=settings)
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    optional = result.artifacts[nwp.OPTIONAL_PROFILE_WAVE_OPS]
    assert common.package.capability.optional_profile_id == ""
    assert optional.package.capability.profile_id == nwp.OPTIONAL_PROFILE_WAVE_OPS
    assert optional.package.capability.requirements_digest == (
        nwp.OPTIONAL_PROFILE_WAVE_OPS_DIGEST
    )
    assert optional.package.capability.optional_profile_id == nwp.OPTIONAL_PROFILE_WAVE_OPS
    assert optional.package.capability.optional_requirements_digest == (
        nwp.OPTIONAL_PROFILE_WAVE_OPS_DIGEST
    )
    assert common.artifact_key != optional.artifact_key
    assert optional.manifest["profile_id"] == nwp.OPTIONAL_PROFILE_WAVE_OPS
    assert optional.manifest["requirements_digest"] == nwp.OPTIONAL_PROFILE_WAVE_OPS_DIGEST
    assert optional.manifest["feature_requirements"]["wave_ops"] is True
    assert common.manifest["feature_requirements"]["wave_ops"] is False
    common_source = (
        tmp_path / "out" / f"{nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)}.player.cpp"
    ).read_text(encoding="utf-8")
    optional_source = (
        tmp_path / "out" / f"{nw05_export._profile_file_stem(nwp.OPTIONAL_PROFILE_WAVE_OPS)}.player.cpp"
    ).read_text(encoding="utf-8")
    # The generated link closure, not an unchanged generic template, determines
    # whether optional feature queries are compiled into each artifact.
    assert "config.query_optional_wave_ops = false;" in common_source
    assert "config.query_optional_wave_ops = true;" in optional_source
    assert "NW05_LINK_MANIFEST_VERIFIED=1" in common_source
    assert result.report["budgets"]["optional_independent"] is True
    assert result.report["budgets"]["common_baseline_raised_by_optional"] is False
    for directory in (tmp_path / "out",):
        assert (
            directory / f"{nw05_export._profile_file_stem(nwp.OPTIONAL_PROFILE_WAVE_OPS)}.nwp"
        ).is_file()


def test_optional_export_does_not_change_common_artifact_or_closure(
    tmp_path: Path,
) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    common_only = _export(tmp_path, name="common-only", dxc=dxc)
    with_optional = _export(
        tmp_path,
        name="with-optional",
        dxc=dxc,
        settings=_settings(
            tmp_path,
            optional_profiles=(nwp.OPTIONAL_PROFILE_WAVE_OPS,),
            allow_optional_profiles=True,
        ),
    )
    common_a = common_only.artifacts[nwp.COMMON_PROFILE_ID]
    common_b = with_optional.artifacts[nwp.COMMON_PROFILE_ID]
    assert common_a.package_bytes == common_b.package_bytes
    assert common_a.manifest == common_b.manifest
    assert common_a.executable_bytes == common_b.executable_bytes
    assert common_a.size_report == common_b.size_report
    assert common_a.emitted_requirements == common_b.emitted_requirements
    assert common_a.artifact_key == common_b.artifact_key


def test_byte_budget_failure_preserves_prior_artifact_and_writes_diagnostic(
    tmp_path: Path,
) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    first = _export(tmp_path, dxc=dxc)
    before = _hashes(tmp_path / "out")
    with pytest.raises(nw05_export.ExportError):
        _export(
            tmp_path,
            dxc=dxc,
            settings=_settings(tmp_path, byte_ceiling=1),
        )
    assert _hashes(tmp_path / "out") == before
    report = json.loads((tmp_path / "out-report.json").read_text(encoding="utf-8"))
    assert report["success"] is False
    assert report["preserved_previous_artifact"] is True
    assert any(
        item["code"] == "export_byte_ceiling_exceeded"
        for item in report["diagnostics"]
    )


def test_memory_budget_failure_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(nw05_export.ExportError):
        _export(
            tmp_path,
            settings=_settings(tmp_path, memory_budget=1),
        )
    assert not (tmp_path / "out").exists()


def test_shader_compilation_failure_preserves_prior_artifact(tmp_path: Path) -> None:
    good = _make_dxc(tmp_path / "good")
    _export(tmp_path, dxc=good)
    before = _hashes(tmp_path / "out")
    failing = _make_dxc(tmp_path / "failing", fail=True)
    with pytest.raises(Exception):
        _export(tmp_path, dxc=failing)
    assert _hashes(tmp_path / "out") == before


def test_linking_publication_failure_preserves_prior_artifact(tmp_path: Path) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    _export(tmp_path, dxc=dxc)
    before = _hashes(tmp_path / "out")

    def fail_linking(staging: Path) -> None:
        raise RuntimeError("injected linking failure")

    with pytest.raises(RuntimeError):
        _export(tmp_path, dxc=dxc, prepare_staging=fail_linking)
    assert _hashes(tmp_path / "out") == before


def test_quantization_tolerance_failure_fails_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path, quantization_tolerance=0.0)
    # The procedural fixture has no quantized fields, so force a field policy
    # through the optimizer-facing settings path instead.
    with pytest.raises(nw05_export.ExportError):
        nw05_export.export_graph(
            REPO_ROOT / "fixtures" / "nw05" / "optimization_semantics.nwgraph",
            tmp_path / "quantized",
            settings=settings,
            dxc_executable=str(_make_dxc(tmp_path / "toolchain")),
            allow_test_payloads=True,
        )


def test_optimization_failure_preserves_prior_artifact(tmp_path: Path) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    _export(tmp_path, dxc=dxc)
    before = _hashes(tmp_path / "out")
    with pytest.raises(nw05_export.ExportError):
        nw05_export.export_graph(
            REPO_ROOT / "fixtures" / "nw05" / "optimization_semantics.nwgraph",
            tmp_path / "out",
            settings=_settings(
                tmp_path,
                quantization_tolerance=0.0,
            ),
            report_path=tmp_path / "optimization-failure-report.json",
            dxc_executable=str(dxc),
            allow_test_payloads=True,
        )
    assert _hashes(tmp_path / "out") == before
    report = json.loads(
        (tmp_path / "optimization-failure-report.json").read_text(encoding="utf-8")
    )
    assert report["success"] is False
    assert report["preserved_previous_artifact"] is True


def test_inspect_size_attributes_all_sections_and_dependencies(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path)
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    size = nw05_export.inspect_size(
        tmp_path / "out" / f"{nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)}.nwp"
    )
    assert set(size["sections"]) == {"CAP ", "RES ", "INIT", "FRAM", "PARM", "SHDR", "RSIG"}
    assert size["structure"]["all_bytes_attributed"] is True
    assert size["structure"]["attributed_bytes"] == len(common.package_bytes)
    assert size["content"]["shader_count"] >= 1
    assert any(item["included"] for item in size["dependencies"])
    assert any(
        item["name"] == "runtime-dxc" and not item["included"]
        for item in size["dependencies"]
    )
    directory = nw05_export.inspect_size(tmp_path / "out")
    assert directory["artifact_count"] >= 1
    assert directory["total_package_bytes"] == len(common.package_bytes)


def test_cli_export_and_inspect_size(tmp_path: Path) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    template = tmp_path / "template.exe"
    template.write_bytes(b"MZ" + b"\x00" * 128)
    export_dir = tmp_path / "cli-out"
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "nodewerkc.py"),
        "export",
        str(PROCEDURAL),
        str(export_dir),
        "--dxc",
        str(dxc),
        "--allow-test-payloads",
        "--player-template",
        str(template),
        "--report",
        str(tmp_path / "cli-report.json"),
    ]
    environment = dict(os.environ)
    environment["NODEWERK_NW03_TEST_DIGEST"] = "1"
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["common_artifact_key"].startswith("sha256:v1:")
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "nodewerkc.py"),
            "inspect-size",
            str(export_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    size = json.loads(completed.stdout)
    assert size["artifact_count"] >= 1
    assert size["total_package_bytes"] > 0


def test_generation_and_bake_overrides_are_explicit(tmp_path: Path) -> None:
    settings = _settings(tmp_path, bake_overrides=(("image", "bake"),))
    with pytest.raises(nw05_export.ExportError) as excinfo:
        nw05_export.export_graph(
            PROCEDURAL,
            tmp_path / "out",
            settings=settings,
            dxc_executable=str(_make_dxc(tmp_path / "toolchain")),
            allow_test_payloads=True,
        )
    assert excinfo.value.dependency_path
    assert excinfo.value.dependency_path[0] == "generation_override"

def test_unmeasured_fusion_uses_real_multi_dispatch_unfused_export(
    tmp_path: Path,
) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    default = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "default",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    disabled = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "disabled",
        settings=_settings(tmp_path, fusion_enabled=False),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    common = default.artifacts[nwp.COMMON_PROFILE_ID]
    dispatches = [
        instruction
        for instruction in common.package.frame
        if instruction.opcode == "frame.dispatch"
    ]
    # Two three-node terminal DAGs: every dependency is an actual pipeline and
    # dispatch, not recursively inlined by the old NW-03 root lowering.
    assert len(common.package.shaders) == 6
    assert len(dispatches) == 6
    manifest_shaders = common.manifest.get("shaders", [])
    assert len(manifest_shaders) == 6
    assert all(item["unfused"] for item in manifest_shaders)
    assert sorted(
        (item["execution_root"], item["execution_order"], item["output_region"])
        for item in manifest_shaders
    ) == [
        ("aux", 0, 1),
        ("aux", 1, 2),
        ("aux", 2, 0),
        ("image", 0, 1),
        ("image", 1, 2),
        ("image", 2, 0),
    ]
    uavs = [resource for resource in common.package.resources if resource.usage == "uav"]
    assert len(uavs) == 2
    assert all(resource.size_bytes == 3 * 256 * 256 * 16 for resource in uavs)
    assert (
        common.package_bytes
        == disabled.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    )
    assert all(
        not record["accepted"]
        for record in default.report["optimized_ir"]["fusions"]
    )


def test_physical_realization_evidence_does_not_change_published_bytes(
    tmp_path: Path,
) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    simulated = _export(tmp_path, name="simulated", dxc=dxc)
    queried = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "queried",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
        allocation_query={
            1: {"size_bytes": 65536, "alignment": 65536},
            2: {"size_bytes": 4194304, "alignment": 65536},
            3: {"size_bytes": 4194304, "alignment": 65536},
        },
        adapter_identity="query:test-adapter",
    )
    left = simulated.artifacts[nwp.COMMON_PROFILE_ID]
    right = queried.artifacts[nwp.COMMON_PROFILE_ID]
    assert left.package_bytes == right.package_bytes
    assert left.executable_bytes == right.executable_bytes
    assert left.artifact_key == right.artifact_key


def test_physical_budget_uses_graph_render_dimensions(tmp_path: Path) -> None:
    result = _export(tmp_path)
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    logical = schedule.build_logical_schedule(
        artifact.package, frames_in_flight=2
    )
    expected = schedule.realize_physical_aliases(
        logical,
        artifact.package,
        frames_in_flight=2,
        allow_simulated_allocation=True,
        player_width=256,
        player_height=256,
    )
    assert (
        artifact.physical_realization.peak_bytes == expected.peak_bytes
    )


def test_quantization_changes_published_package_bytes(tmp_path: Path) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    header = (
        "nwgraph 1\n"
        "profile nw.common.d3d12.fl12_0.sm6_0.v1 "
        f"{nwp.COMMON_REQUIREMENTS_DIGEST}\n"
        "setting optimization O2\n"
        "setting deterministic true\n"
        "setting api d3d12\n"
        "setting feature_level 12_0\n"
        "setting shader_model 6_0\n"
        "setting frequency_policy strict\n"
        "setting shader_blob_policy stable\n"
    )
    quantized_graph = tmp_path / "quantized.nwgraph"
    quantized_graph.write_text(
        header
        + "setting quantization_policy bits\n"
        + "setting quantization_bits 8\n"
        + "setting quantization_tolerance 0.01\n"
        + "setting quantization_fields tone.value\n"
        + "node tone nw.shader.vec4_const@1.0.0 frequency=frame\n"
        + "  param value 0.123456789 0.2 0.3 1.0\n",
        encoding="utf-8",
    )
    plain_graph = tmp_path / "plain.nwgraph"
    plain_graph.write_text(
        header
        + "setting quantization_policy none\n"
        + "node tone nw.shader.vec4_const@1.0.0 frequency=frame\n"
        + "  param value 0.123456789 0.2 0.3 1.0\n",
        encoding="utf-8",
    )
    quantized = nw05_export.export_graph(
        quantized_graph,
        tmp_path / "quantized",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    plain = nw05_export.export_graph(
        plain_graph,
        tmp_path / "plain",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    assert (
        quantized.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
        != plain.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    )
    assert (
        quantized.artifacts[nwp.COMMON_PROFILE_ID].artifact_key
        != plain.artifacts[nwp.COMMON_PROFILE_ID].artifact_key
    )

def test_unavailable_image_oracle_does_not_abort_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(*_args, **_kwargs):
        raise nw03_oracle.ExpressionError("unsupported CPU oracle operation")

    monkeypatch.setattr(nw05_export.oracle, "evaluate", unsupported)
    result = _export(tmp_path)
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    assert common.manifest["image_oracle_status"] == "unavailable"
    assert common.manifest["image_oracle_sha256"] == ""


def test_graph_is_the_sole_source_of_published_procedural_content(
    tmp_path: Path,
) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    baseline = _export(tmp_path, name="baseline", dxc=dxc)
    changed_source = PROCEDURAL.read_text(encoding="utf-8").replace(
        "sin(uv.x * 31.0 + time)", "sin(uv.x * 37.0 + time)"
    )
    changed_graph = tmp_path / "changed.nwgraph"
    changed_graph.write_text(changed_source, encoding="utf-8")
    changed = nw05_export.export_graph(
        changed_graph,
        tmp_path / "changed",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    baseline_artifact = baseline.artifacts[nwp.COMMON_PROFILE_ID]
    changed_artifact = changed.artifacts[nwp.COMMON_PROFILE_ID]
    assert baseline_artifact.package_bytes != changed_artifact.package_bytes
    assert (
        baseline_artifact.package.shaders[0].blob
        == changed_artifact.package.shaders[0].blob
    )
    # The mock DXC returns a stable container; the compiler fingerprint proves
    # the graph-derived HLSL, not fixture content, determined the artifact.
    assert (
        baseline_artifact.package.capability.compiler_fingerprint
        != changed_artifact.package.capability.compiler_fingerprint
    )


def test_unreachable_graph_additions_do_not_change_published_bytes(
    tmp_path: Path,
) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    baseline = _export(tmp_path, name="baseline", dxc=dxc)
    source = PROCEDURAL.read_text(encoding="utf-8")
    extended_source = source + (
        "\nnode unused_extra nw.math.scalar_const@1.0.0 frequency=frame\n"
        "  param value 99\n"
    )
    extended_graph = tmp_path / "extended.nwgraph"
    extended_graph.write_text(extended_source, encoding="utf-8")
    extended = nw05_export.export_graph(
        extended_graph,
        tmp_path / "extended",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    assert (
        baseline.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
        == extended.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    )
    assert (
        baseline.artifacts[nwp.COMMON_PROFILE_ID].executable_bytes
        == extended.artifacts[nwp.COMMON_PROFILE_ID].executable_bytes
    )
    manifest = baseline.artifacts[nwp.COMMON_PROFILE_ID].manifest
    assert "semantic_source_sha256" not in manifest
    assert all("cache_hit" not in shader for shader in manifest["shaders"])


def test_cold_and_warm_shader_cache_produce_identical_executables(
    tmp_path: Path,
) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    cache = tmp_path / "cache"
    cold = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "cold",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        cache_dir=cache,
        allow_test_payloads=True,
    )
    warm = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "warm",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        cache_dir=cache,
        allow_test_payloads=True,
    )
    assert (
        cold.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
        == warm.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    )
    assert (
        cold.artifacts[nwp.COMMON_PROFILE_ID].executable_bytes
        == warm.artifacts[nwp.COMMON_PROFILE_ID].executable_bytes
    )
    assert (
        cold.artifacts[nwp.COMMON_PROFILE_ID].manifest
        == warm.artifacts[nwp.COMMON_PROFILE_ID].manifest
    )


def test_inspect_size_attributes_every_distributed_file(tmp_path: Path) -> None:
    result = _export(tmp_path)
    directory = tmp_path / "out"
    expected = sum(
        path.stat().st_size for path in directory.iterdir() if path.is_file()
    )
    size = nw05_export.inspect_size(directory)
    assert size["total_distributed_bytes"] == expected
    assert result.report["budgets"]["common_distributed_bytes"] == expected
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    common_size = nw05_export.inspect_size(
        directory
        / f"{nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)}.nwp"
    )
    files = common_size["distribution"]["artifact_files"]
    for suffix in (
        ".schedule.json",
        ".aliases.json",
        ".size.json",
        ".player.cpp",
    ):
        assert any(name.endswith(suffix) for name in files)
    assert files["link-manifest.json"] > 0
    assert files["size-report.json"] > 0


def test_inspect_size_reads_embedded_manifest_without_sibling(
    tmp_path: Path,
) -> None:
    result = _export(tmp_path)
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    executable = tmp_path / "out" / common.executable_name
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    isolated_executable = isolated / executable.name
    isolated_executable.write_bytes(executable.read_bytes())
    size = nw05_export.inspect_size(isolated_executable)
    assert size["distribution"]["manifest_bytes"] > 0
    assert size["distribution"]["executable_bytes"] > 0
    assert (
        size["distribution"]["total_distributed_bytes"]
        == isolated_executable.stat().st_size
    )
    normal_size = nw05_export.inspect_size(executable)
    directory_total = sum(
        path.stat().st_size
        for path in (tmp_path / "out").iterdir()
        if path.is_file()
    )
    assert normal_size["distribution"]["total_distributed_bytes"] == directory_total


def test_compose_display_uses_explicit_compiler_output_target(
    tmp_path: Path,
) -> None:
    package = nw04_fixture.build_compute_display_package()
    extra = nwp.replace(
        package.resources[1],
        resource_id=3,
        name="second_output",
    )
    duplicate = package.__class__(
        capability=package.capability,
        resources=list(package.resources) + [extra],
        initialization=list(package.initialization),
        frame=list(package.frame),
        parameters=list(package.parameters),
        shaders=list(package.shaders),
        root_signatures=list(package.root_signatures),
        flags=package.flags,
    )
    composed = nw05_export._compose_display_stream(duplicate)
    present = [
        instruction
        for instruction in composed.frame
        if instruction.opcode == "frame.present"
    ]
    assert present
    assert present[-1].operands[0].value == 2


def test_compose_display_rejects_multiple_unidentified_uav_outputs() -> None:
    package = nw04_fixture.build_compute_display_package()
    resources = [
        nwp.replace(resource, name=f"uav_{index}")
        for index, resource in enumerate(package.resources)
    ]
    duplicate_resources = resources + [
        nwp.replace(resources[1], resource_id=3, name="also_output")
    ]
    duplicate = package.__class__(
        capability=package.capability,
        resources=duplicate_resources,
        initialization=list(package.initialization),
        frame=[
            instruction
            for instruction in package.frame
            if instruction.opcode != "frame.present"
        ],
        parameters=list(package.parameters),
        shaders=list(package.shaders),
        root_signatures=list(package.root_signatures),
        flags=package.flags,
    )
    with pytest.raises(ValueError, match="multiple UAV buffer outputs"):
        nw05_export._compose_display_stream(duplicate)


def test_compose_display_honours_existing_present_target_identity() -> None:
    package = nw04_fixture.build_compute_display_package()
    resources = [
        nwp.replace(resource, name=f"uav_{index}")
        for index, resource in enumerate(package.resources)
    ]
    resources.append(nwp.replace(resources[1], resource_id=3, name="chosen"))
    frame = [
        instruction
        for instruction in package.frame
        if instruction.opcode != "frame.present"
    ] + [
        nwp.Instruction(
            "frame.present",
            (nwp.InstructionOperand("resource_ref", 3),),
        ),
    ]
    chosen = package.__class__(
        capability=package.capability,
        resources=resources,
        initialization=list(package.initialization),
        frame=frame,
        parameters=list(package.parameters),
        shaders=list(package.shaders),
        root_signatures=list(package.root_signatures),
        flags=package.flags,
    )
    composed = nw05_export._compose_display_stream(chosen)
    presents = [
        instruction
        for instruction in composed.frame
        if instruction.opcode == "frame.present"
    ]
    assert presents[-1].operands[0].value == 3


def test_compose_display_rewrites_non_uav_present_target() -> None:
    package = nw04_fixture.build_compute_display_package()
    frame = [
        instruction
        for instruction in package.frame
        if instruction.opcode != "frame.present"
    ] + [
        nwp.Instruction(
            "frame.present",
            (nwp.InstructionOperand("resource_ref", 1),),
        ),
    ]
    composed_package = package.__class__(
        capability=package.capability,
        resources=list(package.resources),
        initialization=list(package.initialization),
        frame=frame,
        parameters=list(package.parameters),
        shaders=list(package.shaders),
        root_signatures=list(package.root_signatures),
        flags=package.flags,
    )
    composed = nw05_export._compose_display_stream(composed_package)
    presents = [
        instruction
        for instruction in composed.frame
        if instruction.opcode == "frame.present"
    ]
    assert presents
    assert presents[-1].operands[0].value == 2


def test_failed_export_never_overwrites_report_inside_prior_artifact(
    tmp_path: Path,
) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    report_path = tmp_path / "out" / "report.json"
    _export(tmp_path, dxc=dxc)
    report_path.write_text('{"prior": true}\n', encoding="utf-8")
    before = report_path.read_bytes()
    with pytest.raises(nw05_export.ExportError):
        nw05_export.export_graph(
            PROCEDURAL,
            tmp_path / "out",
            settings=_settings(tmp_path, byte_ceiling=1),
            report_path=report_path,
            dxc_executable=str(dxc),
            allow_test_payloads=True,
        )
    assert report_path.read_bytes() == before
    diagnostic = json.loads(
        (tmp_path / "out-failed-report.json").read_text(encoding="utf-8")
    )
    assert diagnostic["success"] is False


def test_in_directory_report_is_staged_and_accounted(tmp_path: Path) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    output = tmp_path / "out"
    result = nw05_export.export_graph(
        PROCEDURAL,
        output,
        settings=_settings(tmp_path),
        report_path=output / "report.json",
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    assert (output / "report.json").is_file()
    actual = sum(path.stat().st_size for path in output.iterdir() if path.is_file())
    assert result.report["budgets"]["common_distributed_bytes"] == actual
    size = nw05_export.inspect_size(output)
    assert size["total_distributed_bytes"] == actual
    # The report is part of the common artifact accounting, not free metadata.
    common = result.artifacts[nwp.COMMON_PROFILE_ID]
    assert "report.json" in common.size_report["distribution"]["artifact_files"]


def test_graph_output_dimensions_control_exported_output(tmp_path: Path) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    source = PROCEDURAL.read_text(encoding="utf-8").replace(
        "setting nw05_output_width 256", "setting nw05_output_width 128"
    ).replace(
        "setting nw05_output_height 256", "setting nw05_output_height 64"
    )
    graph = tmp_path / "dims.nwgraph"
    graph.write_text(source, encoding="utf-8")
    result = nw05_export.export_graph(
        graph,
        tmp_path / "dims-out",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    package = artifact.package
    presents = [
        instruction
        for instruction in package.frame
        if instruction.opcode == "frame.present"
    ]
    assert presents
    present_id = presents[-1].operands[0].value
    output = next(
        resource
        for resource in package.resources
        if resource.resource_id == present_id and resource.usage == "uav"
    )
    image_shaders = [
        item
        for item in artifact.manifest.get("shaders", [])
        if item.get("execution_root") == "image"
    ]
    assert image_shaders
    assert {int(item["region_count"]) for item in image_shaders} == {
        len(image_shaders)
    }
    assert {int(item["output_region"]) for item in image_shaders} == set(
        range(len(image_shaders))
    )
    # Region zero is the logical image; the remaining regions are the real
    # unfused intermediate values executed by the selective lowering path.
    assert output.size_bytes == 128 * 64 * 16 * len(image_shaders)
    present_parameters = {
        parameter.name: parameter.value
        for parameter in package.parameters
        if parameter.name.startswith("nw05.present.")
    }
    assert present_parameters["nw05.present.width"] == struct.pack("<I", 128)
    assert present_parameters["nw05.present.height"] == struct.pack("<I", 64)
    dispatch = next(
        instruction
        for instruction in package.frame
        if instruction.opcode == "frame.dispatch"
    )
    assert dispatch.operands[0].value == 1
    assert dispatch.operands[1].value == 64


def test_unsupported_rich_graph_semantics_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(nw05_export.ExportError) as excinfo:
        nw05_export.export_graph(
            REPO_ROOT / "fixtures" / "nw05" / "optimization_semantics.nwgraph",
            tmp_path / "unsupported-rich-graph",
            settings=_settings(tmp_path),
            dxc_executable=str(_make_dxc(tmp_path / "toolchain")),
            allow_test_payloads=True,
        )
    assert excinfo.value.dependency_path[:1] == ["graph"]


def test_shader_only_graph_exports_with_dispatch(tmp_path: Path) -> None:
    graph = tmp_path / "shader_only.nwgraph"
    graph.write_text(
        "nwgraph 1\n"
        "profile nw.common.d3d12.fl12_0.sm6_0.v1 "
        f"{nwp.COMMON_REQUIREMENTS_DIGEST}\n"
        "setting optimization O2\n"
        "setting deterministic true\n"
        "setting api d3d12\n"
        "setting feature_level 12_0\n"
        "setting shader_model 6_0\n"
        "setting frequency_policy strict\n"
        "setting shader_blob_policy stable\n"
        "node image nw.shader.expr@1.0.0 frequency=frame\n"
        '  param expression "vec4(uv.x, uv.y, 0.25, 1.0)"\n',
        encoding="utf-8",
    )
    result = nw05_export.export_graph(
        graph,
        tmp_path / "shader-only-out",
        settings=_settings(tmp_path),
        dxc_executable=str(_make_dxc(tmp_path / "toolchain")),
        allow_test_payloads=True,
    )
    package = result.artifacts[nwp.COMMON_PROFILE_ID].package
    assert any(
        instruction.opcode == "frame.dispatch" for instruction in package.frame
    )
    assert any(
        instruction.opcode == "frame.present" for instruction in package.frame
    )


def test_multiple_shader_roots_get_independent_aliasable_outputs(
    tmp_path: Path,
) -> None:
    result = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "multi-root",
        settings=_settings(tmp_path),
        dxc_executable=str(_make_dxc(tmp_path / "toolchain")),
        allow_test_payloads=True,
    )
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    outputs = [
        resource
        for resource in artifact.package.resources
        if resource.usage == "uav"
    ]
    assert len(outputs) >= 2
    assert len({resource.resource_id for resource in outputs}) == len(outputs)
    dispatches = [
        instruction
        for instruction in artifact.package.frame
        if instruction.opcode == "frame.dispatch"
    ]
    assert len(dispatches) >= 2
    transitions = {
        (barrier.before_resource_id, barrier.resource_id)
        for barrier in artifact.physical_realization.barriers
    }
    assert transitions
    assert any(
        (right, left) in transitions
        for left, right in transitions
        if left != right
    )


def test_relative_dxc_path_survives_working_directory_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_dxc(tmp_path)
    result = nw05_export.export_graph(
        PROCEDURAL,
        tmp_path / "relative-out",
        settings=_settings(tmp_path),
        dxc_executable="mock_dxc.py",
        allow_test_payloads=True,
    )
    assert result.artifacts[nwp.COMMON_PROFILE_ID].package_bytes

