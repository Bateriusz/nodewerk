// NW-11 ray-query experiment: common-profile analytic baseline.
//
// This pixel shader is compiled as ps_6_0 and is valid for the common profile.
// It never requires DXR.  The ray-query variant must match this image at the
// frozen fixed ticks within the NW-07 image tolerance before any performance
// result is considered.

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

struct PSInput
{
    float4 position : SV_Position;
    float3 world_position : TEXCOORD0;
    float3 world_normal : TEXCOORD1;
    float2 uv : TEXCOORD2;
    nointerpolation uint material_index : TEXCOORD3;
    float3 camera_position : TEXCOORD4;
};

#include "nw11_reflection_fixture.hlsli"

float4 ScenePS(PSInput input) : SV_Target
{
    uint offset = input.material_index * 32u;
    float4 base_color = float4(
        asfloat(NwMaterials.Load((offset + 0u) / 4u)),
        asfloat(NwMaterials.Load((offset + 4u) / 4u)),
        asfloat(NwMaterials.Load((offset + 8u) / 4u)),
        asfloat(NwMaterials.Load((offset + 12u) / 4u)));
    float roughness = asfloat(NwMaterials.Load((offset + 16u) / 4u));
    uint width = 0u;
    uint height = 0u;
    NwAlbedo.GetDimensions(width, height);
    int2 texel = int2(clamp(input.uv, 0.0, 1.0) * float2(width, height));
    texel = min(texel, int2(width, height) - 1);
    float noise = NwAlbedo.Load(int3(texel, 0)).r;
    float3 albedo = base_color.rgb * lerp(0.65, 1.35, noise);
    float3 normal = normalize(input.world_normal);
    float3 view = normalize(input.camera_position - input.world_position);
    float3 reflected = reflect(-view, normal);
    float3 light_direction = normalize(-float3(
        asfloat(NwLight.Load(0u)),
        asfloat(NwLight.Load(1u)),
        asfloat(NwLight.Load(2u))));
    float ndotl = saturate(dot(normal, light_direction));
    float fresnel = pow(1.0 - saturate(dot(normal, view)), 5.0);
    float reflectivity = lerp(0.08, 0.55, fresnel) * (1.0 - roughness);
    float3 diffuse = albedo * (0.16 + 0.84 * ndotl);
    float3 reflection = ReflectionRadiance(
        reflected, ReflectionLimit(input.position.x) > ReflectionOffset) * reflectivity;
    return float4(diffuse + reflection, 1.0);
}
