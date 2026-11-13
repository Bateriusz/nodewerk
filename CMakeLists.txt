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
option(NW05_COMPILE_INT64_SHADER_OPS_QUERY "Compile D3D12_OPTIONS1 int64 query support" ON)
option(NW05_COMPILE_FP16_SHADER_OPS_QUERY "Compile D3D12_OPTIONS4 fp16 query support" ON)
option(NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD "Compile procedural texture upload implementation" ON)
option(NW05_COMPILE_PROCEDURAL_TEXTURE_R8 "Compile R8G8B8A8 procedural texture materialization" ON)
option(NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F "Compile RGBA32F procedural texture materialization" ON)

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
        NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD=0
        NW05_COMPILE_PROCEDURAL_TEXTURE_R8=0
        NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F=0
    )
endif()
target_include_directories(nw_player_bootstrap PUBLIC
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated"
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/player"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/platform"
)
nw_apply_player_profile(nw_player_bootstrap)
add_dependencies(nw_player_bootstrap nw_offline_shaders)
add_dependencies(nw_player_bootstrap nw_offline_display_shader)

if(WIN32)
    target_link_libraries(nw_player_bootstrap PUBLIC
        d3d12
        dxgi
        user32
        kernel32
    )
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
    src/package/nwp_format.cpp
)
target_include_directories(nw_package_player PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/package"
)
target_compile_features(nw_package_player PUBLIC cxx_std_20)
nw_apply_player_profile(nw_package_player)
if(MSVC)
    target_compile_options(nw_package_player PRIVATE /W0)
else()
    target_compile_options(nw_package_player PRIVATE -w)
endif()

# NW-04: package-driven compute + fullscreen-display player fixture.  The
# fixture consumes the established NW-03 `.nwp` v1 container and never links
# compiler/editor/reflection tooling.
add_executable(nw_package_player_fixture
    fixtures/nw04_package_player/main.cpp
)
target_link_libraries(nw_package_player_fixture PRIVATE
    nw_player_bootstrap
    nw_package_player
)
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

    # Build the generated NW-05 artifact from an isolated player closure.
    # Generic release/NW-04 targets retain NW_ENABLE_D3D12_VALIDATION=0;
    # this library alone carries the real debug-layer qualification path and
    # only the optional feature-query code retained by the generated manifest.
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
        NW05_SPECIALIZED_VALIDATION=1
        NW05_COMPILE_WAVE_OPS_QUERY=$<BOOL:${NW05_COMPILE_WAVE_OPS_QUERY}>
        NW05_COMPILE_INT64_SHADER_OPS_QUERY=$<BOOL:${NW05_COMPILE_INT64_SHADER_OPS_QUERY}>
        NW05_COMPILE_FP16_SHADER_OPS_QUERY=$<BOOL:${NW05_COMPILE_FP16_SHADER_OPS_QUERY}>
        NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD=$<BOOL:${NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD}>
        NW05_COMPILE_PROCEDURAL_TEXTURE_R8=$<BOOL:${NW05_COMPILE_PROCEDURAL_TEXTURE_R8}>
        NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F=$<BOOL:${NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F}>
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
    endif()

    add_executable(nw05_generated_player "${NW05_GENERATED_PLAYER_SOURCE}")
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
