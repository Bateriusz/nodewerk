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
    "${CMAKE_CURRENT_SOURCE_DIR}/shaders/compute_fixture.dxil"
    "${CMAKE_CURRENT_SOURCE_DIR}/shaders/generated/compute_fixture_dxil.h"
)

add_library(nw_player_bootstrap STATIC
    src/player/diagnostics.cpp
    src/player/capability_check.cpp
    src/player/d3d12_bootstrap.cpp
)
target_include_directories(nw_player_bootstrap PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src/player"
    "${CMAKE_CURRENT_SOURCE_DIR}/shaders/generated"
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
