# Nodewerk NW-01 Reproducible Toolchain Manifest Contract
# Pins and actively verifies the selected compiler/linker, Windows SDK, DXC,
# CMake/Ninja versions, minimum target Windows 11 build, and required feature manifest.
#
# There is exactly one authoritative version contract:
# benchmarks/toolchain-manifest.v1.json. The pins below are read from that JSON
# on every configure so bootstrap, doctor and this downstream verifier cannot
# drift to independently duplicated literals. Missing or malformed pins fail
# closed instead of silently falling back to a hard-coded value.

get_filename_component(_nw_repository_root "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
set(NW_TOOLCHAIN_MANIFEST_JSON "${_nw_repository_root}/benchmarks/toolchain-manifest.v1.json"
    CACHE FILEPATH "Authoritative NW-01 repository toolchain pin contract")
if(NOT EXISTS "${NW_TOOLCHAIN_MANIFEST_JSON}")
    message(FATAL_ERROR "[NW-TOOLCHAIN-ERR] Missing authoritative toolchain manifest: ${NW_TOOLCHAIN_MANIFEST_JSON}")
endif()
file(READ "${NW_TOOLCHAIN_MANIFEST_JSON}" _nw_toolchain_manifest_json)

string(JSON _nw_pinned_selected_toolchain GET "${_nw_toolchain_manifest_json}" selected_toolchain toolchain_id)
string(JSON _nw_pinned_cmake_version GET "${_nw_toolchain_manifest_json}" pinned_dependencies cmake_version)
string(JSON _nw_pinned_ninja_version GET "${_nw_toolchain_manifest_json}" pinned_dependencies ninja_version)
string(JSON _nw_pinned_windows_sdk_version GET "${_nw_toolchain_manifest_json}" pinned_dependencies windows_sdk_version)
string(JSON _nw_pinned_dxc_version GET "${_nw_toolchain_manifest_json}" pinned_dependencies dxc_version)

string(JSON _nw_evaluated_toolchain_count LENGTH "${_nw_toolchain_manifest_json}" evaluated_toolchains)
set(_nw_pinned_clang_version "")
set(_nw_pinned_msvc_compiler_version "")
set(_nw_pinned_msvc_linker_version "")
math(EXPR _nw_evaluated_toolchain_last "${_nw_evaluated_toolchain_count} - 1")
foreach(_nw_evaluated_index RANGE 0 ${_nw_evaluated_toolchain_last})
    string(JSON _nw_evaluated_toolchain_id GET "${_nw_toolchain_manifest_json}" evaluated_toolchains ${_nw_evaluated_index} toolchain_id)
    string(JSON _nw_evaluated_compiler_version GET "${_nw_toolchain_manifest_json}" evaluated_toolchains ${_nw_evaluated_index} compiler_version)
    string(JSON _nw_evaluated_linker_version GET "${_nw_toolchain_manifest_json}" evaluated_toolchains ${_nw_evaluated_index} linker_version)
    if(_nw_evaluated_toolchain_id STREQUAL "clang-cl-lld")
        set(_nw_pinned_clang_version "${_nw_evaluated_compiler_version}")
    elseif(_nw_evaluated_toolchain_id STREQUAL "msvc-ltcg")
        set(_nw_pinned_msvc_compiler_version "${_nw_evaluated_compiler_version}")
        set(_nw_pinned_msvc_linker_version "${_nw_evaluated_linker_version}")
    endif()
endforeach()
if(_nw_pinned_clang_version STREQUAL ""
   OR _nw_pinned_msvc_compiler_version STREQUAL ""
   OR _nw_pinned_msvc_linker_version STREQUAL "")
    message(FATAL_ERROR
        "[NW-TOOLCHAIN-ERR] Toolchain manifest '${NW_TOOLCHAIN_MANIFEST_JSON}' must record clang-cl-lld and msvc-ltcg evaluated toolchain versions.")
endif()

string(JSON _nw_minimum_tested_windows_build
    ERROR_VARIABLE _nw_minimum_tested_windows_build_error
    GET "${_nw_toolchain_manifest_json}" intended_minimum_windows_build)
if(_nw_minimum_tested_windows_build_error)
    string(JSON _nw_minimum_tested_windows_build GET "${_nw_toolchain_manifest_json}" minimum_tested_windows_build)
endif()

# The derived cache entries exist only for inspection/logging. FORCE guarantees
# a changed manifest pin wins over a stale value left in an existing build tree;
# there is no supported independent pin override.
set(NW_PINNED_TOOLCHAIN_ID "${_nw_pinned_selected_toolchain}" CACHE STRING "Selected toolchain ID (from toolchain manifest)" FORCE)
set(NW_PINNED_CLANG_VERSION "${_nw_pinned_clang_version}" CACHE STRING "Pinned LLVM clang-cl/lld-link version (from toolchain manifest)" FORCE)
set(NW_PINNED_MSVC_VERSION "${_nw_pinned_msvc_compiler_version}" CACHE STRING "Pinned MSVC cl.exe version (from toolchain manifest)" FORCE)
set(NW_PINNED_MSVC_LINKER_VERSION "${_nw_pinned_msvc_linker_version}" CACHE STRING "Pinned MSVC link.exe version (from toolchain manifest)" FORCE)
set(NW_PINNED_WINDOWS_SDK_VERSION "${_nw_pinned_windows_sdk_version}" CACHE STRING "Pinned Windows 11 SDK version (from toolchain manifest)" FORCE)
set(NW_PINNED_DXC_VERSION "${_nw_pinned_dxc_version}" CACHE STRING "Pinned offline DXC shader compiler version (from toolchain manifest)" FORCE)
set(NW_PINNED_CMAKE_VERSION "${_nw_pinned_cmake_version}" CACHE STRING "Pinned CMake version (from toolchain manifest)" FORCE)
set(NW_PINNED_NINJA_VERSION "${_nw_pinned_ninja_version}" CACHE STRING "Pinned Ninja version (from toolchain manifest)" FORCE)
set(NW_MIN_TESTED_WINDOWS_BUILD "${_nw_minimum_tested_windows_build}" CACHE STRING "Minimum target Windows 11 x64 build (from toolchain manifest)" FORCE)

set(NW_WINDOWS_SDK_VERSION "" CACHE STRING "Explicit Windows SDK version override or detected version")
set(NW_WINDOWS_SDK_ROOT "" CACHE PATH "Explicit Windows Kits 10 root directory")
set(NW_REQUIRED_FEATURE_MANIFEST "${_nw_repository_root}/benchmarks/capability-manifest.v1.json")
set(NW_DEPENDENCY_INVENTORY_JSON "${_nw_repository_root}/benchmarks/dependency-inventory.v1.json")

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
