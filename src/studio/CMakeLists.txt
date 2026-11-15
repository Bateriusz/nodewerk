# NW-08 `nodewerk-studio` authoring product (optional, not part of the
# frozen NW-07 release closure).
#
# The repository-root CMakeLists.txt is a frozen NW-07 build input.  To keep
# release artifacts byte-identical this project is intentionally separate and
# is configured only when explicitly requested by an authoring build:
#
#   cmake -S src/studio -B build/studio -DNW_STUDIO_ROOT=<repo-root>
#
# It links the existing NW-04 package-player runtime for preview and contains
# no shader compiler, source-map, profiling, or UI dependency in the player.

cmake_minimum_required(VERSION 3.24)
project(nodewerk_studio LANGUAGES CXX)

if(NOT WIN32)
  message(STATUS "nodewerk-studio native preview host is Windows-only")
  return()
endif()

if(NOT DEFINED NW_STUDIO_ROOT)
  get_filename_component(NW_STUDIO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
endif()

if(NOT TARGET nw_package_player)
  message(STATUS "nw_package_player target is not available; add this project "
                 "to an authoring build that defines the Nodewerk player runtime")
  return()
endif()

if(NOT TARGET nw_player_bootstrap)
  message(FATAL_ERROR
    "nodewerk-studio-preview requires the existing player bootstrap sources")
endif()

# Compile a distinct authoring runtime archive from the established player
# implementation.  NW08_AUTHORING_RUNTIME enables the hot-reload surface only
# in this archive: the frozen release nw_player_bootstrap target is untouched,
# so editor-only incremental caching code cannot enter the release closure.
get_target_property(_nw08_bootstrap_sources nw_player_bootstrap SOURCES)
get_target_property(_nw08_bootstrap_includes nw_player_bootstrap INCLUDE_DIRECTORIES)
set(_nw08_authoring_sources)
foreach(_nw08_source IN LISTS _nw08_bootstrap_sources)
  if(IS_ABSOLUTE "${_nw08_source}")
    list(APPEND _nw08_authoring_sources "${_nw08_source}")
  else()
    list(APPEND _nw08_authoring_sources "${NW_STUDIO_ROOT}/${_nw08_source}")
  endif()
endforeach()

# package_player.{h,cpp} are frozen NW-07 release inputs.  Generate an exact
# authoring-only overlay in the build tree instead of editing or recompiling a
# modified source-tree copy into the release closure.
find_package(Python3 COMPONENTS Interpreter REQUIRED)
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
list(FILTER _nw08_authoring_sources EXCLUDE
  REGEX "[/\\]src[/\\]player[/\\]package_player\\.cpp$")
list(APPEND _nw08_authoring_sources
  "${_nw08_generated_runtime_dir}/package_player.cpp")

add_library(nw08_player_bootstrap STATIC ${_nw08_authoring_sources})
target_include_directories(nw08_player_bootstrap PUBLIC
  "${_nw08_generated_runtime_dir}"
  ${_nw08_bootstrap_includes})
target_compile_definitions(nw08_player_bootstrap PRIVATE
  NW08_AUTHORING_RUNTIME=1)
if(COMMAND nw_apply_player_profile)
  nw_apply_player_profile(nw08_player_bootstrap)
else()
  message(FATAL_ERROR
    "nodewerk-studio-preview requires nw_apply_player_profile for a "
    "compatible CRT/build profile")
endif()
add_dependencies(nw08_player_bootstrap nw_offline_shaders)
add_dependencies(nw08_player_bootstrap nw_offline_display_shader)
target_link_libraries(nw08_player_bootstrap PUBLIC
  d3d12 dxgi user32 kernel32)

add_executable(nodewerk-studio-preview nw_studio_preview_host.cpp)
target_include_directories(nodewerk-studio-preview PRIVATE
  "${_nw08_generated_runtime_dir}"
  "${NW_STUDIO_ROOT}/src/player"
  "${NW_STUDIO_ROOT}/src/package"
  "${NW_STUDIO_ROOT}/src")
target_compile_definitions(nodewerk-studio-preview PRIVATE
  NW08_AUTHORING_RUNTIME=1)
# nw_package_player supplies the immutable .nwp format implementation; the
# separate authoring bootstrap supplies PackagePlayer plus hot reload.
target_link_libraries(nodewerk-studio-preview PRIVATE
  nw08_player_bootstrap
  nw_package_player)
if(WIN32)
  target_link_libraries(nodewerk-studio-preview PRIVATE gdi32)
endif()
if(COMMAND nw_apply_player_profile)
  # Match the CRT/profile of the player archives exactly; a /MD vs /MT
  # mismatch is a hard lld-link failure on Windows.
  nw_apply_player_profile(nodewerk-studio-preview)
else()
  message(FATAL_ERROR
    "nodewerk-studio-preview requires nw_apply_player_profile for a "
    "compatible CRT/build profile")
endif()
# The host uses the C stdio readback/stat writers; silence MSVC's
# non-functional deprecation warning so /WX remains meaningful.
if(MSVC)
  target_compile_definitions(nodewerk-studio-preview PRIVATE
    _CRT_SECURE_NO_WARNINGS)
endif()
target_compile_features(nodewerk-studio-preview PRIVATE cxx_std_20)
set_target_properties(nodewerk-studio-preview PROPERTIES
  OUTPUT_NAME "nodewerk-studio-preview")
