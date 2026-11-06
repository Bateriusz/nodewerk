# Nodewerk NW-01 Offline Shader Compilation Rules
# Compiles HLSL shaders offline to Shader Model 6.0 (cs_6_0) DXIL at build time.
# Runtime shader compilation (dxcompiler.dll / D3DCompile) and Agility SDK redistribution
# are strictly forbidden in the baseline.

find_program(NW_DXC_EXECUTABLE NAMES dxc dxc.exe)
find_package(Python3 COMPONENTS Interpreter QUIET)

function(nw_add_offline_compute_shader target_name hlsl_file entry_point profile output_dxil output_header)
    if(NW_DXC_EXECUTABLE)
        add_custom_command(
            OUTPUT "${output_dxil}"
            COMMAND "${NW_DXC_EXECUTABLE}"
                -T "${profile}"
                -E "${entry_point}"
                -HV 2021
                -O3
                -Qstrip_reflect
                -Qstrip_debug
                -Fo "${output_dxil}"
                "${hlsl_file}"
            DEPENDS "${hlsl_file}"
            COMMENT "Compiling offline SM 6.0 shader ${hlsl_file} (${entry_point}) via DXC"
            VERBATIM
        )
    endif()

    if(Python3_Interpreter_FOUND)
        add_custom_target(${target_name}
            COMMAND "${Python3_EXECUTABLE}" "${CMAKE_SOURCE_DIR}/tools/offline_shader_pack.py"
            BYPRODUCTS "${output_dxil}" "${output_header}"
            DEPENDS "${hlsl_file}"
            COMMENT "Verifying offline SM 6.0 DXIL container and embedded C++ header"
            VERBATIM
        )
    else()
        add_custom_target(${target_name}
            DEPENDS "${hlsl_file}"
        )
    endif()
endfunction()
