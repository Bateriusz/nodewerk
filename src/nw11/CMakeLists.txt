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
nw11_add_shader("display_fixture_cs_dxil" "${NW11_REPO_ROOT}/shaders/display_fixture.hlsl"
    "DisplayCS" "cs_6_0" "kNw11DisplayDxil")
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

add_custom_target(nw11_all_shaders DEPENDS
    nw06_generate_cs_dxil
    nw06_instances_cs_dxil
    nw06_material_cs_dxil
    nw06_camera_cs_dxil
    nw06_light_cs_dxil
    nw06_scene_vs_dxil
    nw06_scene_ps_dxil
    nw06_bloom_cs_dxil
    nw06_tonemap_cs_dxil
    display_fixture_cs_dxil
    nw11_analytic_reflection_ps_dxil
    nw11_scene_rayquery_ps_dxil
    nw11_torus_amplification_as_dxil
    nw11_torus_meshlet_ms_dxil)

add_executable(nw11_capability_probe capability_probe.cpp)
target_compile_features(nw11_capability_probe PRIVATE cxx_std_17)
target_compile_definitions(nw11_capability_probe PRIVATE UNICODE _UNICODE)
target_link_libraries(nw11_capability_probe PRIVATE d3d12 dxgi advapi32 version)
if(MSVC)
    target_compile_options(nw11_capability_probe PRIVATE /W4 /permissive- /EHsc)
endif()

function(nw11_add_renderer target_name variant baseline_kind)
    add_executable(${target_name}
        render/nw11_main.cpp
        render/nw11_scene_harness.cpp
        "${NW11_REPO_ROOT}/src/platform/win32_window.cpp")
    target_include_directories(${target_name} PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/render"
        "${NW11_REPO_ROOT}/src/platform"
        "${NW11_GENERATED_DIR}"
        "${NW11_SHADER_DIR}")
    target_link_libraries(${target_name} PRIVATE d3d12 dxgi user32 gdi32)
    target_compile_features(${target_name} PRIVATE cxx_std_20)
    if(variant STREQUAL "mesh")
        target_compile_definitions(${target_name} PRIVATE
            NW11_VARIANT_MESH=1 NW11_VARIANT_RAY=0 NW11_BASELINE_ANALYTIC=0)
    elseif(variant STREQUAL "ray")
        target_compile_definitions(${target_name} PRIVATE
            NW11_VARIANT_MESH=0 NW11_VARIANT_RAY=1 NW11_BASELINE_ANALYTIC=0)
    elseif(baseline_kind STREQUAL "analytic")
        target_compile_definitions(${target_name} PRIVATE
            NW11_VARIANT_MESH=0 NW11_VARIANT_RAY=0 NW11_BASELINE_ANALYTIC=1)
    else()
        target_compile_definitions(${target_name} PRIVATE
            NW11_VARIANT_MESH=0 NW11_VARIANT_RAY=0 NW11_BASELINE_ANALYTIC=0)
    endif()
    add_dependencies(${target_name} nw11_all_shaders)
    add_dependencies(${target_name} nw11_scene_generated)
    if(MSVC)
        target_compile_options(${target_name} PRIVATE
            /W4 /permissive- /EHsc
            /Gy /Gw /Zc:inline
            /guard:cf
            /Brepro
            $<$<CONFIG:Release>:/O1>
            $<$<CONFIG:Release>:/Os>
            $<$<CONFIG:Release>:/GL>
        )
        target_link_options(${target_name} PRIVATE
            /OPT:REF
            /OPT:ICF
            /Brepro
            /INCREMENTAL:NO
            /DYNAMICBASE
            /HIGHENTROPYVA
            /NXCOMPAT
            /guard:cf
            $<$<CONFIG:Release>:/LTCG>
        )
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
