// NW-07-test-demo branch-only forward tunnel camera and geometry deformation.
// Keeps the NW-06 graphics bindings unchanged so the normal package/player path
// can execute this specialization without a second runtime.

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

static const float PI = 3.14159265358979323846;
static const float TAU = 6.28318530717958647692;
static const float TUNNEL_LENGTH = 96.0;

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
    nointerpolation uint instance_seed : TEXCOORD5;
    nointerpolation uint instance_flags : TEXCOORD6;
};

uint LoadUInt(uint byte_offset)
{
    return NwInstances.Load(byte_offset / 4u);
}

float LoadFloat(uint byte_offset)
{
    return asfloat(LoadUInt(byte_offset));
}

uint HashU32(uint value)
{
    value ^= value >> 16u;
    value *= 0x7feb352du;
    value ^= value >> 15u;
    value *= 0x846ca68bu;
    value ^= value >> 16u;
    return value;
}

float HashUnit(uint value)
{
    return float(HashU32(value)) / 4294967296.0;
}

float3 RotateX(float3 value, float angle)
{
    float c = cos(angle);
    float s = sin(angle);
    return float3(value.x, c * value.y - s * value.z, s * value.y + c * value.z);
}

float3 RotateY(float3 value, float angle)
{
    float c = cos(angle);
    float s = sin(angle);
    return float3(c * value.x - s * value.z, value.y, s * value.x + c * value.z);
}

float3 RotateZ(float3 value, float angle)
{
    float c = cos(angle);
    float s = sin(angle);
    return float3(c * value.x - s * value.y, s * value.x + c * value.y, value.z);
}

float WrapCentered(float value, float period)
{
    return value - period * floor((value + 0.5 * period) / period);
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

void BuildTunnelRib(
    float2 uv,
    out float3 local_position,
    out float3 local_normal)
{
    float u = uv.x * TAU;
    float v = uv.y * TAU;
    float tube = 0.015;
    float radial = 1.0 + tube * cos(v);
    local_position = float3(radial * cos(u), radial * sin(u), tube * sin(v));
    local_normal = normalize(float3(cos(v) * cos(u), cos(v) * sin(u), sin(v)));
}

void BuildAsteroid(
    VSInput input,
    uint seed,
    float phase,
    out float3 local_position,
    out float3 local_normal)
{
    float u = input.uv.x * TAU;
    float v = input.uv.y * TAU;
    float seed_phase = HashUnit(seed ^ 0x68bc21ebu) * TAU;

    // The source mesh remains the legal NW-07 torus.  Its near-closed centre
    // plus low topology and deterministic multi-frequency displacement makes
    // it read as irregular asteroid/debris rather than a clean ring.
    float lump =
        1.0
        + 0.18 * sin(3.0 * u + seed_phase)
        + 0.11 * sin(5.0 * v + seed_phase * 1.7)
        + 0.07 * sin(2.0 * u + 7.0 * v + seed_phase * 0.63);

    local_position = input.position * lump;
    local_normal = normalize(input.normal);

    float spin_strength = max(0.35, abs(TwistRadians) / TAU);
    float ax = seed_phase + phase * TAU * (0.35 + 0.11 * spin_strength);
    float ay = seed_phase * 0.73 + phase * TAU * (0.22 + 0.07 * spin_strength);
    float az = seed_phase * 1.31 + phase * TAU * (0.18 + 0.05 * spin_strength);

    local_position = RotateZ(RotateY(RotateX(local_position, ax), ay), az);
    local_normal = normalize(RotateZ(RotateY(RotateX(local_normal, ax), ay), az));
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
    uint instance_seed = LoadUInt(offset + 68u);
    uint instance_flags = LoadUInt(offset + 72u);

    uint loop_ticks = NwCamera.Load(3u);
    uint wrapped_tick = loop_ticks != 0u ? (Tick % loop_ticks) : Tick;
    float phase = loop_ticks != 0u
        ? float(wrapped_tick) / float(loop_ticks)
        : 0.0;

    float3 local_position;
    float3 local_normal;
    if (instance_flags == 1u)
    {
        BuildTunnelRib(input.uv, local_position, local_normal);
    }
    else
    {
        BuildAsteroid(input, instance_seed, phase, local_position, local_normal);
    }

    float4 world4 = mul(float4(local_position, 1.0), transform);
    float3 world = world4.xyz;
    float3 world_normal = normalize(mul(float4(local_normal, 0.0), transform).xyz);

    // Infinite periodic tunnel: the static 96-unit content segment is wrapped
    // around the camera.  One 24 s phase advances exactly one segment.
    float camera_z = phase * TUNNEL_LENGTH;
    world.z = WrapCentered(world.z - camera_z, TUNNEL_LENGTH);

    // Small periodic weave and bank create the "ship through tunnel" sensation
    // while the scene remains a pure function of absolute tick.
    float sway_x = 0.16 * sin(phase * TAU * 2.0);
    float sway_y = 0.10 * sin(phase * TAU * 3.0 + 0.8);
    float aim_x = 0.055 * sin(phase * TAU * 2.0 + 0.65);
    float aim_y = 0.040 * sin(phase * TAU * 3.0 + 1.5);
    float roll = 0.075 * sin(phase * TAU * 2.0 + 0.35);

    float3 eye = float3(sway_x, sway_y, -0.35);
    float3 target = float3(aim_x, aim_y, 1.0);
    float3 up = normalize(float3(sin(roll), cos(roll), 0.0));

    float4x4 view = LookAt(eye, target, up);
    float4x4 projection = Perspective(FovDegrees, Aspect, NearPlane, FarPlane);

    VSOutput output;
    output.position = mul(mul(float4(world, 1.0), view), projection);
    output.world_position = world;
    output.world_normal = world_normal;
    output.uv = input.uv;
    output.material_index = material_index;
    output.camera_position = eye;
    output.instance_seed = instance_seed;
    output.instance_flags = instance_flags;
    return output;
}
