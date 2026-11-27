// Material artwork within the existing NW-06 fragment stage. Lighting below
// is analytic authored shading; no ray-traced shadow/reflection is claimed.
#include "neon_breach_scene.hlsli"
cbuffer Nw06GraphicsParams : register(b0,space0) {
    uint Tick; float FovDegrees; float Aspect; float NearPlane;
    float FarPlane; float Exposure; float BloomIntensity; float TwistRadians;
};
Texture2D<float4> NwAlbedo:register(t0,space0);
Buffer<uint> NwMaterials:register(t2,space0);
Buffer<uint> NwLight:register(t4,space0);
struct PSInput {
    float4 position:SV_Position; float3 world_position:TEXCOORD0;
    float3 world_normal:TEXCOORD1; float2 uv:TEXCOORD2;
    nointerpolation uint material_index:TEXCOORD3; float3 camera_position:TEXCOORD4;
    nointerpolation uint detail:TEXCOORD5; nointerpolation uint role:TEXCOORD6;
    float visibility:TEXCOORD7;
};
float4 ScenePS(PSInput input):SV_Target {
    clip(input.visibility-.5);
    float seconds=StorySeconds(Tick);
    uint role=input.role,detail=input.detail;
    // The authored hotel has an open cutaway facing the escape corridor.
    if(role==2u && detail<48u && input.world_position.x< -3.5 && input.world_position.z> -1.2) discard;
    if(role==0u) {
        float2 uv=input.uv;
        float3 sky=lerp(float3(.006,.012,.030),float3(.025,.012,.046),uv.y);
        if(seconds<8.0) {
            float2 q=(uv-float2(.72,.32))*float2(Aspect,1);
            sky+=float3(.030,.025,.080)*exp(-3.0*dot(q,q));
            float2 cell=floor(uv*float2(560,320));
            uint seed=uint(cell.x)+uint(cell.y)*560u;
            float star=step(.9975,NbRandom(seed))*pow(saturate(1.0-length(frac(uv*float2(560,320))-.5)*2.0),4);
            sky+=star*float3(.5,.68,.9);
        }
        return float4(sky,float(Tick));
    }
    uint mat=input.material_index*8u;
    float3 graphColor=asfloat(uint3(NwMaterials.Load(mat),NwMaterials.Load(mat+1u),NwMaterials.Load(mat+2u)));
    float roughness=asfloat(NwMaterials.Load(mat+4u));
    float specular=asfloat(NwMaterials.Load(mat+6u));
    uint width,height; NwAlbedo.GetDimensions(width,height);
    int2 texel=min(int2(saturate(input.uv)*float2(width,height)),int2(width,height)-1);
    float noise=NwAlbedo.Load(int3(texel,0)).r;
    float3 normal=normalize(input.world_normal),view=normalize(input.camera_position-input.world_position);
    float3 light=normalize(-asfloat(uint3(NwLight.Load(0u),NwLight.Load(1u),NwLight.Load(2u))));
    float3 lightColor=asfloat(uint3(NwLight.Load(4u),NwLight.Load(5u),NwLight.Load(6u)));
    float3 base=float3(.09,.12,.18),tint=NB_CYAN; float emission=0;
    if(role==1u) {
        base=float3(.14,.17,.23);
        tint=(detail>=80u&&detail<104u)?NB_GOLD:NB_CYAN;
        emission=(detail>=56u&&detail<104u)?1.8:0.035;
    } else if(role==2u) {
        base=float3(.095,.075,.13);
        tint=detail>=160u?NB_GOLD:NB_ROSE;
        emission=detail<48u?(detail%2u==1u?.20:.012):(detail>=112u&&detail<160u?1.6:.01);
    } else if(role==3u||role==4u) {
        tint=role==3u?NB_ROSE:NB_GOLD; base=float3(.16,.12,.16);
        emission=detail%5u==0u?.8:.035;
        emission+=CuePulse(Tick,role==3u?CUE_DOOR_IMPACT:CUE_EXIT)*2.5;
    } else if(role==5u) {
        tint=NB_CYAN; base=float3(.08,.17,.23); emission=.15+3.0*CuePulse(Tick,CUE_TELEPORT);
    } else if(role==6u) {
        uint actor=detail/36u,part=detail%36u;
        tint=actor==0u?NB_CYAN:(actor==3u?NB_GOLD:NB_ROSE);
        base=lerp(float3(.16,.20,.25),tint,.20);
        emission=part==35u?2.6:(part==31u?.8:.025);
        if(part>=32u&&part<35u) emission=.2+2.0*max(CuePulse(Tick,CUE_SHOT_ONE),CuePulse(Tick,CUE_SHOT_TWO));
        if(actor==0u) emission+=CuePulse(Tick,CUE_STUN)*2.0;
    } else if(role==7u) {
        tint=detail/40u==3u?NB_VIOLET:NB_GOLD; base=tint*.3; emission=1.6;
    } else if(role==8u) {
        tint=detail/32u==2u?NB_VIOLET:NB_CYAN; base=tint; emission=3.6;
    } else if(role==9u) {
        tint=lerp(NB_CYAN,NB_GOLD,NbRandom(detail)); base=tint; emission=.5;
    }
    base*=lerp(.72,1.16,noise)*lerp(float3(1,1,1),graphColor,.15);
    float ndl=saturate(dot(normal,light));
    float rim=pow(1.0-saturate(dot(normal,view)),3.0);
    float gloss=pow(saturate(dot(normal,normalize(light+view))),lerp(10.0,100.0,1.0-roughness))*specular;
    float3 color=base*(.20+ndl*.9)*lightColor;
    color+=gloss*float3(.8,.86,1.0)+rim*tint*.45+tint*emission;
    // Authored neon spill / transient illumination, all in the existing PS.
    float3 portal=input.world_position-float3(-.35,1,-5.2);
    color+=NB_CYAN*(.22+1.8*CuePulse(Tick,CUE_TELEPORT))/(1.0+dot(portal,portal)*.7)*(.25+saturate(-normal.z));
    float3 lamp=input.world_position-float3(2,2,2);
    color+=NB_ROSE*.27/(1.0+dot(lamp,lamp)*.3)*(.4+saturate(normal.y));
    float fog=1.0-exp(-length(input.camera_position-input.world_position)*(seconds<8.0?.010:.025));
    color=lerp(color,float3(.018,.022,.045),saturate(fog));
    return float4(max(color,0.0),float(Tick));
}
