# NW-08 nodewerk-studio native authoring preview host.
#
# This is a deliberately separate authoring build.  The frozen NW-07 root
# project is configured/built first to generate the exact offline shader
# headers and the content-free player template.  This project then compiles an
# authoring-only PackagePlayer overlay against those generated headers.
#
# Example:
#   cmake -S . -B build/nw08-runtime -DNW_BUILD_PROFILE=dev
#   cmake --build build/nw08-runtime --target nw_procedural_image_player
#   cmake -S src/studio -B build/nw08-studio \
#     -DNW_STUDIO_ROOT=<repo-root> \
#     -DNW_STUDIO_RUNTIME_BUILD_DIR=<repo-root>/build/nw08-runtime \
#     -DNW_BUILD_PROFILE=dev
#   cmake --build build/nw08-studio --target nodewerk-studio-preview
#
# No target from this directory is part of the release dependency closure.

cmake_minimum_required(VERSION 3.28)
project(nodewerk_studio LANGUAGES CXX)

if(NOT WIN32)
  message(STATUS "nodewerk-studio native preview host is Windows-only")
  return()
endif()

if(NOT DEFINED NW_STUDIO_ROOT)
  get_filename_component(NW_STUDIO_ROOT
    "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
endif()
get_filename_component(NW_STUDIO_ROOT "${NW_STUDIO_ROOT}" ABSOLUTE)

if(NOT DEFINED NW_STUDIO_RUNTIME_BUILD_DIR)
  message(FATAL_ERROR
    "NW_STUDIO_RUNTIME_BUILD_DIR is required. Configure/build the frozen "
    "repository root first so the pinned offline shader headers exist.")
endif()
get_filename_component(NW_STUDIO_RUNTIME_BUILD_DIR
  "${NW_STUDIO_RUNTIME_BUILD_DIR}" ABSOLUTE)

set(_nw08_runtime_generated
  "${NW_STUDIO_RUNTIME_BUILD_DIR}/shaders/generated")
foreach(_nw08_required_header IN ITEMS
    compute_fixture_dxil.h
    display_fixture_dxil.h)
  if(NOT EXISTS
      "${_nw08_runtime_generated}/${_nw08_required_header}")
    message(FATAL_ERROR
      "NW-08 authoring runtime requires frozen generated shader header "
      "'${_nw08_runtime_generated}/${_nw08_required_header}'. "
      "Build nw_procedural_image_player in the root runtime build first.")
  endif()
endforeach()

include("${NW_STUDIO_ROOT}/cmake/BuildProfiles.cmake")
find_package(Python3 COMPONENTS Interpreter REQUIRED)

# package_player.{h,cpp} are frozen NW-07 release inputs. Generate an exact,
# fail-closed authoring overlay in this build tree. The source-tree files stay
# byte-for-byte identical to the release inputs.
set(_nw08_generated_runtime_dir
  "${CMAKE_CURRENT_BINARY_DIR}/generated/nw08-authoring-runtime")
execute_process(
  COMMAND "${Python3_EXECUTABLE}"
          "${NW_STUDIO_ROOT}/tools/nw08_authoring_runtime_overlay.py"
          --repo-root "${NW_STUDIO_ROOT}"
          --output-dir "${_nw08_generated_runtime_dir}"
  RESULT_VARIABLE _nw08_overlay_result
  OUTPUT_VARIABLE _nw08_overlay_stdout
  ERROR_VARIABLE _nw08_overlay_stderr)
if(NOT _nw08_overlay_result EQUAL 0)
  message(FATAL_ERROR
    "failed to generate NW-08 authoring PackagePlayer overlay: "
    "${_nw08_overlay_stderr}")
endif()

set(_nw08_player_sources
  "${NW_STUDIO_ROOT}/src/player/diagnostics.cpp"
  "${NW_STUDIO_ROOT}/src/player/capability_check.cpp"
  "${NW_STUDIO_ROOT}/src/player/d3d12_bootstrap.cpp"
  "${NW_STUDIO_ROOT}/src/player/capability_requirements.cpp"
  "${NW_STUDIO_ROOT}/src/player/package_loader.cpp"
  "${NW_STUDIO_ROOT}/src/player/embedded_package.cpp"
  "${NW_STUDIO_ROOT}/src/player/resource_plan.cpp"
  "${NW_STUDIO_ROOT}/src/player/player_instrumentation.cpp"
  "${_nw08_generated_runtime_dir}/package_player.cpp"
  "${NW_STUDIO_ROOT}/src/player/player.cpp"
  "${NW_STUDIO_ROOT}/src/platform/win32_window.cpp"
)

add_library(nw08_player_bootstrap STATIC ${_nw08_player_sources})
target_include_directories(nw08_player_bootstrap PUBLIC
  "${_nw08_generated_runtime_dir}"
  "${_nw08_runtime_generated}"
  "${NW_STUDIO_ROOT}/src"
  "${NW_STUDIO_ROOT}/src/player"
  "${NW_STUDIO_ROOT}/src/platform"
)
target_compile_definitions(nw08_player_bootstrap PRIVATE
  NW08_AUTHORING_RUNTIME=1)
nw_apply_player_profile(nw08_player_bootstrap)
target_link_libraries(nw08_player_bootstrap PUBLIC
  d3d12 dxgi user32 kernel32)

# Build the immutable .nwp implementation with the same player CRT/profile.
# This target is authoring-only and does not alter nw_package_player in the
# frozen root build.
add_library(nw08_package_player STATIC
  "${NW_STUDIO_ROOT}/src/package/nwp_format.cpp")
target_include_directories(nw08_package_player PUBLIC
  "${NW_STUDIO_ROOT}/src"
  "${NW_STUDIO_ROOT}/src/package")
nw_apply_player_profile(nw08_package_player)
if(MSVC)
  target_compile_options(nw08_package_player PRIVATE /W0)
endif()

add_executable(nodewerk-studio-preview nw_studio_preview_host.cpp)
target_include_directories(nodewerk-studio-preview PRIVATE
  "${_nw08_generated_runtime_dir}"
  "${NW_STUDIO_ROOT}/src"
  "${NW_STUDIO_ROOT}/src/player"
  "${NW_STUDIO_ROOT}/src/package")
target_compile_definitions(nodewerk-studio-preview PRIVATE
  NW08_AUTHORING_RUNTIME=1
  _CRT_SECURE_NO_WARNINGS)
target_link_libraries(nodewerk-studio-preview PRIVATE
  nw08_player_bootstrap
  nw08_package_player
  gdi32)
nw_apply_player_profile(nodewerk-studio-preview)
set_target_properties(nodewerk-studio-preview PROPERTIES
  OUTPUT_NAME "nodewerk-studio-preview")
