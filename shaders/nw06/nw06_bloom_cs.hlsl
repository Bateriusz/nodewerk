// NW-06 half-resolution bloom extraction/blur pass.

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
RWTexture2D<float4> NwBloom : register(u0, space0);

[numthreads(8, 8, 1)]
void BloomCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    if (dispatch_id.x >= HalfWidth || dispatch_id.y >= HalfHeight)
    {
        return;
    }
    float2 uv = (float2(dispatch_id.xy) + 0.5) / float2(HalfWidth, HalfHeight);
    float2 texel = 1.0 / float2(Width, Height);
    float3 color = 0.0;
    [unroll]
    for (int y = -1; y <= 1; ++y)
    {
        [unroll]
        for (int x = -1; x <= 1; ++x)
        {
            int2 coordinate = int2(clamp(
                uv + float2(x, y) * texel * Radius, 0.0, 1.0) *
                float2(Width, Height));
            coordinate = min(coordinate, int2(Width, Height) - 1);
            float3 sample_color = NwInput.Load(int3(coordinate, 0)).rgb;
            float luma = dot(sample_color, float3(0.2126, 0.7152, 0.0722));
            color += luma > Threshold ? sample_color : 0.0;
        }
    }
    NwBloom[dispatch_id.xy] = float4(color / 9.0 * Intensity, 1.0);
}
