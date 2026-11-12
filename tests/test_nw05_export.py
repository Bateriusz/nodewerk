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
import nwp_package as nwp  # noqa: E402
import nw05_export  # noqa: E402


PROCEDURAL = REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph"


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
        "memory_budget": 1024 * 1024,
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
    assert common.package_bytes not in player_source.encode("utf-8")


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
    settings = _settings(
        tmp_path,
        bake_overrides=(("image", "bake"),),
    )
    result = _export(tmp_path, settings=settings)
    decisions = {
        item["node_id"]: item
        for item in result.report["generation_vs_bake"]["decisions"]
    }
    assert decisions["image"]["mode"] == "bake"
    assert decisions["image"]["automatic_cost_oracle"] is False
    assert decisions["image"]["incremental_compressed_content_bytes"] > 0
    assert decisions["image"]["baked_content_emitted_bytes"] > 0
    assert decisions["texture_recipe"]["mode"] == "generate"
    assert result.report["generation_vs_bake"]["automatic_oracle"] is False


def test_optimizer_and_fusion_settings_change_published_bytes(tmp_path: Path) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    source = PROCEDURAL.read_text(encoding="utf-8")
    foldable = source.replace(
        "vec4(mix(uv.x, sin(uv.y * 17.0 + time), 0.35), "
        "mix(uv.y, cos(uv.x * 13.0 - time), 0.25), "
        "sin(uv.x * 31.0 + time) * 0.5 + 0.5, 1.0)",
        "vec4(mix(uv.x, sin(uv.y * 17.0 + time), 0.35), "
        "mix(uv.y, cos(uv.x * 13.0 - time), 0.25), "
        "sin(uv.x * 31.0 + time) * 0.5 + (0.25 + 0.25), 1.0)",
    )
    graph = tmp_path / "foldable.nwgraph"
    graph.write_text(foldable, encoding="utf-8")
    fused = nw05_export.export_graph(
        graph,
        tmp_path / "fused",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    unfused = nw05_export.export_graph(
        graph,
        tmp_path / "unfused",
        settings=_settings(tmp_path, fusion_enabled=False),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    assert (
        fused.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
        != unfused.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    )
    assert (
        fused.artifacts[nwp.COMMON_PROFILE_ID].package.capability.compiler_fingerprint
        != unfused.artifacts[nwp.COMMON_PROFILE_ID].package.capability.compiler_fingerprint
    )


def test_quantization_changes_published_package_bytes(tmp_path: Path) -> None:
    dxc = _make_dxc(tmp_path / "toolchain")
    semantics = (
        REPO_ROOT / "fixtures" / "nw05" / "optimization_semantics.nwgraph"
    )
    quantized = nw05_export.export_graph(
        semantics,
        tmp_path / "quantized",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    text = semantics.read_text(encoding="utf-8").replace(
        "setting quantization_policy bits",
        "setting quantization_policy none",
    )
    unquantized_graph = tmp_path / "unquantized.nwgraph"
    unquantized_graph.write_text(text, encoding="utf-8")
    plain = nw05_export.export_graph(
        unquantized_graph,
        tmp_path / "plain",
        settings=_settings(tmp_path),
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    assert (
        quantized.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
        != plain.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    )


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


def test_inspect_size_attributes_every_distributed_file(tmp_path: Path) -> None:
    result = _export(tmp_path)
    directory = tmp_path / "out"
    expected = sum(
        path.stat().st_size for path in directory.iterdir() if path.is_file()
    )
    size = nw05_export.inspect_size(directory)
    assert size["total_distributed_bytes"] == expected
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

