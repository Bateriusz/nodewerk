// Same NW-06 bloom/ACES composite ABI, with the production's titles/cue lamp.
// Absolute time is carried losslessly by scene alpha (RGBA32F), not a new ABI.
#include "neon_breach_scene.hlsli"
cbuffer Nw06PostParams:register(b0,space0) {
    uint Width; uint Height; uint HalfWidth; uint HalfHeight;
    float Threshold; float Intensity; float Radius; float Exposure;
    float Gamma; uint TonemapOperator; float CameraRadiusMin; float CameraRadiusMax; float CameraHeight;
};
Texture2D<float4> NwInput:register(t0,space0);
Texture2D<float4> NwBloomInput:register(t1,space0);
RWByteAddressBuffer NwOutput:register(u0,space0);
float3 NbTonemap(float3 c) {
    c*=Exposure;
    if(TonemapOperator==1u) return c/(1.0+c);
    if(TonemapOperator==2u) return saturate((c*(2.51*c+.03))/(c*(2.43*c+.59)+.14));
    return saturate(c);
}
[numthreads(8,8,1)]
void TonemapCS(uint3 id:SV_DispatchThreadID) {
    if(id.x>=Width||id.y>=Height) return;
    float2 uv=(float2(id.xy)+.5)/float2(Width,Height);
    float4 scene=NwInput.Load(int3(id.xy,0));
    uint tick=uint(round(NwInput.Load(int3(Width/2u,Height/2u,0)).a));
    float seconds=StorySeconds(tick);
    int2 halfPixel=min(int2(uv*float2(HalfWidth,HalfHeight)),int2(HalfWidth,HalfHeight)-1);
    float3 bloom=NwBloomInput.Load(int3(halfPixel,0)).rgb;
    float3 color=pow(max(NbTonemap(scene.rgb+bloom),0.0),1.0/Gamma);
    float2 q=(uv-.5)*float2(1.15,1.0);
    color*=saturate(1.12-.58*dot(q,q));
    // Subtle blue shadows / warm highlights; all grading is after exposure.
    color=lerp(color,color*float3(.96,1.0,1.04),.35);
    float fadeIn=NbEase(0.0,.75,seconds);
    color*=fadeIn;
    bool bar=uv.y<.077||uv.y>.923;
    if(bar) color=float3(.005,.008,.016);
    uint title=0u;
    if(tick>=CUE_TELEPORT) title=1u;
    if(tick>=CUE_DOOR_IMPACT) title=2u;
    if(tick>=STORY_SLOW) title=3u;
    if(tick>=STORY_FREEZE) title=4u;
    if(tick>=STORY_REVERSE) title=5u;
    if(tick>=STORY_FORWARD) title=6u;
    if(tick>=CUE_ESCAPE) title=7u;
    if(tick>=CUE_EXIT) title=8u;
    if(tick>=CUE_DISRUPTOR) title=9u;
    if(tick>=CUE_STUN) title=10u;
    float3 ink=tick<CUE_ESCAPE?float3(.25,.77,.92):float3(1,.61,.24);
    color+=StoryText(uv,float2(.055,.024),0u)*float3(.36,.46,.57);
    color+=StoryText(uv,float2(.055,.941),title+1u)*ink*.8;
    if(uv.y>.918&&uv.y<.920&&uv.x>.055&&uv.x<.055+.89*saturate(float(tick)/float(STORY_DURATION))) color+=ink*.38;
    if(tick>=CUE_FINALE) {
        float appear=NbEase(43.0,43.8,seconds);
        color=lerp(color,color*.08+float3(.008,.010,.023),appear);
        [unroll] for(uint row=0u;row<4u;++row) {
            color+=StoryText(uv,float2(.12,.30+float(row)*.105),STORY_FINALE_TEXT+row)*lerp(NB_CYAN,NB_GOLD,float(row)/3.0)*appear;
        }
    }
    if(id.x==0u&&id.y==0u) {
        float signal=CuePulse(tick,CUE_TELEPORT)+CuePulse(tick,CUE_DOOR_IMPACT)+CuePulse(tick,CUE_SHOT_ONE)+CuePulse(tick,CUE_IMPACT_ONE)+CuePulse(tick,CUE_SHOT_TWO)+CuePulse(tick,CUE_IMPACT_TWO)+CuePulse(tick,CUE_ESCAPE)+CuePulse(tick,CUE_EXIT)+CuePulse(tick,CUE_DISRUPTOR)+CuePulse(tick,CUE_STUN)+CuePulse(tick,CUE_FINALE);
        color=float3(signal>0.0?1.0:0.0,0,0);
    }
    NwOutput.Store4((id.y*Width+id.x)*16u,asuint(float4(saturate(color),1.0)));
}
