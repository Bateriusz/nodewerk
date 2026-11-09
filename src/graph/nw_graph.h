#pragma once

// NW-02: Authoring/compiler-side typed operator graph semantics.
//
// This module is deliberately independent of any graphics API. It contains no
// D3D12/DXGI types, no GPU handles, and no runtime graph interpreter/plugin ABI.
// The player never links this translation unit.

#include <array>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace nw::graph {

inline constexpr std::uint32_t kCurrentSchemaVersion = 1u;
inline constexpr std::string_view kCommonProfileId =
    "nw.common.d3d12.fl12_0.sm6_0.v1";
inline constexpr std::string_view kCommonRequirementsDigest =
    "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a";

// ---------------------------------------------------------------------------
// Stable logical vocabulary
// ---------------------------------------------------------------------------

enum class LogicalType {
    Scalar,
    Vector,
    Matrix,
    Curve,
    ShaderExpr,
    TextureRecipe,
    BufferRecipe,
    MeshRecipe,
    Material,
    SceneTable,
    Camera,
    Light,
    RenderImage,
};

std::string_view ToString(LogicalType type) noexcept;
std::optional<LogicalType> ParseLogicalType(std::string_view text);

enum class Residency {
    Logical,
    Cpu,
    Gpu,
    Readback,
};

std::string_view ToString(Residency residency) noexcept;
std::optional<Residency> ParseResidency(std::string_view text);

enum class Frequency {
    CompileTime = 0,
    Initialization = 1,
    Frame = 2,
    History = 99, // reserved for NW-09; rejected by initial exports
};

std::string_view ToString(Frequency frequency) noexcept;
std::optional<Frequency> ParseFrequency(std::string_view text);

enum class OperatorKind {
    PureShaderExpr,
    GpuPass,
    CpuControl,
};

std::string_view ToString(OperatorKind kind) noexcept;

enum class SeedBehavior {
    None,
    Explicit,
    DerivedFromGraph,
    DerivedFromUpstream,
    ExplicitNodeId,
};

std::string_view ToString(SeedBehavior behavior) noexcept;
std::optional<SeedBehavior> ParseSeedBehavior(std::string_view text);

enum class ParamType {
    Float,
    Int,
    Bool,
    String,
    Enum,
    Vec2,
    Vec3,
    Vec4,
    Color,
    Version,
    Expression,
    Mat4,
};

std::string_view ToString(ParamType type) noexcept;

enum class NormalizationRule {
    None,
    FiniteOnly,
    Clamp01,
    NormalizeVector,
};

std::string_view ToString(NormalizationRule rule) noexcept;

struct SemanticVersion {
    std::uint32_t major = 0;
    std::uint32_t minor = 0;
    std::uint32_t patch = 0;

    std::string ToString() const;
    static std::optional<SemanticVersion> Parse(std::string_view text);
};

bool operator==(const SemanticVersion& a, const SemanticVersion& b) noexcept;
bool operator!=(const SemanticVersion& a, const SemanticVersion& b) noexcept;
bool operator<(const SemanticVersion& a, const SemanticVersion& b) noexcept;

// ---------------------------------------------------------------------------
// Canonical values and schema declarations
// ---------------------------------------------------------------------------

struct ParamValue {
    ParamType type = ParamType::String;
    double number = 0.0;
    std::int64_t integer = 0;
    bool boolean = false;
    std::array<double, 16> components{};
    std::uint32_t component_count = 0;
    std::string text;

    // Full-precision authoring spelling. Never hashed; quantization policies are
    // recorded separately from this authoring value.
    std::string authoring_text;
};

struct QuantizationPolicy {
    std::string policy = "none";
    std::uint32_t bits = 0;
    double step = 0.0;
};

struct CompilerSettings {
    std::string optimization = "O2";
    bool deterministic = true;
    std::string api = "d3d12";
    std::string feature_level = "12_0";
    std::string shader_model = "6_0";
    std::string frequency_policy = "strict";
    std::string shader_blob_policy = "stable";
    QuantizationPolicy quantization;
};

struct ResourceDescriptor {
    std::string kind;
    std::uint32_t max_width = 0;
    std::uint32_t max_height = 0;
    std::uint32_t max_depth = 0;
    std::uint64_t max_bytes = 0;
    std::uint32_t max_vertices = 0;
    std::uint32_t max_indices = 0;
    std::string format_class;
    bool bounded = false;
};

struct PortSpec {
    std::string id;
    LogicalType type = LogicalType::Scalar;
    Residency residency = Residency::Cpu;
    std::uint32_t dimension = 0; // Vector/Matrix width; zero for other types.
    std::optional<ResourceDescriptor> descriptor;
};

struct ParamSpec {
    std::string id;
    ParamType type = ParamType::String;
    bool required = true;
    std::optional<ParamValue> default_value;
    NormalizationRule normalization = NormalizationRule::FiniteOnly;
    bool has_range = false;
    double min_value = 0.0;
    double max_value = 0.0;
    bool bounded_resource_dimension = false;
    std::vector<std::string> enum_values;
};

struct FrequencyPolicy {
    std::vector<Frequency> allowed;
    Frequency default_frequency = Frequency::Frame;
};

struct OperatorSchema {
    std::string id;
    SemanticVersion version{};
    OperatorKind kind = OperatorKind::CpuControl;
    std::string summary;
    FrequencyPolicy frequency_policy;
    std::vector<PortSpec> inputs;
    std::vector<PortSpec> outputs;
    std::vector<ParamSpec> params;
    SeedBehavior default_seed_behavior = SeedBehavior::None;
    std::vector<SeedBehavior> allowed_seed_behaviors;
    bool produces_resource = false;
    bool explicit_readback = false;
    bool explicit_residency_conversion = false;
    std::optional<ResourceDescriptor> resource_descriptor;
    std::string lowering_hook;
    std::string lowering_hook_version;
    std::vector<std::string> shader_operations;

    const PortSpec* FindInput(std::string_view id) const;
    const PortSpec* FindOutput(std::string_view id) const;
    const ParamSpec* FindParam(std::string_view id) const;
    Residency PrimaryResidency() const;
};

class OperatorRegistry {
public:
    bool Register(OperatorSchema schema);
    const OperatorSchema* Find(std::string_view id,
                               const SemanticVersion& version) const;
    const OperatorSchema* FindAny(std::string_view id) const;
    const std::vector<OperatorSchema>& All() const { return operators_; }

    static OperatorRegistry CreateDefault();

private:
    std::vector<OperatorSchema> operators_;
};

struct ExtensionRegistration {
    std::string extension_id;
    SemanticVersion version{};
    std::vector<OperatorSchema> operators;
    std::string lowering_namespace;
};

// Authoring/compiler registration point. This is a static in-process registry;
// there is no dynamic player plugin ABI and no runtime interpreter.
bool RegisterAuthoringExtension(OperatorRegistry& registry,
                                const ExtensionRegistration& extension,
                                std::vector<std::string>& diagnostics);

// ---------------------------------------------------------------------------
// Capability profile identity and observed (non-semantic) hardware metadata
// ---------------------------------------------------------------------------

struct CapabilityProfileIdentity {
    std::string id =
        std::string(kCommonProfileId.data(), kCommonProfileId.size());
    std::string requirements_digest = std::string(
        kCommonRequirementsDigest.data(), kCommonRequirementsDigest.size());
};

struct ObservedHardwareMetadata {
    std::string gpu_model;
    std::string gpu_generation;
    std::string adapter_description;
    std::uint32_t pci_vendor_id = 0;
    std::uint32_t pci_device_id = 0;
};

CapabilityProfileIdentity SelectTargetProfile(
    const ObservedHardwareMetadata& observed) noexcept;

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

enum class DiagnosticSeverity {
    Error,
    Warning,
    Note,
};

enum class DiagnosticCode {
    UnknownSchemaVersion,
    MigrationRequired,
    UnsupportedMigration,
    UnknownOperator,
    UnknownOperatorVersion,
    DuplicateNodeId,
    UnknownInputNode,
    UnknownInputPort,
    UnknownOutputPort,
    DuplicateInputPort,
    MissingInputPort,
    InvalidResidency,
    PortTypeMismatch,
    PortDimensionMismatch,
    ImplicitReadback,
    ImplicitUpload,
    FrequencyTransition,
    HistoryNotSupported,
    UnsupportedFrequency,
    OrdinaryCycle,
    MissingParameter,
    UnknownParameter,
    InvalidParameterType,
    NonFiniteParameter,
    ParameterOutOfRange,
    BoundedResourceExceeded,
    InvalidParameterValue,
    InvalidSeedSource,
    MissingExplicitSeed,
    UnsupportedSeedBehavior,
    MalformedCapabilityProfile,
    UnknownCapabilityProfile,
    CapabilityProfileDigestMismatch,
    InvalidCompilerSetting,
    InvalidExpression,
    EvaluationFailure,
    ParseError,
};

std::string_view ToString(DiagnosticCode code) noexcept;

struct DiagnosticSource {
    std::string node_id;
    std::string operator_id;
    std::string port_id;
    std::string source_node_id;
    std::string source_port_id;
    int line = 0;
    int column = 0;
};

struct Diagnostic {
    DiagnosticSeverity severity = DiagnosticSeverity::Error;
    DiagnosticCode code = DiagnosticCode::ParseError;
    std::string message;
    DiagnosticSource source;
    std::vector<std::string> dependency_path;
};

bool HasErrors(const std::vector<Diagnostic>& diagnostics) noexcept;
std::string DiagnosticToJson(const Diagnostic& diagnostic);

std::vector<Diagnostic> ValidateCapabilityProfile(
    const CapabilityProfileIdentity& identity);

// ---------------------------------------------------------------------------
// Graph document / snapshot
// ---------------------------------------------------------------------------

struct SeedSpec {
    SeedBehavior behavior = SeedBehavior::None;
    bool has_explicit_seed = false;
    std::uint64_t explicit_seed = 0;
    std::string node_id; // only meaningful for ExplicitNodeId
};

struct NodeInput {
    std::string port;
    std::string source_node;
    std::string source_port;
};

struct NodeDefinition {
    std::string id;
    std::string operator_id;
    SemanticVersion operator_version{};
    Frequency frequency = Frequency::Frame;
    Residency residency = Residency::Cpu;
    bool has_declared_residency = false;
    SeedSpec seed;
    std::map<std::string, ParamValue> params;
    std::vector<NodeInput> inputs;
};

struct LayoutNode {
    double x = 0.0;
    double y = 0.0;
    bool collapsed = false;
};

struct EditorLayout {
    std::map<std::string, LayoutNode> nodes;
};

struct GraphDocument {
    std::uint32_t schema_version = kCurrentSchemaVersion;
    CapabilityProfileIdentity profile;
    CompilerSettings settings;
    std::vector<NodeDefinition> nodes;
    EditorLayout layout;
    std::string source_name;
};

struct ValidationResult;
ValidationResult ValidateGraphDocument(const GraphDocument& document,
                                       const OperatorRegistry& registry);

class GraphSnapshot {
public:
    GraphSnapshot() = default;

    std::uint32_t schema_version() const noexcept { return schema_version_; }
    const CapabilityProfileIdentity& profile() const noexcept { return profile_; }
    const CompilerSettings& settings() const noexcept { return settings_; }
    const std::vector<NodeDefinition>& nodes() const noexcept { return nodes_; }
    const std::vector<std::string>& topological_order() const noexcept {
        return topological_order_;
    }
    const std::map<std::string, std::string>& structural_hashes() const noexcept {
        return structural_hashes_;
    }
    const std::string& semantic_digest() const noexcept {
        return semantic_digest_;
    }
    const std::vector<std::uint8_t>& canonical_bytes() const noexcept {
        return canonical_bytes_;
    }
    const std::string& canonical_text() const noexcept { return canonical_text_; }
    const NodeDefinition* FindNode(std::string_view id) const;
    std::string StructuralHash(std::string_view node_id) const;

private:
    friend struct ValidationResult;
    friend ValidationResult ValidateGraphDocument(const GraphDocument&,
                                                  const OperatorRegistry&);

    std::uint32_t schema_version_ = kCurrentSchemaVersion;
    CapabilityProfileIdentity profile_;
    CompilerSettings settings_;
    std::vector<NodeDefinition> nodes_;
    std::vector<std::string> topological_order_;
    std::map<std::string, std::size_t> index_by_id_;
    std::map<std::string, std::string> structural_hashes_;
    std::vector<std::uint8_t> canonical_bytes_;
    std::string canonical_text_;
    std::string semantic_digest_;
};

struct ParseResult {
    std::optional<GraphDocument> document;
    std::vector<Diagnostic> diagnostics;
    bool ok() const { return document.has_value() && !HasErrors(diagnostics); }
};

struct ValidationResult {
    std::optional<GraphSnapshot> snapshot;
    std::vector<Diagnostic> diagnostics;
    bool ok() const { return snapshot.has_value() && !HasErrors(diagnostics); }
};

struct MigrationResult {
    std::optional<GraphDocument> document;
    std::vector<Diagnostic> diagnostics;
    bool ok() const { return document.has_value() && !HasErrors(diagnostics); }
};

struct MigrationDescriptor {
    std::uint32_t from = 0;
    std::uint32_t to = 0;
    std::string id;
    std::string description;
};

std::vector<MigrationDescriptor> AvailableMigrations();

ParseResult ParseGraphText(std::string_view text,
                           const OperatorRegistry& registry,
                           std::string source_name = "<memory>");

MigrationResult MigrateGraphDocument(const GraphDocument& document,
                                     const OperatorRegistry& registry,
                                     std::uint32_t target_version =
                                         kCurrentSchemaVersion);

// Re-emits a normal (v1) textual document, including editor layout. This is an
// authoring/migration utility; it is not the canonical semantic encoding.
std::string EmitDocumentText(const GraphDocument& document);

// ---------------------------------------------------------------------------
// Canonical encoding, hashing, and seed derivation
// ---------------------------------------------------------------------------

std::array<std::uint8_t, 32> Sha256Bytes(const std::uint8_t* data,
                                         std::size_t size);
std::string Sha256Hex(std::string_view bytes);
std::string Sha256Digest(std::string_view bytes);
std::string OperatorContractDigest(const OperatorSchema& schema);
std::string CanonicalNumberText(double value);
std::string ParamCanonicalText(const ParamValue& value);

// Fixed, documented canonical field records used by golden-digest tests.
std::map<std::string, std::string> GoldenFieldDigests();

struct EvaluationContext;

std::uint64_t DeriveSeed(const GraphSnapshot& snapshot,
                         const NodeDefinition& node,
                         const EvaluationContext& context);

// ---------------------------------------------------------------------------
// CPU reference evaluation
// ---------------------------------------------------------------------------

struct EvaluationContext {
    Frequency frequency = Frequency::Frame;
    std::uint64_t frame_index = 0;
    double time_seconds = 0.0;
    std::uint64_t seed_base = 0;
    unsigned worker_count = 1; // accepted for scheduling parity; evaluation is deterministic
};

struct EvaluationValue {
    LogicalType type = LogicalType::Scalar;
    Residency residency = Residency::Cpu;
    std::uint32_t component_count = 0;
    std::array<double, 16> numbers{};
    std::string text;
    std::vector<double> samples;

    std::string CanonicalText() const;
};

// LogicalValue is the typed value contract exchanged between operators. The
// evaluator uses the same representation for CPU reference results.
using LogicalValue = EvaluationValue;

using OutputValues = std::map<std::string, LogicalValue>;

struct CacheStats {
    std::uint64_t hits = 0;
    std::uint64_t misses = 0;
    std::uint64_t regenerated = 0;
};

struct EvaluationResult {
    bool ok = false;
    std::vector<Diagnostic> diagnostics;
    std::map<std::string, OutputValues> values_by_node;
    std::map<std::string, bool> cache_hit_by_node;
    CacheStats stats;
};

class GraphEvaluator {
public:
    explicit GraphEvaluator(const GraphSnapshot& snapshot);

    EvaluationResult Evaluate(const EvaluationContext& context);

    // Invalidates only the changed node and its transitive dependent closure.
    void ApplyInputChange(const std::string& node_id);

    // Marks entries corrupt; the next evaluation treats them as cache misses.
    void CorruptCacheEntry(const std::string& node_id);

    bool HasValidCacheEntry(const std::string& node_id) const;

private:
    struct CacheEntry {
        std::string key;
        OutputValues outputs;
        bool invalid = false;
        bool corrupt = false;
    };

    const GraphSnapshot& snapshot_;
    std::map<std::string, std::vector<std::string>> dependents_;
    std::map<std::string, std::vector<CacheEntry>> entries_;
};

// ---------------------------------------------------------------------------
// Compiler acceptance
// ---------------------------------------------------------------------------

struct CompilerAcceptance {
    bool accepted = false;
    std::string profile_id;
    std::string requirements_digest;
    std::string semantic_digest;
    std::vector<Diagnostic> diagnostics;
};

CompilerAcceptance AcceptGraphForCompilation(
    const GraphDocument& document,
    const ObservedHardwareMetadata& observed,
    bool gpu_available,
    const OperatorRegistry& registry);

// ---------------------------------------------------------------------------
// Authoring session: invalid edits never replace the last valid snapshot.
// ---------------------------------------------------------------------------

class AuthoringSession {
public:
    explicit AuthoringSession(
        OperatorRegistry registry = OperatorRegistry::CreateDefault());

    ValidationResult ApplyEdit(std::string_view text,
                               std::string source_name = "<edit>");

    const GraphSnapshot* LastValid() const {
        return last_valid_ ? &*last_valid_ : nullptr;
    }
    const EditorLayout& LastLayout() const { return last_layout_; }
    const std::vector<Diagnostic>& LastDiagnostics() const {
        return last_diagnostics_;
    }

private:
    OperatorRegistry registry_;
    std::optional<GraphSnapshot> last_valid_;
    EditorLayout last_layout_;
    std::vector<Diagnostic> last_diagnostics_;
};

} // namespace nw::graph
