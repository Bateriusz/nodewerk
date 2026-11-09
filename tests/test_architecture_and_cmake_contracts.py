from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_reference_documentation_inspected_and_preserved() -> None:
    ref_readme = REPO_ROOT / "references" / "README.md"
    assert ref_readme.exists(), "Tracked reference README must exist"
    content = ref_readme.read_text(encoding="utf-8")
    assert "Farbrausch" in content
    assert "not intended to be a port, fork, or compatibility layer" in content

    # Verify references/ directory has no local modifications
    proc = subprocess.run(
        ["git", "status", "--porcelain", "references"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "", f"Forbidden modifications in references/: {proc.stdout}"


def test_architecture_records_complete_and_indexed() -> None:
    docs_readme = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    expected_docs = [
        "docs/architecture/0001-three-product-architecture.md",
        "docs/architecture/0002-platform-and-d3d12-baseline.md",
        "docs/architecture/0003-language-and-toolchain-selection.md",
        "docs/architecture/0004-dependency-closure-and-crt-experiment.md",
        "docs/architecture/0005-task-contracts-nw02-nw12.md",
        "docs/dependency-inventory.md",
        "docs/exit-codes-and-diagnostics.md",
        "docs/experiments-and-blockers.md",
    ]
    for rel_path in expected_docs:
        full_path = REPO_ROOT / rel_path
        assert full_path.exists(), f"Missing architecture/baseline doc: {rel_path}"
        assert full_path.stat().st_size > 500, f"Doc {rel_path} is unexpectedly short"
        rel_from_docs = rel_path.removeprefix("docs/")
        assert rel_from_docs in docs_readme, f"{rel_from_docs} must be indexed in docs/README.md"

    adr1 = (REPO_ROOT / "docs/architecture/0001-three-product-architecture.md").read_text(encoding="utf-8")
    assert "nodewerk-studio" in adr1
    assert "nodewerkc" in adr1
    assert "nodewerk-player" in adr1
    assert "Optimized Expressions" in adr1
    assert "Scheduled GPU Passes" in adr1
    assert "Generated Player Link Manifest" in adr1
    assert "Rendering Hardware Interface (RHI)" in adr1
    assert "NW-10" in adr1

    adr2 = (REPO_ROOT / "docs/architecture/0002-platform-and-d3d12-baseline.md").read_text(encoding="utf-8")
    assert "D3D_FEATURE_LEVEL_12_0" in adr2
    assert "6.0" in adr2
    assert "ResourceDescriptorHeap" in adr2
    assert "EnhancedBarriersSupported" in adr2
    assert "D3D12_MESH_SHADER_TIER" in adr2
    assert "D3D12_RAYTRACING_TIER" in adr2
    assert "RTX 4060" in adr2
    assert "RTX 5070 Ti" in adr2

    adr3 = (REPO_ROOT / "docs/architecture/0003-language-and-toolchain-selection.md").read_text(encoding="utf-8")
    assert "C++20" in adr3
    assert "Rust" in adr3 and "Zig" in adr3
    assert "clang-cl" in adr3 and "lld-link" in adr3 and "LTCG" in adr3

    adr5 = (REPO_ROOT / "docs/architecture/0005-task-contracts-nw02-nw12.md").read_text(encoding="utf-8")
    for task_num in range(2, 13):
        task_id = f"NW-{task_num:02d}"
        assert task_id in adr5, f"Downstream contract missing for {task_id}"


def test_cmake_profiles_and_presets_contract() -> None:
    presets = json.loads((REPO_ROOT / "CMakePresets.json").read_text(encoding="utf-8"))
    preset_names = {p["name"]: p for p in presets["configurePresets"]}
    required_presets = {
        "clang-cl-dev": "dev",
        "clang-cl-release": "release",
        "clang-cl-compact-experiment": "compact_experiment",
        "msvc-dev": "dev",
        "msvc-release-ltcg": "release",
        "msvc-compact-experiment": "compact_experiment",
    }
    for name, expected_profile in required_presets.items():
        assert name in preset_names, f"Missing CMake configure preset '{name}'"
        preset = preset_names[name]
        assert preset["generator"] == "Ninja"
        assert preset["cacheVariables"]["NW_BUILD_PROFILE"] == expected_profile
        assert preset["cacheVariables"]["NW_ENABLE_CRT_ELIMINATION_EXPERIMENT"] == "OFF"

    build_profiles_cmake = (REPO_ROOT / "cmake" / "BuildProfiles.cmake").read_text(encoding="utf-8")
    # Exceptions and RTTI disabled in player builds
    assert "/EHs-c-" in build_profiles_cmake
    assert "_HAS_EXCEPTIONS=0" in build_profiles_cmake
    assert "/GR-" in build_profiles_cmake
    # Platform security mitigations retained
    for flag in ("/GS", "/guard:cf", "/DYNAMICBASE", "/HIGHENTROPYVA", "/NXCOMPAT", "/CETCOMPAT"):
        assert flag in build_profiles_cmake, f"Missing required security mitigation flag {flag}"
    # Reproducibility flags
    assert "/Brepro" in build_profiles_cmake
    assert "/PDBALTPATH:%_PDB%" in build_profiles_cmake
    assert "-ffile-prefix-map=${CMAKE_SOURCE_DIR}=." in build_profiles_cmake
    # CRT elimination strictly gated behind NW_ENABLE_CRT_ELIMINATION_EXPERIMENT
    assert "if(NW_ENABLE_CRT_ELIMINATION_EXPERIMENT)" in build_profiles_cmake
    assert "/NODEFAULTLIB" in build_profiles_cmake
    assert "if(NW_ENABLE_UNSAFE_SECTION_MERGE_EXPERIMENT)" in build_profiles_cmake

    # ToolchainManifest.cmake must actively verify compiler, linker, SDK, DXC, CMake, and Ninja versions
    toolchain_cmake = (REPO_ROOT / "cmake" / "ToolchainManifest.cmake").read_text(encoding="utf-8")
    assert "function(nw_verify_toolchain_manifest)" in toolchain_cmake
    assert "nw_verify_toolchain_manifest()" in toolchain_cmake
    assert "NW_PINNED_CMAKE_VERSION" in toolchain_cmake
    assert "NW_PINNED_NINJA_VERSION" in toolchain_cmake
    assert "NW_PINNED_CLANG_VERSION" in toolchain_cmake
    assert "NW_PINNED_MSVC_VERSION" in toolchain_cmake
    assert "NW_PINNED_WINDOWS_SDK_VERSION" in toolchain_cmake
    assert "NW_WINDOWS_SDK_VERSION" in toolchain_cmake
    assert "NW_WINDOWS_SDK_ROOT" in toolchain_cmake
    assert "KitsRoot10" in toolchain_cmake
    assert "NW_PINNED_DXC_VERSION" in toolchain_cmake


def test_toolchain_verifier_has_no_independent_version_literals() -> None:
    manifest = json.loads(
        (REPO_ROOT / "benchmarks" / "toolchain-manifest.v1.json").read_text(encoding="utf-8")
    )
    toolchain_cmake = (REPO_ROOT / "cmake" / "ToolchainManifest.cmake").read_text(encoding="utf-8")

    # The repository manifest is the only authoritative version contract.
    assert 'file(READ "${NW_TOOLCHAIN_MANIFEST_JSON}"' in toolchain_cmake
    for key in ("cmake_version", "ninja_version", "windows_sdk_version", "dxc_version"):
        assert f"pinned_dependencies {key}" in toolchain_cmake
    for key in ("evaluated_toolchains", "compiler_version", "linker_version"):
        assert key in toolchain_cmake
    assert "FORCE" in toolchain_cmake

    evaluated = {
        entry["toolchain_id"]: entry for entry in manifest["evaluated_toolchains"]
    }
    literal_pins = {
        "cmake_version": manifest["pinned_dependencies"]["cmake_version"],
        "ninja_version": manifest["pinned_dependencies"]["ninja_version"],
        "windows_sdk_version": manifest["pinned_dependencies"]["windows_sdk_version"],
        "dxc_version": manifest["pinned_dependencies"]["dxc_version"],
        "clang compiler_version": evaluated["clang-cl-lld"]["compiler_version"],
        "msvc compiler_version": evaluated["msvc-ltcg"]["compiler_version"],
        "msvc linker_version": evaluated["msvc-ltcg"]["linker_version"],
    }
    for name, value in literal_pins.items():
        assert value not in toolchain_cmake, (
            f"{name} pin '{value}' is duplicated as a literal in "
            "cmake/ToolchainManifest.cmake; the repository manifest must be the only source"
        )


def test_cmake_downstream_pins_follow_manifest_changes(tmp_path: Path) -> None:
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("cmake is required to exercise downstream pin propagation")

    repo = tmp_path / "pin repo"
    (repo / "benchmarks").mkdir(parents=True)
    (repo / "cmake").mkdir()
    shutil.copy2(
        REPO_ROOT / "cmake" / "ToolchainManifest.cmake",
        repo / "cmake" / "ToolchainManifest.cmake",
    )
    for name in (
        "toolchain-manifest.v1.json",
        "capability-manifest.v1.json",
        "dependency-inventory.v1.json",
    ):
        shutil.copy2(REPO_ROOT / "benchmarks" / name, repo / "benchmarks" / name)

    manifest_path = repo / "benchmarks" / "toolchain-manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pinned_dependencies"].update(
        {
            "cmake_version": "9.9.901",
            "ninja_version": "9.9.902",
            "windows_sdk_version": "10.0.99999.0",
            "dxc_version": "9.9.2407",
        }
    )
    for entry in manifest["evaluated_toolchains"]:
        if entry["toolchain_id"] == "clang-cl-lld":
            entry["compiler_version"] = "9.9.903"
            entry["linker_version"] = "9.9.903"
        elif entry["toolchain_id"] == "msvc-ltcg":
            entry["compiler_version"] = "19.99.33821"
            entry["linker_version"] = "14.99.33821.0"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    driver = tmp_path / "load-pins.cmake"
    driver.write_text(
        textwrap.dedent(
            f"""
            set(NW_ENFORCE_PINNED_TOOLCHAIN OFF CACHE BOOL "" FORCE)
            include("{repo.as_posix()}/cmake/ToolchainManifest.cmake")
            message(STATUS "NWPIN_CMAKE=${{NW_PINNED_CMAKE_VERSION}}")
            message(STATUS "NWPIN_NINJA=${{NW_PINNED_NINJA_VERSION}}")
            message(STATUS "NWPIN_CLANG=${{NW_PINNED_CLANG_VERSION}}")
            message(STATUS "NWPIN_MSVC=${{NW_PINNED_MSVC_VERSION}}")
            message(STATUS "NWPIN_MSVC_LINKER=${{NW_PINNED_MSVC_LINKER_VERSION}}")
            message(STATUS "NWPIN_SDK=${{NW_PINNED_WINDOWS_SDK_VERSION}}")
            message(STATUS "NWPIN_DXC=${{NW_PINNED_DXC_VERSION}}")
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [cmake, "-P", str(driver)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    expected = {
        "NWPIN_CMAKE": "9.9.901",
        "NWPIN_NINJA": "9.9.902",
        "NWPIN_CLANG": "9.9.903",
        "NWPIN_MSVC": "19.99.33821",
        "NWPIN_MSVC_LINKER": "14.99.33821.0",
        "NWPIN_SDK": "10.0.99999.0",
        "NWPIN_DXC": "9.9.2407",
    }
    for name, value in expected.items():
        assert f"{name}={value}" in output, output
