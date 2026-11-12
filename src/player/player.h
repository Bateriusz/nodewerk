#pragma once

// Convenience facade for the NW-04 package player.  `PackagePlayer` is the
// concrete implementation; `Player` is the stable name used by fixture code and
// the task-level initialize(package)/renderAtTick(tick)/resize/shutdown
// contract.

#include "package_player.h"

namespace nw::player {

using Player = PackagePlayer;
const char* PlayerRuntimeName() noexcept;

} // namespace nw::player
