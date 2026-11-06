# Initialization Exit Codes, Diagnostics & Inbox-Runtime Blocker Semantics

- **Header Contract:** [`src/player/exit_codes.h`](../src/player/exit_codes.h)
- **Diagnostics Implementation:** [`src/player/diagnostics.h`](../src/player/diagnostics.h), [`src/player/diagnostics.cpp`](../src/player/diagnostics.cpp)

## 1. Distinct Initialization & Execution Exit Codes

Because player and measurement fixture builds disable C++ exceptions (`/EHs-c-`, `_HAS_EXCEPTIONS=0`) and RTTI (`/GR-`), every initialization, capability, and execution failure returns a deterministic, non-overlapping process exit code:

| Exit Code | Enum Name | Category | Failure Stage & Meaning |
| ---: | :--- | :--- | :--- |
| `0` | `Success` | `ok` | Normal completion; all GPU/Win32 resources released cleanly. |
| `10` | `InvalidArguments` | `platform_bootstrap` | Unrecognized CLI flag or invalid argument value. |
| `11` | `OsVersionUnsupported` | `platform_bootstrap` | OS build is older than minimum tested Windows 11 x64 build (`10.0.22631`). |
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
| `40` | `OfflineShaderPayloadInvalid` | `offline_shader_and_pipeline` | Embedded offline DXBC/DXIL container failed header or chunk integrity check. |
| `41` | `RootSignatureCreationFailed` | `offline_shader_and_pipeline` | `D3D12SerializeRootSignature` or `CreateRootSignature` failed. |
| `42` | `PipelineStateCreationFailed` | `offline_shader_and_pipeline` | `CreateComputePipelineState` / graphics PSO creation failed for offline DXIL. |
| `50` | `ExecutionFenceTimeout` | `gpu_execution_and_validation` | Direct queue fence wait exceeded timeout (`5000 ms`). |
| `51` | `DeviceRemoved` | `gpu_execution_and_validation` | `ID3D12Device::GetDeviceRemovedReason` reported TDR / device removal. |
| `60` | `ComputeVerificationFailed` | `gpu_execution_and_validation` | GPU compute readback buffer did not match expected deterministic `LCG ^ SeedMask` output. |

## 2. Development Diagnostics Format

In the `dev` build profile (or when `--diagnostics` is passed on the command line), `ReportAndConvertExitCode` writes a single-line structured diagnostic to `stderr` and `OutputDebugStringA`:

```text
[NW-DIAG][exit_code=23][name=CapabilityBaselineUnmet][category=device_and_capability][stage=shader_model_check][hr=0x887A0004] detail="Shader Model 6.0 unsupported" recovery="Abort initialization; adapter does not satisfy D3D_FEATURE_LEVEL_12_0 + Shader Model 6.0 baseline."
```

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
