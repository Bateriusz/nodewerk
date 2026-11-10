cmake_minimum_required(VERSION 3.28)
project(nodewerk VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/ToolchainManifest.cmake")
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/BuildProfiles.cmake")
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/CompileShaders.cmake")
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/DependencyCheck.cmake")

nw_add_offline_compute_shader(
    nw_offline_shaders
    "${CMAKE_CURRENT_SOURCE_DIR}/shaders/compute_fixture.hlsl"
    "CSMain"
    "cs_6_0"
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/compute_fixture.dxil"
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated/compute_fixture_dxil.h"
)

add_library(nw_player_bootstrap STATIC
    src/player/diagnostics.cpp
    src/player/capability_check.cpp
    src/player/d3d12_bootstrap.cpp
)
target_include_directories(nw_player_bootstrap PUBLIC
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/player"
)
nw_apply_player_profile(nw_player_bootstrap)
add_dependencies(nw_player_bootstrap nw_offline_shaders)

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
add_library(nw_package STATIC
    src/package/nwp_format.cpp
)
target_include_directories(nw_package PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src/package"
)
target_compile_features(nw_package PUBLIC cxx_std_20)

add_library(nw_compiler STATIC
    src/compiler/nw_compiler.cpp
)
target_include_directories(nw_compiler PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src/compiler"
)
target_link_libraries(nw_compiler PUBLIC nw_graph nw_package)
target_compile_features(nw_compiler PUBLIC cxx_std_20)

add_executable(nodewerkc
    src/compiler/nodewerkc_main.cpp
)
target_link_libraries(nodewerkc PRIVATE nw_compiler nw_graph nw_package)
target_compile_features(nodewerkc PRIVATE cxx_std_20)
