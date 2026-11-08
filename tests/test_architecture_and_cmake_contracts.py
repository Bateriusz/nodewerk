from __future__ import annotations

import json
import subprocess
from pathlib import Path


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
