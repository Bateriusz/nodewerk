// NW-06 dense material table generation.

cbuffer Nw06MaterialParams : register(b0, space0)
{
    float BaseR;
    float BaseG;
    float BaseB;
    float BaseA;
    float Roughness;
    float Metallic;
    float Specular;
    uint TextureIndex;
};

RWByteAddressBuffer NwMaterials : register(u0, space0);

[numthreads(1, 1, 1)]
void MaterialCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    if (dispatch_id.x != 0u)
    {
        return;
    }
    NwMaterials.Store4(0u, asuint(float4(BaseR, BaseG, BaseB, BaseA)));
    NwMaterials.Store4(16u, asuint(float4(Roughness, Metallic, Specular, float(TextureIndex))));
}
