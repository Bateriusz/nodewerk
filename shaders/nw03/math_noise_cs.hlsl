// NW-03 fused ShaderExpr compute entry point template for the math/noise fixture.
//
// This file documents the fixed `compute.math_noise` pass-family contract:
//   b0 = parameter block / root constants
//   t0 = optional SRV input
//   u0 = UAV output
//   s0 = optional sampler
//
// The authoritative emitted HLSL is generated deterministically by
// `nodewerkc` from the reachable graph expression DAG into a build directory.
// This template intentionally carries no per-node function calls and no
// reflection metadata.

cbuffer NwParams : register(b0) {
    float4 g_params[4];
};

RWByteAddressBuffer nw_output : register(u0);

[numthreads(64, 1, 1)]
void CSMain(uint3 nw_dispatch_id : SV_DispatchThreadID) {
    float2 uv = (float2(nw_dispatch_id.xy) + 0.5) / float2(256.0, 1.0);
    float g_time = g_params[0].x;
    float4 nw_value = float4(
        lerp(uv.x, sin(uv.y * 17.0 + g_time), 0.35),
        lerp(uv.y, cos(uv.x * 13.0 - g_time), 0.25),
        sin(uv.x * 31.0 + g_time) * 0.5 + 0.5,
        1.0);
    uint nw_index = nw_dispatch_id.x + nw_dispatch_id.y * 256u;
    nw_output.Store4(nw_index * 16u, asuint(nw_value));
}
