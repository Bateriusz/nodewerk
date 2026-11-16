# NW-09 Explicit Temporal State (Particles and Feedback)

- **Status:** Implemented contract
- **Implementation base:** `60d106fde9d5b70f1d53aae89096595eeeb7b8c1` (NW-08A merge)
- **Scope:** Existing DAG, compiler/export/package/player architecture, NW-05
  resource lifetime planner, and NW-08 authoring timeline/seek plus
  asynchronous preview runtime

NW-09 adds explicit temporal state without introducing a second graph model,
package family, release player, timeline, capability system, authoring runtime,
or hardware-model-specific execution path.

## Fixed-step contract

The authoritative timeline is the NW-06 integer timeline: 48,000 ticks per
second. NW-09 fixes simulation at **120 Hz**, so every simulation tick is
**exactly 400 timeline ticks**. A presentation frame may arrive at any cadence
(30 Hz, 60 Hz, irregular, or deliberately long); it converts to an absolute
simulation tick and the runtime processes **every required tick in order**.

A work cap may limit how many ticks one call processes. When the cap is hit,
the remaining ticks stay scheduled and presentation lags. Ticks are never
silently dropped.

## History declarations

A History node carries an explicit declaration:

```text
node particles nw.particles.system@1.0.0 frequency=history residency=gpu
  history state-type nw.particles.v1 initial "mode=seeded;count=12;seed=424242" reset on-loop step-rate 120
```

The declaration is semantic graph content and is covered by the node structural
identity and package semantic digest. It declares:

1. initial value,
2. state type,
3. reset policy,
4. fixed step rate.

Only `nw.particles.system` and `nw.feedback.image` expose History schemas.
Ordinary operators still reject `frequency=history` with
`history_not_supported`.

### External previous-tick input

Previous-tick state is an **external input** to the current-tick DAG. It is
injected by the fixed-step scheduler and is not an ordinary author-supplied
edge. Ordinary cycles remain invalid. A History node commits all of its writes
atomically when the tick completes; consumers in the same tick see a complete
current-tick value, while the next tick sees only the committed previous value.

Temporal state is never a pure cached output. `HIST` state is excluded from
frame-independent memoization; preview realization keys bind the exact
simulation tick, layout hash, and committed state digest.

## Ping-pong resources and lifetime

Each History declaration owns two explicit state resources: `current` and
`previous`. They never alias while both are live. The package extension records
both resource ids and the declared frames-in-flight count. State and
checkpoint allocations, plus pending-retirement memory, are accounted for
against the declared budget:

```text
pending_retirement_bytes = state_bytes * frames_in_flight
```

State resources remain valid until submitted GPU work retires. The existing
single-queue fence/retirement planner remains the only lifetime authority.

## Initialization, advancement, loop reset, seek, and replay

| Operation | Semantics |
| :--- | :--- |
| `initialize` / `reset` | Restore the declared initial state at tick 0, clear loop generation, and reset event cursors. |
| `advance_to(tick)` | Process every simulation tick from the current absolute tick to `tick // 400`, in order. A work cap leaves outstanding ticks scheduled. |
| loop reset | At an exact loop boundary the runtime increments the loop generation, resets all History states to their declared initial values, and resets the event cursor for the new generation. |
| `seek(tick, checkpoint)` | Restore a compatible checkpoint at or before the target, otherwise reset to initial state and replay every required tick from zero. |
| `replay` | Explicit seek/replay entry point used by editor playback, preview, and checkpoint recovery. |
| event replay | Events are emitted once per loop generation and tick. Backward seeks reset the event cursor but do not duplicate an already observed event in the same generation. |
| device reinitialization | Device-resident checkpoints and GPU realizations from an earlier D3D12 device lifetime or recreation epoch are rejected. Recovery must pass through the explicit NW-08 preview reinitialization boundary before replay can publish. |

## Checkpoint compatibility

Compatibility is deterministic and checks:

* checkpoint version (currently `2`; version `1` state semantics are
  incompatible because the per-element mix changed),
* tick (and its exact simulation-tick derivation),
* semantic digest,
* state/layout hash (including every History state layout hash),
* capability-profile ID and requirements digest.

A device-resident editor checkpoint additionally binds to the actual D3D12
device lifetime id and NW-08 recreation epoch. Packaged or disk checkpoints are
host-resident and make **no portable GPU-state replay claim**.

Invalid or incompatible checkpoints fall back to declared initial-state replay.
Backward seeks reset event cursors without emitting duplicate externally
observable events. Replay failures identify the stateful operator/node and the
exact simulation tick. Stale failed work cannot replace the last valid
authoring preview.

## Export and package extension

History-free documents continue to use the unchanged `.nwp` v1.0 exporter and
produce byte-identical artifacts. They link no History or checkpoint execution
support.

Stateful documents use the explicit `.nwp` v1.1 additive extension:

* `HIST` (record layout version 2) for History declarations, exact packed
  float32 initial-state bytes, and ping-pong resource identities;
* `CKPT` for optional packaged release checkpoints.

Stateful packages are still produced and consumed through the existing
`nwp_package`, `PackagePlayer`, resource-plan, schedule, and player
architecture. Packaged checkpoints report their byte cost in the export report,
size report, manifest, and package inspection.

The supplied document is compiled through the established optimizer/offline
DXC/cache/report pipeline. History nodes lower to ordinary compute
specializations; the package uploads the declared initial state with
`init.upload_history_state` and emits `frame.simulation_step` for the packaged
player's fixed-step scheduler. The player derives the target simulation tick
from the presentation timeline tick, processes every outstanding 120 Hz step in
order (or leaves a declared work cap pending), and only then presents. A
backward device playback request fails closed unless the editor first restores
a compatible checkpoint or reinitializes through the NW-08 boundary.
At a declared `nw09.loop_ticks` boundary the packaged player resets only
`on-loop` History nodes to their declared initial bytes; every other node is
still advanced through that tick. `never`/`manual` states that are ahead of the
global replay tick remain frozen until replay catches up, rather than being
stepped as if their old value belonged to tick zero.
State resources are raw `DXGI_FORMAT_R32_UINT` buffers of exactly
`values_per_element * element_count * 4` bytes; the declared particle layout is
five float32 values per particle and the feedback layout is four float32 values
per image element.

Per-element random injection uses one documented uint32 mix in Python, the C++
oracle, and the generated HLSL shader. It is a declared scalar operation, not
an atomic reduction, so CPU and GPU evaluate the same per-element value at a
matching simulation tick subject only to declared numeric tolerance.
The mix revision is included in each History state-layout hash, so a checkpoint
from the earlier SHA-256-derived semantics cannot be accepted merely because
its version, tick, and capability fields happen to match.

## Fixtures

* `fixtures/nw09/particles_seeded.nwgraph` — seeded particle system with a
  declared initial state and 120 Hz step.
* `fixtures/nw09/feedback_rect.nwgraph` — small feedback image preserving the
  explicit NW-05 rectangular presentation geometry
  (`nw05.present.width = 320`, `nw05.present.height = 180`).

## Determinism and evidence

Fixture and runtime determinism use declared scalar update order only. NW-09
does not rely on floating-point atomic reduction order, does not introduce
general asynchronous/distributed simulation, and leaves replay audio side
effects to NW-12.

GPU-less pytest coverage is contract evidence. Actual Windows D3D12 execution
and synchronization remain a distinct evidence class. Cross-device evaluation
uses declared tolerances and exact artifact/capability/environment metadata on
representative desktop Ada and Blackwell hardware; unavailable physical
coverage remains pending and never changes deterministic semantics. GPU
marketing generation or exact SKU is never a semantic, checkpoint, compiler-
target, or state-transition predicate.

