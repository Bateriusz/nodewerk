// NW-07-test-demo branch-only tunnel/obstacle instance generator.
// Reuses the NW-06 instance root signature and dense 80-byte records.

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

static const float TAU = 6.28318530717958647692;
static const float TUNNEL_LENGTH = 96.0;

uint HashU32(uint value)
{
    value ^= value >> 16u;
    value *= 0x7feb352du;
    value ^= value >> 15u;
    value *= 0x846ca68bu;
    value ^= value >> 16u;
    return value;
}

float HashUnit(uint value)
{
    return float(HashU32(value)) / 4294967296.0;
}

void StoreUniformTransform(
    uint offset,
    float scale,
    float tx,
    float ty,
    float tz,
    uint seed,
    uint flags)
{
    NwInstances.Store4(offset + 0u, asuint(float4(scale, 0.0, 0.0, 0.0)));
    NwInstances.Store4(offset + 16u, asuint(float4(0.0, scale, 0.0, 0.0)));
    NwInstances.Store4(offset + 32u, asuint(float4(0.0, 0.0, scale, 0.0)));
    NwInstances.Store4(offset + 48u, asuint(float4(tx, ty, tz, 1.0)));
    NwInstances.Store4(offset + 64u, uint4(0u, seed, flags, 0u));
}

[numthreads(64, 1, 1)]
void GenerateInstancesCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint index = dispatch_id.x;
    if (index >= InstanceCount)
    {
        return;
    }

    // Half of the table is the visible tunnel structure. With the canonical
    // demo count (1024), this gives 512 evenly spaced rings and 512 hazards.
    // The lower ring density avoids high-frequency temporal moire during flight.
    uint ring_count = min(512u, max(128u, InstanceCount / 2u));
    uint seed = InstanceSeed + index;
    uint offset = index * 80u;

    if (index < ring_count)
    {
        float t = (float(index) + 0.5) / float(ring_count);
        float z = t * TUNNEL_LENGTH;
        StoreUniformTransform(
            offset,
            PlacementRadius,
            0.0,
            0.0,
            z,
            seed,
            1u);
    }
    else
    {
        uint obstacle_index = index - ring_count;
        float radial_u = HashUnit(seed ^ 0x51ed270bu);
        float angle_u = HashUnit(seed ^ 0x9e3779b9u);
        float depth_u = HashUnit(seed ^ 0x85ebca6bu);
        float scale_u = HashUnit(seed ^ 0xc2b2ae35u);
        float lane_u = HashUnit(seed ^ 0x27d4eb2fu);

        float angle = angle_u * TAU;

        // Keep the centre of the tunnel visibly open.  Most hazards live in a
        // broad band near the walls; only a small deterministic subset enters
        // the central flight corridor.
        float radius;
        if (lane_u < 0.08)
        {
            radius = PlacementRadius * lerp(0.18, 0.42, sqrt(radial_u));
        }
        else
        {
            radius = PlacementRadius * lerp(0.56, 0.88, sqrt(radial_u));
        }

        float tx = cos(angle) * radius;
        float ty = sin(angle) * radius;
        float tz = depth_u * TUNNEL_LENGTH;
        float scale = lerp(ScaleMin, ScaleMax, scale_u);

        StoreUniformTransform(
            offset,
            scale,
            tx,
            ty,
            tz,
            seed + obstacle_index * 17u,
            2u);
    }

    if (index == 0u)
    {
        NwArgs.Store4(0u, uint4(IndexCount, InstanceCount, 0u, 0u));
        NwArgs.Store(16u, 0u);
    }
}
