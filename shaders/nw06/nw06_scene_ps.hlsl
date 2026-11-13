// NW-06 raster pixel stage: analytic directional lighting and material lookup.

cbuffer Nw06GraphicsParams : register(b0, space0)
{
    float Tick;
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

struct PSInput
{
    float4 position : SV_Position;
    float3 world_position : TEXCOORD0;
    float3 world_normal : TEXCOORD1;
    float2 uv : TEXCOORD2;
    nointerpolation uint material_index : TEXCOORD3;
};

float4 ScenePS(PSInput input) : SV_Target
{
    uint offset = input.material_index * 32u;
    float4 base_color = float4(
        asfloat(NwMaterials.Load((offset + 0u) / 4u)),
        asfloat(NwMaterials.Load((offset + 4u) / 4u)),
        asfloat(NwMaterials.Load((offset + 8u) / 4u)),
        asfloat(NwMaterials.Load((offset + 12u) / 4u)));
    float roughness = asfloat(NwMaterials.Load((offset + 16u) / 4u));
    float metallic = asfloat(NwMaterials.Load((offset + 20u) / 4u));
    float specular = asfloat(NwMaterials.Load((offset + 24u) / 4u));
    uint width = 0u;
    uint height = 0u;
    NwAlbedo.GetDimensions(width, height);
    int2 texel = int2(clamp(input.uv, 0.0, 1.0) * float2(width, height));
    texel = min(texel, int2(width, height) - 1);
    float noise = NwAlbedo.Load(int3(texel, 0)).r;
    float3 albedo = base_color.rgb * lerp(0.65, 1.35, noise);
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
    float3 view_dir = normalize(float3(0.0, 0.0, 0.0) - input.world_position);
    float3 half_vector = normalize(light_dir + view_dir);
    float ndoth = saturate(dot(normal, half_vector));
    float shininess = lerp(4.0, 128.0, 1.0 - saturate(roughness));
    float3 diffuse = albedo * (0.16 + 0.84 * ndotl) * light_color;
    float specular_term =
        (1.0 - roughness) * pow(ndoth, shininess) * specular *
        (1.0 - metallic);
    return float4(diffuse + specular_term, 1.0);
}
