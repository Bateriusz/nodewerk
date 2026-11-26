cmake_minimum_required(VERSION 3.28)
project(nodewerk VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/ToolchainManifest.cmake")
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/BuildProfiles.cmake")
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/CompileShaders.cmake")
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/DependencyCheck.cmake")

set(NW05_GENERATED_PLAYER_SOURCE "" CACHE FILEPATH
    "Generated NW-05 player main to compile into the specialized executable")
option(NW05_COMPILE_WAVE_OPS_QUERY "Compile D3D12_OPTIONS1 wave-ops query support" ON)
option(NW05_FROZEN_EMBEDDED_SPECIALIZATION
    "Specialize the generated player for a build-time-hashed embedded .nwp payload" OFF)
option(NW05_COMPILE_INT64_SHADER_OPS_QUERY "Compile D3D12_OPTIONS1 int64 query support" ON)
option(NW05_COMPILE_FP16_SHADER_OPS_QUERY "Compile D3D12_OPTIONS4 fp16 query support" ON)
option(NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD "Compile procedural texture upload implementation" ON)
option(NW05_COMPILE_PROCEDURAL_TEXTURE_R8 "Compile R8G8B8A8 procedural texture materialization" ON)
option(NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F "Compile RGBA32F procedural texture materialization" ON)
option(NW06_COMPILE_GPU_INITIALIZATION "Compile GPU initialization dispatches" ON)
option(NW06_COMPILE_INDEXED_INSTANCED_INDIRECT "Compile indexed instanced indirect raster path" ON)
option(NW06_COMPILE_ANALYTIC_LIGHTING_TWIST "Compile analytic lighting/twist path" ON)
option(NW06_COMPILE_ABSOLUTE_TICK_CAMERA "Compile absolute-tick camera path" ON)
option(NW06_COMPILE_HALF_RESOLUTION_BLOOM "Compile half-resolution bloom path" ON)
option(NW06_COMPILE_TONEMAP "Compile tonemap path" ON)
option(NW10_COMPACT_PACKAGE
    "Compile the opt-in NWC1 compact-package decoder into the player closure"
    OFF)
option(NW10_BUILD_DECODER_TESTS
    "Build the shipped compact-decoder fuzz and parity harness"
    OFF)

set(NW_COMPACT_PACKAGE_SOURCES)
if(NW10_COMPACT_PACKAGE)
    list(APPEND NW_COMPACT_PACKAGE_SOURCES src/package/nw_compact.cpp)
endif()

if(WIN32)
nw_add_offline_compute_shader(
    nw_offline_shaders
    "${CMAKE_CURRENT_SOURCE_DIR}/shaders/compute_fixture.hlsl"
    "CSMain"
    "cs_6_0"
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/compute_fixture.dxil"
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated/compute_fixture_dxil.h"
)

# NW-04 player-owned fullscreen display pass.  It is compiled offline by the
# same pinned DXC as the compute fixture and embedded into the player, so the
# release binary never needs a runtime shader compiler.
nw_add_offline_compute_shader(
    nw_offline_display_shader
    "${CMAKE_CURRENT_SOURCE_DIR}/shaders/display_fixture.hlsl"
    "DisplayCS"
    "cs_6_0"
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/display_fixture.dxil"
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated/display_fixture_dxil.h"
    "kDisplayFixtureDxil"
)

# NW-06 fixed procedural generation/raster/post shaders.  They are compiled by
# the same pinned offline DXC as the rest of the player payloads; the exporter
# also compiles the exact same sources into the emitted .nwp package.  Runtime
# source compilation is never used.
foreach(_nw06_shader
    "nw06_generate_cs:GenerateGeometryCS:cs_6_0:kNw06GenerateDxil"
    "nw06_instances_cs:GenerateInstancesCS:cs_6_0:kNw06InstancesDxil"
    "nw06_material_cs:MaterialCS:cs_6_0:kNw06MaterialDxil"
    "nw06_camera_cs:CameraDataCS:cs_6_0:kNw06CameraDxil"
    "nw06_light_cs:LightDataCS:cs_6_0:kNw06LightDxil"
    "nw06_scene_vs:SceneVS:vs_6_0:kNw06SceneVsDxil"
    "nw06_scene_ps:ScenePS:ps_6_0:kNw06ScenePsDxil"
    "nw06_bloom_cs:BloomCS:cs_6_0:kNw06BloomDxil"
    "nw06_tonemap_cs:TonemapCS:cs_6_0:kNw06TonemapDxil")
    string(REPLACE ":" ";" _nw06_parts "${_nw06_shader}")
    list(GET _nw06_parts 0 _nw06_name)
    list(GET _nw06_parts 1 _nw06_entry)
    list(GET _nw06_parts 2 _nw06_target)
    list(GET _nw06_parts 3 _nw06_symbol)
    nw_add_offline_compute_shader(
        "nw_offline_${_nw06_name}"
        "${CMAKE_CURRENT_SOURCE_DIR}/shaders/nw06/${_nw06_name}.hlsl"
        "${_nw06_entry}"
        "${_nw06_target}"
        "${CMAKE_CURRENT_BINARY_DIR}/shaders/${_nw06_name}.dxil"
        "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated/${_nw06_name}_dxil.h"
        "${_nw06_symbol}"
    )
    add_dependencies(nw_offline_shaders "nw_offline_${_nw06_name}")
endforeach()
else()
    # Non-Windows configuration does not need offline D3D12 payloads.
    add_custom_target(nw_offline_shaders)
    add_custom_target(nw_offline_display_shader)
endif()

set(NW_PLAYER_BOOTSTRAP_SOURCES
    ${NW_COMPACT_PACKAGE_SOURCES}
    src/player/diagnostics.cpp
    src/player/capability_check.cpp
    src/player/d3d12_bootstrap.cpp
    src/player/capability_requirements.cpp
    src/player/package_loader.cpp
    src/player/embedded_package.cpp
    src/player/resource_plan.cpp
    src/player/player_instrumentation.cpp
    src/player/package_player.cpp
    src/player/player.cpp
    src/platform/win32_window.cpp
)

add_library(nw_player_bootstrap STATIC
    ${NW_PLAYER_BOOTSTRAP_SOURCES}
)

if(NOT WIN32)
    # Focused portability gate for NW-05 PackagePlayer. This object target
    # compiles package_player.cpp without pulling in Windows-only bootstrap
    # fixtures, so CI directly catches accidental non-Windows references such
    # as InitialUploadBytes while leaving unrelated NW-01 portability debt out
    # of the NW-05 acceptance path.
    add_library(nw_package_player_host_compile OBJECT
        src/player/package_player.cpp
    )
    target_include_directories(nw_package_player_host_compile PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/src"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/player"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/platform"
    )
    nw_apply_player_profile(nw_package_player_host_compile)
    # Compile the same shader-only specialization that the real-PE NW-05
    # export uses. This catches dead-code/warning regressions in implementations
    # that are intentionally removed from the specialized runtime.
    target_compile_definitions(nw_package_player_host_compile PRIVATE
        NW05_FROZEN_EMBEDDED_SPECIALIZATION=1
        NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD=0
        NW05_COMPILE_PROCEDURAL_TEXTURE_R8=0
        NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F=0
        NW06_COMPILE_GPU_INITIALIZATION=1
        NW06_COMPILE_INDEXED_INSTANCED_INDIRECT=1
        NW06_COMPILE_ANALYTIC_LIGHTING_TWIST=1
        NW06_COMPILE_ABSOLUTE_TICK_CAMERA=1
        NW06_COMPILE_HALF_RESOLUTION_BLOOM=1
        NW06_COMPILE_TONEMAP=1
    )

    add_library(nw_package_format_frozen_host_compile OBJECT
        src/package/nw_compact.cpp
        src/package/nwp_format.cpp
    )
    target_include_directories(nw_package_format_frozen_host_compile PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/src"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/package"
    )
    nw_apply_player_profile(nw_package_format_frozen_host_compile)
    target_compile_definitions(nw_package_format_frozen_host_compile PRIVATE
        NW05_FROZEN_EMBEDDED_SPECIALIZATION=1
    )
    target_compile_options(nw_package_format_frozen_host_compile PRIVATE -w)

    add_custom_target(nw07_frozen_host_compile
        DEPENDS
            nw_package_player_host_compile
            nw_package_format_frozen_host_compile
    )
endif()

if(NW10_BUILD_DECODER_TESTS)
    add_executable(nw10_compact_decoder_fuzz
        tests/nw10_compact_decoder_fuzz.cpp
        src/package/nw_compact.cpp
        src/package/nwp_format.cpp
    )
    target_include_directories(nw10_compact_decoder_fuzz PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/src"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/package"
    )
    nw_apply_player_profile(nw10_compact_decoder_fuzz)
    if(WIN32)
        target_link_libraries(nw10_compact_decoder_fuzz PRIVATE cabinet)
    endif()
endif()

target_include_directories(nw_player_bootstrap PUBLIC
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated"
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/player"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/platform"
)
nw_apply_player_profile(nw_player_bootstrap)
target_compile_definitions(nw_player_bootstrap PRIVATE
    NW10_COMPACT_PACKAGE=$<BOOL:${NW10_COMPACT_PACKAGE}>
)
add_dependencies(nw_player_bootstrap nw_offline_shaders)
add_dependencies(nw_player_bootstrap nw_offline_display_shader)

if(WIN32)
    target_link_libraries(nw_player_bootstrap PUBLIC
        d3d12
        dxgi
        user32
        kernel32
    )
    if(NW10_COMPACT_PACKAGE)
        target_link_libraries(nw_player_bootstrap PUBLIC cabinet)
    endif()
endif()

add_executable(nw_empty_player
    fixtures/empty_player/main.cpp
)
target_link_libraries(nw_empty_player PRIVATE nw_player_bootstrap)
nw_apply_player_profile(nw_empty_player)
nw_attach_dependency_check(nw_empty_player "empty_player")

add_executable(nw_clear_present_fixture
    fixtures/clear_present/main.cpp
)
target_link_libraries(nw_clear_present_fixture PRIVATE nw_player_bootstrap)
nw_apply_player_profile(nw_clear_present_fixture)
nw_attach_dependency_check(nw_clear_present_fixture "clear_present")

add_executable(nw_compute_shader_fixture
    fixtures/compute_shader/main.cpp
)
target_link_libraries(nw_compute_shader_fixture PRIVATE nw_player_bootstrap)
nw_apply_player_profile(nw_compute_shader_fixture)
nw_attach_dependency_check(nw_compute_shader_fixture "compute_shader")

# NW-02: authoring/compiler-side typed operator graph. This target is portable
# and deliberately has no D3D12/DXGI or player dependency.
add_library(nw_graph STATIC
    src/graph/nw_graph.cpp
)
target_include_directories(nw_graph PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src/graph"
)
target_compile_features(nw_graph PUBLIC cxx_std_20)

add_executable(nw_graph_cli
    src/graph/nw_graph_cli.cpp
)
target_link_libraries(nw_graph_cli PRIVATE nw_graph)
target_compile_features(nw_graph_cli PRIVATE cxx_std_20)

# NW-03: compiler-to-player `.nwp` v1 package contract, compiler-side
# expression lowering, and the `nodewerkc` command frontend.  These targets are
# authoring/compiler-side and never linked into the player.
set(NW_NATIVE_TOOL_EXCLUDE_FROM_ALL "")
if(WIN32)
    # The NW-01 Windows concrete-build/reproducibility gate builds the default
    # player/fixture target set.  Native authoring tools remain available via
    # `cmake --build <dir> --target nodewerkc`, but are not a prerequisite for
    # that player-only gate.
    set(NW_NATIVE_TOOL_EXCLUDE_FROM_ALL EXCLUDE_FROM_ALL)
endif()

add_library(nw_package STATIC ${NW_NATIVE_TOOL_EXCLUDE_FROM_ALL}
    ${NW_COMPACT_PACKAGE_SOURCES}
    src/package/nwp_format.cpp
)
target_include_directories(nw_package PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src/package"
)
target_compile_features(nw_package PUBLIC cxx_std_20)

# The player consumes the same immutable `.nwp` v1 implementation with the
# player build profile (same CRT, exception/RTTI, NDEBUG, and reproducibility
# flags as the bootstrap archive).  Authoring-side warning policy is disabled
# for this archive only; NW-03's package implementation is not player code.
add_library(nw_package_player STATIC
    ${NW_COMPACT_PACKAGE_SOURCES}
    src/package/nwp_format.cpp
)
target_include_directories(nw_package_player PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/package"
)
target_compile_features(nw_package_player PUBLIC cxx_std_20)
nw_apply_player_profile(nw_package_player)
target_compile_definitions(nw_package_player PRIVATE
    NW10_COMPACT_PACKAGE=$<BOOL:${NW10_COMPACT_PACKAGE}>
)
if(NW05_FROZEN_EMBEDDED_SPECIALIZATION)
    target_compile_definitions(nw_package_player PRIVATE
        NW05_FROZEN_EMBEDDED_SPECIALIZATION=1
    )
endif()
if(MSVC)
    target_compile_options(nw_package_player PRIVATE /W0)
else()
    target_compile_options(nw_package_player PRIVATE -w)
endif()

# Fully validating external-package decoder for diagnostic fixtures.  The
# frozen release decoder intentionally substitutes the canonical-HD CAP limits
# and omits CAP decoding for the exact-SHA generated artifact.  The fixture
# loads arbitrary presentation sizes, so it must keep the full decoder while
# still linking the frozen player implementation.
add_library(nw_package_player_diagnostic STATIC
    ${NW_COMPACT_PACKAGE_SOURCES}
    src/package/nwp_format.cpp
)
target_include_directories(nw_package_player_diagnostic PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/package"
)
target_compile_features(nw_package_player_diagnostic PUBLIC cxx_std_20)
nw_apply_player_profile(nw_package_player_diagnostic)
target_compile_definitions(nw_package_player_diagnostic PRIVATE
    NW10_COMPACT_PACKAGE=$<BOOL:${NW10_COMPACT_PACKAGE}>
)
target_compile_definitions(nw_package_player_diagnostic PRIVATE
    NW05_FROZEN_EMBEDDED_SPECIALIZATION=0
)
if(MSVC)
    target_compile_options(nw_package_player_diagnostic PRIVATE /W0)
else()
    target_compile_options(nw_package_player_diagnostic PRIVATE -w)
endif()

# Host-only NW-06 contract check: decodes an exported common package and runs
# the exact native capability derivation/validation used by PackagePlayer
# initialization.  It has no D3D12 dependency and is built on every platform.
add_executable(nw06_capability_check
    fixtures/nw06_capability/main.cpp
    src/player/capability_requirements.cpp
)
target_link_libraries(nw06_capability_check PRIVATE nw_package_player)
target_include_directories(nw06_capability_check PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/player"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/package"
)
target_compile_features(nw06_capability_check PRIVATE cxx_std_20)
# Link with the same player CRT/profile as nw_package_player.  Without this,
# MSVC builds the checker's main object as /MD while the package library is
# /MT and lld-link reports a RuntimeLibrary /failifmismatch error.
nw_apply_player_profile(nw06_capability_check)

# Build the frozen NW-05 player closure used by generated artifacts and the
# frozen diagnostic fixture.
# Generic release/NW-04 targets retain NW_ENABLE_D3D12_VALIDATION=0; this
# library alone carries the real debug-layer qualification path and only the
# optional feature-query code retained by the generated manifest.
#
# The frozen PackagePlayer fixture links this exact library so its
# `NW05_FROZEN_EMBEDDED_SPECIALIZATION` frame-plan branch is the implementation
# that actually executes.  Defining the macro only on a generic bootstrap
# archive or on a fixture main would leave both fixture modes running the
# generic planner.
if(NW05_GENERATED_PLAYER_SOURCE OR NW05_FROZEN_EMBEDDED_SPECIALIZATION)
    add_library(nw05_player_bootstrap STATIC
        ${NW_PLAYER_BOOTSTRAP_SOURCES}
    )
    target_include_directories(nw05_player_bootstrap PUBLIC
        "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated"
        "${CMAKE_CURRENT_SOURCE_DIR}/src"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/player"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/platform"
    )
    nw_apply_player_profile(nw05_player_bootstrap)
    target_compile_definitions(nw05_player_bootstrap PRIVATE
        NW10_COMPACT_PACKAGE=$<BOOL:${NW10_COMPACT_PACKAGE}>
        NW05_SPECIALIZED_VALIDATION=1
        NW05_FROZEN_EMBEDDED_SPECIALIZATION=$<BOOL:${NW05_FROZEN_EMBEDDED_SPECIALIZATION}>
        NW05_COMPILE_WAVE_OPS_QUERY=$<BOOL:${NW05_COMPILE_WAVE_OPS_QUERY}>
        NW05_COMPILE_INT64_SHADER_OPS_QUERY=$<BOOL:${NW05_COMPILE_INT64_SHADER_OPS_QUERY}>
        NW05_COMPILE_FP16_SHADER_OPS_QUERY=$<BOOL:${NW05_COMPILE_FP16_SHADER_OPS_QUERY}>
        NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD=$<BOOL:${NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD}>
        NW05_COMPILE_PROCEDURAL_TEXTURE_R8=$<BOOL:${NW05_COMPILE_PROCEDURAL_TEXTURE_R8}>
        NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F=$<BOOL:${NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F}>
        NW06_COMPILE_GPU_INITIALIZATION=$<BOOL:${NW06_COMPILE_GPU_INITIALIZATION}>
        NW06_COMPILE_INDEXED_INSTANCED_INDIRECT=$<BOOL:${NW06_COMPILE_INDEXED_INSTANCED_INDIRECT}>
        NW06_COMPILE_ANALYTIC_LIGHTING_TWIST=$<BOOL:${NW06_COMPILE_ANALYTIC_LIGHTING_TWIST}>
        NW06_COMPILE_ABSOLUTE_TICK_CAMERA=$<BOOL:${NW06_COMPILE_ABSOLUTE_TICK_CAMERA}>
        NW06_COMPILE_HALF_RESOLUTION_BLOOM=$<BOOL:${NW06_COMPILE_HALF_RESOLUTION_BLOOM}>
        NW06_COMPILE_TONEMAP=$<BOOL:${NW06_COMPILE_TONEMAP}>
    )
    add_dependencies(nw05_player_bootstrap nw_offline_shaders)
    add_dependencies(nw05_player_bootstrap nw_offline_display_shader)
    if(WIN32)
        target_link_libraries(nw05_player_bootstrap PUBLIC
            d3d12
            dxgi
            user32
            kernel32
        )
        if(NW10_COMPACT_PACKAGE)
            target_link_libraries(nw05_player_bootstrap PUBLIC cabinet)
        endif()
    endif()

    # The frozen diagnostic fixture links the full external-package decoder.
    # Give its player bootstrap the same full NW-09 Package model ABI while
    # preserving the size-specialized Package layout in the real release
    # bootstrap above. This avoids an ODR/ABI mismatch without charging the
    # 256 KiB generated player for History/checkpoint containers.
    if(NW05_FROZEN_EMBEDDED_SPECIALIZATION)
        add_library(nw05_player_bootstrap_diagnostic STATIC
            ${NW_PLAYER_BOOTSTRAP_SOURCES}
        )
        target_include_directories(nw05_player_bootstrap_diagnostic PUBLIC
            "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated"
            "${CMAKE_CURRENT_SOURCE_DIR}/src"
            "${CMAKE_CURRENT_SOURCE_DIR}/src/player"
            "${CMAKE_CURRENT_SOURCE_DIR}/src/platform"
        )
        nw_apply_player_profile(nw05_player_bootstrap_diagnostic)
        target_compile_definitions(nw05_player_bootstrap_diagnostic PRIVATE
            NW10_COMPACT_PACKAGE=$<BOOL:${NW10_COMPACT_PACKAGE}>
            NW09_FORCE_STATEFUL_PACKAGE_MODEL=1
            NW05_SPECIALIZED_VALIDATION=1
            NW05_FROZEN_EMBEDDED_SPECIALIZATION=1
            NW05_COMPILE_WAVE_OPS_QUERY=$<BOOL:${NW05_COMPILE_WAVE_OPS_QUERY}>
            NW05_COMPILE_INT64_SHADER_OPS_QUERY=$<BOOL:${NW05_COMPILE_INT64_SHADER_OPS_QUERY}>
            NW05_COMPILE_FP16_SHADER_OPS_QUERY=$<BOOL:${NW05_COMPILE_FP16_SHADER_OPS_QUERY}>
            NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD=$<BOOL:${NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD}>
            NW05_COMPILE_PROCEDURAL_TEXTURE_R8=$<BOOL:${NW05_COMPILE_PROCEDURAL_TEXTURE_R8}>
            NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F=$<BOOL:${NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F}>
            NW06_COMPILE_GPU_INITIALIZATION=$<BOOL:${NW06_COMPILE_GPU_INITIALIZATION}>
            NW06_COMPILE_INDEXED_INSTANCED_INDIRECT=$<BOOL:${NW06_COMPILE_INDEXED_INSTANCED_INDIRECT}>
            NW06_COMPILE_ANALYTIC_LIGHTING_TWIST=$<BOOL:${NW06_COMPILE_ANALYTIC_LIGHTING_TWIST}>
            NW06_COMPILE_ABSOLUTE_TICK_CAMERA=$<BOOL:${NW06_COMPILE_ABSOLUTE_TICK_CAMERA}>
            NW06_COMPILE_HALF_RESOLUTION_BLOOM=$<BOOL:${NW06_COMPILE_HALF_RESOLUTION_BLOOM}>
            NW06_COMPILE_TONEMAP=$<BOOL:${NW06_COMPILE_TONEMAP}>
        )
        add_dependencies(nw05_player_bootstrap_diagnostic nw_offline_shaders)
        add_dependencies(nw05_player_bootstrap_diagnostic nw_offline_display_shader)
        if(WIN32)
            target_link_libraries(nw05_player_bootstrap_diagnostic PUBLIC
                d3d12
                dxgi
                user32
                kernel32
            )
            if(NW10_COMPACT_PACKAGE)
                target_link_libraries(nw05_player_bootstrap_diagnostic PUBLIC cabinet)
            endif()
        endif()
    endif()
endif()

# NW-04: package-driven compute + fullscreen-display player fixture.  The
# fixture consumes the established NW-03 `.nwp` v1 container and never links
# compiler/editor/reflection tooling.
add_executable(nw_package_player_fixture
    fixtures/nw04_package_player/main.cpp
)
if(NW05_FROZEN_EMBEDDED_SPECIALIZATION)
    target_compile_definitions(nw_package_player_fixture PRIVATE
        NW09_FORCE_STATEFUL_PACKAGE_MODEL=1
    )
    target_link_libraries(nw_package_player_fixture PRIVATE
        nw05_player_bootstrap_diagnostic
        nw_package_player_diagnostic
    )
else()
    target_link_libraries(nw_package_player_fixture PRIVATE
        nw_player_bootstrap
        nw_package_player_diagnostic
    )
endif()
if(WIN32)
    target_link_libraries(nw_package_player_fixture PRIVATE gdi32)
endif()
nw_apply_player_profile(nw_package_player_fixture)
nw_attach_dependency_check(nw_package_player_fixture "package_player")

# NW-05: content-free specialized-player template.  `nodewerkc export` appends
# the graph-derived .nwp package and generated link manifest to this executable
# (or an equivalent player build) using the deterministic embedded footer.  It
# is part of the default Windows build so the self-contained player is actually
# compiled and reproducible.
add_executable(nw_procedural_image_player
    fixtures/nw05_procedural_image/main.cpp
)
target_link_libraries(nw_procedural_image_player PRIVATE
    nw_player_bootstrap
    nw_package_player
)
if(WIN32)
    target_link_libraries(nw_procedural_image_player PRIVATE gdi32)
endif()
nw_apply_player_profile(nw_procedural_image_player)
nw_attach_dependency_check(nw_procedural_image_player "procedural_image_player")

if(NW05_GENERATED_PLAYER_SOURCE)
    if(NOT EXISTS "${NW05_GENERATED_PLAYER_SOURCE}")
        message(FATAL_ERROR "NW05_GENERATED_PLAYER_SOURCE does not exist: ${NW05_GENERATED_PLAYER_SOURCE}")
    endif()

    add_executable(nw05_generated_player "${NW05_GENERATED_PLAYER_SOURCE}")
    target_compile_definitions(nw05_generated_player PRIVATE
        NW05_FROZEN_EMBEDDED_SPECIALIZATION=$<BOOL:${NW05_FROZEN_EMBEDDED_SPECIALIZATION}>
    )
    target_link_libraries(nw05_generated_player PRIVATE
        nw05_player_bootstrap
        nw_package_player
    )
    if(WIN32)
        target_link_libraries(nw05_generated_player PRIVATE gdi32)
    endif()
    nw_apply_player_profile(nw05_generated_player)
    set_target_properties(nw05_generated_player PROPERTIES
        OUTPUT_NAME "nw05_generated_player"
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/nw05-generated"
    )
endif()

add_library(nw_compiler STATIC ${NW_NATIVE_TOOL_EXCLUDE_FROM_ALL}
    src/compiler/nw_compiler.cpp
)
target_include_directories(nw_compiler PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src/compiler"
)
target_link_libraries(nw_compiler PUBLIC nw_graph nw_package)
target_compile_features(nw_compiler PUBLIC cxx_std_20)

add_executable(nodewerkc ${NW_NATIVE_TOOL_EXCLUDE_FROM_ALL}
    src/compiler/nodewerkc_main.cpp
)
target_link_libraries(nodewerkc PRIVATE nw_compiler nw_graph nw_package)
target_compile_features(nodewerkc PRIVATE cxx_std_20)

# Optional audio targets. No source or link dependency is added to any silent
# player. The fixture shares the existing PackagePlayer and clock boundary.
option(NW_BUILD_AUDIO "Build the optional procedural audio targets" OFF)
option(NW_GENERATED_PLAYER_AUDIO "Link the selected generated player with audio" OFF)
if(TARGET nw05_generated_player AND NOT NW_GENERATED_PLAYER_AUDIO)
    target_compile_definitions(nw_package_player PRIVATE NW_PACKAGE_AUDIO=0)
endif()
if(NW_GENERATED_PLAYER_AUDIO AND (NOT WIN32 OR NOT TARGET nw05_generated_player))
    message(FATAL_ERROR "Audio export requires a generated Windows player")
endif()
if(NW_BUILD_AUDIO OR NW_GENERATED_PLAYER_AUDIO)
    add_library(nw_procedural_audio STATIC src/audio/procedural_audio.cpp)
    target_include_directories(nw_procedural_audio PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/src")
    nw_apply_player_profile(nw_procedural_audio)
    add_executable(nw_audio_offline fixtures/audio/offline.cpp)
    target_link_libraries(nw_audio_offline PRIVATE nw_procedural_audio)
    nw_apply_player_profile(nw_audio_offline)
    add_executable(nw_audio_contract tests/audio_contract.cpp)
    target_link_libraries(nw_audio_contract PRIVATE nw_procedural_audio)
    nw_apply_player_profile(nw_audio_contract)
    if(WIN32)
        add_library(nw_wasapi_output STATIC src/audio/wasapi_output.cpp)
        target_link_libraries(nw_wasapi_output PUBLIC nw_procedural_audio PRIVATE ole32)
        nw_apply_player_profile(nw_wasapi_output)
        if(NW_GENERATED_PLAYER_AUDIO)
            target_link_libraries(nw05_generated_player PRIVATE nw_wasapi_output)
        endif()
        add_executable(nw_audio_player fixtures/audio/main.cpp)
        target_link_libraries(nw_audio_player PRIVATE nw_wasapi_output nw_player_bootstrap nw_package_player_diagnostic gdi32)
        nw_apply_player_profile(nw_audio_player)
    endif()
endif()
