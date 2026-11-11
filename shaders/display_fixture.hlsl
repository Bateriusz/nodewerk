// NW-04 package-driven fullscreen display pass.
//
// The package output is a raw UAV buffer of RGBA32F pixels (the NW-03
// RWByteAddressBuffer output).  This offline-compiled compute shader reads it
// by dispatch id and writes the player-owned R8G8B8A8_UNORM display texture.
// No runtime shader compilation, reflection, source map, or graph traversal is
// involved.

RWByteAddressBuffer NwDisplaySource : register(u0, space0);
RWTexture2D<float4> NwDisplayTarget : register(u1, space0);

cbuffer DisplayParams : register(b0, space0)
{
    uint SourceWidth;
    uint SourceHeight;
    uint TargetWidth;
    uint TargetHeight;
};

[numthreads(8, 8, 1)]
void DisplayCS(uint3 dispatchThreadId : SV_DispatchThreadID)
{
    if (dispatchThreadId.x >= TargetWidth ||
        dispatchThreadId.y >= TargetHeight)
    {
        return;
    }
    const uint source_x = min(
        SourceWidth - 1u,
        dispatchThreadId.x * SourceWidth / TargetWidth);
    const uint source_y = min(
        SourceHeight - 1u,
        dispatchThreadId.y * SourceHeight / TargetHeight);
    const uint source_index = source_y * SourceWidth + source_x;
    const float4 color =
        asfloat(NwDisplaySource.Load4(source_index * 16u));
    NwDisplayTarget[dispatchThreadId.xy] =
        float4(saturate(color.rgb), 1.0f);
}
