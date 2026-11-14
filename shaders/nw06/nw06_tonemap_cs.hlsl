// NW-06 final composite and tonemap to the presentable RGBA32F raw buffer.

cbuffer Nw06PostParams : register(b0, space0)
{
    uint Width;
    uint Height;
    uint HalfWidth;
    uint HalfHeight;
    float Threshold;
    float Intensity;
    float Radius;
    float Exposure;
    float Gamma;
    uint TonemapOperator;
    float CameraRadiusMin;
    float CameraRadiusMax;
    float CameraHeight;
};

Texture2D<float4> NwInput : register(t0, space0);
Texture2D<float4> NwBloomInput : register(t1, space0);
RWByteAddressBuffer NwOutput : register(u0, space0);

float3 Tonemap(float3 color)
{
    color *= Exposure;
    if (TonemapOperator == 1u)
    {
        return color / (1.0 + color);
    }
    if (TonemapOperator == 2u)
    {
        const float a = 2.51;
        const float b = 0.03;
        const float c = 2.43;
        const float d = 0.59;
        const float e = 0.14;
        return saturate((color * (a * color + b)) / (color * (c * color + d) + e));
    }
    return saturate(color);
}

[numthreads(8, 8, 1)]
void TonemapCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    if (dispatch_id.x >= Width || dispatch_id.y >= Height)
    {
        return;
    }
    float2 uv = (float2(dispatch_id.xy) + 0.5) / float2(Width, Height);
    int2 coordinate = min(
        int2(uv * float2(Width, Height)),
        int2(Width, Height) - 1);
    float3 scene = NwInput.Load(int3(coordinate, 0)).rgb;
    int2 bloom_coordinate = min(
        int2(uv * float2(HalfWidth, HalfHeight)),
        int2(HalfWidth, HalfHeight) - 1);
    float3 bloom = NwBloomInput.Load(int3(bloom_coordinate, 0)).rgb;
    float3 color = Tonemap(scene + bloom);
    color = pow(max(color, 0.0), 1.0 / Gamma);
    uint index = dispatch_id.y * Width + dispatch_id.x;
    NwOutput.Store4(index * 16u, asuint(float4(color, 1.0)));
}
