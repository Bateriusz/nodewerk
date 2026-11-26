"""Shared renderer exit-code taxonomy and evidence-bound failure reports."""
from __future__ import annotations

import re
from pathlib import Path

RENDERER_EXIT_NAMES = {
    0: "Success",
    3: "OptionalFeatureUnsupported",
    10: "InvalidArguments",
    11: "OsVersionUnsupported",
    12: "WindowCreationFailed",
    20: "DxgiFactoryCreationFailed",
    21: "AdapterEnumerationFailed",
    22: "D3D12DeviceCreationFailed",
    23: "CapabilityBaselineUnmet",
    24: "OptionalFeatureUnsupported",
    25: "InboxRuntimeBlocker",
    26: "DebugLayerUnavailable",
    30: "CommandQueueCreationFailed",
    31: "SwapChainCreationFailed",
    32: "DescriptorHeapCreationFailed",
    33: "ResourceAllocationFailed",
    34: "CommandListRecordingFailed",
    35: "PresentationFailed",
    36: "PresentationOccluded",
    40: "OfflineShaderPayloadInvalid",
    41: "RootSignatureCreationFailed",
    42: "PipelineStateCreationFailed",
    50: "ExecutionFenceTimeout",
    51: "DeviceRemoved",
    52: "GpuSubmissionFailed",
    60: "ComputeVerificationFailed",
    61: "D3D12ValidationError",
    70: "PackageValidationFailed",
    80: "InvalidMeasurementContract",
    81: "ArtifactIdentityUnavailable",
    82: "EvidenceIoFailure",
    83: "PresentationSourceGeometryMismatch",
    84: "PresentationClientGeometryMismatch",
    85: "PresentationWindowClosed",
    86: "MeasurementUnavailable",
    87: "CaptureValidationFailed",
    89: "UnhandledHarnessFailure",
}

def renderer_failure(launch: dict, log_path: Path) -> dict:
    """Preserve the actual process status and diagnostic without conflating them."""
    code = launch["exit_code"]
    timed_out = launch["timed_out"]
    diagnostic = {}
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(("[NW11-PRODUCTION-ERR]", "[NODEWERK-RENDERER-ERR]")):
                tags = dict(re.findall(r"\[(\w+)=([^\]]+)\]", line))
                diagnostic = {
                    "native_code": tags.get("code"),
                    "player_exit_code": int(tags["player_exit_code"]) if tags.get("player_exit_code", "").isdigit() else None,
                    "message": re.sub(r"^\[[^\]]+\](?:\[[^\]]+\])*\s*", "", line),
                }
    return {
        "format": "nodewerk-validation-failure", "version": 1,
        **{key: launch[key] for key in ("launch_id", "role", "mode", "commit", "bundle_id", "artifact_path", "executable_sha256")},
        "exit_code": code, "timed_out": timed_out,
        "code": "ProcessTimeout" if timed_out else RENDERER_EXIT_NAMES.get(code, "UnknownRendererExit"),
        "diagnostic": diagnostic,
    }
