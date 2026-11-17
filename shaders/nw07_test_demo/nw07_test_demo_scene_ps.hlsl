// Asteroid Tunnel showcase shading.
//
// NW-09A adds two bounded visual layers inside the existing raster pass:
//   flags=3: bright seeded dust streaks updated on fixed 120 Hz steps
//   flags=4: dithered delayed asteroid samples that read as temporal echoes
//
// Bloom/ACES remain the established NW-06/NW-07 post chain, so the new
// emissive details naturally create short hot trails without a second runtime.

cbuffer Nw06GraphicsParams : register(b0, space0)
{
    uint Tick;
    float FovDegrees;
    float Aspect;
    float NearPlane;
    float FarPlane;
    float Exposure;
    float BloomIntensity;
    float TwistRadians;
};

Texture2D<float4> NwAlbedo : register(t0, space0);
Buffer<uint> NwMaterials : register(t2, space0);
Buffer<uint> NwLight : register(t4, space0);

static const float TAU = 6.28318530717958647692;
static const uint NW09_FIXED_STEP_TICKS = 400u;

struct PSInput
{
    float4 position : SV_Position;
    float3 world_position : TEXCOORD0;
    float3 world_normal : TEXCOORD1;
    float2 uv : TEXCOORD2;
    nointerpolation uint material_index : TEXCOORD3;
    float3 camera_position : TEXCOORD4;
    nointerpolation uint instance_seed : TEXCOORD5;
    nointerpolation uint instance_flags : TEXCOORD6;
    nointerpolation uint temporal_age_steps : TEXCOORD7;
};

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

float4 ScenePS(PSInput input) : SV_Target
{
    uint offset = input.material_index * 32u;
    float roughness = asfloat(NwMaterials.Load((offset + 16u) / 4u));
    float specular = asfloat(NwMaterials.Load((offset + 24u) / 4u));

    uint width = 0u;
    uint height = 0u;
    NwAlbedo.GetDimensions(width, height);
    int2 texel = int2(clamp(input.uv, 0.0, 1.0) * float2(width, height));
    texel = min(texel, int2(width, height) - 1);
    float noise = NwAlbedo.Load(int3(texel, 0)).r;

    float3 normal = normalize(input.world_normal);
    float3 light_direction = float3(
        asfloat(NwLight.Load(0u)),
        asfloat(NwLight.Load(1u)),
        asfloat(NwLight.Load(2u)));
    float3 light_color = float3(
        asfloat(NwLight.Load(4u)),
        asfloat(NwLight.Load(5u)),
        asfloat(NwLight.Load(6u)));

    float3 light_dir = normalize(-light_direction);
    float ndotl = saturate(dot(normal, light_dir));
    float3 view_dir = normalize(input.camera_position - input.world_position);
    float3 half_vector = normalize(light_dir + view_dir);
    float ndoth = saturate(dot(normal, half_vector));
    float shininess = lerp(5.0, 96.0, 1.0 - saturate(roughness));
    float specular_term = pow(ndoth, shininess) * specular;

    if (input.instance_flags == 1u)
    {
        // Structural rings stay stable.  Temporal variation is confined to the
        // new particle/echo layers so the tunnel never regresses into moire.
        float cross_section = 0.82 + 0.18 * cos(input.uv.y * TAU);
        float3 ring_base = float3(0.62, 0.035, 0.004);
        float3 ring_hot = float3(1.55, 0.19, 0.018);
        float lighting = 0.38 + 0.62 * ndotl;
        float3 color =
            lerp(ring_base, ring_hot, cross_section) * lighting * light_color
            + specular_term * float3(0.45, 0.10, 0.025);
        return float4(color, 1.0);
    }

    if (input.instance_flags == 3u)
    {
        // Seeded dust: a hot core with deterministic 120 Hz pulsation.  The
        // existing bloom pass turns these small geometry streaks into glowing
        // motion accents without adding any fullscreen demo-only pass.
        uint simulation_tick = Tick / NW09_FIXED_STEP_TICKS;
        float seed_phase = HashUnit(input.instance_seed ^ 0xd1b54a35u) * TAU;
        float pulse =
            0.68 + 0.32 * sin(float(simulation_tick) * 0.19 + seed_phase);
        float core =
            0.35 + 0.65 * pow(saturate(1.0 - abs(input.uv.y * 2.0 - 1.0)), 2.0);
        float3 dust_color = lerp(
            float3(1.8, 0.22, 0.025),
            float3(3.4, 1.15, 0.24),
            HashUnit(input.instance_seed ^ 0x94d049bbu));
        return float4(dust_color * pulse * core, 1.0);
    }

    // Asteroid/debris base shading shared by live hazards and delayed echoes.
    float seed_tint = HashUnit(input.instance_seed ^ 0xa511e9b3u);
    float3 rock_a = float3(0.58, 0.075, 0.012);
    float3 rock_b = float3(1.05, 0.28, 0.045);
    float3 rock = lerp(rock_a, rock_b, seed_tint);
    rock *= lerp(0.52, 1.38, noise);

    float diffuse = 0.12 + 0.88 * ndotl;
    float rim = pow(1.0 - saturate(dot(normal, view_dir)), 2.7);
    float3 asteroid_color =
        rock * diffuse * light_color
        + float3(1.8, 0.42, 0.055) * rim * 0.65
        + specular_term * float3(1.0, 0.42, 0.12);

    if (input.instance_flags == 4u)
    {
        // No alpha-blending contract is added for the showcase.  Instead,
        // stable screen-space dithering creates sparse ghost coverage while
        // age attenuates the delayed sample.  Bloom integrates the surviving
        // hot pixels into a convincing feedback-like trail.
        uint2 pixel = uint2(input.position.xy);
        uint dither_seed =
            pixel.x * 0x9e3779b9u
            ^ pixel.y * 0x85ebca6bu
            ^ input.instance_seed;
        float coverage = HashUnit(dither_seed);
        clip(coverage - 0.62);

        float age = saturate(float(input.temporal_age_steps) / 24.0);
        float fade = lerp(0.34, 0.10, age);
        float3 echo_color =
            asteroid_color * fade
            + float3(1.4, 0.18, 0.025) * rim * (0.22 * (1.0 - age));
        return float4(echo_color, 1.0);
    }

    return float4(asteroid_color, 1.0);
}
