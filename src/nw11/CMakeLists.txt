cmake_minimum_required(VERSION 3.24)
project(nw11_optional_tools LANGUAGES CXX)

# Standalone optional-profile tooling.  This directory is intentionally NOT
# added to the root NW-07 production build: common and fallback artifacts must
# not gain optional meshes, ray tracing, Agility SDK, or vendor SDK bytes.
add_executable(nw11_capability_probe capability_probe.cpp)
target_compile_features(nw11_capability_probe PRIVATE cxx_std_17)
target_compile_definitions(nw11_capability_probe PRIVATE UNICODE _UNICODE)
target_link_libraries(nw11_capability_probe PRIVATE d3d12 dxgi advapi32 version)

if(MSVC)
    target_compile_options(nw11_capability_probe PRIVATE /W4 /permissive- /EHsc)
endif()
