# ADR-0008: NW-09 Explicit Temporal State Extension

- **Status:** Accepted
- **Task:** NW-09
- **Date:** 2026-11-16
- **Implementation base:** `60d106fde9d5b70f1d53aae89096595eeeb7b8c1`

## Context

NW-08 and NW-08A delivered the authoring editor, integer timeline/seek,
asynchronous generation/device-epoch-safe preview, and the explicit
rectangular presentation geometry correction. NW-09 must add particles and
feedback without creating a second graph model, package family, release player,
timeline, capability system, authoring runtime, or hardware-specific execution
path.

## Decision

1. **History is node metadata, not a second graph model.** The authoritative
   `GraphDocument` gains a History declaration containing state type, initial
   value, reset policy, and fixed 120 Hz rate.
2. **Previous-tick state is external.** It is supplied by the fixed-step
   scheduler; it is not an ordinary edge. Ordinary cycles stay invalid.
3. **Fixed-step scheduling is explicit.** 120 Hz at 48,000 integer timeline
   ticks/second means exactly 400 timeline ticks per simulation tick. Work caps
   can delay presentation but never drop ticks.
4. **State is ping-pong and fence-safe.** Current/previous resources are
   distinct, never aliased while live, and stay valid until submitted work
   retires. Existing NW-05 lifetime and memory-budget accounting remains the
   authority.
5. **Checkpoints are explicit and narrow.** Compatibility covers version, tick,
   semantic/state-layout hashes, and capability-profile identity. Device
   checkpoints additionally bind to the actual D3D12 device lifetime and NW-08
   recreation epoch.
6. **The package extension is additive and versioned.** Stateless `.nwp` 1.0
   bytes remain unchanged. Stateful packages use `.nwp` 1.1 with optional
   `HIST`/`CKPT` sections and report checkpoint byte cost.
7. **Replay integrates with NW-08 safety.** Stale compile/evaluation/preview/
   replay work cannot publish after a newer graph generation or explicit
   device reinitialization. Replay failures leave the last valid preview.

## Consequences

- History-free artifacts remain baseline-compatible and link no History or
  checkpoint execution support.
- Stateful artifacts still flow through the existing compiler/package/player
  architecture.
- Physical Ada/Blackwell execution evidence remains distinct from GPU-less
  contract tests and does not alter deterministic semantics when unavailable.

