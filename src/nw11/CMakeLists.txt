cmake_minimum_required(VERSION 3.24)
project(nw11_optional_experiments LANGUAGES CXX)

# Validation bundles must run on a clean Windows machine without the Visual
# C++ redistributable.  Use the static CRT for every NW-11 experiment target;
# inbox D3D12/DXGI/Win32 DLLs remain the only runtime dependencies.
if(MSVC)
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

# Optional-profile experiment artifacts.  This project is intentionally NOT
# part of the root NW-07 production build: common and fallback artifacts must
# not gain optional mesh-shader, DXR, Agility SDK, or vendor SDK bytes.
#
# Targets:
#   nw11_capability_probe   exact CheckFeatureSupport observation
#   nw11_common_baseline    indexed/instanced + analytic reflection baseline
#   nw11_fallback_common    separately built common fallback artifact
#   nw11_optional_mesh      amplification + mesh shader variant
#   nw11_optional_ray       inline DXR ray-query variant

if(NOT WIN32)
    message(STATUS "NW-11 optional experiments require Windows; no targets configured.")
    return()
endif()

set(NW11_REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." CACHE PATH "Repository root")
set(NW11_GRAPH "${NW11_REPO_ROOT}/fixtures/nw07/production_loop.nwgraph" CACHE FILEPATH "Frozen NW-07 graph")
set(NW11_GENERATED_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated")
set(NW11_SHADER_DIR "${CMAKE_CURRENT_BINARY_DIR}/shaders")
set(NW11_GRAPH_TOOL "${NW11_REPO_ROOT}/tools/nw11_graph_scene.py")
file(MAKE_DIRECTORY "${NW11_GENERATED_DIR}" "${NW11_SHADER_DIR}")

find_package(Python3 COMPONENTS Interpreter REQUIRED)
find_program(NW11_DXC_EXECUTABLE NAMES dxc dxc.exe)
if(NOT NW11_DXC_EXECUTABLE)
    message(FATAL_ERROR
        "[NW11-SHADER-ERR] Offline DXC (dxc/dxc.exe) is required to build the "
        "NW-11 experiment artifacts.  Runtime shader compilation is forbidden.")
endif()
file(READ "${NW11_REPO_ROOT}/benchmarks/toolchain-manifest.v1.json"
    _nw11_toolchain_manifest)
string(JSON _nw11_pinned_dxc GET "${_nw11_toolchain_manifest}"
    pinned_dependencies dxc_version)
execute_process(
    COMMAND "${NW11_DXC_EXECUTABLE}" --version
    OUTPUT_VARIABLE _nw11_dxc_version_stdout
    ERROR_VARIABLE _nw11_dxc_version_stderr
    RESULT_VARIABLE _nw11_dxc_result)
set(_nw11_dxc_version_text
    "${_nw11_dxc_version_stdout}${_nw11_dxc_version_stderr}")
if(NOT _nw11_dxc_result EQUAL 0 OR
   NOT _nw11_dxc_version_text MATCHES "${_nw11_pinned_dxc}")
    message(FATAL_ERROR
        "[NW11-TOOLCHAIN-ERR] Offline DXC must match pinned version "
        "${_nw11_pinned_dxc}; observed: ${_nw11_dxc_version_text}")
endif()

# Lower the frozen graph into graph-derived C++ constants.  The renderer and
# shaders consume these values; no torus radius or segment count is hard-coded.
set(NW11_SCENE_HEADER "${NW11_GENERATED_DIR}/nw11_scene_generated.h")
set(NW11_SCENE_JSON "${NW11_GENERATED_DIR}/nw11_scene_generated.json")
add_custom_command(
    OUTPUT "${NW11_SCENE_HEADER}" "${NW11_SCENE_JSON}"
    COMMAND "${Python3_EXECUTABLE}" "${NW11_GRAPH_TOOL}"
        --repo-root "${NW11_REPO_ROOT}"
        --graph "${NW11_GRAPH}"
        --header "${NW11_SCENE_HEADER}"
        --json "${NW11_SCENE_JSON}"
    DEPENDS "${NW11_GRAPH_TOOL}" "${NW11_GRAPH}"
            "${NW11_REPO_ROOT}/benchmarks/nw07/canonical-production.v1.json"
    COMMENT "Lowering frozen NW-07 graph into NW-11 renderer constants"
    VERBATIM
)
add_custom_target(nw11_scene_generated
    DEPENDS "${NW11_SCENE_HEADER}" "${NW11_SCENE_JSON}")

function(nw11_add_shader target_name hlsl_file entry_point profile symbol)
    get_filename_component(_hlsl_dir "${hlsl_file}" DIRECTORY)
    set(_header "${NW11_SHADER_DIR}/${target_name}.h")
    set(_dxil "${NW11_SHADER_DIR}/${target_name}.dxil")
    add_custom_command(
        OUTPUT "${_header}" "${_dxil}"
        COMMAND "${NW11_DXC_EXECUTABLE}"
            -T "${profile}"
            -E "${entry_point}"
            -HV 2021
            -O3
            -Qstrip_reflect
            -Qstrip_debug
            -I "${NW11_REPO_ROOT}/shaders/nw06"
            -I "${_hlsl_dir}"
            -Fo "${_dxil}"
            -Fh "${_header}"
            -Vn "${symbol}"
            "${hlsl_file}"
        DEPENDS "${hlsl_file}" "${NW11_REPO_ROOT}/shaders/nw06/nw06_scene_data_cs.hlsl"
                "${NW11_REPO_ROOT}/shaders/nw11/reflection/nw11_reflection_fixture.hlsli"
        COMMENT "Compiling NW-11 offline shader ${hlsl_file} (${entry_point}, ${profile})"
        VERBATIM
    )
    add_custom_target(${target_name} DEPENDS "${_header}")
endfunction()

nw11_add_shader("nw06_generate_cs_dxil" "${NW11_REPO_ROOT}/shaders/nw06/nw06_generate_cs.hlsl"
    "GenerateGeometryCS" "cs_6_0" "kNw06GenerateDxil")
nw11_add_shader("nw06_instances_cs_dxil" "${NW11_REPO_ROOT}/shaders/nw06/nw06_instances_cs.hlsl"
    "GenerateInstancesCS" "cs_6_0" "kNw06InstancesDxil")
nw11_add_shader("nw06_material_cs_dxil" "${NW11_REPO_ROOT}/shaders/nw06/nw06_material_cs.hlsl"
    "MaterialCS" "cs_6_0" "kNw06MaterialDxil")
nw11_add_shader("nw06_camera_cs_dxil" "${NW11_REPO_ROOT}/shaders/nw06/nw06_camera_cs.hlsl"
    "CameraDataCS" "cs_6_0" "kNw06CameraDxil")
nw11_add_shader("nw06_light_cs_dxil" "${NW11_REPO_ROOT}/shaders/nw06/nw06_light_cs.hlsl"
    "LightDataCS" "cs_6_0" "kNw06LightDxil")
nw11_add_shader("nw06_scene_vs_dxil" "${NW11_REPO_ROOT}/shaders/nw06/nw06_scene_vs.hlsl"
    "SceneVS" "vs_6_0" "kNw06SceneVsDxil")
nw11_add_shader("nw06_scene_ps_dxil" "${NW11_REPO_ROOT}/shaders/nw06/nw06_scene_ps.hlsl"
    "ScenePS" "ps_6_0" "kNw06ScenePsDxil")
nw11_add_shader("nw06_bloom_cs_dxil" "${NW11_REPO_ROOT}/shaders/nw06/nw06_bloom_cs.hlsl"
    "BloomCS" "cs_6_0" "kNw06BloomDxil")
nw11_add_shader("nw06_tonemap_cs_dxil" "${NW11_REPO_ROOT}/shaders/nw06/nw06_tonemap_cs.hlsl"
    "TonemapCS" "cs_6_0" "kNw06TonemapDxil")
nw11_add_shader("display_fixture_dxil" "${NW11_REPO_ROOT}/shaders/display_fixture.hlsl"
    "DisplayCS" "cs_6_0" "kDisplayFixtureDxil")
nw11_add_shader("nw11_analytic_reflection_ps_dxil"
    "${NW11_REPO_ROOT}/shaders/nw11/reflection/nw11_analytic_reflection_ps.hlsl"
    "ScenePS" "ps_6_0" "kNw11AnalyticReflectionDxil")
nw11_add_shader("nw11_scene_rayquery_ps_dxil"
    "${NW11_REPO_ROOT}/shaders/nw11/reflection/nw11_scene_rayquery_ps.hlsl"
    "ScenePS" "ps_6_5" "kNw11SceneRayQueryDxil")
nw11_add_shader("nw11_torus_amplification_as_dxil"
    "${NW11_REPO_ROOT}/shaders/nw11/mesh/nw11_torus_amplification.as.hlsl"
    "AmplificationMain" "as_6_5" "kNw11TorusAsDxil")
nw11_add_shader("nw11_torus_meshlet_ms_dxil"
    "${NW11_REPO_ROOT}/shaders/nw11/mesh/nw11_torus_meshlet.ms.hlsl"
    "MeshMain" "ms_6_5" "kNw11TorusMsDxil")

nw11_add_shader("compute_fixture_dxil" "${NW11_REPO_ROOT}/shaders/compute_fixture.hlsl"
    "CSMain" "cs_6_0" "kComputeFixtureDxil")
nw11_add_shader("nw11_deform_cs_dxil" "${NW11_REPO_ROOT}/shaders/nw11/reflection/nw11_deform_cs.hlsl"
    "DeformCS" "cs_6_0" "kNw11DeformDxil")

add_custom_target(nw11_all_shaders DEPENDS
    compute_fixture_dxil
    nw11_deform_cs_dxil
    nw06_generate_cs_dxil
    nw06_instances_cs_dxil
    nw06_material_cs_dxil
    nw06_camera_cs_dxil
    nw06_light_cs_dxil
    nw06_scene_vs_dxil
    nw06_scene_ps_dxil
    nw06_bloom_cs_dxil
    nw06_tonemap_cs_dxil
    display_fixture_dxil
    nw11_analytic_reflection_ps_dxil
    nw11_scene_rayquery_ps_dxil
    nw11_torus_amplification_as_dxil
    nw11_torus_meshlet_ms_dxil)

add_executable(nw11_capability_probe capability_probe.cpp)
target_compile_features(nw11_capability_probe PRIVATE cxx_std_17)
target_compile_definitions(nw11_capability_probe PRIVATE UNICODE _UNICODE)
target_link_libraries(nw11_capability_probe PRIVATE d3d12 dxgi version gdi32 user32 dwmapi)
if(MSVC)
    target_compile_options(nw11_capability_probe PRIVATE /W4 /permissive- /EHsc /Brepro)
    target_link_options(nw11_capability_probe PRIVATE /Brepro /INCREMENTAL:NO)
endif()

# Build the same runtime translation units as the production PackagePlayer.
# Only package_player.cpp is specialized per optional profile.
set(NW11_RUNTIME_SOURCES
    src/player/diagnostics.cpp
    src/player/capability_check.cpp
    src/player/d3d12_bootstrap.cpp
    src/player/capability_requirements.cpp
    src/player/package_loader.cpp
    src/player/embedded_package.cpp
    src/player/resource_plan.cpp
    src/player/player_instrumentation.cpp
    src/player/player.cpp
    src/platform/win32_window.cpp
    src/package/nwp_format.cpp)
list(TRANSFORM NW11_RUNTIME_SOURCES PREPEND "${NW11_REPO_ROOT}/")
add_library(nw11_production_runtime STATIC ${NW11_RUNTIME_SOURCES})
target_include_directories(nw11_production_runtime PUBLIC
    "${NW11_REPO_ROOT}/src" "${NW11_REPO_ROOT}/src/player"
    "${NW11_REPO_ROOT}/src/platform" "${NW11_GENERATED_DIR}" "${NW11_SHADER_DIR}")
target_compile_features(nw11_production_runtime PUBLIC cxx_std_20)
target_compile_definitions(nw11_production_runtime PUBLIC
    WIN32_LEAN_AND_MEAN NOMINMAX
    NW10_COMPACT_PACKAGE=0
    NW_PROFILE_DEV=1
    NW05_FROZEN_EMBEDDED_SPECIALIZATION=1
    NW05_COMPILE_PROCEDURAL_TEXTURE_UPLOAD=0
    NW05_COMPILE_PROCEDURAL_TEXTURE_R8=0
    NW05_COMPILE_PROCEDURAL_TEXTURE_RGBA32F=0
    NW06_COMPILE_GPU_INITIALIZATION=1
    NW06_COMPILE_INDEXED_INSTANCED_INDIRECT=1
    NW06_COMPILE_ANALYTIC_LIGHTING_TWIST=1
    NW06_COMPILE_ABSOLUTE_TICK_CAMERA=1
    NW06_COMPILE_HALF_RESOLUTION_BLOOM=1
    NW06_COMPILE_TONEMAP=1)
target_link_libraries(nw11_production_runtime PUBLIC d3d12 dxgi user32 kernel32 gdi32 psapi)
add_dependencies(nw11_production_runtime compute_fixture_dxil display_fixture_dxil)
if(MSVC)
    target_compile_options(nw11_production_runtime PRIVATE /W4 /permissive- /EHsc /Gy /Gw /Zc:inline /guard:cf /Brepro /O1 /Os /GL)
endif()

# Isolate NW-11's experimental graphics seams from the frozen NW-07/NW-10
# inputs without duplicating the production implementation. The generated
# translation unit is the canonical player plus the verified bounded overlay.
set(NW11_PLAYER_SOURCE "${NW11_GENERATED_DIR}/nw11_package_player.cpp")
set(NW11_PLAYER_OVERLAY "${CMAKE_CURRENT_SOURCE_DIR}/production/package_player.overlay.json")
set(NW11_PLAYER_GENERATOR "${NW11_REPO_ROOT}/tools/nw11_specialize_player.py")
add_custom_command(
    OUTPUT "${NW11_PLAYER_SOURCE}"
    COMMAND "${Python3_EXECUTABLE}" "${NW11_PLAYER_GENERATOR}"
        --source "${NW11_REPO_ROOT}/src/player/package_player.cpp"
        --overlay "${NW11_PLAYER_OVERLAY}"
        --output "${NW11_PLAYER_SOURCE}"
    DEPENDS "${NW11_PLAYER_GENERATOR}" "${NW11_PLAYER_OVERLAY}"
        "${NW11_REPO_ROOT}/src/player/package_player.cpp"
    VERBATIM)
add_custom_target(nw11_specialized_player_source DEPENDS "${NW11_PLAYER_SOURCE}")

function(nw11_add_renderer target_name variant baseline_kind)
    add_executable(${target_name}
        render/nw11_main.cpp render/nw11_scene_harness.cpp
        "${NW11_PLAYER_SOURCE}")
    target_include_directories(${target_name} PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/render"
        "${CMAKE_CURRENT_SOURCE_DIR}/production"
        "${NW11_GENERATED_DIR}" "${NW11_SHADER_DIR}")
    target_link_libraries(${target_name} PRIVATE nw11_production_runtime)
    target_compile_definitions(${target_name} PRIVATE NW11_PRODUCTION_EXPERIMENT=1)
    if(variant STREQUAL "mesh")
        target_compile_definitions(${target_name} PRIVATE
            NW11_VARIANT_MESH=1 NW11_VARIANT_RAY=0 NW11_BASELINE_ANALYTIC=0)
        add_dependencies(${target_name} nw11_torus_amplification_as_dxil nw11_torus_meshlet_ms_dxil)
    elseif(variant STREQUAL "ray")
        target_compile_definitions(${target_name} PRIVATE
            NW11_VARIANT_MESH=0 NW11_VARIANT_RAY=1 NW11_BASELINE_ANALYTIC=0)
        add_dependencies(${target_name} nw11_scene_rayquery_ps_dxil nw11_deform_cs_dxil)
    elseif(baseline_kind STREQUAL "analytic")
        target_compile_definitions(${target_name} PRIVATE
            NW11_VARIANT_MESH=0 NW11_VARIANT_RAY=0 NW11_BASELINE_ANALYTIC=1)
        add_dependencies(${target_name} nw11_analytic_reflection_ps_dxil)
    else()
        target_compile_definitions(${target_name} PRIVATE
            NW11_VARIANT_MESH=0 NW11_VARIANT_RAY=0 NW11_BASELINE_ANALYTIC=0)
    endif()
    add_dependencies(${target_name} nw11_all_shaders nw11_scene_generated nw11_specialized_player_source)
    if(MSVC)
        target_compile_options(${target_name} PRIVATE /W4 /permissive- /EHsc /Gy /Gw /Zc:inline /guard:cf /Brepro /O1 /Os /GL)
        target_link_options(${target_name} PRIVATE /OPT:REF /OPT:ICF /Brepro /INCREMENTAL:NO /DYNAMICBASE /HIGHENTROPYVA /NXCOMPAT /guard:cf /LTCG)
    endif()
endfunction()

nw11_add_renderer(nw11_common_baseline common raster)
nw11_add_renderer(nw11_common_analytic common analytic)
nw11_add_renderer(nw11_optional_mesh mesh raster)
nw11_add_renderer(nw11_optional_ray ray raster)
nw11_add_renderer(nw11_fallback_common common raster)
nw11_add_renderer(nw11_fallback_analytic common analytic)

# Variant-specific shaders are compiled but the executables only include the
# payload headers selected by their NW11_VARIANT_* definitions.  The common
# and fallback targets never reference mesh/ray payload headers, so unused
# optional implementations cannot leak into their byte closure.
