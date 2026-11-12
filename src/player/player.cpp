#include "player.h"

// The task-level Player name is an alias for the concrete PackagePlayer.  This
// translation unit keeps the public facade compiled with the same profile flags
// as the implementation.

namespace nw::player {

const char* PlayerRuntimeName() noexcept { return "nw.player.d3d12"; }

} // namespace nw::player
