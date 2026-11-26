// NW-11 optional mesh-shader experiment: amplification stage.
//
// This shader is never part of the common or fallback artifact.  It is emitted
// only for the capability-qualified mesh-shader profile.  One amplification
// workgroup emits one mesh workgroup per graph-generated torus meshlet per
// instance; the instance and meshlet counts come from the same graph-derived
// root constants that produce the dispatch grid.

cbuffer Nw11MeshParams : register(b1, space0)
{
    uint MajorSegments;
    uint MinorSegments;
    float MajorRadius;
    float MinorRadius;
    uint VertexCount;
    uint IndexCount;
    uint InstanceCount;
    uint MeshletsPerInstance;
};

struct NwMeshPayload
{
    uint instance_index;
    uint meshlet_index;
};

groupshared NwMeshPayload payload;

[numthreads(1, 1, 1)]
void AmplificationMain(uint3 group_id : SV_GroupID)
{
    // The launch grid is exactly instances * meshlets.  Keeping the mapping
    // explicit makes the dispatch bound auditable and prevents a partial or
    // duplicated torus geometry emission.
    uint meshlets = max(1u, MeshletsPerInstance);
    payload.instance_index = group_id.x / meshlets;
    payload.meshlet_index = group_id.x % meshlets;
    if (payload.instance_index >= InstanceCount)
    {
        payload.instance_index = 0u;
        payload.meshlet_index = 0u;
    }
    DispatchMesh(1, 1, 1, payload);
}
