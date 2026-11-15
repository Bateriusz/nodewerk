// NW-07-test-demo branch-only orange/red tunnel and asteroid shading.
// Uses the existing NW-06 material, noise and directional-light bindings.

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
        // Every tunnel ring uses the same stable shading. Do not introduce
        // per-instance phase/noise here: at flight speed that turns into
        // temporal moire and makes the tunnel look like changing patterns.
        float cross_section = 0.82 + 0.18 * cos(input.uv.y * TAU);
        float3 ring_base = float3(0.62, 0.035, 0.004);
        float3 ring_hot = float3(1.55, 0.19, 0.018);
        float lighting = 0.38 + 0.62 * ndotl;
        float3 color =
            lerp(ring_base, ring_hot, cross_section) * lighting * light_color
            + specular_term * float3(0.45, 0.10, 0.025);
        return float4(color, 1.0);
    }

    // Asteroid/debris: warm rock, strong faceting and a bright rim so hazards
    // remain readable at speed against the dark tunnel.
    float seed_tint = HashUnit(input.instance_seed ^ 0xa511e9b3u);
    float3 rock_a = float3(0.58, 0.075, 0.012);
    float3 rock_b = float3(1.05, 0.28, 0.045);
    float3 rock = lerp(rock_a, rock_b, seed_tint);
    rock *= lerp(0.52, 1.38, noise);

    float diffuse = 0.12 + 0.88 * ndotl;
    float rim = pow(1.0 - saturate(dot(normal, view_dir)), 2.7);
    float3 color =
        rock * diffuse * light_color
        + float3(1.8, 0.42, 0.055) * rim * 0.65
        + specular_term * float3(1.0, 0.42, 0.12);

    return float4(color, 1.0);
}
