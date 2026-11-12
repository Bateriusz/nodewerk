# Initialization Exit Codes, Diagnostics & Inbox-Runtime Blocker Semantics

- **Header Contract:** [`src/player/exit_codes.h`](../src/player/exit_codes.h)
- **Diagnostics Implementation:** [`src/player/diagnostics.h`](../src/player/diagnostics.h), [`src/player/diagnostics.cpp`](../src/player/diagnostics.cpp)

## 1. Distinct Initialization & Execution Exit Codes

Because player and measurement fixture builds disable C++ exceptions (`/EHs-c-`, `_HAS_EXCEPTIONS=0`) and RTTI (`/GR-`), every initialization, capability, and execution failure returns a deterministic, non-overlapping process exit code:

| Exit Code | Enum Name | Category | Failure Stage & Meaning |
| ---: | :--- | :--- | :--- |
| `0` | `Success` | `ok` | Normal completion; all GPU/Win32 resources released cleanly. |
| `10` | `InvalidArguments` | `platform_bootstrap` | Unrecognized CLI flag or invalid argument value. |
| `11` | `OsVersionUnsupported` | `platform_bootstrap` | OS build is older than the intended runtime floor (`10.0.22631`). The actual measured minimum tested build is `10.0.26200.9711`; the lower floor remains a target, not a measured claim. |
| `12` | `WindowCreationFailed` | `platform_bootstrap` | Win32 `RegisterClassA` or `CreateWindowExA` failed. |
| `20` | `DxgiFactoryCreationFailed` | `device_and_capability` | `CreateDXGIFactory2` failed. |
| `21` | `AdapterEnumerationFailed` | `device_and_capability` | No hardware DXGI adapter supporting `D3D_FEATURE_LEVEL_12_0` was found. |
| `22` | `D3D12DeviceCreationFailed` | `device_and_capability` | `D3D12CreateDevice` failed for `D3D_FEATURE_LEVEL_12_0`. |
| `23` | `CapabilityBaselineUnmet` | `device_and_capability` | Adapter fails Shader Model `6.0` (`D3D_SHADER_MODEL_6_0`) or baseline resource binding/heap tier. |
| `24` | `OptionalFeatureUnsupported` | `device_and_capability` | Emitted content manifest requested an optional queried feature (`WaveOps`, `Int64ShaderOps`, etc.) unavailable on the adapter. |
| `25` | `InboxRuntimeBlocker` | `device_and_capability` | Inbox Windows 11 `d3d12.dll` cannot support the stated baseline; triggers mandatory blocker documentation policy. |
| `26` | `DebugLayerUnavailable` | `device_and_capability` | `--validation` was requested, but `D3D12GetDebugInterface` (`d3d12sdklayers.dll` / Graphics Tools) is unavailable. |
| `30` | `CommandQueueCreationFailed` | `resource_and_queue_init` | Direct `ID3D12CommandQueue` creation failed. |
| `31` | `SwapChainCreationFailed` | `resource_and_queue_init` | `CreateSwapChainForHwnd` (`DXGI_SWAP_EFFECT_FLIP_DISCARD`) failed. |
| `32` | `DescriptorHeapCreationFailed` | `resource_and_queue_init` | Bounded `RTV` or `CBV_SRV_UAV` descriptor heap creation failed. |
| `33` | `ResourceAllocationFailed` | `resource_and_queue_init` | `CreateCommittedResource` failed for default UAV or readback buffer. |
| `34` | `CommandListRecordingFailed` | `resource_and_queue_init` | `ID3D12CommandAllocator::Reset`, `ID3D12GraphicsCommandList::Reset`, or `Close` returned a failing HRESULT. |
| `35` | `PresentationFailed` | `resource_and_queue_init` | `IDXGISwapChain3::Present` failed without a reported device removal. |
| `36` | `PresentationOccluded` | `resource_and_queue_init` | `IDXGISwapChain3::Present` returned `DXGI_STATUS_OCCLUDED`; the window was not visibly presented, so the run cannot certify visible clear/present execution. |
| `40` | `OfflineShaderPayloadInvalid` | `offline_shader_and_pipeline` | Embedded offline DXBC/DXIL container failed header or chunk integrity check. |
| `41` | `RootSignatureCreationFailed` | `offline_shader_and_pipeline` | `D3D12SerializeRootSignature` or `CreateRootSignature` failed. |
| `42` | `PipelineStateCreationFailed` | `offline_shader_and_pipeline` | `CreateComputePipelineState` / graphics PSO creation failed for offline DXIL. |
| `50` | `ExecutionFenceTimeout` | `gpu_execution_and_validation` | Direct queue fence wait exceeded timeout (`5000 ms`). |
| `51` | `DeviceRemoved` | `gpu_execution_and_validation` | `ID3D12Device::GetDeviceRemovedReason` reported TDR / device removal. |
| `52` | `GpuSubmissionFailed` | `gpu_execution_and_validation` | `ID3D12CommandQueue::Signal`, `ID3D12Fence::SetEventOnCompletion`, or a non-timeout event wait failed. |
| `60` | `ComputeVerificationFailed` | `gpu_execution_and_validation` | GPU compute readback buffer did not match expected deterministic `LCG ^ SeedMask` output. |
| `61` | `D3D12ValidationError` | `gpu_execution_and_validation` | The D3D12 debug layer stored an error or corruption message in `ID3D12InfoQueue`; the run fails even when the process otherwise returned zero. |
| `70` | `PackageValidationFailed` | `package_validation` | The bounded `.nwp` v1 reader rejected a malformed, contradictory, unknown, or unsupported package requirement before bulk GPU resource allocation. |

## 2. Development Diagnostics Format

In the `dev` build profile (or when `--diagnostics` is passed on the command line), `ReportAndConvertExitCode` writes a single-line structured diagnostic to `stderr` and `OutputDebugStringA`:

```text
[NW-DIAG][exit_code=23][name=CapabilityBaselineUnmet][category=device_and_capability][stage=shader_model_check][hr=0x887A0004] detail="Shader Model 6.0 unsupported" recovery="Abort initialization; adapter does not satisfy D3D_FEATURE_LEVEL_12_0 + Shader Model 6.0 baseline."
```

When `--validation` is used, the fixtures hold an `ID3D12InfoQueue` and drain every stored
message after execution. Error and corruption messages are emitted as
`NW_D3D12_VALIDATION_ERROR=...` lines and cause `ExitCode::D3D12ValidationError` (`61`), so a
process that returns zero can never be treated as a clean validation run merely because the
debug layer was enabled.

## 3. Inbox-Runtime Blocker Policy (`ExitCode::InboxRuntimeBlocker = 25`)

If the Windows 11 inbox runtime (`d3d12.dll` / `dxgi.dll`) cannot support a stated baseline capability on a pinned Windows build and GPU driver:

1. **Do Not Silently Change the Profile or Add a Redistributable:** The executable returns `ExitCode::InboxRuntimeBlocker` (`25`) and emits an `INBOX_RUNTIME_BLOCKER` diagnostic recording:
   - `windows_build_number` (e.g., `22631`)
   - `d3d12_dll_version` (e.g., `10.0.22631.4317`)
   - `hresult_code` (e.g., `0x887A0004`)
   - `failed_api_or_feature`
   - `blocker_summary`
2. **Document Before Profile Change:** The exact blocker must be recorded in [`docs/experiments-and-blockers.md`](experiments-and-blockers.md) and [`benchmarks/experiments/`](../benchmarks/experiments/) before any ADR proposes altering `benchmarks/capability-manifest.v1.json` or shipping `D3D12Core.dll`.
3. **Strict Byte Accounting:** If a redistributable DLL is ever approved by a subsequent ADR, its full byte size must be added to `distributed_components` and `total_distributed_bytes` in all size ledgers.

## 4. Machine-Readable Compute Readback Evidence

The compute fixture can be invoked with `--emit-readback-values`. After the D3D12 dispatch
and readback complete, it prints the observed GPU values to stdout:

```text
NW_READBACK_ELEMENT_COUNT=256
NW_READBACK_U32=99A44A05,99313F76,...
```

`tools/verify_nw01.py` parses those observed values, recomputes SHA-256 over their
little-endian byte encoding, and compares the result with
`fixtures/compute_shader/expected_output.json`. The `observed_output_sha256` field in
`benchmarks/clean-system/clean-system-validation.json` is therefore derived from real
readback bytes; it is never copied from the expected hash or from the committed report.
A mismatch fails the CleanSystem gate even when the process exit code is zero.

## 5. Recorded Offline Shader Evidence

`Mode=Full` verifies the offline `cs_6_0` DXIL container produced by pinned DXC and then
records the measured DXC version, DXIL byte size, and DXIL SHA-256 in
`shaders/manifest.json` (`verification_status: "verified"`, `compiled_offline_with_dxc: true`).
`Mode=CleanSystem` refuses to claim offline-compiled shader execution unless that recorded
manifest evidence is verified and the deployed compute executable embeds a valid DXIL
container whose size and SHA-256 match the recorded manifest. Missing DXC or missing build output remains
`verification_status: "unverified"` rather than being reported as a passing result.

## 6. NW-04 Package-Player Diagnostics

`PackagePlayer` reports failures through the same stable exit-code categories and a
machine-readable diagnostic record. Package decoding and exact emitted-requirement
defects use `PackageValidationFailed` (`70`, category `package_validation`). Adapter
capability mismatches keep the established `CapabilityBaselineUnmet` (`23`, category
`device_and_capability`). Resource realization, pipeline, submission, and device-removal
failures use `ResourceAllocationFailed` (`33`), `PipelineStateCreationFailed` (`42`),
`GpuSubmissionFailed` (`52`), and `DeviceRemoved` (`51`) respectively.

Each diagnostic identifies the unmet requirement, the required value, the observed
value or `unavailable` state, the package profile ID and requirements digest, and the
failure stage:

```text
[NW-DIAG][category=device_and_capability][code=package_capability_feature_level]
[stage=capability_validation][profile_id=nw.common.d3d12.fl12_0.sm6_0.v1]
[requirements_digest=sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a]
[requirement=feature_level][required=0xC000][observed=0xB000]
detail="adapter feature level is below the exact package requirement"
```

Static capability-record defects (profile/digest mismatches, contradictory
records, or requirements that do not exactly match the emitted package) retain
the `package_validation` category and `PackageValidationFailed` (`70`). Adapter
query mismatches use `device_and_capability` and `CapabilityBaselineUnmet`
(`23`). The Python contract checker mirrors that distinction.

The player validates package structure and exact emitted requirements before creating the
swap chain or any package resource. Adapter names, marketing generations, evidence-tier
labels, and PCI identifiers are recorded metadata only and never affect eligibility.

## 7. NW-05 Generated Standalone Image Diagnostics

The generated `nw_procedural_image_player` (and the standalone executable
produced by `nodewerkc export`) validates the deterministic embedded package
footer before entering the package decoder. A missing, truncated, or
checksum-mismatched embedded package prints `NW05_EMBEDDED_PACKAGE_INVALID` and
exits `70` (`PackageValidationFailed`). Subsequent decode, capability,
allocation, pipeline, submission, and device-removal failures return the
existing stable NW-03/NW-04 exit codes.

The generated main never falls back to lower image quality, never changes the
selected capability profile, and never loads a trusted build-time authoring or
compiler extension at runtime. Package bounds checks remain active regardless
of the integrity checksum or a successful export.
