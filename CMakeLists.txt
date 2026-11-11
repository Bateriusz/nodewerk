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
    src/player/capability_requirements.cpp
    src/player/package_loader.cpp
    src/player/resource_plan.cpp
    src/player/player_instrumentation.cpp
    src/player/package_player.cpp
    src/player/player.cpp
    src/platform/win32_window.cpp
)
target_include_directories(nw_player_bootstrap PUBLIC
    "${CMAKE_CURRENT_BINARY_DIR}/shaders/generated"
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/player"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/platform"
)
nw_apply_player_profile(nw_player_bootstrap)
add_dependencies(nw_player_bootstrap nw_offline_shaders)

if(WIN32)
    target_link_libraries(nw_player_bootstrap PUBLIC
        d3d12
        dxgi
        user32
        gdi32
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
# player runtime/flags, but without the authoring-side warning surface.  Keeping
# this as a separate archive also prevents the compiler-facing `nw_package`
# target from changing the player's release CRT/import closure.
add_library(nw_package_player STATIC
    src/package/nwp_format.cpp
)
target_include_directories(nw_package_player PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/package"
)
target_compile_features(nw_package_player PUBLIC cxx_std_20)
if(NW_BUILD_PROFILE STREQUAL "dev")
    set_property(TARGET nw_package_player PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreadedDebugDLL")
else()
    set_property(TARGET nw_package_player PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded")
    target_compile_definitions(nw_package_player PRIVATE NDEBUG)
endif()
if(MSVC)
    target_compile_options(nw_package_player PRIVATE
        /EHs-c-
        /GR-
        /GS
        /Brepro
    )
    if(CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
        target_compile_options(nw_package_player PRIVATE
            /clang:-ffile-prefix-map=${CMAKE_SOURCE_DIR}=.
        )
    endif()
    target_compile_definitions(nw_package_player PRIVATE
        _HAS_EXCEPTIONS=0
        WIN32_LEAN_AND_MEAN
        NOMINMAX
    )
else()
    target_compile_options(nw_package_player PRIVATE
        -fno-exceptions
        -fno-rtti
    )
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
nw_apply_player_profile(nw_package_player_fixture)
nw_attach_dependency_check(nw_package_player_fixture "package_player")

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
