#pragma once

#include "capability_requirements.h"
#include "exit_codes.h"
#include "package/nwp_format.h"

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace nw::player {

struct PackageLoadResult {
    ExitCode exit_code = ExitCode::Success;
    package::Package package{};
    std::string package_sha256;
    std::uint64_t package_size_bytes = 0u;
    std::vector<CapabilityRequirementFailure> failures;
};

// Reads at most the NW-03 bounded package limit, verifies the container
// checksum and all structural contracts, then validates the exact emitted
// requirements.  No GPU resource is allocated here.
PackageLoadResult LoadPackageFromBytes(const std::uint8_t* data,
                                      std::size_t size_bytes);

PackageLoadResult LoadPackageFromFile(const char* path);

} // namespace nw::player
