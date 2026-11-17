// Asteroid Tunnel showcase forward camera and geometry deformation.
//
// flags:
//   1 = structural tunnel rib
//   2 = asteroid hazard
//   3 = seeded fixed-step dust particle
//   4 = delayed temporal echo of a hazard
//
// The particle/echo motion intentionally quantizes to NW-09's fixed 120 Hz
// cadence (400 timeline ticks at 48 kHz).  This shader does not create a second
// History implementation; it visualizes the same temporal ideas inside the
// showcase while real History remains covered by the NW-09 example artifacts.

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
static const uint NW09_FIXED_STEP_TICKS = 400u;

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
    nointerpolation uint temporal_age_steps : TEXCOORD7;
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

float WrapPositive(float value, float period)
{
    return value - period * floor(value / period);
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

void BuildParticleStreak(
    VSInput input,
    uint seed,
    out float3 local_position,
    out float3 local_normal)
{
    // Reuse the legal source torus but squash its cross-section and stretch it
    // along Z so the same indexed mesh becomes a luminous dust streak.
    float seed_phase = HashUnit(seed ^ 0x1b873593u) * TAU;
    float3 stretched = float3(
        input.position.x * 0.22,
        input.position.y * 0.22,
        input.position.z * 4.4);
    local_position = RotateZ(stretched, seed_phase);
    local_normal = normalize(
        RotateZ(float3(input.normal.x, input.normal.y, input.normal.z * 0.25), seed_phase));
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

    uint simulation_tick = wrapped_tick / NW09_FIXED_STEP_TICKS;
    uint temporal_age_steps = 0u;
    float geometry_phase = phase;

    if (instance_flags == 4u && loop_ticks != 0u)
    {
        temporal_age_steps = 6u + (instance_seed % 18u);
        uint lag_ticks = temporal_age_steps * NW09_FIXED_STEP_TICKS;
        uint delayed_tick =
            (wrapped_tick + loop_ticks - (lag_ticks % loop_ticks)) % loop_ticks;
        geometry_phase = float(delayed_tick) / float(loop_ticks);
    }

    float3 local_position;
    float3 local_normal;
    if (instance_flags == 1u)
    {
        BuildTunnelRib(input.uv, local_position, local_normal);
    }
    else if (instance_flags == 3u)
    {
        BuildParticleStreak(input, instance_seed, local_position, local_normal);
    }
    else
    {
        BuildAsteroid(input, instance_seed, geometry_phase, local_position, local_normal);
    }

    float4 world4 = mul(float4(local_position, 1.0), transform);
    float3 world = world4.xyz;
    float3 world_normal = normalize(mul(float4(local_normal, 0.0), transform).xyz);

    float3 base_center = float3(
        LoadFloat(offset + 48u),
        LoadFloat(offset + 52u),
        LoadFloat(offset + 56u));
    float3 instance_center = base_center;
    float3 local_world = world - base_center;

    if (instance_flags == 3u)
    {
        // Deterministic seeded particle integration at exactly 120 Hz.  The
        // position changes only when simulation_tick changes, mirroring NW-09's
        // explicit fixed-step behavior rather than presentation frame rate.
        float velocity =
            lerp(0.045, 0.082, HashUnit(instance_seed ^ 0x94d049bbu));
        float particle_step = float(simulation_tick);
        instance_center.z =
            WrapPositive(base_center.z + particle_step * velocity, TUNNEL_LENGTH);

        float drift_phase =
            particle_step * lerp(0.035, 0.075, HashUnit(instance_seed ^ 0x369dea0fu))
            + HashUnit(instance_seed ^ 0x7f4a7c15u) * TAU;
        instance_center.x += 0.18 * sin(drift_phase);
        instance_center.y += 0.14 * cos(drift_phase * 1.17 + 0.7);
        world = instance_center + local_world;
    }
    else if (instance_flags == 4u && loop_ticks != 0u)
    {
        // A delayed sample of the same deterministic hazard position.  Since
        // the camera advances one 96-unit segment per loop, older samples lie
        // slightly farther ahead and form a short history trail.
        float lag_distance =
            (float(temporal_age_steps * NW09_FIXED_STEP_TICKS)
             / float(loop_ticks)) * TUNNEL_LENGTH;
        instance_center.z = base_center.z + lag_distance;
        world = instance_center + local_world;
    }

    // Infinite periodic tunnel: wrap the INSTANCE CENTRE, never individual
    // vertices.  This preserves triangle continuity at the segment seam.
    float camera_z = phase * TUNNEL_LENGTH;
    float local_world_z = world.z - instance_center.z;
    float wrapped_center_z =
        WrapCentered(instance_center.z - camera_z, TUNNEL_LENGTH);
    world.z = wrapped_center_z + local_world_z;

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
    output.temporal_age_steps = temporal_age_steps;
    return output;
}
