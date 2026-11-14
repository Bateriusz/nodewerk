// NW-06 GPU initialization generator for the Noise2D texture and torus mesh.
// Initialization-only: this shader never appears in the frame schedule.

cbuffer Nw06GenerateParams : register(b0, space0)
{
    uint Width;
    uint Height;
    uint TextureSeed;
    uint NoiseOctaves;
    float NoiseScale;
    float NoiseLacunarity;
    float NoiseGain;
    uint MajorSegments;
    uint MinorSegments;
    float MajorRadius;
    float MinorRadius;
};

RWTexture2D<float4> NwTexture : register(u0, space0);
RWByteAddressBuffer NwVertices : register(u1, space0);
RWByteAddressBuffer NwIndices : register(u2, space0);

uint HashU32(uint value)
{
    value ^= value >> 16u;
    value *= 0x7feb352du;
    value ^= value >> 15u;
    value *= 0x846ca68bu;
    value ^= value >> 16u;
    return value;
}

uint HashCoord(uint x, uint y, uint seed)
{
    uint value = x * 0x9e3779b9u;
    value ^= y * 0x85ebca6bu;
    value ^= seed;
    return HashU32(value);
}

float HashUnit(uint x, uint y, uint seed)
{
    return float(HashCoord(x, y, seed)) / 4294967296.0;
}

float Fade(float t)
{
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0);
}

float ValueNoise(float x, float y, uint seed)
{
    float fx0 = floor(x);
    float fy0 = floor(y);
    uint x0 = uint(fx0);
    uint y0 = uint(fy0);
    float tx = Fade(x - fx0);
    float ty = Fade(y - fy0);
    float v00 = HashUnit(x0, y0, seed);
    float v10 = HashUnit(x0 + 1u, y0, seed);
    float v01 = HashUnit(x0, y0 + 1u, seed);
    float v11 = HashUnit(x0 + 1u, y0 + 1u, seed);
    return lerp(lerp(v00, v10, tx), lerp(v01, v11, tx), ty);
}

float Noise2D(float u, float v)
{
    float amplitude = 1.0;
    float frequency = NoiseScale;
    float total = 0.0;
    float normalization = 0.0;
    [loop]
    for (uint octave = 0u; octave < NoiseOctaves; ++octave)
    {
        uint octave_seed = TextureSeed + 0x9e3779b9u * octave;
        total += amplitude * ValueNoise(u * frequency, v * frequency, octave_seed);
        normalization += amplitude;
        amplitude *= NoiseGain;
        frequency *= NoiseLacunarity;
    }
    return normalization > 0.0 ? total / normalization : 0.0;
}

void WriteTexture(uint index)
{
    uint pixel_count = Width * Height;
    if (index >= pixel_count)
    {
        return;
    }
    uint y = index / Width;
    uint x = index - y * Width;
    float u = (float(x) + 0.5) / float(Width);
    float v = (float(y) + 0.5) / float(Height);
    float n = Noise2D(u, v);
    NwTexture[uint2(x, y)] = float4(n, n, n, 1.0);
}

void WriteVertex(uint index)
{
    uint vertex_count = MajorSegments * MinorSegments;
    if (index >= vertex_count)
    {
        return;
    }
    uint major = index / MinorSegments;
    uint minor = index - major * MinorSegments;
    float u = float(major) / float(MajorSegments) * 6.283185307179586;
    float v = float(minor) / float(MinorSegments) * 6.283185307179586;
    float cos_u = cos(u);
    float sin_u = sin(u);
    float cos_v = cos(v);
    float sin_v = sin(v);
    float radial = MajorRadius + MinorRadius * cos_v;
    float3 position = float3(radial * cos_u, MinorRadius * sin_v, radial * sin_u);
    float3 normal = float3(cos_v * cos_u, sin_v, cos_v * sin_u);
    float2 uv = float2(float(major) / float(MajorSegments), float(minor) / float(MinorSegments));
    NwVertices.Store4(index * 32u, asuint(float4(position, normal.x)));
    NwVertices.Store4(index * 32u + 16u, asuint(float4(normal.y, normal.z, uv)));
}

void WriteIndex(uint index)
{
    uint index_count = MajorSegments * MinorSegments * 6u;
    if (index >= index_count)
    {
        return;
    }
    uint quad = index / 6u;
    uint corner = index - quad * 6u;
    uint major = quad / MinorSegments;
    uint minor = quad - major * MinorSegments;
    uint next_major = (major + 1u) % MajorSegments;
    uint next_minor = (minor + 1u) % MinorSegments;
    uint a = major * MinorSegments + minor;
    uint b = next_major * MinorSegments + minor;
    uint c = major * MinorSegments + next_minor;
    uint d = next_major * MinorSegments + next_minor;
    uint value = a;
    if (corner == 1u || corner == 4u) value = c;
    if (corner == 2u || corner == 3u) value = b;
    if (corner == 5u) value = d;
    NwIndices.Store(index * 4u, value);
}

[numthreads(64, 1, 1)]
void GenerateGeometryCS(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint index = dispatch_id.x;
    WriteTexture(index);
    WriteVertex(index);
    WriteIndex(index);
}
