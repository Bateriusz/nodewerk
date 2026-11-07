# Nodewerk NW-01 Build Profiles Contract
# Defines dev, release, and compact_experiment profiles for Windows 11 x64 C++20 builds.
# - Authoring/compiler targets (future NW-02..NW-09) may use the C++ standard library.
# - Player and measurement fixture targets disable exceptions (/EHs-c-, _HAS_EXCEPTIONS=0)
#   and RTTI (/GR-), avoid heavy STL dependencies, and retain platform security mitigations.
# - CRT elimination (/NODEFAULTLIB) is strictly an opt-in experiment
#   (NW_ENABLE_CRT_ELIMINATION_EXPERIMENT=ON) with explicit initialization/cleanup obligations.
# - Unsafe section merging (/MERGE:.rdata=.text /ALIGN:16) is strictly an opt-in experiment
#   (NW_ENABLE_UNSAFE_SECTION_MERGE_EXPERIMENT=ON) preserved to reproduce CFG/CET loader conflict.

set(NW_ALLOWED_PROFILES "dev" "release" "compact_experiment")
if(NOT DEFINED NW_BUILD_PROFILE)
    set(NW_BUILD_PROFILE "release" CACHE STRING "Nodewerk build profile (dev, release, compact_experiment)")
endif()
set_property(CACHE NW_BUILD_PROFILE PROPERTY STRINGS ${NW_ALLOWED_PROFILES})

if(NOT NW_BUILD_PROFILE IN_LIST NW_ALLOWED_PROFILES)
    message(FATAL_ERROR "Invalid NW_BUILD_PROFILE='${NW_BUILD_PROFILE}'. Expected one of: ${NW_ALLOWED_PROFILES}")
endif()

option(NW_ENABLE_CRT_ELIMINATION_EXPERIMENT
    "Explicit opt-in experiment for /NODEFAULTLIB CRT elimination (requires custom entry and obligations)"
    OFF)

option(NW_ENABLE_UNSAFE_SECTION_MERGE_EXPERIMENT
    "Explicit opt-in compact experiment testing /MERGE:.rdata=.text and /ALIGN:16 with /guard:cf and /CETCOMPAT retained"
    OFF)

function(nw_apply_player_profile target_name)
    target_compile_features(${target_name} PRIVATE cxx_std_20)
    target_compile_definitions(${target_name} PRIVATE
        _HAS_EXCEPTIONS=0
        WIN32_LEAN_AND_MEAN
        NOMINMAX
    )

    if(NW_BUILD_PROFILE STREQUAL "dev")
        target_compile_definitions(${target_name} PRIVATE
            NW_PROFILE_DEV=1
            NW_ENABLE_D3D12_VALIDATION=1
            NW_DEPENDENCY_CLOSURE="dev"
        )
    elseif(NW_BUILD_PROFILE STREQUAL "release")
        target_compile_definitions(${target_name} PRIVATE
            NW_PROFILE_RELEASE=1
            NW_ENABLE_D3D12_VALIDATION=0
            NW_DEPENDENCY_CLOSURE="release"
            NDEBUG
        )
    elseif(NW_BUILD_PROFILE STREQUAL "compact_experiment")
        target_compile_definitions(${target_name} PRIVATE
            NW_PROFILE_COMPACT_EXPERIMENT=1
            NW_ENABLE_D3D12_VALIDATION=0
            NW_DEPENDENCY_CLOSURE="compact_experiment"
            NDEBUG
        )
        if(NW_ENABLE_CRT_ELIMINATION_EXPERIMENT)
            target_compile_definitions(${target_name} PRIVATE NW_CRT_EXPERIMENT=1)
        endif()
    endif()

    if(MSVC)
        # Shared player constraints across clang-cl and cl.exe:
        # 1. Disable C++ exceptions (/EHs-c-) and RTTI (/GR-)
        # 2. Preserve platform security mitigations (/GS, /guard:cf)
        # 3. Deterministic reproducible builds (/Brepro)
        target_compile_options(${target_name} PRIVATE
            /W4
            /WX
            /EHs-c-
            /GR-
            /GS
            /guard:cf
            /Zc:inline
            /Gw
            /Gy
            /GF
            /Brepro
        )
        if(CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
            target_compile_options(${target_name} PRIVATE
                -Wno-c++98-compat
                -Wno-c++98-compat-pedantic
                /clang:-ffile-prefix-map=${CMAKE_SOURCE_DIR}=.
            )
        endif()

        # Linker security mitigations retained across all profiles (no speculative disabling)
        target_link_options(${target_name} PRIVATE
            /DYNAMICBASE
            /HIGHENTROPYVA
            /NXCOMPAT
            /CETCOMPAT
            /guard:cf
            /Brepro
            /INCREMENTAL:NO
        )

        if(NW_BUILD_PROFILE STREQUAL "dev")
            set_property(TARGET ${target_name} PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreadedDebugDLL")
            target_compile_options(${target_name} PRIVATE /Od /Zi)
            target_link_options(${target_name} PRIVATE /DEBUG:FULL)
        elseif(NW_BUILD_PROFILE STREQUAL "release")
            # Fully static CRT (equivalent to /MT): supplies native startup (mainCRTStartup), /GS security
            # cookie support, and the floating-point ABI marker in one supported
            # configuration. This avoids both the forbidden vcruntime140 DLL imports and
            # the incomplete split linkage that would otherwise omit startup code.
            set_property(TARGET ${target_name} PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded")
            target_compile_options(${target_name} PRIVATE /O1 /Os)
            target_compile_definitions(${target_name} PRIVATE
                NW_CRT_LINKAGE_STATIC=1)
            if(CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
                target_compile_options(${target_name} PRIVATE -flto=thin)
            else()
                target_compile_options(${target_name} PRIVATE /GL)
                target_link_options(${target_name} PRIVATE /LTCG)
            endif()
            target_link_options(${target_name} PRIVATE
                /OPT:REF
                /OPT:ICF=4
                /PDBALTPATH:%_PDB%
            )
        elseif(NW_BUILD_PROFILE STREQUAL "compact_experiment")
            # Fully static CRT (/MT) baseline; /NODEFAULTLIB remains an explicit experiment.
            set_property(TARGET ${target_name} PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded")
            target_compile_options(${target_name} PRIVATE /O1 /Os)
            target_compile_definitions(${target_name} PRIVATE
                NW_CRT_LINKAGE_STATIC=1)
            if(CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
                target_compile_options(${target_name} PRIVATE -flto)
            else()
                target_compile_options(${target_name} PRIVATE /GL)
                target_link_options(${target_name} PRIVATE /LTCG)
            endif()
            target_link_options(${target_name} PRIVATE
                /OPT:REF
                /OPT:ICF=8
                /PDBALTPATH:%_PDB%
            )
            if(NW_ENABLE_UNSAFE_SECTION_MERGE_EXPERIMENT)
                target_link_options(${target_name} PRIVATE
                    /MERGE:.rdata=.text
                    /ALIGN:16
                )
            endif()
            if(NW_ENABLE_CRT_ELIMINATION_EXPERIMENT)
                # Explicit CRT-elimination experiment obligations:
                # Must provide WinMainCRTStartup / mainCRTStartup, __security_init_cookie, _fltused,
                # and deterministic GPU/COM cleanup before ExitProcess.
                target_link_options(${target_name} PRIVATE
                    /NODEFAULTLIB
                    /ENTRY:mainCRTStartup
                )
            endif()
        endif()
    else()
        # Host simulation / syntax verification flags on non-MSVC hosts
        target_compile_options(${target_name} PRIVATE
            -Wall
            -Wextra
            -Werror
            -fno-exceptions
            -fno-rtti
            -fstack-protector-strong
            -ffile-prefix-map=${CMAKE_SOURCE_DIR}=.
        )
    endif()
endfunction()
