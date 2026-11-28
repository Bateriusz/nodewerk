#include "package_loader.h"
#include "package_player.h"
#include "nw13/inline_ray_root.h"

#include <algorithm>
#include <cstdint>
#include <cstdio>

struct RootRecorder {
    unsigned writes = 0;
    std::uint32_t slot = 0;
    std::uint64_t address = 0;
    void SetComputeRootShaderResourceView(std::uint32_t root_slot, std::uint64_t gpu_address) {
        ++writes;
        slot = root_slot;
        address = gpu_address;
    }
};

int main(int argc, char** argv) {
    if (argc != 2) return 1;
    RootRecorder restore;
    nw::film::BindSampleRootTlas(&restore, 1u, 100u);
    // The player switches from sample root 1 to feedback root 2, temporarily
    // uses its own display root to present, then restores feedback root 2.
    nw::film::BindSampleRootTlas(&restore, 2u, 200u);
    if (restore.writes != 1 || restore.slot != 2u || restore.address != 100u) return 4;
    const auto loaded = nw::player::LoadPackageFromFile(argv[1]);
    if (loaded.exit_code != nw::player::ExitCode::Success) {
        return static_cast<int>(loaded.exit_code);
    }
    auto rejected = [](const nw::package::Package& invalid) {
        std::vector<nw::package::Diagnostic> notes;
        if (nw::package::ValidatePackage(invalid, notes)) return false;
        return std::any_of(notes.begin(), notes.end(), [](const auto& note) {
            return note.code == "package_history_sampled_schedule";
        });
    };
    for (const std::size_t destination : {1u, 7u}) {
        auto malformed = loaded.package;
        auto step = malformed.frame[4];
        malformed.frame.erase(malformed.frame.begin() + 4);
        malformed.frame.insert(malformed.frame.begin() + destination, step);
        if (!rejected(malformed)) return 5;
    }
    auto malformed = loaded.package;
    malformed.frame[12].operands[2] = malformed.frame[13].operands[2];
    if (!rejected(malformed)) return 6;
    nw::player::PackagePlayer player;
    if (!player.BuildFramePlanForTesting(loaded.package)) {
        std::fprintf(stderr, "%s\n", player.diagnostics_json().c_str());
        return 2;
    }
    constexpr std::uint64_t largest_uint = UINT32_MAX;
    constexpr std::uint64_t ticks[] = {0, 399, 400, 288000, 192000, 1200, 148800,
                                       largest_uint, largest_uint + 1u};
    for (auto tick : ticks) {
        const auto previous_work = player.history_steps_executed();
        if (!player.ApplyFramePlanForTesting(tick) ||
            player.history_steps_executed() != previous_work + 1 ||
            player.history_pending_ticks() != 0 ||
            player.last_root_constants()[0] != std::min(tick, largest_uint)) {
            std::fprintf(stderr, "planner failed at tick %llu: %s\n",
                         static_cast<unsigned long long>(tick), player.diagnostics_json().c_str());
            return 3;
        }
    }
    std::printf("Scene History verified %llu bounded predecessor steps\n",
                static_cast<unsigned long long>(player.history_steps_executed()));
}
