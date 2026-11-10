// NW-03 bounded `.nwp` reader fuzz harness.
//
// Build with AddressSanitizer + UndefinedBehaviorSanitizer and feed it corpus
// files.  Invalid packages are expected and return 0; any memory-safety,
// undefined-behavior, or unexpected termination is a sanitizer failure.

#include "nwp_format.h"

#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s <candidate.nwp>\n", argv[0]);
        return 2;
    }
    std::ifstream input(argv[1], std::ios::binary);
    if (!input) {
        std::fprintf(stderr, "cannot read %s\n", argv[1]);
        return 2;
    }
    std::vector<std::uint8_t> data(
        (std::istreambuf_iterator<char>(input)),
        std::istreambuf_iterator<char>());
    if (data.empty()) {
        return 0;
    }
    nw::package::Package package;
    std::vector<nw::package::Diagnostic> diagnostics;
    // Exercise both the integrity-gated and structural parser paths.
    (void)nw::package::DecodePackage(data.data(), data.size(), package,
                                     diagnostics, false, nw::package::kMaxPackageBytes,
                                     nw::package::kMaxSectionBytes, true);
    diagnostics.clear();
    (void)nw::package::DecodePackage(data.data(), data.size(), package,
                                     diagnostics, false, nw::package::kMaxPackageBytes,
                                     nw::package::kMaxSectionBytes, false);
    return 0;
}
