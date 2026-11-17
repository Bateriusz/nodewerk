// NW-10 restricted intro: offline-compiled sphere/torus SDF fullscreen shader.
//
// This shader is compiled by the pinned offline DXC through the existing
// NW-05 generated-shader path.  It uses the unchanged compute.math_noise
// bindings:
//   b0 = root constants (g_params[4])
//   t0 = seeded procedural material texture
//   u0 = RWByteAddressBuffer output
//
// The graph remains the sole demo source; this file is a frozen shader
// specialization for the restricted intro profile, not a second runtime.

Texture2D<float4> nw_input : register(t0, space0);
RWByteAddressBuffer nw_output : register(u0, space0);

cbuffer NwParams : register(b0, space0)
{
    float4 g_params[4];
};

static const float NW_PI = 3.14159265358979323846;
static const float NW_WIDTH = 1920.0;
static const float NW_HEIGHT = 1080.0;
static const uint NW_LOOP_TICKS = 960000u;
static const uint NW_TICKS_PER_SECOND = 48000u;

float sdSphere(float3 p, float radius)
{
    return length(p) - radius;
}

float sdTorus(float3 p, float2 t)
{
    float2 q = float2(length(p.xz) - t.x, p.y);
    return length(q) - t.y;
}

float2 rotate2(float2 v, float angle)
{
    float c = cos(angle);
    float s = sin(angle);
    return float2(c * v.x - s * v.y, s * v.x + c * v.y);
}

float mapScene(float3 p)
{
    // One absolute-tick camera curve: a full revolution over the 20 s loop.
    float time_seconds = g_params[0].x;
    float phase = time_seconds * (NW_PI * 2.0 / 20.0);
    float3 sphere_point = p;
    sphere_point.xz = rotate2(sphere_point.xz, -phase);
    sphere_point.y += 0.15 * sin(phase * 2.0);
    float sphere = sdSphere(sphere_point - float3(-1.15, 0.0, 0.0), 0.82);

    float3 torus_point = p;
    torus_point.xz = rotate2(torus_point.xz, phase * 1.5);
    torus_point.xy = rotate2(torus_point.xy, 0.35 * sin(phase));
    float torus = sdTorus(torus_point - float3(1.15, 0.0, 0.0), float2(0.62, 0.22));

    // Smooth sphere/torus composition with a bounded blend.
    float blend = 0.18;
    float h = saturate(0.5 + 0.5 * (sphere - torus) / blend);
    return lerp(torus, sphere, h) - blend * h * (1.0 - h);
}

float3 estimateNormal(float3 p)
{
    const float e = 0.002;
    return normalize(float3(
        mapScene(p + float3(e, 0.0, 0.0)) - mapScene(p - float3(e, 0.0, 0.0)),
        mapScene(p + float3(0.0, e, 0.0)) - mapScene(p - float3(0.0, e, 0.0)),
        mapScene(p + float3(0.0, 0.0, e)) - mapScene(p - float3(0.0, 0.0, e))));
}

float3 acesTonemap(float3 color)
{
    const float a = 2.51;
    const float b = 0.03;
    const float c = 2.43;
    const float d = 0.59;
    const float e = 0.14;
    float3 numerator = color * (a * color + b);
    float3 denominator = color * (c * color + d) + e;
    return saturate(numerator / max(denominator, 1.0e-5));
}

float4 sampleMaterial(float2 uv, float seed)
{
    uint width = 0u;
    uint height = 0u;
    nw_input.GetDimensions(width, height);
    if (width == 0u || height == 0u)
    {
        return float4(0.72, 0.78, 0.86, 1.0);
    }
    float2 wrapped = frac(uv * 3.0 + seed * 0.137);
    int2 texel = int2(min(wrapped * float2(width, height), float2(width, height) - 1.0));
    float4 value = nw_input.Load(int3(texel, 0));
    return lerp(float4(0.42, 0.52, 0.72, 1.0), value, 0.35);
}

[numthreads(1024, 1, 1)]
void SDFIntroCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    if (dispatch_id.x >= (uint)NW_WIDTH || dispatch_id.y >= (uint)NW_HEIGHT)
    {
        return;
    }
    float2 uv = (float2(dispatch_id.xy) + 0.5) / float2(NW_WIDTH, NW_HEIGHT);
    float2 ndc = float2(uv.x * 2.0 - 1.0, 1.0 - uv.y * 2.0);

    // Camera curves over the frozen 20-second loop.  The phase is derived from
    // the absolute tick, never accumulated frame deltas.
    float time_seconds = g_params[0].x;
    float phase = time_seconds * (NW_PI * 2.0 / 20.0);
    float camera_radius = 4.1 + 0.35 * sin(phase * 2.0);
    float3 eye = float3(
        cos(phase) * camera_radius,
        0.25 + 0.55 * sin(phase * 0.5),
        sin(phase) * camera_radius);
    float3 forward = normalize(float3(0.0, 0.0, 0.0) - eye);
    float3 right = normalize(cross(float3(0.0, 1.0, 0.0), forward));
    float3 up = cross(forward, right);
    float3 ray_direction = normalize(
        forward +
        right * ndc.x * 1.7777777777777777 * 0.72 +
        up * ndc.y * 0.72);

    float travel = 0.0;
    float hit = 0.0;
    [unroll]
    for (uint step_index = 0u; step_index < 72u; ++step_index)
    {
        float3 p = eye + ray_direction * travel;
        float distance_value = mapScene(p);
        if (distance_value < 0.0015)
        {
            hit = 1.0;
            break;
        }
        travel += max(distance_value, 0.002);
        if (travel > 32.0)
        {
            break;
        }
    }

    float3 background = lerp(
        float3(0.015, 0.022, 0.040),
        float3(0.080, 0.110, 0.180),
        saturate(uv.y));
    float3 color = background;
    if (hit > 0.5)
    {
        float3 p = eye + ray_direction * travel;
        float3 normal = estimateNormal(p);
        float3 light_direction = normalize(float3(-0.4, -1.0, -0.3));
        float ndotl = saturate(dot(normal, -light_direction));
        float3 material_color = sampleMaterial(uv + 0.05 * normal.xy, g_params[0].y).rgb;
        float rim = pow(1.0 - saturate(dot(normal, -ray_direction)), 2.2);
        float3 diffuse = material_color * (0.18 + 0.82 * ndotl);
        float3 specular = float3(1.0, 0.93, 0.82) * pow(saturate(dot(normal, normalize(-light_direction - ray_direction))), 48.0) * 0.35;
        color = diffuse + specular + rim * float3(0.10, 0.16, 0.24);
    }
    color *= 1.1;
    color = acesTonemap(color);
    color = pow(color, float3(1.0 / 2.2, 1.0 / 2.2, 1.0 / 2.2));

    uint index = dispatch_id.x + dispatch_id.y * (uint)NW_WIDTH;
    nw_output.Store4(index * 16u, asuint(float4(color, 1.0)));
}
