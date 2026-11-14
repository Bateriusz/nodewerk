// NW-06 conventional indexed/instanced raster vertex stage.
//
// Instances are read from a dense row-major 80-byte table.  The camera path is
// sampled from absolute 48 kHz tick keys; no frame-delta accumulation occurs.

cbuffer Nw06GraphicsParams : register(b0, space0)
{
    // Absolute integer tick at the fixed NW-06 rate of 48,000 ticks/second.
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

struct VSInput
{
    float3 position : POSITION;
    float3 normal : NORMAL;
    float2 uv : TEXCOORD0;
    uint instance_id : SV_InstanceID;
};

struct VSOutput
{
    float4 position : SV_Position;
    float3 world_position : TEXCOORD0;
    float3 world_normal : TEXCOORD1;
    float2 uv : TEXCOORD2;
    nointerpolation uint material_index : TEXCOORD3;
    float3 camera_position : TEXCOORD4;
};

uint LoadUInt(uint byte_offset)
{
    return NwInstances.Load(byte_offset / 4u);
}

float LoadFloat(uint byte_offset)
{
    return asfloat(LoadUInt(byte_offset));
}

float SampleCurve(uint tick, uint interpolation)
{
    uint tick0 = NwCamera.Load(0u);
    uint tick1 = NwCamera.Load(1u);
    uint tick2 = NwCamera.Load(2u);
    uint tick3 = NwCamera.Load(3u);
    float value0 = asfloat(NwCamera.Load(4u));
    float value1 = asfloat(NwCamera.Load(5u));
    float value2 = asfloat(NwCamera.Load(6u));
    float value3 = asfloat(NwCamera.Load(7u));
    float t = float(tick);
    if (t <= float(tick0)) return value0;
    if (t >= float(tick3)) return value3;
    if (interpolation == 1u)
    {
        // (previous, current] step semantics: a sample on a key uses that
        // key's value.
        if (t < float(tick1)) return value0;
        if (t < float(tick2)) return value1;
        return value2;
    }
    if (t <= float(tick1))
    {
        float d = max(1.0, float(tick1 - tick0));
        return lerp(value0, value1, saturate((t - float(tick0)) / d));
    }
    if (t <= float(tick2))
    {
        float d = max(1.0, float(tick2 - tick1));
        return lerp(value1, value2, saturate((t - float(tick1)) / d));
    }
    float d = max(1.0, float(tick3 - tick2));
    return lerp(value2, value3, saturate((t - float(tick2)) / d));
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

VSOutput SceneVS(VSInput input)
{
    uint offset = input.instance_id * 80u;
    float4x4 transform = float4x4(
        LoadFloat(offset + 0u), LoadFloat(offset + 4u), LoadFloat(offset + 8u), LoadFloat(offset + 12u),
        LoadFloat(offset + 16u), LoadFloat(offset + 20u), LoadFloat(offset + 24u), LoadFloat(offset + 28u),
        LoadFloat(offset + 32u), LoadFloat(offset + 36u), LoadFloat(offset + 40u), LoadFloat(offset + 44u),
        LoadFloat(offset + 48u), LoadFloat(offset + 52u), LoadFloat(offset + 56u), LoadFloat(offset + 60u));
    uint material_index = LoadUInt(offset + 64u);

    float phase = float(Tick) * (6.283185307179586 / 48000.0);
    float twist = TwistRadians * sin(phase) * input.position.y;
    float3 local_position = RotateY(input.position, twist);
    float3 local_normal = RotateY(input.normal, twist);
    float4 world = mul(float4(local_position, 1.0), transform);
    float3 world_normal = normalize(mul(float4(local_normal, 0.0), transform).xyz);

    uint camera_interpolation = NwCamera.Load(11u);
    float camera_angle = SampleCurve(Tick, camera_interpolation);
    float radius_min = asfloat(NwCamera.Load(8u));
    float radius_max = asfloat(NwCamera.Load(9u));
    float camera_height = asfloat(NwCamera.Load(10u));
    float camera_radius = 0.5 * (radius_min + radius_max);
    float3 eye = float3(
        cos(camera_angle) * camera_radius,
        camera_height,
        sin(camera_angle) * camera_radius);
    float4x4 view = LookAt(eye, float3(0.0, 0.0, 0.0), float3(0.0, 1.0, 0.0));
    float4x4 projection = Perspective(FovDegrees, Aspect, NearPlane, FarPlane);

    VSOutput output;
    output.position = mul(mul(world, view), projection);
    output.world_position = world.xyz;
    output.world_normal = world_normal;
    output.uv = input.uv;
    output.material_index = material_index;
    output.camera_position = eye;
    return output;
}
