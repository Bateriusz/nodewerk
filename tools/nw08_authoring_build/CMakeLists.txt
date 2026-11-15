# NW-08 authoring build compatibility guard.
#
# Do not configure this directory as CMake's top-level source.  Frozen NW-07
# shader rules resolve repository tools through CMAKE_SOURCE_DIR, so a wrapper
# top level would point them at tools/nw08_authoring_build instead of the repo.
#
# Configure the repository root and inject NW-08 instead:
#
#   cmake -S . -B build/nw08-authoring \
#     -DCMAKE_PROJECT_INCLUDE=tools/nw08_authoring_build/inject-studio.cmake
#   cmake --build build/nw08-authoring --target nodewerk-studio-preview

cmake_minimum_required(VERSION 3.28)
project(nodewerk_authoring_guard LANGUAGES NONE)

message(FATAL_ERROR
  "NW-08 authoring builds must configure the repository root. "
  "Use -DCMAKE_PROJECT_INCLUDE="
  "${CMAKE_CURRENT_LIST_DIR}/inject-studio.cmake")
