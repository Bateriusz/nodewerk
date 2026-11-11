# Nodewerk NW-01 Offline Shader Compilation Rules
# Compiles HLSL shaders offline to Shader Model 6.0 (cs_6_0) DXIL at build time
# into ${CMAKE_CURRENT_BINARY_DIR} without mutating tracked source-tree files.
# Runtime shader compilation (dxcompiler.dll / D3DCompile) and Agility SDK redistribution
# are strictly forbidden in the baseline.

find_program(NW_DXC_EXECUTABLE NAMES dxc dxc.exe)
find_package(Python3 COMPONENTS Interpreter QUIET)

function(nw_add_offline_compute_shader target_name hlsl_file entry_point profile output_dxil output_header)
    set(_symbol_name "kComputeFixtureDxil")
    if(ARGC GREATER 6)
        set(_symbol_name "${ARGV6}")
    endif()
    get_filename_component(_dxil_dir "${output_dxil}" DIRECTORY)
    get_filename_component(_header_dir "${output_header}" DIRECTORY)

    # Guard against accidental source-tree mutation for both DXIL and C++ header outputs
    foreach(_out_artifact IN ITEMS "${output_dxil}" "${output_header}")
        file(RELATIVE_PATH _rel_to_src "${CMAKE_SOURCE_DIR}" "${_out_artifact}")
        if(NOT _rel_to_src MATCHES "^\\.\\.")
            file(RELATIVE_PATH _rel_to_bin "${CMAKE_BINARY_DIR}" "${_out_artifact}")
            if(_rel_to_bin MATCHES "^\\.\\.")
                message(FATAL_ERROR
                    "[NW-SHADER-ERR] Offline shader output '${_out_artifact}' must reside inside "
                    "CMAKE_BINARY_DIR ('${CMAKE_BINARY_DIR}'), never in the tracked source tree.")
            endif()
        endif()
    endforeach()

    if(NOT NW_DXC_EXECUTABLE)
        message(FATAL_ERROR
            "[NW-SHADER-ERR] Offline DXC shader compiler (dxc / dxc.exe) not found in PATH. "
            "Nodewerk NW-01 requires offline SM 6.0 shader compilation via pinned DXC (${NW_PINNED_DXC_VERSION}) "
            "and forbids synthetic containers or runtime shader compilation.")
    endif()

    if(Python3_Interpreter_FOUND)
        add_custom_command(
            OUTPUT "${output_dxil}" "${output_header}"
            COMMAND "${CMAKE_COMMAND}" -E make_directory "${_dxil_dir}" "${_header_dir}"
            COMMAND "${NW_DXC_EXECUTABLE}"
                -T "${profile}"
                -E "${entry_point}"
                -HV 2021
                -O3
                -Qstrip_reflect
                -Qstrip_debug
                -Fo "${output_dxil}"
                "${hlsl_file}"
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_SOURCE_DIR}/tools/offline_shader_pack.py"
                --mode pack-header
                --repo-root "${CMAKE_SOURCE_DIR}"
                --cxx-compiler "${CMAKE_CXX_COMPILER}"
                --require-cpp-validator
                --hlsl "${hlsl_file}"
                --dxil "${output_dxil}"
                --output-header "${output_header}"
                --symbol "${_symbol_name}"
            DEPENDS
                "${hlsl_file}"
                "${CMAKE_SOURCE_DIR}/tools/offline_shader_pack.py"
                "${CMAKE_SOURCE_DIR}/src/player/capability_check.cpp"
                "${CMAKE_SOURCE_DIR}/src/player/capability_check.h"
                "${CMAKE_SOURCE_DIR}/src/player/exit_codes.h"
            COMMENT "Compiling offline SM 6.0 shader ${hlsl_file} (${entry_point}) via DXC into build directory"
            VERBATIM
        )
    else()
        # Hermetic fallback when Python3 is absent: use DXC's built-in -Fh / -Vn C++ header emission directly
        add_custom_command(
            OUTPUT "${output_dxil}" "${output_header}"
            COMMAND "${CMAKE_COMMAND}" -E make_directory "${_dxil_dir}" "${_header_dir}"
            COMMAND "${NW_DXC_EXECUTABLE}"
                -T "${profile}"
                -E "${entry_point}"
                -HV 2021
                -O3
                -Qstrip_reflect
                -Qstrip_debug
                -Fo "${output_dxil}"
                -Fh "${output_header}"
                -Vn "${_symbol_name}"
                "${hlsl_file}"
            DEPENDS "${hlsl_file}"
            COMMENT "Compiling offline SM 6.0 shader ${hlsl_file} (${entry_point}) via DXC (-Fo/-Fh) into build directory"
            VERBATIM
        )
    endif()

    add_custom_target(${target_name}
        DEPENDS "${output_dxil}" "${output_header}"
    )
endfunction()
