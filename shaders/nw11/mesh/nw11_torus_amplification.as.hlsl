// NW-11 optional mesh-shader experiment: amplification stage.
//
// This shader is never part of the common or fallback artifact.  It is emitted
// only for the capability-qualified mesh-shader profile.  One amplification
// workgroup emits one mesh workgroup per torus meshlet per instance; the exact
// meshlet count is derived from the frozen NW-07 turus parameters
// (64 major x 32 minor segments, 8x8 vertices per meshlet = 32 meshlets).

#define NW_TORUS_INSTANCES 256u
#define NW_MESHLETS_PER_INSTANCE 32u

struct NwMeshPayload
{
    uint instance_index;
    uint meshlet_index;
};

[numthreads(1, 1, 1)]
void AmplificationMain(
    uint group_thread_id : SV_GroupThreadID,
    uint group_id : SV_GroupID,
    out NwMeshPayload payload)
{
    // The launch grid is exactly instances * meshlets.  Keeping the mapping
    // explicit makes the dispatch bound auditable and prevents a partial or
    // duplicated torus geometry emission.
    payload.instance_index = group_id / NW_MESHLETS_PER_INSTANCE;
    payload.meshlet_index = group_id % NW_MESHLETS_PER_INSTANCE;
    if (payload.instance_index >= NW_TORUS_INSTANCES)
    {
        payload.instance_index = 0u;
        payload.meshlet_index = 0u;
    }
    DispatchMesh(1, 1, 1, payload);
}
