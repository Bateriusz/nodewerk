# NW-08 authoring superbuild wrapper.
#
# The repository-root CMakeLists.txt is a frozen NW-07 build input and must not
# be edited.  This wrapper adds the same Nodewerk project plus the optional
# authoring-only studio targets, so CI and local Windows review can build:
#
#   cmake -S tools/nw08_authoring_build -B build/nw08-authoring
#   cmake --build build/nw08-authoring --target nodewerk-studio-preview
#
# Nothing here is part of the release player closure.

cmake_minimum_required(VERSION 3.28)
project(nodewerk_authoring LANGUAGES CXX)

add_subdirectory(
    "${CMAKE_CURRENT_LIST_DIR}/../.."
    "${CMAKE_BINARY_DIR}/nodewerk"
)

include("${CMAKE_CURRENT_LIST_DIR}/../../cmake/Nw08Studio.cmake")
nw_add_studio_authoring_targets()
