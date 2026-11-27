// Neon Breach: authored scene math for the existing indexed/instanced torus
// pipeline. Every visible solid uses the same generated NW-06 torus mesh.
// These are bounded production-specific transforms, not new graph operators.
static const float NB_PI = 3.14159265358979323846;
static const float NB_TAU = 6.28318530717958647692;
static const float3 NB_CYAN = float3(0.035, 0.72, 1.0);
static const float3 NB_ROSE = float3(1.0, 0.045, 0.19);
static const float3 NB_GOLD = float3(1.0, 0.43, 0.075);
static const float3 NB_VIOLET = float3(0.55, 0.12, 1.0);

uint NbHash(uint v) {
    v ^= v >> 16u; v *= 0x7feb352du;
    v ^= v >> 15u; v *= 0x846ca68bu; v ^= v >> 16u;
    return v;
}
float NbRandom(uint v) { return float(NbHash(v)) / 4294967296.0; }
float NbEase(float start, float end, float time) { return smoothstep(start, end, time); }
float3 NbRotateX(float3 p, float a) {
    float c=cos(a), s=sin(a); return float3(p.x,c*p.y-s*p.z,s*p.y+c*p.z);
}
float3 NbRotateY(float3 p, float a) {
    float c=cos(a), s=sin(a); return float3(c*p.x+s*p.z,p.y,-s*p.x+c*p.z);
}
float3 NbRotateZ(float3 p, float a) {
    float c=cos(a), s=sin(a); return float3(c*p.x-s*p.y,s*p.x+c*p.y,p.z);
}
float3 NbRotate(float3 p, float3 a) { return NbRotateZ(NbRotateY(NbRotateX(p,a.x),a.y),a.z); }

float3 NbActorRoot(uint actor, float seconds, float motion) {
    if (seconds < 8.0) return float3(0.0,1.42,0.0);
    if (actor==0u) {
        float walk=NbEase(8.5,19.0,seconds);
        float3 p=lerp(float3(-0.35,0.0,-5.2),float3(-0.9,0.0,-0.1),walk);
        // Wait on the near side until the door impact; never walk through the
        // still-closed shutter while the establishing camera is watching.
        p.z=seconds<15.0 ? -5.2+NbEase(8.5,14.5,seconds)
                        : lerp(-4.2,-.1,NbEase(15.15,19.5,seconds));
        p.y+=0.045*sin(motion*11.0)*sin(walk*NB_PI);
        p.z+=2.2*NbEase(37.0,39.5,seconds);
        return p;
    }
    if (actor==1u) return float3(1.35,0.0,2.0);
    if (actor==2u) return float3(2.0,0.0,4.5);
    float run=NbEase(31.5,38.5,seconds);
    float3 p=lerp(float3(-1.8,0.0,3.2),float3(-6.8,0.0,3.2),run);
    p.y+=0.10*abs(sin(motion*14.0))*sin(run*NB_PI);
    return p;
}

float3 NbAimAxis(float3 v, float3 direction) {
    float3 axis=normalize(direction);
    float3 right=normalize(cross(abs(axis.y)>.95 ? float3(1,0,0) : float3(0,1,0),axis));
    float3 side=cross(axis,right);
    return right*v.x + axis*v.y + side*v.z;
}

// One initialization-only record per torus. The 80-byte instance ABI and the
// single indirect draw are unchanged. role/detail are existing flags/seed data.
void NbInstance(uint index, out float3 p, out float3 s, out float3 r,
                out uint role, out uint detail) {
    p=0.0; s=1.0; r=0.0; role=0u; detail=index;
    if(index==0u) return; // viewport-filling distant torus surface (sky)
    if(index<129u) {
        role=1u; uint j=index-1u; detail=j;
        if(j<32u) {
            float h=float(j)/31.0;
            float radius=6.1*sqrt(max(.03,1.0-pow((h-.5)*1.88,2.0)));
            p=float3(0.0,-2.2+h*2.3,0.0);
            s=float3(radius,.6,radius);
        } else if(j<56u) {
            float h=float(j-32u)/23.0;
            p=float3(0.0,.26+h*.52,0.0);
            s=float3(1.95*(1.0-.18*h),.30,1.95*(1.0-.18*h));
        } else if(j<80u) {
            float a=float(j-56u)*NB_TAU/24.0;
            p=float3(cos(a)*6.9,-1.0,sin(a)*6.9);
            s=float3(.18,1.9,.18); r=float3(0.0,0.0,.35);
        } else if(j<104u) {
            float a=float(j-80u)*NB_TAU/24.0;
            p=float3(cos(a)*6.9,-1.1,sin(a)*6.9);
            s=float3(.28,.7,.28); r=float3(0,0,NB_PI*.5);
        } else {
            float h=float(j-104u)/23.0;
            p=float3(0,.18+h*.1,0); s=float3(1.25+h*.8,.2,1.25+h*.8);
        }
    } else if(index<385u) {
        role=2u; uint j=index-129u; detail=j;
        if(j<48u) { // long ribbed hotel, plus its inset accent rings
            uint rib=j/2u;
            p=float3(0,1.8,-7.0+float(rib)*.72);
            s=float3(4.6+(j%2u)*.12,.32,3.2+(j%2u)*.1);
            r=float3(NB_PI*.5,0,0);
        } else if(j<112u) { // structural floor strips, all flattened tori
            uint q=j-48u;
            p=float3(-4.0+float(q%16u)*.54,-1.35,float(q/16u)*4.8-5.0);
            s=float3(.30,.16,2.5);
        } else if(j<160u) {
            uint q=j-112u;
            p=float3((q%2u==0u ? -4.38 : 4.38),.1+float((q/2u)%3u)*1.3,
                     -5.0+float(q/6u)*1.65);
            s=float3(.42,.22,.42); r=float3(0,0,NB_PI*.5);
        } else if(j<224u) { // exterior escape corridor, perpendicular to room
            uint q=j-160u;
            p=float3(-5.0-float(q/4u)*.72,1.0,3.2);
            s=float3(2.4+float(q%4u)*.025,.22,1.8+float(q%4u)*.02); r=float3(0,0,NB_PI*.5);
        } else {
            uint q=j-224u;
            p=float3(-11.0+float(q%8u)*.8,-1.35,1.8+float(q/8u)*.94);
            s=float3(.5,.15,.5);
        }
    } else if(index<481u) {
        uint j=index-385u; detail=j;
        if(j<32u) { // main door: narrow concentric shutter leaves
            role=3u; float q=float(j)/31.0;
            p=float3(0,1.0,-2.9); s=float3(.08+q*1.65,.20,.08+q*2.25);
            r=float3(NB_PI*.5,0,0);
        } else if(j<64u) {
            role=4u; float q=float(j-32u)/31.0;
            p=float3(-5.0,1.0,3.2); s=float3(1.45+q*.18,.19,1.65+q*.18);
            r=float3(0,0,NB_PI*.5);
        } else {
            role=5u; float q=float(j-64u)/31.0;
            p=float3(-.35,-1.24,-5.2); s=float3(.2+q*1.5,.26,.2+q*1.5);
        }
    } else if(index<625u) {
        role=6u; uint q=index-481u; detail=q;
        uint part=q%36u;
        if(part<5u) { // helmet crown
            float h=float(part)/4.0;
            p=float3(0,1.43+h*.37,0); s=float3(.34+.10*sin(h*NB_PI),.65,.34+.10*sin(h*NB_PI));
        } else if(part<13u) {
            float h=float(part-5u)/7.0;
            p=float3(0,.18+h*1.02,0); s=float3(.33+.19*sin(h*NB_PI),.72,.26+.09*sin(h*NB_PI));
        } else if(part<21u) {
            uint arm=(part-13u)/4u; float h=float((part-13u)%4u)/3.0;
            p=float3(arm==0u ? .62 : -.62,1.03-h*.78,0);
            s=float3(.16,.85,.16);
        } else if(part<29u) {
            uint leg=(part-21u)/4u; float h=float((part-21u)%4u)/3.0;
            p=float3(leg==0u ? .23 : -.23,-.05-h*1.05,0);
            s=float3(.17,1.0,.18);
        } else if(part<31u) {
            p=float3(part==29u ? .23 : -.23,-1.18,.12); s=float3(.21,.6,.36);
        } else if(part==31u) {
            p=float3(0,1.3,0); s=float3(.18,.8,.18);
        } else if(part<35u) {
            p=float3(.62,.19,.12+float(part-32u)*.13);
            s=float3(.11,.7,.11); r=float3(NB_PI*.5,0,0);
        } else {
            p=float3(0,1.62,.47); s=float3(.31,.22,.105); r=float3(NB_PI*.5,0,0);
        }
    } else if(index<785u) {
        role=7u; detail=index-625u;
        uint q=detail; float a=NbRandom(q+71u)*NB_TAU;
        p=float3(cos(a),NbRandom(q+87u)*2.0-1.0,sin(a));
        s=float3(.055+.075*NbRandom(q+98u),.6,.055+.075*NbRandom(q+98u));
        r=float3(NbRandom(q+91u)*6.0,NbRandom(q+92u)*6.0,NbRandom(q+93u)*6.0);
    } else if(index<913u) {
        role=8u; detail=index-785u;
        s=float3(.06,.3,.06);
    } else {
        role=9u; detail=index-913u;
        p=float3((NbRandom(index+3u)-.5)*8.0,-.6+NbRandom(index+4u)*4.4,
                 (NbRandom(index+5u)-.5)*20.0);
        s=float3(.013,.07,.013); r=float3(NbRandom(index)*6.0,0,0);
    }
}
