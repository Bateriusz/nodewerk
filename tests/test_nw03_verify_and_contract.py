from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_verify_nw03_contracts_only_entry_point(tmp_path: Path) -> None:
    summary_path = tmp_path / "pytest-nw03-contracts.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "verify_nw03.py"),
            "--mode",
            "ContractsOnly",
            "--summary-json",
            str(summary_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["passed"] is True
    names = {check["name"]: check for check in summary["checks"]}
    for name in (
        "fixture_oracle_and_lowering",
        "package_roundtrip_and_malformed",
        "release_leakage",
        "python_bounded_package_fuzz",
    ):
        assert names[name]["passed"] is True, names[name]


def test_nwp_documentation_and_schema_contract() -> None:
    docs = (REPO_ROOT / "docs" / "nwp-format-v1.md").read_text(encoding="utf-8")
    for required in (
        "little-endian",
        "CAP ",
        "RES ",
        "INIT",
        "FRAM",
        "PARM",
        "SHDR",
        "RSIG",
        "integrity checksum",
        "sandbox for hostile",
        "does **not** establish",
        "authenticity",
        "compute.math_noise",
        "b0",
        "t0",
        "u0",
        "source maps",
        "reflection",
        "DXC",
        "Slang",
        "compression",
        "Archival",
    ):
        assert required in docs, required

    contract = (
        REPO_ROOT / "docs" / "nw03-compiler-player-contract.md"
    ).read_text(encoding="utf-8")
    for required in (
        "validate",
        "compile-package",
        "inspect",
        "atomically",
        "pinned DXC",
        "1.8.2407",
        "Source-linked",
        "tolerance",
        "cache",
        "verify_nw03.py --mode Full",
    ):
        assert required in contract, required

    schema = json.loads(
        (REPO_ROOT / "schemas" / "nwp-v1.schema.json").read_text(encoding="utf-8")
    )
    assert schema["properties"]["format"]["const"] == "nwp"
    assert schema["properties"]["version"]["properties"]["major"]["const"] == 1
    assert (
        schema["properties"]["checksum"]["properties"]["purpose"]["const"]
        == "corruption_detection_not_authenticity"
    )


def test_cmake_and_architecture_expose_nw03_targets() -> None:
    cmake = (REPO_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "src/package/nwp_format.cpp" in cmake
    assert "src/compiler/nw_compiler.cpp" in cmake
    assert "src/compiler/nodewerkc_main.cpp" in cmake
    assert "add_library(nw_package" in cmake
    assert "add_library(nw_compiler" in cmake
    assert "add_executable(nodewerkc" in cmake

    capability_header = (REPO_ROOT / "src" / "player" / "capability_check.h").read_text(
        encoding="utf-8"
    )
    assert "kAllowRuntimeShaderCompilation = false" in capability_header
    for source in (REPO_ROOT / "src" / "player").glob("*.h"):
        text = source.read_text(encoding="utf-8", errors="ignore")
        assert "dxcompiler.dll" not in text or "forbidden" in text or "never" in text


def test_no_runtime_graph_parser_in_player_sources() -> None:
    player_sources = list((REPO_ROOT / "src" / "player").glob("*"))
    for path in player_sources:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "nw_graph.h" not in text
        assert "nwgraph" not in text


def test_no_runtime_dxc_or_slang_dependency_in_player_sources() -> None:
    for path in (REPO_ROOT / "src" / "player").glob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for forbidden_call in (
            "LoadLibraryA(\"dxcompiler",
            "LoadLibraryW(L\"dxcompiler",
            "LoadLibraryA(\"slang",
            "LoadLibraryW(L\"slang",
            "D3DCompile(",
        ):
            assert forbidden_call not in text, (path.name, forbidden_call)


def test_capability_validation_does_not_branch_on_gpu_marketing_metadata() -> None:
    package_source = (REPO_ROOT / "tools" / "nwp_package.py").read_text(encoding="utf-8")
    compiler_source = (REPO_ROOT / "tools" / "nw03_compiler.py").read_text(
        encoding="utf-8"
    )
    for source in (package_source, compiler_source):
        for forbidden in (
            "gpu_model ==",
            "gpu_generation ==",
            "adapter_description",
            "pci_vendor_id",
            "pci_device_id",
            "coverage_tier",
            "RTX 4060",
            "RTX 50",
        ):
            assert forbidden not in source

    fixture = (REPO_ROOT / "fixtures" / "nw03" / "math_noise.nwgraph").read_text(
        encoding="utf-8"
    )
    assert "gpu_model" not in fixture
    assert "pci" not in fixture.lower()


def test_fixture_contains_math_noise_oracle_and_optional_profile_cases() -> None:
    text = (REPO_ROOT / "fixtures" / "nw03" / "math_noise.nwgraph").read_text(
        encoding="utf-8"
    )
    assert "nw.shader.expr" in text
    assert "nw.proc.value_noise" in text
    assert "fullscreen_a" in text
    assert "unused_expr" in text
