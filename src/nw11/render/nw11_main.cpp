// Entry point for the NW-11 baseline, mesh-shader, ray-query, and fallback
// artifacts.  The compile-time NW11_VARIANT_* definition selects the single
// pipeline that exists in this executable; unused optional implementations
// and shaders are stripped from the other artifacts.

#include "nw11_scene_harness.h"

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

bool Consume(int& index, int argc, char** argv, const char* flag,
             std::string& value) {
    if (std::strcmp(argv[index], flag) != 0) {
        return false;
    }
    if (index + 1 >= argc) {
        std::fprintf(stderr, "missing value for %s\n", flag);
        value.clear();
        ++index;
        return true;
    }
    value = argv[++index];
    return true;
}

bool ConsumeU32(int& index, int argc, char** argv, const char* flag,
                std::uint32_t& value) {
    std::string text;
    if (!Consume(index, argc, argv, flag, text)) {
        return false;
    }
    try {
        value = static_cast<std::uint32_t>(std::stoul(text));
    } catch (...) {
        std::fprintf(stderr, "invalid unsigned value for %s\n", flag);
        value = 0;
    }
    return true;
}

bool ConsumeU64(int& index, int argc, char** argv, const char* flag,
                std::uint64_t& value) {
    std::string text;
    if (!Consume(index, argc, argv, flag, text)) {
        return false;
    }
    try {
        value = std::stoull(text);
    } catch (...) {
        std::fprintf(stderr, "invalid unsigned value for %s\n", flag);
        value = 0;
    }
    return true;
}

std::vector<std::string> Split(const std::string& value, char separator) {
    std::vector<std::string> result;
    std::size_t begin = 0;
    while (begin <= value.size()) {
        const std::size_t end = value.find(separator, begin);
        const std::string item = value.substr(
            begin, end == std::string::npos ? std::string::npos : end - begin);
        if (!item.empty()) {
            result.push_back(item);
        }
        if (end == std::string::npos) {
            break;
        }
        begin = end + 1;
    }
    return result;
}

}  // namespace

int main(int argc, char** argv) {
    nw11::RendererOptions options;
#if NW11_VARIANT_MESH
    options.variant = nw11::Variant::OptionalMesh;
#elif NW11_VARIANT_RAY
    options.variant = nw11::Variant::OptionalRayQuery;
#else
    options.variant = nw11::Variant::CommonBaseline;
#endif
    for (int index = 1; index < argc; ++index) {
        std::string text;
        if (std::strcmp(argv[index], "--self-check") == 0) {
            options.self_check = true;
        } else if (Consume(index, argc, argv, "--benchmark-json", text)) {
            options.benchmark_json = text;
        } else if (Consume(index, argc, argv, "--capture-dir", text)) {
            options.capture_dir = text;
        } else if (Consume(index, argc, argv, "--capture-manifest", text)) {
            options.capture_manifest = text;
        } else if (Consume(index, argc, argv, "--role", text)) {
            options.role = text;
        } else if (Consume(index, argc, argv, "--artifact-path", text)) {
            options.artifact_path = text;
        } else if (Consume(index, argc, argv, "--device-tier", text)) {
            options.device_tier = text;
        } else if (Consume(index, argc, argv, "--device-key", text)) {
            options.device_key = text;
        } else if (Consume(index, argc, argv, "--environment-key", text)) {
            options.environment_key = text;
        } else if (Consume(index, argc, argv, "--adapter-luid", text)) {
            options.adapter_luid = text;
        } else if (Consume(index, argc, argv, "--capability-decision", text)) {
            options.capability_decision_path = text;
        } else if (Consume(index, argc, argv, "--reference-kind", text)) {
            options.reference_kind = text;
        } else if (Consume(index, argc, argv, "--fixed-quality", text)) {
            options.fixed_quality = text;
        } else if (Consume(index, argc, argv, "--shader-ids", text)) {
            options.shader_ids = Split(text, ';');
        } else if (ConsumeU32(
                       index, argc, argv, "--warmup-seconds",
                       options.warmup_seconds)) {
        } else if (ConsumeU32(index, argc, argv, "--runs", options.runs)) {
        } else if (ConsumeU32(
                       index, argc, argv, "--run-seconds",
                       options.run_seconds)) {
        } else if (ConsumeU64(
                       index, argc, argv, "--distribution-bytes",
                       options.distribution_bytes)) {
        } else {
            std::fprintf(
                stderr, "[NW11-RENDER-ERR][exit_code=2] unknown argument '%s'\n",
                argv[index]);
            return 2;
        }
    }
    return nw11::RunSceneRenderer(options);
}
