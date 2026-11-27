// Authored choreography over the existing torus vertices and instance table.
// No topology generation, new render pass, or runtime animation API is added.
#include "neon_breach_scene.hlsli"
cbuffer Nw06GraphicsParams : register(b0,space0) {
    uint Tick; float FovDegrees; float Aspect; float NearPlane;
    float FarPlane; float Exposure; float BloomIntensity; float TwistRadians;
};
Buffer<uint> NwInstances : register(t1,space0);
Buffer<uint> NwCamera : register(t3,space0);
struct VSInput { float3 position:POSITION; float3 normal:NORMAL; float2 uv:TEXCOORD0; uint instance_id:SV_InstanceID; };
struct VSOutput {
    float4 position:SV_Position;
    float3 world_position:TEXCOORD0;
    float3 world_normal:TEXCOORD1;
    float2 uv:TEXCOORD2;
    nointerpolation uint material_index:TEXCOORD3;
    float3 camera_position:TEXCOORD4;
    nointerpolation uint detail:TEXCOORD5;
    nointerpolation uint role:TEXCOORD6;
    float visibility:TEXCOORD7;
};
float3 Load3(uint byteOffset) {
    return asfloat(uint3(NwInstances.Load(byteOffset/4u),NwInstances.Load(byteOffset/4u+1u),NwInstances.Load(byteOffset/4u+2u)));
}
float NbCameraCurve(uint tick) {
    uint a=NwCamera.Load(0u),b=NwCamera.Load(1u),c=NwCamera.Load(2u),d=NwCamera.Load(3u);
    float va=asfloat(NwCamera.Load(4u)),vb=asfloat(NwCamera.Load(5u));
    float vc=asfloat(NwCamera.Load(6u)),vd=asfloat(NwCamera.Load(7u));
    if(tick<=a) return va; if(tick>=d) return vd;
    if(NwCamera.Load(11u)==1u) return tick<b?va:(tick<c?vb:vc);
    if(tick<=b) return lerp(va,vb,float(tick-a)/max(1.0,float(b-a)));
    if(tick<=c) return lerp(vb,vc,float(tick-b)/max(1.0,float(c-b)));
    return lerp(vc,vd,float(tick-c)/max(1.0,float(d-c)));
}
void NbCamera(float t,out float3 eye,out float3 target) {
    if(t<4.8) {
        float a=-.64+t*.10; eye=float3(sin(a)*17.0,6.5-t*.22,-cos(a)*17.0); target=float3(0,-.5,0);
    } else if(t<8.0) {
        float u=NbEase(4.8,8.0,t); eye=lerp(float3(5.6,3.6,-8.0),float3(3.8,2.8,-5.6),u); target=float3(0,1.6,0);
    } else if(t<15.0) {
        float u=NbEase(8.0,15.0,t); eye=lerp(float3(-2.4,2.8,-9.5),float3(-2.2,2.3,-7.2),u); target=float3(0,.9,-2.0);
    } else if(t<21.0) {
        float u=NbEase(15.0,21.0,t); eye=lerp(float3(-3.0,2.8,-6.4),float3(-3.2,2.3,-3.2),u); target=float3(.1,.7,1.4);
    } else if(t<29.0) {
        float a=-1.2+(t-21.0)*.19;
        eye=float3(sin(a)*3.4,2.9+.15*sin(t*.7),1.3-cos(a)*6.0); target=float3(.2,.65,1.6);
    } else if(t<37.0) {
        float u=NbEase(29,37,t); eye=lerp(float3(-2.6,2.8,-5.8),float3(-2.8,3.0,-3.7),u); target=float3(-1.2,.8,2.3);
    } else {
        float u=NbEase(37,43,t); eye=lerp(float3(-9.2,2.4,2.3),float3(-8.8,2.3,2.25),u); target=float3(-.2,.65,2.1);
    }
}
VSOutput SceneVS(VSInput input) {
    uint base=input.instance_id*80u;
    float3 x=Load3(base),y=Load3(base+16u),z=Load3(base+32u),origin=Load3(base+48u);
    uint detail=NwInstances.Load((base+68u)/4u), role=NwInstances.Load((base+72u)/4u);
    float seconds=StorySeconds(Tick), motion=StorySeconds(StoryMotionTick(Tick));
    float3 p=input.position.x*x+input.position.y*y+input.position.z*z+origin;
    float3 n=normalize(input.normal.x*x/max(dot(x,x),.000001)+input.normal.y*y/max(dot(y,y),.000001)+input.normal.z*z/max(dot(z,z),.000001));
    float visibility=1.0;
    float3 eye,target;
    NbCamera(NbCameraCurve(Tick)/NB_TAU*48.0,eye,target);
    // Camera graph values remain live inputs for framing as well as its curve.
    eye.y+=asfloat(NwCamera.Load(10u))*.02;
    if(role==1u) {
        visibility=seconds<8.0?1.0:0.0;
        if(detail>=56u && detail<104u) {
            float spin=motion*.07*max(.1,abs(TwistRadians)); p=NbRotateY(p,spin); n=NbRotateY(n,spin);
        }
    } else if(role>=2u && role<=5u) {
        visibility=seconds>=8.0?1.0:0.0;
        if(role==3u) {
            float open=NbEase(15.0,15.7,seconds);
            p-=float3(0,-1.25,-2.9); p=NbRotateX(p,-open*1.42); p+=float3(0,-1.25,-2.9);
            n=NbRotateX(n,-open*1.42);
        } else if(role==4u) {
            float opening=NbEase(32.0,34.0,seconds)*(1.0-NbEase(38.0,39.0,seconds));
            float scale=1.0+opening*1.4;
            p.yz=(p.yz-float2(1,3.2))*scale+float2(1,3.2);
        } else if(role==5u) {
            float rise=NbEase(7.95,8.35,seconds)*(1.0-NbEase(8.5,10.5,seconds));
            p.y+=rise*float(detail-64u)/31.0*3.8;
        }
    } else if(role==6u) {
        uint actor=detail/36u,part=detail%36u;
        visibility=actor==0u||seconds>=15.0 ? 1.0:0.0;
        float aim=actor==0u?NbEase(18,20,motion):(actor==3u?NbEase(39,40,seconds):.6);
        if((part>=13u&&part<17u)||(part>=32u&&part<35u)) {
            float3 pivot=float3(.62,1.16,0);
            p=NbRotateX(p-pivot,-aim*1.55)+pivot; n=NbRotateX(n,-aim*1.55);
        }
        if(actor==0u&&part>=21u&&part<=24u) {
            float kick=(1.0-NbEase(.0,.65,abs(seconds-15.0)))*1.25;
            float3 pivot=float3(.23,.08,0); p=NbRotateX(p-pivot,-kick)+pivot; n=NbRotateX(n,-kick);
        }
        if(actor==3u && seconds>=31.5 && seconds<38.5 && part>=21u&&part<29u) {
            float angle=sin(motion*14.0+(part<25u?0.0:NB_PI))*.65;
            float3 pivot=float3(part<25u?.23:-.23,.08,0);
            p=NbRotateX(p-pivot,angle)+pivot; n=NbRotateX(n,angle);
        }
        float yaw=actor==0u?0.0:NB_PI;
        if(actor==3u) yaw=seconds<31.5?NB_PI:(seconds<39.0?-NB_PI*.5:NB_PI*.5);
        p=NbRotateY(p,yaw); n=NbRotateY(n,yaw);
        float fall=0.0;
        if(actor==1u) fall=NbEase(21.5,22.45,motion);
        if(actor==2u && Tick>=CUE_IMPACT_TWO) fall=NbEase(StorySeconds(StoryMotionTick(CUE_IMPACT_TWO)),StorySeconds(StoryMotionTick(CUE_IMPACT_TWO))+1.0,motion);
        if(actor==0u) fall=NbEase(40.5,41.6,seconds);
        p=NbRotateZ(p-float3(0,-1.2,0),fall*(actor==0u?1.48:-1.48))+float3(0,-1.2,0);
        n=NbRotateZ(n,fall*(actor==0u?1.48:-1.48));
        p+=NbActorRoot(actor,seconds,motion);
    } else if(role==7u) {
        uint group=detail/40u; uint j=detail%40u;
        uint cue=group==0u?CUE_DOOR_IMPACT:(group==1u?CUE_IMPACT_ONE:(group==2u?CUE_IMPACT_TWO:CUE_STUN));
        float start=StorySeconds(StoryMotionTick(cue));
        float dt=motion-start; visibility=(Tick>=cue?1.0:0.0)*step(0.0,dt)*(1.0-step(1.5,dt));
        float3 center=group==0u?float3(0,.9,-2.9):(group==1u?float3(1.35,.7,2):(group==2u?float3(2,.7,4.5):float3(-.9,.7,-.1)));
        float3 velocity=origin*float3(3.2,3.8,3.2);
        p=(p-origin)+center+velocity*dt+float3(0,-2.0*dt*dt,0);
    } else if(role==8u) {
        uint group=detail/32u,j=detail%32u;
        float3 a,b; float phase=0; float start=0;
        if(group<2u) {
            start=StorySeconds(group==0u?CUE_SHOT_ONE:CUE_SHOT_TWO);
            a=NbActorRoot(0u,seconds,motion)+float3(.62,1.15,.95);
            b=NbActorRoot(group+1u,seconds,motion)+float3(0,1,0);
            float dt=seconds-start; visibility=step(0,dt)*(1.0-step(.6,dt));
            phase=saturate(dt/.5);
        } else if(group==2u) {
            start=40.0; a=NbActorRoot(3u,seconds,motion)+NbRotateY(float3(.62,1.15,.95),NB_PI*.5); b=NbActorRoot(0u,seconds,motion)+float3(0,1,0);
            float dt=seconds-start; visibility=step(0,dt)*(1.0-step(.65,dt)); phase=saturate(dt/.5);
        } else {
            start=8.0; a=float3(-.35,-1.1,-5.2); b=float3(-.35,3,-5.2);
            float dt=seconds-start; visibility=step(0,dt)*(1.0-step(1.5,dt)); phase=saturate(dt/.8);
        }
        float trail=float(j)/31.0;
        float travel=group==3u?trail:clamp(phase-trail*.21,0.0,1.0);
        float radius=group==3u?1.2:(.08+.09*(1.0-trail));
        float3 local=float3(input.position.x*radius,input.position.y*.25,input.position.z*radius);
        p=NbAimAxis(local,b-a)+lerp(a,b,travel);
        n=NbAimAxis(input.normal,b-a);
    } else if(role==9u) {
        visibility=seconds>=8.0?1.0:0.0;
        p.y+=sin(motion*.4+float(detail))*.1;
        p.z+=sin(motion*.15+float(detail)*.6)*.65;
    }
    VSOutput o;
    o.world_position=p; o.world_normal=n; o.uv=input.uv;
    o.material_index=NwInstances.Load((base+64u)/4u); o.camera_position=eye;
    o.detail=detail; o.role=role; o.visibility=visibility;
    float3 forward=normalize(target-eye),right=normalize(cross(float3(0,1,0),forward)),up=cross(forward,right);
    float3 v=float3(dot(p-eye,right),dot(p-eye,up),dot(p-eye,forward));
    float f=1.0/tan(radians(FovDegrees)*.5);
    o.position=float4(v.x*f/Aspect,v.y*f,v.z*FarPlane/(FarPlane-NearPlane)-NearPlane*FarPlane/(FarPlane-NearPlane),v.z);
    if(role==0u) {
        // A distant transformed patch of the generated torus covers the view;
        // its hole is outside the frustum. This guarantees valid clock alpha.
        float2 screen=float2((input.position.x-1.0)*120.0,input.position.z*120.0);
        o.position=float4(screen,.999999,1.0); o.uv=screen*.5+.5;
    }
    if(visibility<.5) o.position=float4(2,2,2,1);
    return o;
}
