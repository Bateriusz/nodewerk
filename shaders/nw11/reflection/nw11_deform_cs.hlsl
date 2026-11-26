// Deform the exact production-generated mesh at the same absolute tick as the VS.
// BLAS and the hit-normal reconstruction consume this same position buffer.
cbuffer Params : register(b0) { uint Tick; uint VertexCount; float TwistRadians; uint LoopTicks; };
ByteAddressBuffer Source : register(t0);
RWByteAddressBuffer Positions : register(u0);
[numthreads(64, 1, 1)]
void DeformCS(uint3 id : SV_DispatchThreadID) {
    if (id.x >= VertexCount) return;
    float3 p = asfloat(Source.Load3(id.x * 32u));
    float phase = LoopTicks != 0u ? float(Tick % LoopTicks) * (6.283185307179586 / float(LoopTicks)) : 0.0;
    float angle = TwistRadians * sin(phase) * p.y;
    float c = cos(angle), s = sin(angle);
    Positions.Store3(id.x * 12u, asuint(float3(c * p.x - s * p.z, p.y, s * p.x + c * p.z)));
}
