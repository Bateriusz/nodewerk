#include "package_loader.h"

#include <cstdio>
#include <cstring>
#include <limits>
#include <string>
#include <utility>
#include <vector>

namespace nw::player {
namespace {

CapabilityRequirementFailure PackageDiagnosticToFailure(
    const package::Diagnostic& diagnostic,
    const package::Package& pkg) {
    CapabilityRequirementFailure failure{};
    failure.code = diagnostic.code.empty() ? "package_validation"
                                           : diagnostic.code;
    failure.requirement = diagnostic.section.empty() ? "package"
                                                     : diagnostic.section;
    failure.required_value = "valid .nwp v1 container";
    failure.observed_value = diagnostic.has_offset
                                 ? "offset=" + std::to_string(diagnostic.offset)
                                 : "invalid";
    failure.profile_id = pkg.capability.profile_id;
    failure.requirements_digest = pkg.capability.requirements_digest;
    failure.detail = diagnostic.message;
    return failure;
}

PackageLoadResult LoadFailure(ExitCode code,
                              const char* code_name,
                              const char* detail) {
    PackageLoadResult result{};
    result.exit_code = code;
    CapabilityRequirementFailure failure{};
    failure.code = code_name;
    failure.requirement = "package";
    failure.required_value = "valid .nwp v1 container";
    failure.observed_value = "unavailable";
    failure.detail = detail;
    result.failures.push_back(std::move(failure));
    return result;
}

} // namespace

PackageLoadResult LoadPackageFromBytes(const std::uint8_t* data,
                                      std::size_t size_bytes) {
    PackageLoadResult result{};
    if (data == nullptr || size_bytes == 0u) {
        return LoadFailure(ExitCode::PackageValidationFailed,
                           "package_missing",
                           "package bytes are null or empty");
    }
    if (size_bytes > package::kMaxPackageBytes) {
        return LoadFailure(ExitCode::PackageValidationFailed,
                           "package_decode_limit",
                           "package exceeds the bounded v1 decode limit");
    }
    std::vector<package::Diagnostic> diagnostics;
    result.package = package::Package{};
    if (!package::DecodePackage(data, size_bytes, result.package, diagnostics,
                                /*allow_dev=*/false,
                                package::kMaxPackageBytes,
                                package::kMaxSectionBytes,
                                /*verify_integrity=*/true)) {
        result.exit_code = ExitCode::PackageValidationFailed;
        for (const auto& diagnostic : diagnostics) {
            result.failures.push_back(
                PackageDiagnosticToFailure(diagnostic, result.package));
        }
        if (result.failures.empty()) {
            result.failures.push_back(PackageDiagnosticToFailure(
                package::Diagnostic{"package_validation",
                                    "package decode failed without diagnostics",
                                    "", 0u, false},
                result.package));
        }
        return result;
    }

    result.package_size_bytes = static_cast<std::uint64_t>(size_bytes);
    result.package_sha256 = package::Sha256Hex(data, size_bytes);
    const PackageCapabilityEvaluation exact =
        ValidateEmittedRequirementsAgainstPackage(result.package);
    if (!exact.ok()) {
        result.exit_code = ExitCode::PackageValidationFailed;
        result.failures = exact.failures;
        return result;
    }
    result.exit_code = ExitCode::Success;
    return result;
}

PackageLoadResult LoadPackageFromFile(const char* path) {
    if (path == nullptr || path[0] == '\0') {
        return LoadFailure(ExitCode::InvalidArguments, "package_path_missing",
                           "package path is empty");
    }
    std::FILE* file = std::fopen(path, "rb");
    if (file == nullptr) {
        return LoadFailure(ExitCode::PackageValidationFailed,
                           "package_open_failed",
                           "package could not be opened for reading");
    }
    if (std::fseek(file, 0, SEEK_END) != 0) {
        std::fclose(file);
        return LoadFailure(ExitCode::PackageValidationFailed,
                           "package_size_unavailable",
                           "package size query failed");
    }
    const long file_size = std::ftell(file);
    if (file_size < 0 ||
        static_cast<std::uint64_t>(file_size) > package::kMaxPackageBytes) {
        std::fclose(file);
        return LoadFailure(ExitCode::PackageValidationFailed,
                           "package_decode_limit",
                           "package size is invalid or exceeds the bounded limit");
    }
    if (std::fseek(file, 0, SEEK_SET) != 0) {
        std::fclose(file);
        return LoadFailure(ExitCode::PackageValidationFailed,
                           "package_read_failed",
                           "package seek failed");
    }
    std::vector<std::uint8_t> bytes(static_cast<std::size_t>(file_size));
    if (!bytes.empty()) {
        const std::size_t read =
            std::fread(bytes.data(), 1u, bytes.size(), file);
        if (read != bytes.size()) {
            std::fclose(file);
            return LoadFailure(ExitCode::PackageValidationFailed,
                               "package_read_failed",
                               "package read was shorter than its declared size");
        }
    }
    std::fclose(file);
    return LoadPackageFromBytes(bytes.data(), bytes.size());
}

} // namespace nw::player
