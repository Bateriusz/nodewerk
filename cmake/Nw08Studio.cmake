# NW-08 authoring-only target hook.
#
# The frozen NW-07 root CMakeLists.txt must not be modified.  Authoring builds
# that explicitly opt in can include this module and call
# `nw_add_studio_authoring_targets()` after the NW-04 player runtime target is
# defined.  Nothing here is part of the release player closure.

function(nw_add_studio_authoring_targets)
  if(NOT WIN32)
    message(STATUS "NW-08 nodewerk-studio native targets are Windows-only")
    return()
  endif()
  if(NOT TARGET nw_package_player OR NOT TARGET nw_player_bootstrap)
    message(STATUS
      "NW-08 studio authoring targets require nw_package_player and "
      "nw_player_bootstrap")
    return()
  endif()
  add_subdirectory("${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../src/studio"
                   "${CMAKE_BINARY_DIR}/nw08-studio")
endfunction()
