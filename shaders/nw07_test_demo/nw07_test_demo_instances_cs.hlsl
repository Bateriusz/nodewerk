// Asteroid Tunnel showcase instance generator.
//
// The first generation of the demo split 1024 instances between tunnel ribs
// and asteroid hazards.  NW-09A keeps the same bounded instance table but
// reserves deterministic slices for two temporal visual extensions:
//   flags=3: seeded dust particles animated at NW-09's fixed 120 Hz cadence
//   flags=4: delayed asteroid echo instances used for feedback-like trails
//
// This remains a showcase-only shader specialization.  It does not alter the
// canonical NW-06/NW-07 instance operator or NW-09 History contracts.

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

void HazardPlacement(
    uint seed,
    float placement_radius,
    float scale_min,
    float scale_max,
    out float tx,
    out float ty,
    out float tz,
    out float scale)
{
    float radial_u = HashUnit(seed ^ 0x51ed270bu);
    float angle_u = HashUnit(seed ^ 0x9e3779b9u);
    float depth_u = HashUnit(seed ^ 0x85ebca6bu);
    float scale_u = HashUnit(seed ^ 0xc2b2ae35u);
    float lane_u = HashUnit(seed ^ 0x27d4eb2fu);

    float angle = angle_u * TAU;

    // Keep the central flight corridor mostly clear.
    float radius;
    if (lane_u < 0.08)
    {
        radius = placement_radius * lerp(0.18, 0.42, sqrt(radial_u));
    }
    else
    {
        radius = placement_radius * lerp(0.56, 0.88, sqrt(radial_u));
    }

    tx = cos(angle) * radius;
    ty = sin(angle) * radius;
    tz = depth_u * TUNNEL_LENGTH;
    scale = lerp(scale_min, scale_max, scale_u);
}

[numthreads(64, 1, 1)]
void GenerateInstancesCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint index = dispatch_id.x;
    if (index >= InstanceCount)
    {
        return;
    }

    // At the canonical 1024-instance budget this resolves to:
    //   384 structural ribs
    //   384 asteroid hazards
    //   128 NW-09-inspired seeded dust particles
    //   128 fixed-step temporal asteroid echoes
    uint ring_count = min(384u, max(1u, (InstanceCount * 3u) / 8u));
    uint remaining = InstanceCount - ring_count;
    uint particle_count = min(128u, remaining / 4u);
    uint echo_count = min(128u, (remaining - particle_count) / 3u);
    uint hazard_count = InstanceCount - ring_count - particle_count - echo_count;

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
    else if (index < ring_count + hazard_count)
    {
        float tx;
        float ty;
        float tz;
        float scale;
        HazardPlacement(
            seed,
            PlacementRadius,
            ScaleMin,
            ScaleMax,
            tx,
            ty,
            tz,
            scale);
        StoreUniformTransform(offset, scale, tx, ty, tz, seed, 2u);
    }
    else if (index < ring_count + hazard_count + particle_count)
    {
        // Seeded particle dust is concentrated inside the flight corridor.
        // The VS advances these records in explicit 400-timeline-tick steps.
        float radial_u = HashUnit(seed ^ 0x243f6a88u);
        float angle = HashUnit(seed ^ 0xb7e15162u) * TAU;
        float radius = PlacementRadius * lerp(0.08, 0.48, sqrt(radial_u));
        float tx = cos(angle) * radius;
        float ty = sin(angle) * radius;
        float tz = HashUnit(seed ^ 0x9e3779b9u) * TUNNEL_LENGTH;
        float scale = lerp(0.075, 0.145, HashUnit(seed ^ 0x85ebca6bu));
        StoreUniformTransform(offset, scale, tx, ty, tz, seed, 3u);
    }
    else
    {
        // Echo records deliberately duplicate deterministic hazards.  Their VS
        // position/spin is sampled several fixed simulation steps in the past,
        // producing a feedback-like trail without changing NW-09 History ABI.
        uint echo_index =
            index - (ring_count + hazard_count + particle_count);
        uint source_index = hazard_count != 0u
            ? (echo_index % hazard_count)
            : 0u;
        uint source_seed = InstanceSeed + ring_count + source_index;

        float tx;
        float ty;
        float tz;
        float scale;
        HazardPlacement(
            source_seed,
            PlacementRadius,
            ScaleMin,
            ScaleMax,
            tx,
            ty,
            tz,
            scale);
        StoreUniformTransform(
            offset,
            scale * 1.025,
            tx,
            ty,
            tz,
            source_seed,
            4u);
    }

    if (index == 0u)
    {
        NwArgs.Store4(0u, uint4(IndexCount, InstanceCount, 0u, 0u));
        NwArgs.Store(16u, 0u);
    }
}
