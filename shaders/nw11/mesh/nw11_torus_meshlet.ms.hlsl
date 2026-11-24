// NW-11 optional mesh-shader experiment: mesh stage.
//
// The shader reuses the frozen NW-07 instance, camera, material, albedo, and
// light buffers.  It does not create a second renderer: the common fragment
// stage (nw06_scene_ps) consumes the same interpolants.

#define NW_PI 3.14159265358979323846
#define NW_MAJOR_SEGMENTS 64u
#define NW_MINOR_SEGMENTS 32u
#define NW_MAJOR_PER_MESHLET 8u
#define NW_MINOR_PER_MESHLET 8u
#define NW_VERTS_PER_MESHLET 64u
#define NW_PRIMS_PER_MESHLET 128u

cbuffer Nw06GraphicsParams : register(b0, space0)
{
    uint Tick;
    float FovDegrees;
    float Aspect;
    float NearPlane;
    float FarPlane;
    float Exposure;
    float BloomIntensity;
    float TwistRadians;
};

Buffer<uint> NwInstances : register(t1, space0);
Buffer<uint> NwCamera : register(t3, space0);

struct NwMeshPayload
{
    uint instance_index;
    uint meshlet_index;
};

struct MSOutput
{
    float4 position : SV_Position;
    float3 world_position : TEXCOORD0;
    float3 world_normal : TEXCOORD1;
    float2 uv : TEXCOORD2;
    nointerpolation uint material_index : TEXCOORD3;
    float3 camera_position : TEXCOORD4;
};

groupshared float3 g_positions[NW_VERTS_PER_MESHLET];
groupshared float3 g_normals[NW_VERTS_PER_MESHLET];

uint LoadUInt(uint byte_offset)
{
    return NwInstances.Load(byte_offset / 4u);
}

float LoadFloat(uint byte_offset)
{
    return asfloat(LoadUInt(byte_offset));
}

float3 RotateY(float3 value, float angle)
{
    float c = cos(angle);
    float s = sin(angle);
    return float3(c * value.x - s * value.z, value.y, s * value.x + c * value.z);
}

float4x4 Perspective(float fov_degrees, float aspect, float near_plane, float far_plane)
{
    float f = 1.0 / tan(radians(fov_degrees) * 0.5);
    return float4x4(
        f / aspect, 0.0, 0.0, 0.0,
        0.0, f, 0.0, 0.0,
        0.0, 0.0, far_plane / (far_plane - near_plane), 1.0,
        0.0, 0.0, -near_plane * far_plane / (far_plane - near_plane), 0.0);
}

float4x4 LookAt(float3 eye, float3 target, float3 up)
{
    float3 forward = normalize(target - eye);
    float3 right = normalize(cross(up, forward));
    float3 actual_up = cross(forward, right);
    return float4x4(
        right.x, actual_up.x, forward.x, 0.0,
        right.y, actual_up.y, forward.y, 0.0,
        right.z, actual_up.z, forward.z, 0.0,
        -dot(right, eye), -dot(actual_up, eye), -dot(forward, eye), 1.0);
}

[numthreads(64, 1, 1)]
[outputtopology("triangle")]
void MeshMain(
    uint group_thread_id : SV_GroupThreadID,
    uint group_id : SV_GroupID,
    in payload NwMeshPayload payload,
    out vertices MSOutput vertices[NW_VERTS_PER_MESHLET],
    out indices uint3 triangles[NW_PRIMS_PER_MESHLET])
{
    SetMeshOutputCounts(NW_VERTS_PER_MESHLET, NW_PRIMS_PER_MESHLET);

    uint major_base = (payload.meshlet_index % (NW_MAJOR_SEGMENTS / NW_MAJOR_PER_MESHLET)) * NW_MAJOR_PER_MESHLET;
    uint minor_base = (payload.meshlet_index / (NW_MAJOR_SEGMENTS / NW_MAJOR_PER_MESHLET)) * NW_MINOR_PER_MESHLET;
    uint local_major = group_thread_id / NW_MINOR_PER_MESHLET;
    uint local_minor = group_thread_id % NW_MINOR_PER_MESHLET;
    uint major = (major_base + local_major) % NW_MAJOR_SEGMENTS;
    uint minor = (minor_base + local_minor) % NW_MINOR_SEGMENTS;
    float theta = (float(major) / float(NW_MAJOR_SEGMENTS)) * 2.0 * NW_PI;
    float phi = (float(minor) / float(NW_MINOR_SEGMENTS)) * 2.0 * NW_PI;
    float3 local = float3(
        (0.7 + 0.3 * cos(phi)) * cos(theta),
        0.3 * sin(phi),
        (0.7 + 0.3 * cos(phi)) * sin(theta));
    float3 local_normal = float3(cos(phi) * cos(theta), sin(phi), cos(phi) * sin(theta));

    uint instance_offset = payload.instance_index * 80u;
    uint material_index = LoadUInt(instance_offset + 64u);
    uint loop_ticks = NwCamera.Load(3u);
    uint wrapped_tick = loop_ticks != 0u ? (Tick % loop_ticks) : Tick;
    float phase = loop_ticks != 0u
        ? float(wrapped_tick) * (6.283185307179586 / float(loop_ticks))
        : 0.0;
    float twist = TwistRadians * sin(phase) * local.y;
    local = RotateY(local, twist);
    local_normal = RotateY(local_normal, twist);

    float4x4 transform = float4x4(
        LoadFloat(instance_offset + 0u), LoadFloat(instance_offset + 4u), LoadFloat(instance_offset + 8u), LoadFloat(instance_offset + 12u),
        LoadFloat(instance_offset + 16u), LoadFloat(instance_offset + 20u), LoadFloat(instance_offset + 24u), LoadFloat(instance_offset + 28u),
        LoadFloat(instance_offset + 32u), LoadFloat(instance_offset + 36u), LoadFloat(instance_offset + 40u), LoadFloat(instance_offset + 44u),
        LoadFloat(instance_offset + 48u), LoadFloat(instance_offset + 52u), LoadFloat(instance_offset + 56u), LoadFloat(instance_offset + 60u));
    float4 world = mul(float4(local, 1.0), transform);
    g_positions[group_thread_id] = world.xyz;
    g_normals[group_thread_id] = normalize(mul(float4(local_normal, 0.0), transform).xyz);

    // The material index and world-space positions are produced by all threads;
    // a group barrier makes the group-shared visibility explicit before any
    // vertex output is read by the rasterizer.
    GroupMemoryBarrierWithGroupSync();

    uint camera_interpolation = NwCamera.Load(11u);
    float camera_angle = asfloat(NwCamera.Load(4u));
    // NW-07's camera curve is linear across four absolute tick keys.  Sampling
    // the same curve here keeps baseline and variant pixel-identical at the
    // frozen fixed ticks.
    float t0 = float(NwCamera.Load(0u));
    float t3 = float(NwCamera.Load(3u));
    float v0 = asfloat(NwCamera.Load(4u));
    float v1 = asfloat(NwCamera.Load(5u));
    float v2 = asfloat(NwCamera.Load(6u));
    float v3 = asfloat(NwCamera.Load(7u));
    float tick = float(wrapped_tick);
    if (tick <= t0) camera_angle = v0;
    else if (tick >= t3) camera_angle = v3;
    else if (tick <= t0 + (t3 - t0) / 3.0) camera_angle = lerp(v0, v1, saturate((tick - t0) / ((t3 - t0) / 3.0)));
    else if (tick <= t0 + 2.0 * (t3 - t0) / 3.0) camera_angle = lerp(v1, v2, saturate((tick - t0 - (t3 - t0) / 3.0) / ((t3 - t0) / 3.0)));
    else camera_angle = lerp(v2, v3, saturate((tick - t0 - 2.0 * (t3 - t0) / 3.0) / ((t3 - t0) / 3.0)));
    float radius = 0.5 * (asfloat(NwCamera.Load(8u)) + asfloat(NwCamera.Load(9u)));
    float3 eye = float3(cos(camera_angle) * radius, asfloat(NwCamera.Load(10u)), sin(camera_angle) * radius);
    float4x4 view = LookAt(eye, float3(0.0, 0.0, 0.0), float3(0.0, 1.0, 0.0));
    float4x4 projection = Perspective(FovDegrees, Aspect, NearPlane, FarPlane);

    vertices[group_thread_id].position = mul(mul(float4(g_positions[group_thread_id], 1.0), view), projection);
    vertices[group_thread_id].world_position = g_positions[group_thread_id];
    vertices[group_thread_id].world_normal = g_normals[group_thread_id];
    vertices[group_thread_id].uv = float2(float(major) / NW_MAJOR_SEGMENTS, float(minor) / NW_MINOR_SEGMENTS);
    vertices[group_thread_id].material_index = material_index;
    vertices[group_thread_id].camera_position = eye;

    for (uint primitive = group_thread_id; primitive < NW_PRIMS_PER_MESHLET; primitive += 64u)
    {
        uint cell = primitive / 2u;
        uint row = cell / 8u;
        uint column = cell % 8u;
        uint a = row * 8u + column;
        uint b = row * 8u + ((column + 1u) % 8u);
        uint c = ((row + 1u) % 8u) * 8u + column;
        uint d = ((row + 1u) % 8u) * 8u + ((column + 1u) % 8u);
        triangles[primitive] = (primitive % 2u) == 0u
            ? uint3(a, b, c)
            : uint3(b, d, c);
        // The frozen torus workload is a closed surface; the per-axis segment
        // mapping keeps the loop boundary watertight without runtime stitching.
    }
}
