# Nodewerk NW-01 Reproducible Toolchain Manifest Contract
# Pins and actively verifies the selected compiler/linker, Windows SDK, DXC,
# CMake/Ninja versions, minimum target Windows 11 build, and required feature manifest.

set(NW_PINNED_TOOLCHAIN_ID "clang-cl-lld" CACHE STRING "Primary selected toolchain ID")
set(NW_PINNED_CLANG_VERSION "18.1.8" CACHE STRING "Pinned LLVM clang-cl/lld-link version")
set(NW_PINNED_MSVC_VERSION "19.40.33813" CACHE STRING "Pinned MSVC cl.exe/link.exe comparison version")
set(NW_PINNED_MSVC_LINKER_VERSION "14.40.33813.0" CACHE STRING "Pinned MSVC link.exe comparison version")
set(NW_PINNED_WINDOWS_SDK_VERSION "10.0.26100.0" CACHE STRING "Pinned Windows 11 SDK version")
set(NW_WINDOWS_SDK_VERSION "" CACHE STRING "Explicit Windows SDK version override or detected version (e.g. 10.0.26100.0)")
set(NW_WINDOWS_SDK_ROOT "" CACHE PATH "Explicit Windows Kits 10 root directory (e.g. C:/Program Files (x86)/Windows Kits/10)")
set(NW_PINNED_DXC_VERSION "1.8.2407" CACHE STRING "Pinned offline DXC shader compiler version")
set(NW_PINNED_CMAKE_VERSION "3.30.2" CACHE STRING "Pinned CMake version")
set(NW_PINNED_NINJA_VERSION "1.12.1" CACHE STRING "Pinned Ninja version")
set(NW_MIN_TESTED_WINDOWS_BUILD "10.0.22631.4317" CACHE STRING "Minimum tested Windows 11 x64 build")
set(NW_REQUIRED_FEATURE_MANIFEST "${CMAKE_SOURCE_DIR}/benchmarks/capability-manifest.v1.json")
set(NW_TOOLCHAIN_MANIFEST_JSON "${CMAKE_SOURCE_DIR}/benchmarks/toolchain-manifest.v1.json")
set(NW_DEPENDENCY_INVENTORY_JSON "${CMAKE_SOURCE_DIR}/benchmarks/dependency-inventory.v1.json")

option(NW_ENFORCE_PINNED_TOOLCHAIN
    "Enforce exact pinned compiler, linker, Windows SDK, DXC, CMake, and Ninja versions during configure"
    ON)

function(nw_report_toolchain_drift drift_msg)
    if(NW_ENFORCE_PINNED_TOOLCHAIN)
        message(FATAL_ERROR "[NW-TOOLCHAIN-DRIFT] ${drift_msg} (Set -DNW_ENFORCE_PINNED_TOOLCHAIN=OFF only for unpinned local exploration)")
    else()
        message(WARNING "[NW-TOOLCHAIN-DRIFT] ${drift_msg}")
    endif()
endfunction()

function(nw_verify_toolchain_manifest)
    foreach(_manifest_file IN ITEMS
        "${NW_REQUIRED_FEATURE_MANIFEST}"
        "${NW_TOOLCHAIN_MANIFEST_JSON}"
        "${NW_DEPENDENCY_INVENTORY_JSON}")
        if(NOT EXISTS "${_manifest_file}")
            message(FATAL_ERROR "[NW-TOOLCHAIN-ERR] Missing required baseline manifest: ${_manifest_file}")
        endif()
    endforeach()

    # 1. Verify CMake version against NW_PINNED_CMAKE_VERSION
    if(NOT CMAKE_VERSION VERSION_EQUAL "${NW_PINNED_CMAKE_VERSION}")
        nw_report_toolchain_drift(
            "CMake version '${CMAKE_VERSION}' does not match pinned NW_PINNED_CMAKE_VERSION='${NW_PINNED_CMAKE_VERSION}'")
    endif()

    # 2. Verify Ninja version against NW_PINNED_NINJA_VERSION
    if(CMAKE_GENERATOR STREQUAL "Ninja" AND CMAKE_MAKE_PROGRAM)
        execute_process(
            COMMAND "${CMAKE_MAKE_PROGRAM}" --version
            OUTPUT_VARIABLE _nw_ninja_ver
            ERROR_QUIET
            OUTPUT_STRIP_TRAILING_WHITESPACE
        )
        if(NOT _nw_ninja_ver STREQUAL "${NW_PINNED_NINJA_VERSION}")
            nw_report_toolchain_drift(
                "Ninja version '${_nw_ninja_ver}' does not match pinned NW_PINNED_NINJA_VERSION='${NW_PINNED_NINJA_VERSION}'")
        endif()
    endif()

    # 3. Verify C++ compiler version against pinned clang-cl or MSVC version
    if(NW_ACTIVE_TOOLCHAIN_ID STREQUAL "clang-cl-lld" OR CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
        if(NOT CMAKE_CXX_COMPILER_VERSION VERSION_EQUAL "${NW_PINNED_CLANG_VERSION}")
            nw_report_toolchain_drift(
                "Clang compiler version '${CMAKE_CXX_COMPILER_VERSION}' does not match pinned NW_PINNED_CLANG_VERSION='${NW_PINNED_CLANG_VERSION}'")
        endif()
    elseif(NW_ACTIVE_TOOLCHAIN_ID STREQUAL "msvc-ltcg" OR CMAKE_CXX_COMPILER_ID STREQUAL "MSVC")
        if(NOT CMAKE_CXX_COMPILER_VERSION VERSION_EQUAL "${NW_PINNED_MSVC_VERSION}")
            nw_report_toolchain_drift(
                "MSVC compiler version '${CMAKE_CXX_COMPILER_VERSION}' does not match pinned NW_PINNED_MSVC_VERSION='${NW_PINNED_MSVC_VERSION}'")
        endif()
    endif()

    # 4. Verify linker version against pinned lld-link or link.exe version
    if(CMAKE_LINKER)
        if(NW_ACTIVE_TOOLCHAIN_ID STREQUAL "clang-cl-lld")
            execute_process(
                COMMAND "${CMAKE_LINKER}" --version
                OUTPUT_VARIABLE _nw_linker_out
                ERROR_QUIET
                OUTPUT_STRIP_TRAILING_WHITESPACE
            )
            if(NOT _nw_linker_out MATCHES "${NW_PINNED_CLANG_VERSION}")
                nw_report_toolchain_drift(
                    "lld-link version output '${_nw_linker_out}' does not match pinned '${NW_PINNED_CLANG_VERSION}'")
            endif()
        elseif(NW_ACTIVE_TOOLCHAIN_ID STREQUAL "msvc-ltcg")
            execute_process(
                COMMAND "${CMAKE_LINKER}"
                OUTPUT_VARIABLE _nw_msvc_linker_out
                ERROR_VARIABLE _nw_msvc_linker_err
                OUTPUT_STRIP_TRAILING_WHITESPACE
            )
            set(_nw_msvc_linker_combined "${_nw_msvc_linker_out} ${_nw_msvc_linker_err}")
            if(NOT _nw_msvc_linker_combined MATCHES "${NW_PINNED_MSVC_LINKER_VERSION}")
                nw_report_toolchain_drift(
                    "MSVC link.exe version output does not match pinned '${NW_PINNED_MSVC_LINKER_VERSION}'")
            endif()
        endif()
    endif()

    # 5. Verify Windows SDK version on Windows targets (supports Ninja/non-VS shells via cache, env, registry, or Kits root)
    if(WIN32 OR CMAKE_SYSTEM_NAME STREQUAL "Windows")
        set(_detected_win_sdk "${NW_WINDOWS_SDK_VERSION}")
        if(NOT _detected_win_sdk AND CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION)
            set(_detected_win_sdk "${CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION}")
        endif()
        if(NOT _detected_win_sdk AND DEFINED ENV{WindowsSDKVersion} AND NOT "$ENV{WindowsSDKVersion}" STREQUAL "")
            string(REGEX REPLACE "\\\\$" "" _detected_win_sdk "$ENV{WindowsSDKVersion}")
        endif()
        if(NOT _detected_win_sdk)
            set(_sdk_root "${NW_WINDOWS_SDK_ROOT}")
            if(NOT _sdk_root AND DEFINED ENV{WindowsSdkDir} AND NOT "$ENV{WindowsSdkDir}" STREQUAL "")
                string(REGEX REPLACE "\\\\$" "" _sdk_root "$ENV{WindowsSdkDir}")
            endif()
            if(NOT _sdk_root AND CMAKE_HOST_WIN32)
                cmake_host_system_information(
                    RESULT _reg_kits_root
                    QUERY WINDOWS_REGISTRY "HKLM/SOFTWARE/Microsoft/Windows Kits/Installed Roots"
                    VALUE "KitsRoot10"
                    VIEW BOTH
                    ERROR_VARIABLE _reg_kits_err
                )
                if(_reg_kits_root AND EXISTS "${_reg_kits_root}")
                    string(REGEX REPLACE "[/\\\\]$" "" _sdk_root "${_reg_kits_root}")
                endif()
            endif()
            if(NOT _sdk_root AND EXISTS "C:/Program Files (x86)/Windows Kits/10")
                set(_sdk_root "C:/Program Files (x86)/Windows Kits/10")
            endif()
            if(_sdk_root AND EXISTS "${_sdk_root}/Include")
                if(EXISTS "${_sdk_root}/Include/${NW_PINNED_WINDOWS_SDK_VERSION}")
                    set(_detected_win_sdk "${NW_PINNED_WINDOWS_SDK_VERSION}")
                else()
                    file(GLOB _sdk_include_dirs RELATIVE "${_sdk_root}/Include" "${_sdk_root}/Include/10.0.*")
                    if(_sdk_include_dirs)
                        list(SORT _sdk_include_dirs COMPARE NATURAL ORDER DESCENDING)
                        list(GET _sdk_include_dirs 0 _detected_win_sdk)
                    endif()
                endif()
            endif()
        endif()
        if(NOT _detected_win_sdk STREQUAL "${NW_PINNED_WINDOWS_SDK_VERSION}")
            nw_report_toolchain_drift(
                "Windows SDK version '${_detected_win_sdk}' does not match pinned NW_PINNED_WINDOWS_SDK_VERSION='${NW_PINNED_WINDOWS_SDK_VERSION}'")
        endif()
    endif()

    # 6. Verify offline DXC shader compiler version against NW_PINNED_DXC_VERSION
    find_program(NW_DXC_EXECUTABLE NAMES dxc dxc.exe)
    if(NOT NW_DXC_EXECUTABLE)
        nw_report_toolchain_drift(
            "Offline DXC shader compiler (dxc / dxc.exe) not found in PATH; expected version '${NW_PINNED_DXC_VERSION}'")
    else()
        execute_process(
            COMMAND "${NW_DXC_EXECUTABLE}" --version
            OUTPUT_VARIABLE _nw_dxc_out
            ERROR_VARIABLE _nw_dxc_err
            OUTPUT_STRIP_TRAILING_WHITESPACE
        )
        set(_nw_dxc_combined "${_nw_dxc_out} ${_nw_dxc_err}")
        if(NOT _nw_dxc_combined MATCHES "${NW_PINNED_DXC_VERSION}")
            nw_report_toolchain_drift(
                "DXC version output '${_nw_dxc_combined}' does not match pinned NW_PINNED_DXC_VERSION='${NW_PINNED_DXC_VERSION}'")
        endif()
    endif()
endfunction()

nw_verify_toolchain_manifest()
