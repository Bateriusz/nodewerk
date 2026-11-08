// Nodewerk NW-01 Offline Compute Shader Measurement Fixture
// Target Profile: cs_6_0 (Shader Model 6.0 baseline)
// Binding Model: Bounded descriptor table (u0, space0)
// Excluded Features: No SM 6.6 ResourceDescriptorHeap, no enhanced barriers, no raytracing.

RWStructuredBuffer<uint> OutBuffer : register(u0, space0);

cbuffer ComputeConstants : register(b0, space0)
{
    uint ElementCount;
    uint SeedMask;
    uint Reserved0;
    uint Reserved1;
};

[numthreads(64, 1, 1)]
void CSMain(uint3 dispatchThreadId : SV_DispatchThreadID)
{
    const uint tid = dispatchThreadId.x;
    if (tid < ElementCount)
    {
        // Deterministic LCG step + XOR mask for exact readback verification
        const uint lcg = tid * 1664525u + 1013904223u;
        OutBuffer[tid] = lcg ^ SeedMask;
    }
}
