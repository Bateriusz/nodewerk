float3 AnalyticEnvironment(float3 direction)
{
    // Deterministic analytic sky/horizon; no environment texture, cubemap, or
    // model weight is distributed.
    float horizon = saturate(direction.y * 0.5 + 0.5);
    return lerp(float3(0.21, 0.27, 0.36), float3(0.72, 0.78, 0.88), horizon);
}

// Trace a short reflected segment back toward the rasterized torus surface.
// Alternating 32-pixel bands bracket the surface or end before it. The
// tangent-plane intersection is analytically at ReflectionOffset.
static const float ReflectionOffset = 0.02;

float ReflectionLimit(float screen_x)
{
    return ((uint(screen_x) / 32u) & 1u) == 0u
        ? ReflectionOffset + 0.001
        : ReflectionOffset - 0.005;
}

float3 ReflectionRadiance(float3 reflected, bool hit)
{
    return hit ? float3(0.92, 0.16, 0.05) : AnalyticEnvironment(reflected);
}
