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

add_executable(nodewerk-studio-preview nw_studio_preview_host.cpp)
target_include_directories(nodewerk-studio-preview PRIVATE
  "${NW_STUDIO_ROOT}/src/player"
  "${NW_STUDIO_ROOT}/src/package"
  "${NW_STUDIO_ROOT}/src")
if(NOT TARGET nw_player_bootstrap)
  message(FATAL_ERROR
    "nodewerk-studio-preview requires nw_player_bootstrap (PackagePlayer "
    "implementation) in addition to nw_package_player")
endif()
# ``nw_package_player`` only contains the package format implementation.
# PackagePlayer itself lives in ``nw_player_bootstrap``; both archives are
# required or the host has unresolved PackagePlayer symbols.  The bootstrap
# archive exports d3d12/dxgi/user32/kernel32 publicly; gdi32 mirrors the
# release package-player fixtures.
target_link_libraries(nodewerk-studio-preview PRIVATE
  nw_player_bootstrap
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
