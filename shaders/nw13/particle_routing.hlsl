// Matched bounded particle routing microbenchmark. Both arms write one value
// into each candidate slot, including zero for an occluded candidate.
RWStructuredBuffer<uint> result : register(u0);
cbuffer Frame : register(b0) { uint tick; };

uint Hash(uint x) {
    x = (x ^ (x >> 16)) * 0x7feb352d;
    x = (x ^ (x >> 15)) * 0x846ca68b;
    return x ^ (x >> 16);
}
bool Visible(uint index) { return (Hash(index ^ tick) & 7) < 3; }
uint ShadeParticle(uint index) {
    uint value = Hash(index + tick);
    [loop]
    for (uint i=0; i<24; ++i) value = Hash(value ^ i);
    return value;
}
#ifndef WORK_GRAPH
[numthreads(64,1,1)]
void Conventional(uint3 thread : SV_DispatchThreadID) {
    uint index = thread.x;
    result[index] = Visible(index) ? ShadeParticle(index) : 0;
}
#else
struct ParticleRecord { uint index; };

[Shader("node")]
[NodeLaunch("broadcasting")]
[NodeIsProgramEntry]
[NodeDispatchGrid(16,1,1)]
[numthreads(64,1,1)]
void Route(uint3 thread : SV_DispatchThreadID,
           [MaxRecords(64)] [NodeID("Shade")] NodeOutput<ParticleRecord> output) {
    uint index = thread.x;
    bool visible = Visible(index);
    ThreadNodeOutputRecords<ParticleRecord> records = output.GetThreadNodeOutputRecords(visible ? 1 : 0);
    if (visible) records.Get().index = index;
    else result[index] = 0;
    records.OutputComplete();
}

[Shader("node")]
[NodeLaunch("thread")]
void Shade(ThreadNodeInputRecord<ParticleRecord> input) {
    uint index = input.Get().index;
    result[index] = ShadeParticle(index);
}
#endif
