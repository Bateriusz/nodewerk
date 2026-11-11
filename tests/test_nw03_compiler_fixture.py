from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as compiler  # noqa: E402
import nw03_oracle as oracle  # noqa: E402
import nwp_package as nwp  # noqa: E402
import verify_nw03  # noqa: E402

FIXTURE = REPO_ROOT / "fixtures" / "nw03" / "math_noise.nwgraph"


@pytest.fixture(autouse=True)
def _test_semantic_digest_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    if compiler._ensure_nw02_digest_binary() is None:
        monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")


def _cli_env() -> dict[str, str]:
    environment = dict(os.environ)
    if compiler._ensure_nw02_digest_binary() is None:
        environment["NODEWERK_NW03_TEST_DIGEST"] = "1"
    return environment


def test_load_graph_preserves_raw_newlines_for_source_hash(tmp_path: Path) -> None:
    fixture_text = FIXTURE.read_text(encoding="utf-8")
    fixture_text = fixture_text.replace("\r\n", "\n").replace("\r", "\n")
    raw_source = fixture_text.replace("\n", "\r\n").encode("utf-8")
    graph_path = tmp_path / "math_noise_crlf.nwgraph"
    graph_path.write_bytes(raw_source)

    document = compiler.load_graph(graph_path)

    assert document.source_text.encode("utf-8") == raw_source
    assert hashlib.sha256(document.source_text.encode("utf-8")).hexdigest() == hashlib.sha256(raw_source).hexdigest()


def make_dxil_container() -> bytes:
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
            (5 << 16) | (6 << 4),
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
    header = (
        b"DXBC"
        + digest
        + struct.pack("<HHII", 1, 0, cursor, len(chunks))
        + struct.pack(f"<{len(chunks)}I", *offsets)
    )
    return header + joined


def make_mock_dxc(
    directory: Path, *, fail: bool = False, version: str = "1.8.2407.12"
) -> Path:
    dxc = directory / "mock_dxc.py"
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        f"    print('dxcompiler.dll: 1.8 - {version}')\n"
        "    sys.exit(0)\n"
        + ("sys.stderr.write('mock DXC failure\\n')\nsys.exit(1)\n" if fail else
           "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
           "fo.parent.mkdir(parents=True, exist_ok=True)\n"
           f"fo.write_bytes(bytes.fromhex({make_dxil_container().hex()!r}))\n"),
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    return dxc


def test_math_noise_fixture_lowers_reachable_fused_hlsl_only() -> None:
    document = compiler.load_graph(FIXTURE)
    generated, diagnostics = compiler.lower_to_fused_hlsl(document)
    assert diagnostics == []
    assert len(generated) == 2
    ids = {shader.specialization_id for shader in generated}
    assert ids == {"noise_a", "noise_alt"}
    assert "unused_expr" not in {node for shader in generated for node in shader.root_node_ids}
    for shader in generated:
        assert shader.target == "cs_6_0"
        assert shader.entry_point == "CSMain"
        assert "float4 nw_value" in shader.hlsl
        assert "nwgraph" not in shader.hlsl
        assert "fixtures/" not in shader.hlsl
        assert shader.source_map

    # The two identical reachable roots deduplicate to one generated
    # specialization.
    lowering = compiler.build_report_from_graph(FIXTURE)
    assert lowering["specialization_count"] == 2
    assert lowering["reachable_root_count"] == 3
    assert lowering["deduplicated_root_count"] == 1
    assert "noise_b" in lowering["deduplicated_roots"]
    assert "unused_expr" in lowering["non_reachable_shader_nodes"]


def test_oracle_matches_documented_tolerance() -> None:
    report = oracle.oracle_report()
    assert report["passed"], report["failures"]
    assert report["sample_count"] >= 16
    assert report["absolute_tolerance"] == 1e-5
    assert report["relative_tolerance"] == 1e-4
    lowered = oracle.lower_expression_text_to_hlsl(oracle.MATH_NOISE_EXPRESSION)
    assert "float4(" in lowered
    assert "lerp(" in lowered
    assert "g_time" in lowered


def test_generated_hlsl_expression_matches_cpu_oracle() -> None:
    document = compiler.load_graph(FIXTURE)
    generated, diagnostics = compiler.lower_to_fused_hlsl(document)
    assert diagnostics == []
    report = oracle.oracle_report(
        hlsl_expressions=[
            (shader.root_expression, shader.hlsl) for shader in generated
        ]
    )
    assert report["passed"], report["hlsl_expression_evaluation"]["failures"]
    evaluation = report["hlsl_expression_evaluation"]
    assert evaluation["source_count"] == 2
    assert evaluation["sample_count"] >= 32
    assert evaluation["passed"] is True

    # A deliberate artifact mutation must be detected, proving the comparison
    # evaluates generated HLSL rather than replaying the source expression.
    mutated = generated[0].hlsl.replace("17.0", "18.0")
    bad_report = oracle.oracle_report(
        samples=[((0.25, 0.75), 0.0)],
        hlsl_expressions=[(generated[0].root_expression, mutated)],
    )
    assert bad_report["passed"] is False


def test_fixture_expected_json_matches_cpu_oracle() -> None:
    expected = json.loads(
        (REPO_ROOT / "fixtures" / "nw03" / "math_noise_expected.json").read_text(
            encoding="utf-8"
        )
    )
    for sample in expected["samples"]:
        actual = oracle.math_noise_oracle(sample["uv"], sample["time"])
        assert oracle.tolerance_failure(sample["expected"], actual) is None


def test_compile_package_with_offline_dxc_and_dev_sidecar(tmp_path: Path) -> None:
    dxc = make_mock_dxc(tmp_path)
    package_path = tmp_path / "out" / "math_noise.nwp"
    report_path = tmp_path / "out" / "report.json"
    sidecar_path = tmp_path / "out" / "math_noise.dev.json"
    report = compiler.compile_package(
        FIXTURE,
        package_path,
        report_path=report_path,
        dev_sidecar_path=sidecar_path,
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    assert report["fixture"]["shader_specialization_count"] == 2
    assert report["fixture"]["unique_shader_blob_count"] == 1
    expected_release = (
        compiler.load_graph(FIXTURE).semantic_identity().provenance == "nw02"
    )
    assert report["package"]["release"] is expected_release
    assert (
        report["toolchain"]["semantic_digest_test_identity"]
        is (not expected_release)
    )
    assert report["release"]["source_maps_in_package"] is False
    assert report["compiler"]["fingerprint"].startswith("sha256:v1:")
    assert report["profile"]["profile_fingerprint"].startswith("sha256:v1:")

    data = package_path.read_bytes()
    inspection = nwp.inspect_package_bytes(data)
    assert inspection.valid
    assert inspection.package is not None
    assert inspection.package.capability.profile_id == nwp.COMMON_PROFILE_ID
    assert inspection.package.capability.emitted_shader_targets == ("cs_6_0",)
    assert len(inspection.package.shaders) == 2
    assert len({shader.blob for shader in inspection.package.shaders}) == 1
    pipeline_shaders = [
        instruction.operands[0].value
        for instruction in inspection.package.initialization
        if instruction.opcode == "init.create_pipeline"
    ]
    dispatched_shaders = [
        instruction.operands[-1].value
        for instruction in inspection.package.frame
        if instruction.opcode == "frame.dispatch"
    ]
    assert pipeline_shaders == [1, 2]
    assert dispatched_shaders == [1, 2]
    if expected_release:
        assert nwp.check_release_leakage(data) == []
    else:
        assert "package is not marked as a release package" in (
            nwp.check_release_leakage(data)
        )
    assert b"math_noise.nwgraph" not in data
    assert b"nw.shader.expr" not in data

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["format"] == "nw03-development-sidecar"
    assert sidecar["source_maps"]
    assert sidecar["reflection"]["bindings"]["output"]["slot"] == 0
    assert sidecar["source_graph"]["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()


def test_compile_package_reuses_and_invalidates_shader_cache(tmp_path: Path) -> None:
    dxc = make_mock_dxc(tmp_path)
    cache = tmp_path / "cache"
    first = compiler.compile_package(
        FIXTURE,
        tmp_path / "first.nwp",
        cache_dir=cache,
        dxc_executable=str(dxc),
        optimization="O2",
        allow_test_payloads=True,
    )
    second = compiler.compile_package(
        FIXTURE,
        tmp_path / "second.nwp",
        cache_dir=cache,
        dxc_executable=str(dxc),
        optimization="O2",
        allow_test_payloads=True,
    )
    third = compiler.compile_package(
        FIXTURE,
        tmp_path / "third.nwp",
        cache_dir=cache,
        dxc_executable=str(dxc),
        optimization="O1",
        allow_test_payloads=True,
    )
    assert first["shader_cache"]["misses"] > 0
    assert second["shader_cache"]["hits"] > 0
    assert third["shader_cache"]["misses"] > 0
    assert first["package"]["sha256"] == second["package"]["sha256"]


def test_unpinned_dxc_override_produces_truthful_non_release_artifact(
    tmp_path: Path,
) -> None:
    dxc = make_mock_dxc(tmp_path, version="1.8.9999.1")
    package_path = tmp_path / "unpinned.nwp"
    report_path = tmp_path / "unpinned-report.json"
    sidecar_path = tmp_path / "unpinned.dev.json"
    report = compiler.compile_package(
        FIXTURE,
        package_path,
        report_path=report_path,
        dev_sidecar_path=sidecar_path,
        dxc_executable=str(dxc),
        allow_unpinned_dxc=True,
        allow_test_payloads=True,
    )
    assert report["package"]["release"] is False
    assert report["toolchain"]["dxc_version"] == "1.8.9999.1"
    assert report["toolchain"]["pinned"] is False
    package = nwp.decode_package(package_path.read_bytes())
    assert package.flags & 1 == 0
    assert package.capability.toolchain_dxc_version == "1.8.9999.1"
    assert package.capability.toolchain_identity == "dxc:1.8.9999.1"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["toolchain"]["dxc_version"] == "1.8.9999.1"
    assert sidecar["toolchain"]["dxc_identity"] == "dxc:1.8.9999.1"


def test_shader_cache_key_covers_semantic_binding_profile_option_and_toolchain() -> None:
    shader = compiler.default_fixture_shader()
    common = dict(
        shader=shader,
        semantic_digest="sha256:v1:" + "1" * 64,
        profile_id=nwp.COMMON_PROFILE_ID,
        requirements_digest=nwp.COMMON_REQUIREMENTS_DIGEST,
        compiler_settings={"optimization": "O2"},
        dxc_identity="dxc:1.8.2407.12",
        dxc_version="1.8.2407.12",
        optimization="O2",
        bindings_identity="bindings-a",
    )
    base = compiler.ShaderCache.make_key(**common)
    variants = [
        compiler.ShaderCache.make_key(**{**common, "semantic_digest": "sha256:v1:" + "2" * 64}),
        compiler.ShaderCache.make_key(**{**common, "bindings_identity": "bindings-b"}),
        compiler.ShaderCache.make_key(
            **{
                **common,
                "profile_id": "nw.common.d3d12.fl12_0.sm6_0.v1",
                "requirements_digest": "sha256:v1:" + "3" * 64,
            }
        ),
        compiler.ShaderCache.make_key(**{**common, "optimization": "O1"}),
        compiler.ShaderCache.make_key(**{**common, "dxc_identity": "dxc:1.8.9999.1"}),
    ]
    assert len({base, *variants}) == len(variants) + 1


def test_failed_compilation_preserves_previous_package_and_no_partial_output(
    tmp_path: Path,
) -> None:
    good_dxc = make_mock_dxc(tmp_path)
    package_path = tmp_path / "preserved.nwp"
    compiler.compile_package(
        FIXTURE,
        package_path,
        dxc_executable=str(good_dxc),
        allow_test_payloads=True,
    )
    previous = package_path.read_bytes()
    failing_dxc = make_mock_dxc(tmp_path / "failure", fail=True) if False else None
    # Put the failing mock in a separate directory to avoid replacing good_dxc.
    failure_dir = tmp_path / "failure"
    failure_dir.mkdir()
    failing_dxc = make_mock_dxc(failure_dir, fail=True)
    with pytest.raises(compiler.CompilerError) as exc_info:
        compiler.compile_package(
            FIXTURE,
            package_path,
            dxc_executable=str(failing_dxc),
            allow_test_payloads=True,
        )
    assert exc_info.value.diagnostics
    assert exc_info.value.diagnostics[0]["category"] == "package_validation"
    assert package_path.read_bytes() == previous


def test_compile_package_fails_without_dxc_instead_of_runtime_compilation(
    tmp_path: Path,
) -> None:
    with pytest.raises(compiler.CompilerError, match="DXC"):
        compiler.compile_package(
            FIXTURE,
            tmp_path / "out.nwp",
            dxc_executable=str(tmp_path / "missing_dxc"),
            allow_test_payloads=True,
        )


def test_two_clean_directories_are_byte_identical_with_offline_dxc(tmp_path: Path) -> None:
    dxc = make_mock_dxc(tmp_path)
    result = verify_nw03.check_clean_directory_reproducibility(
        str(dxc), tmp_path / "clean-work", allow_test_payloads=True
    )
    assert result.passed, result.detail
    assert result.evidence["shader_specializations"] == 2


def test_real_pinned_dxc_compiles_fixture_when_available(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip(
            "Windows CI covers real DXC through tools/verify_nw03.py --mode Full; "
            "the POSIX pytest path avoids duplicating that long-running gate."
        )
    dxc = compiler.resolve_dxc()
    if not dxc:
        pytest.skip("offline pinned DXC is unavailable")
    report = compiler.compile_package(
        FIXTURE,
        tmp_path / "real.nwp",
        dxc_executable=dxc,
    )
    assert report["fixture"]["shader_specialization_count"] == 2
    valid, diagnostics = nwp.validate_package_bytes((tmp_path / "real.nwp").read_bytes())
    assert valid, diagnostics


def test_nodewerkc_cli_validate_compile_and_inspect(tmp_path: Path) -> None:
    dxc = make_mock_dxc(tmp_path)
    cli = REPO_ROOT / "tools" / "nodewerkc.py"
    validate = subprocess.run(
        [sys.executable, str(cli), "validate", str(FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(),
    )
    assert validate.returncode == 0, validate.stderr
    payload = json.loads(validate.stdout)
    assert payload["valid"] is True
    assert payload["lowering"]["specialization_count"] == 2

    package_path = tmp_path / "cli.nwp"
    compile_run = subprocess.run(
        [
            sys.executable,
            str(cli),
            "compile-package",
            str(FIXTURE),
            str(package_path),
            "--dxc",
            str(dxc),
            "--allow-test-payloads",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(),
    )
    assert compile_run.returncode == 0, compile_run.stderr
    inspect_run = subprocess.run(
        [sys.executable, str(cli), "inspect", str(package_path)],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(),
    )
    inspection = json.loads(inspect_run.stdout)
    if compiler.load_graph(FIXTURE).semantic_identity().is_test:
        assert inspect_run.returncode != 0
        assert inspection["valid"] is False
    else:
        assert inspect_run.returncode == 0, inspect_run.stderr
        assert inspection["valid"] is True
    assert inspection["capability"]["profile_id"] == nwp.COMMON_PROFILE_ID
    assert inspection["release"]["contains_authoring_metadata"] is False


def test_nodewerkc_inspect_rejects_release_leakage(tmp_path: Path) -> None:
    package = nwp.make_test_package(
        shader_blob=make_dxil_container(), specialization_count=1
    )
    leaky = replace(
        package,
        parameters=[replace(package.parameters[0], name="nwgraph")],
    )
    data = nwp.encode_package(leaky)
    assert nwp.leak_scan_report(data)["valid"] is False
    package_path = tmp_path / "leaky.nwp"
    package_path.write_bytes(data)
    cli = REPO_ROOT / "tools" / "nodewerkc.py"
    proc = subprocess.run(
        [sys.executable, str(cli), "inspect", str(package_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    payload = json.loads(proc.stdout)
    assert payload["valid"] is False
    assert payload["leak_scan"]["valid"] is False


def test_nodewerkc_validate_rejects_all_invalid_graph_fixtures() -> None:
    cli = REPO_ROOT / "tools" / "nodewerkc.py"
    invalid = sorted((REPO_ROOT / "fixtures" / "graphs").glob("invalid_*.nwgraph"))
    assert invalid
    for path in invalid:
        proc = subprocess.run(
            [sys.executable, str(cli), "validate", str(path)],
            capture_output=True,
            text=True,
            check=False,
            env=_cli_env(),
        )
        assert proc.returncode != 0, path.name
        payload = json.loads(proc.stdout)
        assert payload["valid"] is False, path.name
        assert payload["diagnostics"], path.name


def test_nodewerkc_validate_accepts_valid_graph_fixtures() -> None:
    cli = REPO_ROOT / "tools" / "nodewerkc.py"
    valid = sorted((REPO_ROOT / "fixtures" / "graphs").glob("valid_*.nwgraph"))
    for path in valid:
        if path.name == "valid_legacy_v0.nwgraph":
            continue
        proc = subprocess.run(
            [sys.executable, str(cli), "validate", str(path)],
            capture_output=True,
            text=True,
            check=False,
            env=_cli_env(),
        )
        assert proc.returncode == 0, (path.name, proc.stdout, proc.stderr)


def _graph_text(nodes: str) -> str:
    return (
        "nwgraph 1\n"
        "profile nw.common.d3d12.fl12_0.sm6_0.v1 "
        "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a\n"
        "setting optimization O2\n"
        "setting deterministic true\n"
        "setting api d3d12\n"
        "setting feature_level 12_0\n"
        "setting shader_model 6_0\n"
        "setting frequency_policy strict\n"
        "setting shader_blob_policy stable\n"
        "setting quantization_policy none\n"
        "setting quantization_bits 0\n"
        "setting quantization_step 0\n"
        + nodes
    )


def test_clamp01_parameters_use_nw02_normalization_before_lowering() -> None:
    text = _graph_text(
        "node a nw.shader.vec4_const@1.0.0 frequency=frame\n"
        "  param value 0 0 0 1\n"
        "node b nw.shader.vec4_const@1.0.0 frequency=frame\n"
        "  param value 1 1 1 1\n"
        "node mixed nw.shader.mix@1.0.0 frequency=frame\n"
        "  input a a.out\n"
        "  input b b.out\n"
        "  param factor 2.0\n"
    )
    document, diagnostics = compiler.parse_graph_text(text)
    assert diagnostics == []
    assert document is not None
    assert compiler._param_tokens(document.nodes["mixed"], "factor") == ["1.0"]
    generated, lowering_diagnostics = compiler.lower_to_fused_hlsl(document)
    assert lowering_diagnostics == []
    assert any("lerp(" in shader.hlsl and ", 1.0)" in shader.hlsl for shader in generated)
    assert all("1.5" not in shader.hlsl for shader in generated)


def test_semantic_digest_preserves_vector_component_order() -> None:
    first, diagnostics = compiler.parse_graph_text(
        _graph_text(
            "node value nw.math.vec4_const@1.0.0 frequency=frame\n"
            "  param value 1 2 3 4\n"
        )
    )
    second, second_diagnostics = compiler.parse_graph_text(
        _graph_text(
            "node value nw.math.vec4_const@1.0.0 frequency=frame\n"
            "  param value 4 3 2 1\n"
        )
    )
    assert diagnostics == [] and second_diagnostics == []
    assert first is not None and second is not None
    assert first.semantic_digest() != second.semantic_digest()


def test_const_operation_is_lowered_away() -> None:
    expression = "vec4(const(1), 0, 0, 1)"
    lowered = oracle.lower_expression_text_to_hlsl(expression)
    assert "const(" not in lowered
    assert "float4((1.0)" in lowered
    assert oracle.evaluate_text(expression) == (1.0, 0.0, 0.0, 1.0)


def test_float32_oracle_rounds_every_intermediate_operation() -> None:
    expression = "(16777216 + 1) - 16777216"
    assert oracle.evaluate_text(expression, float32=False) == 1.0
    assert oracle.evaluate_text(expression, float32=True) == 0.0


def test_mock_dxc_requires_explicit_test_payload_opt_in(tmp_path: Path) -> None:
    dxc = make_mock_dxc(tmp_path)
    with pytest.raises(compiler.CompilerError):
        compiler.compile_package(
            FIXTURE,
            tmp_path / "release.nwp",
            dxc_executable=str(dxc),
        )
    result = verify_nw03.check_clean_directory_reproducibility(
        str(dxc), tmp_path / "clean-work"
    )
    assert not result.passed
    assert "mock" in result.detail.lower()


def test_semantic_digest_matches_nw02_canonical_cli() -> None:
    binary = compiler._ensure_nw02_digest_binary()
    if binary is None:
        pytest.skip("no host C++ compiler for the NW-02 digest CLI")
    document = compiler.load_graph(FIXTURE)
    proc = subprocess.run(
        [str(binary), "digest", str(FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert document.semantic_digest() == proc.stdout.strip()


def test_semantic_digest_canonicalizes_numeric_spellings() -> None:
    if compiler._ensure_nw02_digest_binary() is None:
        pytest.skip("no host C++ compiler for the NW-02 digest CLI")
    first, diagnostics = compiler.parse_graph_text(
        _graph_text(
            "node value nw.math.vec4_const@1.0.0 frequency=frame\n"
            "  param value 1 2 3 4\n"
        )
    )
    second, second_diagnostics = compiler.parse_graph_text(
        _graph_text(
            "node value nw.math.vec4_const@1.0.0 frequency=frame\n"
            "  param value 1.0 2.0 3.0 4.0\n"
        )
    )
    assert diagnostics == [] and second_diagnostics == []
    assert first is not None and second is not None
    assert first.semantic_digest() == second.semantic_digest()


def test_semantic_digest_fails_without_native_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NODEWERK_NW03_TEST_DIGEST", raising=False)
    monkeypatch.setattr(compiler, "_NW02_DIGEST_BINARY", False)
    monkeypatch.setattr(compiler, "_NW02_DIGEST_CACHE", {})
    document = compiler.load_graph(FIXTURE)
    with pytest.raises(compiler.CompilerError, match="canonical semantic digest"):
        document.semantic_digest()


def test_failing_helper_cannot_masquerade_as_canonical_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    monkeypatch.setattr(
        compiler, "_NW02_DIGEST_BINARY", Path(sys.executable)
    )
    monkeypatch.setattr(compiler, "_NW02_DIGEST_CACHE", {})
    dxc = make_mock_dxc(tmp_path)
    package_path = tmp_path / "failing-helper.nwp"
    report = compiler.compile_package(
        FIXTURE,
        package_path,
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    assert report["package"]["release"] is False
    assert report["toolchain"]["semantic_digest_test_identity"] is True
    package = nwp.decode_package(package_path.read_bytes())
    assert (package.flags & 1) == 0
