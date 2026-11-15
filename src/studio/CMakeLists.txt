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
  "${NW_STUDIO_ROOT}/src")
target_link_libraries(nodewerk-studio-preview PRIVATE nw_package_player)
target_compile_features(nodewerk-studio-preview PRIVATE cxx_std_20)
set_target_properties(nodewerk-studio-preview PROPERTIES
  OUTPUT_NAME "nodewerk-studio-preview")
