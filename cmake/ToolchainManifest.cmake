# Nodewerk NW-01 Reproducible Toolchain Manifest Contract
# Pins the selected compiler/linker, Windows SDK, DXC, CMake/Ninja versions,
# minimum tested Windows 11 build, and required feature manifest.

set(NW_PINNED_TOOLCHAIN_ID "clang-cl-lld" CACHE STRING "Primary selected toolchain ID")
set(NW_PINNED_CLANG_VERSION "18.1.8" CACHE STRING "Pinned LLVM clang-cl/lld-link version")
set(NW_PINNED_MSVC_VERSION "19.40.33813" CACHE STRING "Pinned MSVC cl.exe/link.exe comparison version")
set(NW_PINNED_WINDOWS_SDK_VERSION "10.0.26100.0" CACHE STRING "Pinned Windows 11 SDK version")
set(NW_PINNED_DXC_VERSION "1.8.2407" CACHE STRING "Pinned offline DXC shader compiler version")
set(NW_PINNED_CMAKE_VERSION "3.30.2" CACHE STRING "Pinned CMake version")
set(NW_PINNED_NINJA_VERSION "1.12.1" CACHE STRING "Pinned Ninja version")
set(NW_MIN_TESTED_WINDOWS_BUILD "10.0.22631.4317" CACHE STRING "Minimum tested Windows 11 x64 build")
set(NW_REQUIRED_FEATURE_MANIFEST "${CMAKE_SOURCE_DIR}/benchmarks/capability-manifest.v1.json")
set(NW_TOOLCHAIN_MANIFEST_JSON "${CMAKE_SOURCE_DIR}/benchmarks/toolchain-manifest.v1.json")
set(NW_DEPENDENCY_INVENTORY_JSON "${CMAKE_SOURCE_DIR}/benchmarks/dependency-inventory.v1.json")

if(NOT EXISTS "${NW_REQUIRED_FEATURE_MANIFEST}")
    message(WARNING "Required capability manifest not yet present at ${NW_REQUIRED_FEATURE_MANIFEST}")
endif()
