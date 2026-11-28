// Bounded film compute pass. Emits the ordinary PackagePlayer RGBA32F buffer.
cbuffer NwParams : register(b0) { uint presentationTick; }
RWByteAddressBuffer outputImage : register(u0);
#ifdef INLINE_RAY
RaytracingAccelerationStructure objectBounds : register(t0);
#endif

/* FILM_SETTINGS */

struct Camera { float3 eye, target, up; float4 lens; };
struct ObjectFrame { float4x4 inverse; float3 low, high; float stepScale; };
struct Material { float3 base, emission, response; };
struct Light { float3 position, color; float intensity, range; };
struct Hit { float distance; float3 position, normal; uint objectId; };

float sdBox(float3 p,float3 halfSize) {
    float3 q=abs(p)-halfSize;
    return length(max(q,0))+min(max(q.x,max(q.y,q.z)),0);
}
float3 rotateAxis(float3 p,uint axis,float angle) {
    float s,c;
    sincos(angle,s,c);
    if (axis==0) return float3(p.x,c*p.y-s*p.z,s*p.y+c*p.z);
    if (axis==1) return float3(c*p.x+s*p.z,p.y,-s*p.x+c*p.z);
    return float3(c*p.x-s*p.y,s*p.x+c*p.y,p.z);
}
float4x4 localInverse(float3 p,float3 angles,float3 scale) {
    float3 a=-radians(angles);
    float3 x=rotateAxis(rotateAxis(rotateAxis(float3(1,0,0),2,a.z),1,a.y),0,a.x)/scale;
    float3 y=rotateAxis(rotateAxis(rotateAxis(float3(0,1,0),2,a.z),1,a.y),0,a.x)/scale;
    float3 z=rotateAxis(rotateAxis(rotateAxis(float3(0,0,1),2,a.z),1,a.y),0,a.x)/scale;
    float3 offset=-(x*p.x+y*p.y+z*p.z);
    return transpose(float4x4(float4(x,0),float4(y,0),float4(z,0),float4(offset,1)));
}
float smoothMin(float a,float b,float k) {
    if (k<=0) return min(a,b);
    float h=saturate(0.5+0.5*(b-a)/k);
    return lerp(b,a,h)-k*h*(1-h);
}
uint mixBits(uint x) {
    x=(x^(x>>16))*0x7feb352du;
    x=(x^(x>>15))*0x846ca68bu;
    return x^(x>>16);
}
float cornerNoise(int3 p,uint seed) {
    uint h=(uint(p.x)*0x9e3779b9u)^(uint(p.y)*0x85ebca6bu)^(uint(p.z)*0xc2b2ae35u)^seed;
    return mixBits(h)*(1.0/4294967296.0);
}
float noise3(float3 p,uint seed) {
    int3 q=int3(floor(p));
    float3 f=frac(p);
    f=f*f*f*(f*(f*6-15)+10);
    float a=lerp(cornerNoise(q,seed),cornerNoise(q+int3(1,0,0),seed),f.x);
    float b=lerp(cornerNoise(q+int3(0,1,0),seed),cornerNoise(q+int3(1,1,0),seed),f.x);
    float c=lerp(cornerNoise(q+int3(0,0,1),seed),cornerNoise(q+int3(1,0,1),seed),f.x);
    float d=lerp(cornerNoise(q+int3(0,1,1),seed),cornerNoise(q+int3(1,1,1),seed),f.x);
    return lerp(lerp(a,b,f.y),lerp(c,d,f.y),f.z);
}

/* FILM_DATA */

bool traceObject(ObjectFrame pose,uint i,float3 origin,float3 ray,float begin,inout Hit hit) {
    float3 localOrigin=mul(pose.inverse,float4(origin,1)).xyz;
    float3 raw=mul(pose.inverse,float4(ray,0)).xyz;
    float scale=length(raw);
    float3 dir=raw/scale;
    float t=begin*scale, end=hit.distance*scale;
    [unroll]
    for (uint iAxis=0;iAxis<3;++iAxis) {
        if (dir[iAxis]==0) {
            if (localOrigin[iAxis]<pose.low[iAxis] || localOrigin[iAxis]>pose.high[iAxis]) return false;
        } else {
            float a=(pose.low[iAxis]-localOrigin[iAxis])/dir[iAxis];
            float b=(pose.high[iAxis]-localOrigin[iAxis])/dir[iAxis];
            t=max(t,min(a,b));
            end=min(end,max(a,b));
        }
    }
    [loop]
    for (uint step=0;step<TRACE_STEPS && t<end;++step) {
        float3 p=localOrigin+dir*t;
        float d=abs(objectField(i,p));
        if (d<=EPS) {
            hit.distance=t/scale;
            hit.position=origin+ray*hit.distance;
            float3 normal=0;
            [loop]
            for (uint sampleIndex=0;sampleIndex<6;++sampleIndex) {
                uint axis=sampleIndex/2;
                float direction=sampleIndex%2==0?1:-1;
                float3 q=p;
                q[axis]+=direction*2*EPS;
                normal[axis]+=direction*objectField(i,q);
            }
            hit.normal=normalize(mul(normal,(float3x3)pose.inverse));
            hit.objectId=i;
            return true;
        }
        float following=t+d*pose.stepScale;
        if (following<=t) break;
        t=following;
    }
    return false;
}

bool trace(ObjectFrame objects[OBJECT_COUNT],float3 origin,float3 ray,
           float begin,float limit,uint ignored,out Hit hit) {
    hit=(Hit)0;
    hit.distance=min(limit,TRACE_LIMIT);
    hit.objectId=0xffffffffu;
    if (begin>=hit.distance) return false;
#ifdef INLINE_RAY
    RayDesc rayDesc;
    rayDesc.Origin=origin;
    rayDesc.Direction=ray;
    rayDesc.TMin=begin;
    rayDesc.TMax=hit.distance;
    RayQuery<RAY_FLAG_FORCE_NON_OPAQUE | RAY_FLAG_SKIP_TRIANGLES> query;
    query.TraceRayInline(objectBounds, RAY_FLAG_NONE, 0xff, rayDesc);
    [loop]
    while (query.Proceed()) {
        uint index=query.CandidatePrimitiveIndex();
        if (index!=ignored && index<OBJECT_COUNT &&
            traceObject(objects[index],index,origin,ray,begin,hit)) {
            query.CommitProceduralPrimitiveHit(hit.distance);
        }
    }
#else
    [loop]
    for (uint index=0;index<OBJECT_COUNT;++index) {
        if (index!=ignored) traceObject(objects[index],index,origin,ray,begin,hit);
    }
#endif
    return hit.objectId!=0xffffffffu;
}

float3 miss(float3 ray) {
    return MISS_RADIANCE+max(0,ray.y)*float3(0.10,0.14,0.22);
}
float3 direct(ObjectFrame objects[OBJECT_COUNT],Hit hit,float3 ray,uint t) {
    Material m=materialAt(hit.objectId);
    float3 color=0.025+m.base*0.04+m.emission;
    [loop]
    for (uint i=0;i<LIGHT_COUNT;++i) {
        Light light=lightAt(i,t);
        float3 delta=light.position-hit.position;
        float d=length(delta);
        if (light.intensity==0 || d>=light.range || d<EPS) continue;
        float3 toward=delta/d;
        float cosine=dot(hit.normal,toward);
        if (cosine<=0) continue;
        Hit occluder;
        if (SHADOWS && trace(objects,hit.position+toward*4*EPS,toward,0,d-2*EPS,hit.objectId,occluder)) continue;
        float falloff=saturate(1-d/light.range);
        float attenuation=light.intensity*falloff*falloff/(1+d*d);
        float3 halfRay=normalize(toward-ray);
        float shine=pow(saturate(dot(hit.normal,halfRay)),max(2,64*(1-m.response.x)));
        color+=(m.base*cosine+shine*m.response.z*(0.25+m.response.y))*light.color*attenuation;
    }
    return color;
}
float3 scenePixel(int2 pixel,Camera cam,uint t,uint action,ObjectFrame objects[OBJECT_COUNT]) {
    float2 uv=(float2(pixel)+0.5)/float2(FRAME_WIDTH,FRAME_HEIGHT);
    float3 forward=normalize(cam.target-cam.eye), right=normalize(cross(forward,cam.up));
    float3 up=normalize(cross(right,forward));
    float2 q=(uv*2-1)*tan(radians(cam.lens.x)*0.5)*float2(cam.lens.y,-1);
    float3 ray=normalize(forward+q.x*right+q.y*up);
    float projection=dot(ray,forward);
    Hit hit;
    float3 color=miss(ray);
    float depth=1e20;
    if (trace(objects,cam.eye,ray,cam.lens.z/projection,cam.lens.w/projection,0xffffffffu,hit)) {
        color=direct(objects,hit,ray,t);
        depth=hit.distance*projection;
        Material m=materialAt(hit.objectId);
        if (REFLECTIONS && m.response.y>0.2) {
            float3 reflected=reflect(ray,hit.normal);
            Hit bounce;
            float3 radiance=miss(reflected);
            if (trace(objects,hit.position+reflected*6*EPS,reflected,0,TRACE_LIMIT,hit.objectId,bounce))
                radiance=direct(objects,bounce,reflected,t);
            color=lerp(color,radiance,m.response.y*(1-m.response.x*0.5));
        }
    }
    return dust(color,uv,depth,action,cam);
}

#define TILE_WIDTH (GROUP_X+2*BLOOM_RADIUS)
#define TILE_HEIGHT (GROUP_Y+2*BLOOM_RADIUS)
groupshared float3 tile[TILE_WIDTH*TILE_HEIGHT];

[numthreads(GROUP_X,GROUP_Y,1)]
void FILM_ENTRY(uint3 pixel:SV_DispatchThreadID, uint3 group:SV_GroupID, uint lane:SV_GroupIndex) {
    uint t=min(presentationTick,(uint)FILM_DURATION_TICKS), action=actionAt(t);
    Camera cam=cameraAt(t);
    ObjectFrame objects[OBJECT_COUNT];
    setupObjects(action,objects);
    int2 corner=int2(group.xy)*int2(GROUP_X,GROUP_Y)-BLOOM_RADIUS;
    for (uint slot=lane;slot<TILE_WIDTH*TILE_HEIGHT;slot+=GROUP_X*GROUP_Y) {
        int2 pixelCoord=corner+int2(slot%TILE_WIDTH,slot/TILE_WIDTH);
        float3 value=0;
        if (all(pixelCoord>=0) && pixelCoord.x<FRAME_WIDTH && pixelCoord.y<FRAME_HEIGHT)
            value=scenePixel(pixelCoord,cam,t,action,objects);
        tile[slot]=value;
    }
    GroupMemoryBarrierWithGroupSync();
    if (pixel.x>=FRAME_WIDTH || pixel.y>=FRAME_HEIGHT) return;
    int2 local=int2(pixel.xy)-corner;
    float3 color=tile[local.x+local.y*TILE_WIDTH];
    float3 bloom=0;
    uint neighbors=0;
    for (int dy=-BLOOM_RADIUS;dy<=BLOOM_RADIUS;++dy) {
        for (int dx=-BLOOM_RADIUS;dx<=BLOOM_RADIUS;++dx) {
            int2 pixelCoord=int2(pixel.xy)+int2(dx,dy);
            if (any(pixelCoord<0) || pixelCoord.x>=FRAME_WIDTH || pixelCoord.y>=FRAME_HEIGHT) continue;
            float3 value=tile[local.x+dx+(local.y+dy)*TILE_WIDTH];
            bloom+=value*max(dot(value,float3(0.2126,0.7152,0.0722))-BLOOM_THRESHOLD,0);
            ++neighbors;
        }
    }
    color+=bloom*(BLOOM_GAIN/max(neighbors,1u));
#if TONEMAP_ENABLED
    color=max(color,0)*EXPOSURE;
    color=pow(color/(1+color),1.0/GAMMA);
#endif
    outputImage.Store4((pixel.x+FRAME_WIDTH*pixel.y)*16,asuint(float4(color,1)));
}
