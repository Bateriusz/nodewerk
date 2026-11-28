// Resample the previous scheduler-owned film color/depth from the same shot.
// A packed float4 has RGB and positive view-space depth, not alpha.
cbuffer NwParams : register(b0) { uint presentationTick; }
RWByteAddressBuffer outputImage : register(u0);
Buffer<uint> previousFrame : register(t0);
Buffer<uint> currentFrame : register(t1);
struct Camera { float3 eye, target, up; float4 lens; };
/* SETTINGS */
/* CAMERA */

[numthreads(8,8,1)]
void FilmFeedback(uint3 id : SV_DispatchThreadID) {
    if (id.x >= WIDTH || id.y >= HEIGHT) return;
    uint p = (id.x + id.y*WIDTH)*4;
    float3 color = asfloat(uint3(currentFrame[p], currentFrame[p+1], currentFrame[p+2]));
    float depth = asfloat(currentFrame[p+3]);
    uint tick = min(presentationTick, DURATION);
    if (tick >= STEP_TICKS && WEIGHT > 0) {
        uint past = (tick/STEP_TICKS - 1u)*STEP_TICKS;
        if (!resetBetween(past, tick)) {
            Camera now = cameraAt(tick);
            Camera before = cameraAt(past);
            if (depth >= now.lens.z && depth <= now.lens.w) {
                float3 f = normalize(now.target - now.eye);
                float3 r = normalize(cross(f, now.up));
                float3 u = cross(r, f);
                float2 uv = (float2(id.xy)+0.5)/float2(WIDTH,HEIGHT);
                float2 plane = (uv*2-1)*tan(radians(now.lens.x)*0.5)*float2(now.lens.y,-1);
                float3 relative = now.eye + depth*(f + plane.x*r + plane.y*u) - before.eye;
                float3 pf = normalize(before.target - before.eye);
                float z = dot(pf, relative);
                if (z >= before.lens.z && z <= before.lens.w) {
                    float3 pr = normalize(cross(pf, before.up));
                    float3 pu = cross(pr, pf);
                    float2 prior = 0.5 + float2(dot(pr,relative)/before.lens.y,-dot(pu,relative)) /
                                                  (2*tan(radians(before.lens.x)*0.5)*z);
                    if (all(prior>=0) && all(prior<1)) {
                        uint2 q = uint2(prior*float2(WIDTH,HEIGHT));
                        uint word = (q.x + q.y*WIDTH)*4;
                        if (abs(asfloat(previousFrame[word+3])-z) <= DEPTH_TOLERANCE) {
                            float3 old = asfloat(uint3(previousFrame[word],previousFrame[word+1],previousFrame[word+2]));
                            color = lerp(color, old, WEIGHT);
                        }
                    }
                }
            }
        }
    }
    outputImage.Store4(p*4, asuint(float4(color,1)));
}
