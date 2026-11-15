# NW-08 authoring target injection for a repository-root CMake configure.
#
# CMAKE_PROJECT_INCLUDE is evaluated after every project() command, including
# nested projects.  Only the repository-root project may schedule the authoring
# target injection; src/studio's own project() must not recurse into this hook.
#
# The deferred call runs at the end of the root directory after the immutable
# NW-07 player/package targets exist, while CMAKE_SOURCE_DIR remains the actual
# repository root for the frozen offline-shader rules.

if(PROJECT_SOURCE_DIR STREQUAL CMAKE_SOURCE_DIR)
  include("${CMAKE_CURRENT_LIST_DIR}/../../cmake/Nw08Studio.cmake")
  cmake_language(DEFER CALL nw_add_studio_authoring_targets)
endif()
