# NW-09 — Introduce explicit temporal state and replayable simulation

## Goal

Extend the DAG to particles and feedback without hidden frame-rate dependence.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Implement explicit History read/write boundaries: previous-tick state is an external input to the current-tick DAG, writes become visible only after the tick completes, and ordinary cycles remain invalid. Use fixed 120 Hz simulation ticks, exactly 400 timeline ticks each, with ping-pong resources and declared reset values. Add one seeded particle fixture and a small feedback-image fixture. Seeking restores an editor checkpoint or resets and replays from tick zero; release checkpoints are optional packaged data whose byte cost must be reported. A delayed frame processes all required simulation ticks in order; when a work cap is reached, presentation may wait or lag, but simulation ticks are never silently discarded.

## Scope

- History schemas and state-resource scheduling.
- Fixed-step simulation and reset/replay.
- Optional editor checkpoints.
- Representative particle and feedback tests.

## Affected components

- Proposed timeline/state scheduler
- Proposed History and particle operators
- Proposed resource lifetime planner

## Required behavior

- Initialization, tick advancement, loop reset, seek, and event replay have explicit semantics.
- Different render frame rates produce equivalent state at the same simulation tick.
- History resources never alias concurrently live current/previous state.
- Stateless exports link no history/checkpoint support.

## State, configuration, and API changes

- Add History declarations with initial value, state type, reset policy, and fixed-step rate.
- Define checkpoint version, tick, semantic hash, and device compatibility.
- Extend the package only through an explicit versioned change.

## Recovery semantics

- Invalid checkpoints are discarded and replay starts from the initial state.
- Backward seek resets event cursors and rebuilds state.
- Device recreation in the editor invalidates device-resident checkpoints.
- Replay errors identify the stateful operator and tick.

## Compatibility and safety constraints

- No floating-point atomic reduction whose unspecified order is relied upon for exact reproducibility.
- Cross-device results use declared tolerances.
- Temporal state cannot be disguised as a pure cached output.

## Test plan

- Compare direct replay and checkpoint replay at matching ticks.
- Compare state under 30, 60, and irregular presentation rates.
- Test loop boundaries, backward seeks, event duplication, and long-frame catch-up.
- Run synchronization validation with multiple frames in flight.

## Regressions

- The stateless proof must retain its original bytes and behavior when history is unused.
- Seeking must not duplicate externally observable events.
- State resources must remain within declared memory budgets.

## Acceptance criteria

- Particle and feedback fixtures reproduce equivalent state at fixed ticks.
- Checkpoint and full replay agree within the declared contract.
- Long-frame handling preserves every simulation tick.
- History-free exports contain no stateful execution handlers.

## Decomposition dependencies and boundaries

Dependencies:
- NW-07 provides the validated player and timeline.
- NW-08 supplies authoring seek and checkpoint integration.

Boundaries:
- No general asynchronous simulation graph or distributed execution.
- No audio side effects in replay; NW-12 defines their separate policy.
