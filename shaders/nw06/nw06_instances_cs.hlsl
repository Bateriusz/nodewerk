// NW-06 GPU initialization generator for dense instance records and the single
// DRAW_INDEXED_INSTANCED argument record.

cbuffer Nw06InstanceParams : register(b0, space0)
{
    uint InstanceCount;
    uint InstanceSeed;
    float PlacementRadius;
    float ScaleMin;
    float ScaleMax;
    uint IndexCount;
};

RWByteAddressBuffer NwInstances : register(u0, space0);
RWByteAddressBuffer NwArgs : register(u1, space0);

uint HashU32(uint value)
{
    value ^= value >> 16u;
    value *= 0x7feb352du;
    value ^= value >> 15u;
    value *= 0x846ca68bu;
    value ^= value >> 16u;
    return value;
}

uint HashCoord(uint x, uint y, uint seed)
{
    uint value = x * 0x9e3779b9u;
    value ^= y * 0x85ebca6bu;
    value ^= seed;
    return HashU32(value);
}

float HashUnit(uint x, uint y, uint seed)
{
    return float(HashCoord(x, y, seed)) / 4294967296.0;
}

[numthreads(64, 1, 1)]
void GenerateInstancesCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint index = dispatch_id.x;
    if (index >= InstanceCount)
    {
        return;
    }
    float golden_angle = 2.399963229728653;
    float jitter = HashUnit(index, 0x51edu, InstanceSeed);
    float angle = float(index) * golden_angle + jitter * 0.17;
    float radius = PlacementRadius * sqrt((float(index) + 0.5) / float(InstanceCount));
    float scale_t = HashUnit(index, 0xa11cu, InstanceSeed);
    float scale = ScaleMin + (ScaleMax - ScaleMin) * scale_t;
    float height = (HashUnit(index, 0xbeefu, InstanceSeed) - 0.5) * 0.7;
    float c = cos(angle);
    float s = sin(angle);
    float tx = radius * c;
    float tz = radius * s;
    uint offset = index * 80u;
    NwInstances.Store4(offset + 0u, asuint(float4(scale * c, 0.0, -scale * s, 0.0)));
    NwInstances.Store4(offset + 16u, asuint(float4(0.0, scale, 0.0, 0.0)));
    NwInstances.Store4(offset + 32u, asuint(float4(scale * s, 0.0, scale * c, 0.0)));
    NwInstances.Store4(offset + 48u, asuint(float4(tx, height, tz, 1.0)));
    NwInstances.Store4(offset + 64u, uint4(0u, InstanceSeed + index, 0u, 0u));
    if (index == 0u)
    {
        // The index count is a compile-time, operator-derived constant.  The
        // initialized argument buffer carries the per-instance count and the
        // remaining DRAW_INDEXED_INSTANCED fields.
        NwArgs.Store4(0u, uint4(IndexCount, InstanceCount, 0u, 0u));
        NwArgs.Store(16u, 0u);
    }
}
