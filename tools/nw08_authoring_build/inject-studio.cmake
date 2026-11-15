# NW-08 authoring target injection for a repository-root CMake configure.
#
# CMAKE_PROJECT_INCLUDE loads this immediately after the frozen root project()
# call.  Defer target creation until the end of that directory so the immutable
# NW-07 player/package targets already exist, while CMAKE_SOURCE_DIR remains the
# actual repository root for the frozen offline-shader rules.

include("${CMAKE_CURRENT_LIST_DIR}/../../cmake/Nw08Studio.cmake")
cmake_language(DEFER CALL nw_add_studio_authoring_targets)
