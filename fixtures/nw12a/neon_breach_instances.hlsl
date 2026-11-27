// Export-local specialization of the existing 80-byte GPU instance table.
// Same initialization dispatch and indirect arguments as NW-06 / Orbital Vortex.
#include "neon_breach_scene.hlsli"
cbuffer Nw06InstanceParams : register(b0, space0) {
    uint InstanceCount; uint InstanceSeed; float PlacementRadius;
    float ScaleMin; float ScaleMax; uint IndexCount;
};
RWByteAddressBuffer NwInstances : register(u0, space0);
RWByteAddressBuffer NwArgs : register(u1, space0);
[numthreads(64,1,1)]
void GenerateInstancesCS(uint3 id : SV_DispatchThreadID) {
    if(id.x>=InstanceCount) return;
    float3 p,s,r; uint role,detail;
    NbInstance(id.x,p,s,r,role,detail);
    uint base=id.x*80u;
    NwInstances.Store4(base,asuint(float4(NbRotate(float3(s.x,0,0),r),0)));
    NwInstances.Store4(base+16u,asuint(float4(NbRotate(float3(0,s.y,0),r),0)));
    NwInstances.Store4(base+32u,asuint(float4(NbRotate(float3(0,0,s.z),r),0)));
    NwInstances.Store4(base+48u,asuint(float4(p,1)));
    NwInstances.Store4(base+64u,uint4(0u,detail,role,InstanceSeed));
    if(id.x==0u) {
        NwArgs.Store4(0u,uint4(IndexCount,InstanceCount,0u,0u));
        NwArgs.Store(16u,0u);
    }
}
