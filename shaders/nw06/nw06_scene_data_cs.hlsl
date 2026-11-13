// NW-06 GPU initialization of absolute-tick camera keys and the light record.

cbuffer Nw06SceneDataParams : register(b0, space0)
{
    uint Param0;
    uint Param1;
    uint Param2;
    uint Param3;
    float Param4;
    float Param5;
    float Param6;
    float Param7;
    float Param8;
    float Param9;
    float Param10;
    float Param11;
};

RWByteAddressBuffer NwOutput : register(u0, space0);

[numthreads(1, 1, 1)]
void CameraDataCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    if (dispatch_id.x != 0u) return;
    NwOutput.Store4(0u, uint4(Param0, Param1, Param2, Param3));
    NwOutput.Store4(16u, asuint(float4(Param4, Param5, Param6, Param7)));
    NwOutput.Store4(32u, asuint(float4(Param8, Param9, Param10, 0.0)));
}

[numthreads(1, 1, 1)]
void LightDataCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    if (dispatch_id.x != 0u) return;
    // The light family reuses b0: direction.xyz, pad, color.rgb, pad.
    NwOutput.Store4(0u, asuint(float4(Param0, Param1, Param2, Param3)));
    NwOutput.Store4(16u, asuint(float4(Param4, Param5, Param6, Param7)));
}
