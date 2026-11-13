#include "package/nwp_format.h"
#include "player/capability_requirements.h"

#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr,
                     "usage: nw06_capability_check <common.nwp>\n");
        return 2;
    }
    std::ifstream input(argv[1], std::ios::binary);
    if (!input) {
        std::fprintf(stderr, "cannot open package: %s\n", argv[1]);
        return 2;
    }
    const std::vector<char> raw(
        (std::istreambuf_iterator<char>(input)),
        std::istreambuf_iterator<char>());
    const auto* bytes =
        reinterpret_cast<const std::uint8_t*>(raw.data());
    nw::package::Package package;
    std::vector<nw::package::Diagnostic> diagnostics;
    if (!nw::package::DecodePackage(
            bytes, raw.size(), package, diagnostics,
            /*allow_dev=*/false, nw::package::kMaxPackageBytes,
            nw::package::kMaxSectionBytes, /*verify_integrity=*/true)) {
        std::fprintf(stderr, "package decode failed\n");
        return 1;
    }
    const nw::player::PackageCapabilityEvaluation evaluation =
        nw::player::ValidateEmittedRequirementsAgainstPackage(package);
    if (!evaluation.ok()) {
        for (const auto& failure : evaluation.failures) {
            std::fprintf(stderr, "%s: %s (required %s, observed %s)\n",
                         failure.code.c_str(),
                         failure.detail.c_str(),
                         failure.required_value.c_str(),
                         failure.observed_value.c_str());
        }
        return 1;
    }
    std::printf(
        "NW06_NATIVE_CAPABILITY_OK descriptors=%u root_dwords=%u\n",
        package.capability.max_cbv_srv_uav_descriptors_per_frame,
        package.capability.max_root_signature_dword_cost);
    return 0;
}
