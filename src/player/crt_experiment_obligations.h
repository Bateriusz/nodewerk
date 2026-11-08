#pragma once
#include <cstdint>

namespace nw::player {

// CRT Elimination Experiment Obligations Contract (ADR-0004)
// CRT elimination (/NODEFAULTLIB) is an explicit opt-in experiment
// (NW_ENABLE_CRT_ELIMINATION_EXPERIMENT=ON) and NEVER a baseline assumption.
// Any build enabling NW_CRT_EXPERIMENT must satisfy every obligation below:
//
// 1. Stack Alignment & Entry Point:
//    Provide custom entry point `WinMainCRTStartup` or `mainCRTStartup` aligned to 16 bytes
//    prior to executing any SSE2/AVX2 instructions.
// 2. Security Cookie Initialization:
//    Retain `/GS` buffer security check and call `__security_init_cookie()` before entering
//    any function with stack buffer protection.
// 3. Floating-Point ABI Symbol:
//    Define `extern "C" int _fltused = 0x9875;` when linking without default CRT libraries.
// 4. Intrinsic Memory Primitives:
//    Provide freestanding implementations of `memcpy`, `memset`, and `memcmp` compiled with
//    `-fno-builtin` / `/Oi-` so the optimizer does not emit recursive calls.
// 5. Static Initializer & TLS Restrictions:
//    Prohibit non-trivial dynamic global constructors (`.CRT$XCU`) and `thread_local` storage,
//    or explicitly walk `.CRT$XCA`..`.CRT$XCZ` on startup and reverse-walk on teardown.
// 6. Deterministic GPU & OS Cleanup:
//    Wait on the direct queue fence, release all D3D12/DXGI COM interfaces, destroy Win32
//    window handles, and terminate via `ExitProcess(static_cast<UINT>(exit_code))`.

struct CrtExperimentObligationStatus {
    bool stack_aligned_16b = true;
    bool security_cookie_initialized = true;
    bool fltused_symbol_defined = true;
    bool freestanding_mem_primitives_provided = true;
    bool no_unhandled_static_constructors = true;
    bool deterministic_gpu_and_os_cleanup = true;

    constexpr bool AllSatisfied() const noexcept {
        return stack_aligned_16b &&
               security_cookie_initialized &&
               fltused_symbol_defined &&
               freestanding_mem_primitives_provided &&
               no_unhandled_static_constructors &&
               deterministic_gpu_and_os_cleanup;
    }
};

} // namespace nw::player
