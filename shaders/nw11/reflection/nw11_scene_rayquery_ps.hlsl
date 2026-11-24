// NW-11 DXR inline ray-query variant pixel stage.
//
// Compiled as ps_6_5 and usable only when D3D12_RAYTRACING_TIER_1_1 and
// D3D12_SHADER_MODEL_6_5 are both queried and satisfied.  The BLAS/TLAS are
// built from the same graph-generated torus mesh and instance buffer already
// consumed by the indexed baseline; no second renderer or geometry source is
// introduced.

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
RaytracingAccelerationStructure NwScene : register(t5, space0);
ByteAddressBuffer NwVertices : register(t6, space0);
ByteAddressBuffer NwIndices : register(t7, space0);

struct PSInput
{
    float4 position : SV_Position;
    float3 world_position : TEXCOORD0;
    float3 world_normal : TEXCOORD1;
    float2 uv : TEXCOORD2;
    nointerpolation uint material_index : TEXCOORD3;
    float3 camera_position : TEXCOORD4;
};

float3 LoadPosition(uint vertex)
{
    return asfloat(NwVertices.Load3(vertex * 12u));
}

float3 TraceReflection(float3 origin, float3 direction)
{
    RayDesc ray;
    ray.Origin = origin;
    ray.Direction = direction;
    ray.TMin = 0.001;
    ray.TMax = 100.0;
    RayQuery<RAY_FLAG_NONE> query;
    query.TraceRayInline(NwScene, RAY_FLAG_NONE, 0xFF, ray);
    while (query.Proceed())
    {
        // No any-hit work is required for the opaque NW-07 torus scene.  The
        // explicit loop keeps all committed candidates visible to the compiler
        // and to the resource-lifetime audit.
    }
    if (query.CommittedStatus() != COMMITTED_TRIANGLE_HIT)
    {
        float horizon = saturate(direction.y * 0.5 + 0.5);
        return lerp(float3(0.21, 0.27, 0.36), float3(0.72, 0.78, 0.88), horizon);
    }
    // DXR 1.1 exposes the primitive index and object-to-world matrix, not
    // a geometry-normal intrinsic. Reconstruct the normal from the exact
    // indexed triangle used to build the BLAS, including instance scale.
    uint first = query.CommittedPrimitiveIndex() * 3u;
    float3 p0 = LoadPosition(NwIndices.Load(first * 4u));
    float3 p1 = LoadPosition(NwIndices.Load((first + 1u) * 4u));
    float3 p2 = LoadPosition(NwIndices.Load((first + 2u) * 4u));
    float3x4 transform = query.CommittedObjectToWorld3x4();
    float3 e1 = mul(transform, float4(p1 - p0, 0.0));
    float3 e2 = mul(transform, float4(p2 - p0, 0.0));
    float3 hit_normal = normalize(cross(e1, e2));
    if (dot(hit_normal, direction) > 0.0) hit_normal = -hit_normal;
    float3 light_direction = normalize(-float3(
        asfloat(NwLight.Load(0u)),
        asfloat(NwLight.Load(1u)),
        asfloat(NwLight.Load(2u))));
    return float3(0.45, 0.5, 0.58) * (0.2 + 0.8 * saturate(dot(hit_normal, light_direction)));
}

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
    // Offset the reflection origin off the surface to avoid self-intersection;
    // the BLAS/TLAS and all buffers must outlive the frame command list.
    float3 reflection = TraceReflection(input.world_position + normal * 0.002, reflected);
    return float4(diffuse + reflection * reflectivity, 1.0);
}
