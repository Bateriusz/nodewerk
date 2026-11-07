# Nodewerk NW-01 Automated Dependency Closure Verification
# Ensures development and release executables maintain separate dependency closures,
# and fails the build if unexpected imports or required sidecars appear.

find_package(Python3 COMPONENTS Interpreter QUIET)

function(nw_attach_dependency_check target_name fixture_id)
    # Isolate each fixture in its own deployment directory so the post-build dependency
    # check inventories exactly that fixture's payload tree (plus a flat staging copy for
    # clean-system execution). Sibling fixture executables are therefore never mistaken
    # for unexpected sidecars.
    set(_nw_deploy_root "${CMAKE_BINARY_DIR}/deploy")
    set(_nw_fixture_deploy_dir "${_nw_deploy_root}/${fixture_id}")
    set_target_properties(${target_name} PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY "${_nw_fixture_deploy_dir}"
        RUNTIME_OUTPUT_DIRECTORY_DEBUG "${_nw_fixture_deploy_dir}"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${_nw_fixture_deploy_dir}"
        RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${_nw_fixture_deploy_dir}"
        RUNTIME_OUTPUT_DIRECTORY_MINSIZEREL "${_nw_fixture_deploy_dir}")

    if(Python3_Interpreter_FOUND)
        add_custom_command(
            TARGET ${target_name}
            POST_BUILD
            COMMAND "${CMAKE_COMMAND}" -E make_directory "${_nw_deploy_root}"
            COMMAND "${CMAKE_COMMAND}" -E copy_if_different
                "$<TARGET_FILE:${target_name}>"
                "${_nw_deploy_root}/$<TARGET_FILE_NAME:${target_name}>"
            COMMAND "${Python3_EXECUTABLE}"
                "${CMAKE_SOURCE_DIR}/tools/dependency_check.py"
                --binary "$<TARGET_FILE:${target_name}>"
                --deployment-root "${_nw_fixture_deploy_dir}"
                --profile "${NW_BUILD_PROFILE}"
                --fixture "${fixture_id}"
                --inventory "${CMAKE_SOURCE_DIR}/benchmarks/dependency-inventory.v1.json"
            COMMENT "Verifying dependency closure for ${target_name} (${NW_BUILD_PROFILE})"
            VERBATIM
        )
    endif()
endfunction()
