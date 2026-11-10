from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPHS = REPO_ROOT / "fixtures" / "graphs"
COMMON_PROFILE = "nw.common.d3d12.fl12_0.sm6_0.v1"
COMMON_DIGEST = (
    "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a"
)


def _find_cxx() -> str | None:
    configured = os.environ.get("CXX")
    if configured:
        candidate = shutil.which(configured) or (
            configured if Path(configured).exists() else None
        )
        configured_name = Path(candidate or configured).name.lower()
        if candidate and (
            "++" in configured_name
            or configured_name in {"cl", "cl.exe", "clang-cl", "clang-cl.exe"}
        ):
            return candidate
    for name in ("c++", "g++", "clang++", "cl", "cl.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture(scope="session")
def cli_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = _find_cxx()
    if compiler is None:
        pytest.skip(
            "NW-02 graph semantics require a C++20 compiler; no compiler was found"
        )
    source_dir = REPO_ROOT / "src" / "graph"
    sources = [source_dir / "nw_graph.cpp", source_dir / "nw_graph_cli.cpp"]
    build_dir = tmp_path_factory.mktemp("nw02_graph_build")
    exe = build_dir / ("nw_graph_cli.exe" if os.name == "nt" else "nw_graph_cli")
    compiler_name = Path(compiler).name.lower()
    if compiler_name in {"cl", "cl.exe"}:
        command = [
            compiler,
            "/nologo",
            "/std:c++20",
            "/EHsc",
            "/O2",
            "/I",
            str(source_dir),
            *[str(source) for source in sources],
            f"/Fe:{exe}",
        ]
    else:
        command = [
            compiler,
            "-std=c++20",
            "-O2",
            "-I",
            str(source_dir),
            *[str(source) for source in sources],
            "-o",
            str(exe),
        ]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        cwd=build_dir,
    )
    assert proc.returncode == 0, (
        "C++20 build failed\n"
        f"command: {' '.join(command)}\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}\n"
    )
    assert exe.exists()
    return exe


def run_cli(binary: Path, *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), *[str(arg) for arg in args]],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def run_json(binary: Path, *args: object) -> tuple[subprocess.CompletedProcess[str], dict]:
    proc = run_cli(binary, *args)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - failure aid
        raise AssertionError(
            f"command {' '.join(map(str, args))} did not emit JSON\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        ) from exc
    return proc, payload


def fixture(name: str) -> Path:
    return GRAPHS / name


def diagnostic_codes(payload: dict) -> list[str]:
    return [item["code"] for item in payload.get("diagnostics", [])]


def find_diagnostic(payload: dict, code: str) -> dict:
    for item in payload.get("diagnostics", []):
        if item["code"] == code:
            return item
    raise AssertionError(f"missing diagnostic {code}: {payload}")


def split_graph(text: str) -> tuple[list[str], list[str], dict[str, list[str]], list[str]]:
    header: list[str] = []
    order: list[str] = []
    blocks: dict[str, list[str]] = {}
    layouts: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("node "):
            current = stripped.split()[1]
            order.append(current)
            blocks[current] = [line]
        elif stripped.startswith("layout "):
            layouts.append(line)
            current = None
        elif current is not None and line[:1] in {" ", "\t"} and stripped:
            blocks[current].append(line)
        elif current is not None and line[:1] in {" ", "\t"} and not stripped:
            blocks[current].append(line)
        elif stripped.startswith("#") or not stripped:
            if current is None:
                header.append(line)
        else:
            header.append(line)
            current = None
    return header, order, blocks, layouts


def join_graph(
    header: list[str],
    order: list[str],
    blocks: dict[str, list[str]],
    layouts: list[str],
) -> str:
    body = "\n".join(line for node_id in order for line in blocks[node_id])
    header_text = "\n".join(header).rstrip()
    layout_text = "\n".join(layouts).rstrip()
    return f"{header_text}\n\n{body}\n\n{layout_text}\n"


def rename_graph(text: str, mapping: dict[str, str]) -> str:
    output: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        indent = line[: len(line) - len(line.lstrip())]
        tokens = stripped.split()
        if not tokens:
            output.append(line)
            continue
        if tokens[0] == "node" and len(tokens) >= 2:
            tokens[1] = mapping.get(tokens[1], tokens[1])
            output.append(indent + " ".join(tokens))
        elif tokens[0] == "input" and len(tokens) >= 3:
            source, port = tokens[2].rsplit(".", 1)
            tokens[2] = mapping.get(source, source) + "." + port
            output.append(indent + " ".join(tokens))
        elif tokens[:2] == ["seed", "node-id"] and len(tokens) >= 3:
            tokens[2] = mapping.get(tokens[2], tokens[2])
            output.append(indent + " ".join(tokens))
        elif tokens[0] == "layout" and len(tokens) >= 2:
            tokens[1] = mapping.get(tokens[1], tokens[1])
            output.append(indent + " ".join(tokens))
        else:
            output.append(line)
    return "\n".join(output) + "\n"


def with_layout(text: str, coordinates: dict[str, tuple[float, float]]) -> str:
    output: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        tokens = stripped.split()
        if tokens and tokens[0] == "layout" and tokens[1] in coordinates:
            x, y = coordinates[tokens[1]]
            output.append(f"layout {tokens[1]} {x!r} {y!r}")
        else:
            output.append(line)
    return "\n".join(output) + "\n"


def with_setting(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        tokens = line.strip().split()
        if len(tokens) >= 3 and tokens[0] == "setting" and tokens[1] == key:
            lines[index] = f"setting {key} {value}"
            return "\n".join(lines) + "\n"
    insert_at = 2
    lines.insert(insert_at, f"setting {key} {value}")
    return "\n".join(lines) + "\n"


def write_variant(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_typed_fixture_validates_and_evaluates_without_gpu(cli_binary: Path) -> None:
    proc, payload = run_json(cli_binary, "validate", fixture("valid_typed_pipeline.nwgraph"))
    assert proc.returncode == 0, payload
    assert payload["valid"] is True
    assert payload["profile_id"] == COMMON_PROFILE
    assert payload["requirements_digest"] == COMMON_DIGEST
    assert payload["semantic_digest"].startswith("sha256:v1:")
    assert payload["node_count"] >= 30

    proc, evaluation = run_json(cli_binary, "eval", fixture("valid_typed_pipeline.nwgraph"))
    assert proc.returncode == 0, evaluation
    assert evaluation["ok"] is True
    values = evaluation["values"]
    assert values
    types = {item["type"] for item in values}
    assert types == {
        "scalar",
        "vector",
        "matrix",
        "curve",
        "shader_expr",
        "texture_recipe",
        "buffer_recipe",
        "mesh_recipe",
        "material",
        "scene_table",
        "camera",
        "light",
        "render_image",
    }
    ports = {(item["operator"], item["port"]) for item in values}
    assert ("nw.math.vec4_split@1.0.0", "x") in ports
    assert ("nw.math.vec4_split@1.0.0", "y") in ports
    assert ("nw.math.vec4_split@1.0.0", "z") in ports
    assert ("nw.math.vec4_split@1.0.0", "w") in ports
    assert ("nw.render.gbuffer@1.0.0", "color") in ports
    assert ("nw.render.gbuffer@1.0.0", "normals") in ports
    assert all("value" in item for item in values)


def test_evaluation_and_canonical_serialization_reproducible(cli_binary: Path) -> None:
    graph = fixture("valid_typed_pipeline.nwgraph")
    first_eval = run_cli(cli_binary, "eval", graph, "--workers", "1")
    second_eval = run_cli(cli_binary, "eval", graph, "--workers", "4")
    assert first_eval.returncode == 0
    assert first_eval.stdout == second_eval.stdout

    first_digest = run_cli(cli_binary, "digest", graph)
    second_digest = run_cli(cli_binary, "digest", graph)
    assert first_digest.returncode == 0
    assert first_digest.stdout == second_digest.stdout
    assert first_digest.stdout.strip().startswith("sha256:v1:")

    first_canonical = run_cli(cli_binary, "canonical", graph)
    second_canonical = run_cli(cli_binary, "canonical", graph)
    assert first_canonical.returncode == 0
    assert first_canonical.stdout == second_canonical.stdout
    assert "layout" not in first_canonical.stdout


INVALID_CASES = {
    "invalid_cycle.nwgraph": "ordinary_cycle",
    "invalid_type_mismatch.nwgraph": "port_type_mismatch",
    "invalid_dimension_mismatch.nwgraph": "port_dimension_mismatch",
    "invalid_nonfinite_parameter.nwgraph": "non_finite_parameter",
    "invalid_frequency_transition.nwgraph": "frequency_transition",
    "invalid_history.nwgraph": "history_not_supported",
    "invalid_bounded_resource.nwgraph": "bounded_resource_exceeded",
    "invalid_implicit_readback.nwgraph": "implicit_readback",
    "invalid_implicit_upload.nwgraph": "implicit_upload",
    "invalid_unknown_profile.nwgraph": "unknown_capability_profile",
    "invalid_malformed_profile.nwgraph": "malformed_capability_profile",
    "invalid_profile_digest_mismatch.nwgraph": "capability_profile_digest_mismatch",
    "invalid_schema_version.nwgraph": "unknown_schema_version",
    "invalid_operator_version.nwgraph": "unknown_operator_version",
    "invalid_unknown_operator.nwgraph": "unknown_operator",
    "invalid_unsupported_seed.nwgraph": "unsupported_seed_behavior",
    "invalid_unknown_input_node.nwgraph": "unknown_input_node",
    "invalid_unknown_input_port.nwgraph": "unknown_input_port",
    "invalid_unknown_output_port.nwgraph": "unknown_output_port",
}


@pytest.mark.parametrize("name,code", sorted(INVALID_CASES.items()))
def test_invalid_fixtures_have_precise_dependency_diagnostics(
    cli_binary: Path, name: str, code: str
) -> None:
    proc, payload = run_json(cli_binary, "validate", fixture(name))
    assert proc.returncode != 0, payload
    assert payload["valid"] is False
    assert code in diagnostic_codes(payload), payload
    diagnostic = find_diagnostic(payload, code)
    assert diagnostic["message"]
    if diagnostic.get("node_id"):
        assert diagnostic["node_id"]
    if diagnostic.get("port_id"):
        assert diagnostic["port_id"]


def test_dependency_diagnostics_identify_source_node_and_port(cli_binary: Path) -> None:
    _, payload = run_json(cli_binary, "validate", fixture("invalid_type_mismatch.nwgraph"))
    diagnostic = find_diagnostic(payload, "port_type_mismatch")
    assert diagnostic["node_id"] == "scalar_sink"
    assert diagnostic["port_id"] == "x"
    assert diagnostic["source_node_id"] == "vector_value"
    assert diagnostic["source_port_id"] == "out"

    _, payload = run_json(cli_binary, "validate", fixture("invalid_cycle.nwgraph"))
    cycle = find_diagnostic(payload, "ordinary_cycle")
    assert cycle["node_id"] in {"cycle_a", "cycle_b"}
    assert cycle["port_id"]
    assert cycle["source_node_id"] in {"cycle_a", "cycle_b"}
    assert cycle["source_port_id"] == "out"
    assert cycle["dependency_path"]

    _, payload = run_json(cli_binary, "validate", fixture("invalid_implicit_readback.nwgraph"))
    readback = find_diagnostic(payload, "implicit_readback")
    assert readback["node_id"] == "implicit_cpu"
    assert readback["port_id"] == "image"
    assert readback["source_node_id"] == "image"
    assert readback["source_port_id"] == "image"


def test_operator_registry_declarations_are_complete(cli_binary: Path) -> None:
    proc, payload = run_json(cli_binary, "registry")
    assert proc.returncode == 0
    operators = payload["operators"]
    assert operators

    output_types: set[str] = set()
    kinds: set[str] = set()
    saw_readback = False
    saw_conversion = False
    for operator in operators:
        assert operator["id"].startswith("nw.")
        assert re.fullmatch(r"\d+\.\d+\.\d+", operator["version"])
        assert operator["kind"] in {"pure_shader_expr", "gpu_pass", "cpu_control"}
        kinds.add(operator["kind"])
        assert operator["frequency"]
        assert operator["default_frequency"] in operator["frequency"]
        assert operator["allowed_seed_behaviors"]
        assert operator["seed_behavior"] in operator["allowed_seed_behaviors"]
        assert operator["lowering_hook"]
        assert operator["lowering_hook_version"]
        assert operator["contract_digest"].startswith("sha256:v1:")
        assert re.fullmatch(r"sha256:v1:[0-9a-f]{64}", operator["contract_digest"])

        input_ids = [port["id"] for port in operator["inputs"]]
        output_ids = [port["id"] for port in operator["outputs"]]
        assert len(input_ids) == len(set(input_ids))
        assert len(output_ids) == len(set(output_ids))
        for port in operator["inputs"] + operator["outputs"]:
            assert port["id"]
            assert port["type"]
            assert port["residency"] in {"logical", "cpu", "gpu", "readback"}
        for port in operator["outputs"]:
            output_types.add(port["type"])
            if port["type"] in {
                "texture_recipe",
                "buffer_recipe",
                "mesh_recipe",
                "render_image",
            } and port["resource"] is not None:
                assert port["resource"]["bounded"] is True
        for param in operator["params"]:
            assert param["id"]
            assert param["type"]
            assert param["normalization"]
        if operator["explicit_readback"]:
            saw_readback = True
        if operator["explicit_residency_conversion"]:
            saw_conversion = True
        if operator["kind"] == "gpu_pass":
            assert set(operator["frequency"]) <= {"initialization", "frame"}
        if operator["kind"] == "pure_shader_expr":
            assert operator["shader_operations"]

    assert kinds == {"pure_shader_expr", "gpu_pass", "cpu_control"}
    assert output_types == {
        "scalar",
        "vector",
        "matrix",
        "curve",
        "shader_expr",
        "texture_recipe",
        "buffer_recipe",
        "mesh_recipe",
        "material",
        "scene_table",
        "camera",
        "light",
        "render_image",
    }
    assert saw_readback is True
    assert saw_conversion is True


def test_semantic_digest_ignores_node_order_ids_and_layout(
    cli_binary: Path, tmp_path: Path
) -> None:
    text = fixture("valid_cache.nwgraph").read_text(encoding="utf-8")
    header, order, blocks, layouts = split_graph(text)
    reordered = join_graph(header, list(reversed(order)), blocks, layouts)
    renamed = rename_graph(
        text,
        {
            "a": "alpha",
            "b": "beta",
            "c": "gamma",
            "d": "delta",
            "unrelated": "other",
            "unrelated_sin": "other_sin",
        },
    )
    relaid = with_layout(
        text,
        {
            "a": (101.5, 202.25),
            "b": (303.0, 404.0),
            "c": (505.0, 606.0),
            "d": (707.0, 808.0),
            "unrelated": (909.0, 1010.0),
            "unrelated_sin": (1111.0, 1212.0),
        },
    )
    paths = {
        "base": fixture("valid_cache.nwgraph"),
        "reordered": write_variant(tmp_path, "reordered.nwgraph", reordered),
        "renamed": write_variant(tmp_path, "renamed.nwgraph", renamed),
        "relaid": write_variant(tmp_path, "relaid.nwgraph", relaid),
    }
    digests = {}
    canonical = {}
    eval_output = {}
    bytes_output = {}
    for label, path in paths.items():
        proc = run_cli(cli_binary, "digest", path)
        assert proc.returncode == 0, (label, proc.stderr)
        digests[label] = proc.stdout.strip()
        proc = run_cli(cli_binary, "canonical", path)
        assert proc.returncode == 0
        canonical[label] = proc.stdout
        proc = run_cli(cli_binary, "eval", path)
        assert proc.returncode == 0
        eval_output[label] = proc.stdout
        bytes_path = tmp_path / f"{label}.bin"
        proc = run_cli(cli_binary, "canonical-bytes", path, "--out", bytes_path)
        assert proc.returncode == 0
        bytes_output[label] = bytes_path.read_bytes()

    assert len(set(digests.values())) == 1
    assert len(set(canonical.values())) == 1
    assert len(set(eval_output.values())) == 1
    assert len({bytes(value) for value in bytes_output.values()}) == 1


def test_explicit_node_id_seed_changes_randomness_but_other_renames_do_not(
    cli_binary: Path, tmp_path: Path
) -> None:
    text = fixture("valid_seed_sources.nwgraph").read_text(encoding="utf-8")
    renamed_b = rename_graph(text, {"source_b": "source_beta"})
    renamed_seed = rename_graph(text, {"source_a": "source_alpha"})
    header, order, blocks, layouts = split_graph(text)
    reordered = join_graph(header, list(reversed(order)), blocks, layouts)
    relaid = with_layout(
        text,
        {
            "source_a": (11.0, 12.0),
            "source_b": (13.0, 14.0),
            "seeded": (15.0, 16.0),
            "explicit_seed": (17.0, 18.0),
            "derived": (19.0, 20.0),
        },
    )
    base_path = fixture("valid_seed_sources.nwgraph")
    b_path = write_variant(tmp_path, "seed_b_renamed.nwgraph", renamed_b)
    seed_path = write_variant(tmp_path, "seed_source_renamed.nwgraph", renamed_seed)
    reordered_path = write_variant(tmp_path, "seed_reordered.nwgraph", reordered)
    relaid_path = write_variant(tmp_path, "seed_relaid.nwgraph", relaid)

    base_digest = run_cli(cli_binary, "digest", base_path).stdout.strip()
    b_digest = run_cli(cli_binary, "digest", b_path).stdout.strip()
    seed_digest = run_cli(cli_binary, "digest", seed_path).stdout.strip()
    reordered_digest = run_cli(cli_binary, "digest", reordered_path).stdout.strip()
    relaid_digest = run_cli(cli_binary, "digest", relaid_path).stdout.strip()
    assert base_digest == b_digest
    assert base_digest == reordered_digest
    assert base_digest == relaid_digest
    assert base_digest != seed_digest

    base_seeds = run_json(cli_binary, "seed", base_path)[1]
    b_seeds = run_json(cli_binary, "seed", b_path)[1]
    seed_seeds = run_json(cli_binary, "seed", seed_path)[1]
    reordered_seeds = run_json(cli_binary, "seed", reordered_path)[1]
    relaid_seeds = run_json(cli_binary, "seed", relaid_path)[1]
    assert base_seeds == b_seeds
    assert base_seeds == reordered_seeds
    assert base_seeds == relaid_seeds
    assert base_seeds != seed_seeds


def test_semantic_digest_sensitive_to_versions_upstream_settings_and_quantization(
    cli_binary: Path, tmp_path: Path
) -> None:
    base = fixture("valid_cache.nwgraph").read_text(encoding="utf-8")
    base_path = fixture("valid_cache.nwgraph")
    base_digest = run_cli(cli_binary, "digest", base_path).stdout.strip()

    upstream_changed = base.replace("param value 0.125", "param value 0.126")
    optimization_changed = with_setting(base, "optimization", "O1")
    quantization_changed = with_setting(
        with_setting(
            with_setting(base, "quantization_policy", "unorm8"),
            "quantization_bits",
            "8",
        ),
        "quantization_step",
        "0.0039215686274509803",
    )
    float_spelling_changed = base.replace("param value 0.125", "param value 1.25e-1")

    digests = {}
    for label, text in {
        "upstream": upstream_changed,
        "optimization": optimization_changed,
        "quantization": quantization_changed,
        "float_spelling": float_spelling_changed,
    }.items():
        path = write_variant(tmp_path, f"{label}.nwgraph", text)
        proc = run_cli(cli_binary, "digest", path)
        assert proc.returncode == 0, (label, proc.stdout)
        digests[label] = proc.stdout.strip()

    assert digests["upstream"] != base_digest
    assert digests["optimization"] != base_digest
    assert digests["quantization"] != base_digest
    assert digests["float_spelling"] == base_digest

    proc, selftest = run_json(cli_binary, "selftest")
    assert proc.returncode == 0, selftest
    assert selftest["all_passed"] is True
    assert all(check["passed"] for check in selftest["checks"])


def _canonical_field_bytes() -> dict[str, bytes]:
    def enc_string(value: str) -> bytes:
        raw = value.encode("utf-8")
        return b"\x01" + struct.pack(">I", len(raw)) + raw

    def enc_u64(value: int) -> bytes:
        return b"\x02" + struct.pack(">Q", value)

    def enc_f64(value: float) -> bytes:
        if value == 0.0:
            value = 0.0
        return b"\x03" + struct.pack(">d", value)

    def enc_bool(value: bool) -> bytes:
        return b"\x04" + (b"\x01" if value else b"\x00")

    def enc_version(major: int, minor: int, patch: int) -> bytes:
        return b"\x05" + struct.pack(">III", major, minor, patch)

    def list_header(count: int) -> bytes:
        return b"\x08" + struct.pack(">I", count)

    def map_header(count: int) -> bytes:
        return b"\x09" + struct.pack(">I", count)

    def quantization(policy: str, bits: int, step: float) -> bytes:
        return enc_string(policy) + enc_u64(bits) + enc_f64(step)

    return {
        "base": list_header(6)
        + enc_string("nwgraph.golden.v1")
        + enc_u64(1)
        + enc_version(1, 2, 3)
        + enc_string("hello")
        + enc_bool(True)
        + enc_f64(0.25),
        "float-normalization": list_header(3)
        + enc_f64(0.1)
        + enc_f64(-0.0)
        + enc_f64(1e-1),
        "upstream": list_header(2)
        + enc_string("sha256:v1:" + "a" * 64)
        + enc_string("sha256:v1:" + "b" * 64),
        "compiler-settings": map_header(3)
        + enc_string("api")
        + enc_string("d3d12")
        + enc_string("deterministic")
        + enc_string("true")
        + enc_string("optimization")
        + enc_string("O2"),
        "quantization": list_header(2)
        + quantization("unorm8", 8, 0.0039215686274509803)
        + enc_f64(0.5),
        "version": list_header(1) + enc_version(1, 2, 3),
    }


def test_canonical_field_encoding_golden_digests(cli_binary: Path) -> None:
    proc, payload = run_json(cli_binary, "field-digest")
    assert proc.returncode == 0
    expected = {
        name: "sha256:v1:" + hashlib.sha256(data).hexdigest()
        for name, data in _canonical_field_bytes().items()
    }
    golden = json.loads(
        (GRAPHS / "golden-field-digests.json").read_text(encoding="utf-8")
    )
    assert golden == expected
    assert payload == golden
    assert run_cli(cli_binary, "field-digest").stdout == proc.stdout
    assert (
        run_cli(cli_binary, "hash-text", "abc").stdout.strip()
        == "sha256:v1:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_versioned_migrations_are_explicit_and_unknown_versions_rejected(
    cli_binary: Path, tmp_path: Path
) -> None:
    legacy = fixture("valid_legacy_v0.nwgraph")
    proc, payload = run_json(cli_binary, "validate", legacy)
    assert proc.returncode != 0
    assert "migration_required" in diagnostic_codes(payload)

    migrated = run_cli(cli_binary, "migrate", legacy)
    assert migrated.returncode == 0, migrated.stderr
    migrated_path = write_variant(tmp_path, "migrated.nwgraph", migrated.stdout)
    proc, payload = run_json(cli_binary, "validate", migrated_path)
    assert proc.returncode == 0, payload
    assert payload["valid"] is True

    proc, direct = run_json(cli_binary, "validate", legacy, "--migrate")
    assert proc.returncode == 0, direct
    assert direct["semantic_digest"] == payload["semantic_digest"]

    proc, payload = run_json(cli_binary, "validate", fixture("invalid_schema_version.nwgraph"))
    assert proc.returncode != 0
    assert "unknown_schema_version" in diagnostic_codes(payload)
    proc, payload = run_json(cli_binary, "migrate", fixture("invalid_schema_version.nwgraph"))
    assert proc.returncode != 0
    assert payload["migrated"] is False

    proc, payload = run_json(cli_binary, "migrations")
    assert proc.returncode == 0
    migration_ids = {item["id"] for item in payload["migrations"]}
    assert "nwgraph-v0-to-v1" in migration_ids


def test_profile_identity_and_hardware_metadata_independence(cli_binary: Path) -> None:
    proc, payload = run_json(cli_binary, "profile", COMMON_PROFILE, COMMON_DIGEST)
    assert proc.returncode == 0
    assert payload["valid"] is True

    graph = fixture("valid_cache.nwgraph")
    proc_a, without_gpu = run_json(cli_binary, "accept", graph)
    assert proc_a.returncode == 0, without_gpu
    assert without_gpu["accepted"] is True
    assert without_gpu["profile_id"] == COMMON_PROFILE
    assert without_gpu["requirements_digest"] == COMMON_DIGEST
    assert without_gpu["gpu_available"] is False

    proc_b, with_fake_gpu = run_json(
        cli_binary,
        "accept",
        graph,
        "--gpu-model",
        "Definitely Not A Real SKU",
        "--gpu-generation",
        "future",
        "--adapter",
        "ignored adapter description",
        "--pci-vendor",
        "65535",
        "--pci-device",
        "65535",
        "--gpu-present",
    )
    assert proc_b.returncode == 0, with_fake_gpu
    assert with_fake_gpu["accepted"] is True
    assert with_fake_gpu["gpu_available"] is True
    assert with_fake_gpu["profile_id"] == without_gpu["profile_id"]
    assert with_fake_gpu["requirements_digest"] == without_gpu["requirements_digest"]
    assert with_fake_gpu["semantic_digest"] == without_gpu["semantic_digest"]


def test_cache_invalidates_only_dependent_closure_and_recovers_corruption(
    cli_binary: Path,
) -> None:
    proc, payload = run_json(
        cli_binary,
        "cache",
        fixture("valid_cache.nwgraph"),
        "--invalidate",
        "a",
        "--corrupt",
        "unrelated_sin",
    )
    assert proc.returncode == 0, payload
    assert payload["initial"]["cache"]["misses"] == 6
    assert payload["initial"]["cache"]["hits"] == 0
    assert all(hit is False for hit in payload["initial"]["hits_by_node"].values())
    assert payload["repeat"]["cache"]["hits"] == 6
    assert all(hit is True for hit in payload["repeat"]["hits_by_node"].values())

    after_invalidate = payload["after_invalidate"]
    assert after_invalidate["hits_by_node"]["a"] is False
    assert after_invalidate["hits_by_node"]["b"] is False
    assert after_invalidate["hits_by_node"]["c"] is False
    assert after_invalidate["hits_by_node"]["d"] is False
    assert after_invalidate["hits_by_node"]["unrelated"] is True
    assert after_invalidate["hits_by_node"]["unrelated_sin"] is True
    assert after_invalidate["cache"]["misses"] == 4
    assert after_invalidate["cache"]["regenerated"] == 4

    after_corrupt = payload["after_corrupt"]
    assert after_corrupt["hits_by_node"]["unrelated_sin"] is False
    assert after_corrupt["hits_by_node"]["a"] is True
    assert after_corrupt["hits_by_node"]["unrelated"] is True
    assert after_corrupt["cache"]["misses"] == 1
    assert after_corrupt["cache"]["regenerated"] == 1


def test_invalid_edit_retains_last_valid_snapshot(cli_binary: Path) -> None:
    proc, payload = run_json(
        cli_binary,
        "last-valid",
        fixture("valid_cache.nwgraph"),
        fixture("invalid_cycle.nwgraph"),
    )
    assert proc.returncode == 0, payload
    assert payload["initial_valid"] is True
    assert payload["invalid_edit_rejected"] is True
    assert payload["retained"] is True
    assert payload["initial_digest"] == payload["after_invalid_digest"]
    assert "ordinary_cycle" in diagnostic_codes(payload)


def test_export_json_contains_separate_editor_layout(cli_binary: Path) -> None:
    proc, payload = run_json(cli_binary, "export-json", fixture("valid_cache.nwgraph"))
    assert proc.returncode == 0, payload
    assert payload["textual_schema"] == "nwgraph-v1"
    assert payload["schema_version"] == 1
    assert payload["profile"]["id"] == COMMON_PROFILE
    assert payload["profile"]["requirements_digest"] == COMMON_DIGEST
    assert payload["compiler_settings"]
    assert payload["nodes"]
    assert payload["editor_layout"]
    schema_path = REPO_ROOT / "schemas" / "nwgraph-v1.schema.json"
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["properties"]["schema_version"]["const"] == 1
    assert "editor_layout" in schema["properties"]


def test_graph_sources_have_no_d3d12_or_player_plugin_abi() -> None:
    graph_sources = [
        REPO_ROOT / "src" / "graph" / "nw_graph.h",
        REPO_ROOT / "src" / "graph" / "nw_graph.cpp",
        REPO_ROOT / "src" / "graph" / "nw_graph_cli.cpp",
    ]
    for path in graph_sources:
        text = path.read_text(encoding="utf-8")
        code = re.sub(r"//.*", "", text)
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
        lowered = code.lower()
        assert "d3d12.h" not in lowered
        assert "dxgi" not in lowered
        assert "id3d12" not in lowered
        assert "loadlibrary" not in lowered
        assert "dlopen" not in lowered
        assert "src/player" not in lowered

    player_sources = list((REPO_ROOT / "src" / "player").glob("*"))
    for path in player_sources:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "nw_graph.h" not in text

    cmake = (REPO_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "add_library(nw_graph STATIC" in cmake
    assert "add_executable(nw_graph_cli" in cmake
    assert "nw_apply_player_profile(nw_graph" not in cmake


def test_positive_frequency_and_residency_fixtures(cli_binary: Path) -> None:
    proc, payload = run_json(cli_binary, "validate", fixture("valid_frequencies.nwgraph"))
    assert proc.returncode == 0, payload
    assert payload["valid"] is True
    proc, evaluation = run_json(cli_binary, "eval", fixture("valid_frequencies.nwgraph"))
    assert proc.returncode == 0, evaluation
    assert evaluation["ok"] is True

    proc, payload = run_json(cli_binary, "validate", fixture("valid_typed_pipeline.nwgraph"))
    assert proc.returncode == 0, payload
    assert payload["valid"] is True
