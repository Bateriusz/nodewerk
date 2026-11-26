#pragma once

namespace nw::validation {
// Shared measurement/validation failures; player errors retain their own codes.
enum class HarnessExitCode : int {
    InvalidMeasurementContract = 80,
    ArtifactIdentityUnavailable = 81,
    EvidenceIoFailure = 82,
    PresentationSourceGeometryMismatch = 83,
    PresentationClientGeometryMismatch = 84,
    PresentationWindowClosed = 85,
    MeasurementUnavailable = 86,
    CaptureValidationFailed = 87,
    UnhandledFailure = 89,
};
} // namespace nw::validation
