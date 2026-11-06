# Nodewerk NW-01 Automated Dependency Closure Verification
# Ensures development and release executables maintain separate dependency closures,
# and fails the build if unexpected imports or required sidecars appear.

find_package(Python3 COMPONENTS Interpreter QUIET)

function(nw_attach_dependency_check target_name fixture_id)
    if(Python3_Interpreter_FOUND)
        add_custom_command(
            TARGET ${target_name}
            POST_BUILD
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_SOURCE_DIR}/tools/dependency_check.py"
                --binary "$<TARGET_FILE:${target_name}>"
                --profile "${NW_BUILD_PROFILE}"
                --fixture "${fixture_id}"
                --inventory "${CMAKE_SOURCE_DIR}/benchmarks/dependency-inventory.v1.json"
            COMMENT "Verifying dependency closure for ${target_name} (${NW_BUILD_PROFILE})"
            VERBATIM
        )
    endif()
endfunction()
