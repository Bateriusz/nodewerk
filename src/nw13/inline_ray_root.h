#pragma once

#include <cstdint>

namespace nw::film {

inline constexpr std::uint32_t kSampleRootId = 1u;
inline constexpr std::uint32_t kSampleShaderId = 1u;
inline constexpr std::uint32_t kSampleTlasRootParameter = 2u;

// A typed SRV descriptor table in another root can use this parameter
// index too; never overwrite it with the sample root's acceleration structure.
template <class CommandList>
void BindSampleRootTlas(CommandList* list, std::uint32_t root_id, std::uint64_t address) {
    if (root_id == kSampleRootId)
        list->SetComputeRootShaderResourceView(kSampleTlasRootParameter, address);
}

} // namespace nw::film
