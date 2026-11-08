## Goal

Extend the DAG to particles and feedback without hidden frame-rate dependence.

## Source context

- Repository: github.com/bateriusz/nodewerk
- Source issue: #1 (https://github.com/Bateriusz/nodewerk/issues/1)
- Frozen source requirements: sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f
- Planning base: 270d298f1b037548e0ab4808b2c123c12ddcc4e6

## Final technical design

Implement explicit History read/write boundaries: previous-tick state is an external input to the current-tick DAG, writes become visible only after the tick completes, and ordinary cycles remain invalid. Use fixed 120 Hz simulation ticks, exactly 400 timeline ticks each, with ping-pong resources and declared reset values. Add one seeded particle fixture and a small feedback-image fixture. Seeking restores an editor checkpoint or resets and replays from tick zero; release checkpoints are optional packaged data whose byte cost must be reported. A delayed frame processes all required simulation ticks in order; when a work cap is reached, presentation may wait or lag, but simulation ticks are never silently discarded. Checkpoint compatibility is deterministic over checkpoint version, tick, semantic/state-layout hash, and capability-profile ID/digest; device-resident editor checkpoints additionally bind to the actual D3D12 device lifetime/epoch and are invalidated on device recreation. Packaged or disk checkpoints do not promise portable GPU-state replay across devices; cross-device correctness uses representative Ada and Blackwell hardware and declared tolerances.

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
- Distinguish semantic/state-layout compatibility from device-resident lifetime; checkpoint replay never claims portable GPU-state replay across adapters.

## State, configuration, and API changes

- Add History declarations with initial value, state type, reset policy, and fixed-step rate.
- Define checkpoint version, tick, semantic/state-layout hash, capability-profile ID/digest, and device-lifetime binding for device-resident checkpoints.
- Extend the package only through an explicit versioned change.

## Recovery semantics

- Invalid checkpoints are discarded and replay starts from the initial state.
- Backward seek resets event cursors and rebuilds state.
- Device recreation in the editor invalidates device-resident checkpoints; semantic/state-layout and capability-profile compatibility remain the packaged-checkpoint contract.
- Replay errors identify the stateful operator and tick.

## Compatibility and safety constraints

- No floating-point atomic reduction whose unspecified order is relied upon for exact reproducibility.
- Cross-device results use declared tolerances on representative desktop Ada and Blackwell hardware; no portable GPU-state replay claim is made.
- Temporal state cannot be disguised as a pure cached output.
- Device-resident state remains tied to its device lifetime.

## Test plan

- Compare direct replay and checkpoint replay at matching ticks.
- Test checkpoint compatibility across semantic/state-layout and capability-profile changes, and verify device-resident checkpoints are rejected after device recreation.
- Qualify existing cross-device tolerance tests on representative desktop Ada and Blackwell hardware with exact artifact/capability/environment metadata; missing coverage remains pending.
- Compare state under 30, 60, and irregular presentation rates.
- Test loop boundaries, backward seeks, event duplication, and long-frame catch-up.
- Run synchronization validation with multiple frames in flight.

## Regressions

- The stateless proof must retain its original bytes and behavior when history is unused.
- Seeking must not duplicate externally observable events.
- State resources must remain within declared memory budgets.
- Checkpoint compatibility rules must not drive semantic product behavior or imply portable GPU-state replay.

## Acceptance criteria

- Particle and feedback fixtures reproduce equivalent state at fixed ticks.
- Checkpoint and full replay agree within the declared contract.
- Long-frame handling preserves every simulation tick.
- History-free exports contain no stateful execution handlers.
- Checkpoint compatibility identifies semantic/state-layout and capability-profile requirements, device-resident state is tied to its device lifetime, and existing cross-device tolerance coverage runs on representative Ada and Blackwell hardware without portable GPU-state replay claims.

## Decomposition dependencies and boundaries

Dependencies:
- NW-07 provides the validated player and timeline.
- NW-08 supplies authoring seek and checkpoint integration.
- NW-01A defines the common capability-profile identity, correctness coverage, optional-profile boundaries, evidence contract, and nw07_perf_ref_v1.
- NW-01B supplies the schema, capture-tool, verifier, and backlog-publication implementation.
- Global prerequisite: NW-01A and NW-01B must both complete before implementation begins; NW-01A unblocks NW-01B only.

Boundaries:
- No general asynchronous simulation graph or distributed execution.
- No audio side effects in replay; NW-12 defines their separate policy.
- No portable GPU-state replay or exact-SKU cross-device prerequisite.

<!-- pairallel-sections:v1 {"sections":[{"end":87,"slug":"goal","start":9},{"end":382,"slug":"source-context","start":108},{"end":1590,"slug":"final-technical-design","start":411},{"end":1769,"slug":"scope","start":1602},{"end":1909,"slug":"affected-components","start":1795},{"end":2398,"slug":"required-behavior","start":1933},{"end":2753,"slug":"state-configuration-api-changes","start":2442},{"end":3144,"slug":"recovery-semantics","start":2778},{"end":3557,"slug":"compatibility-and-safety-constraints","start":3187},{"end":4208,"slug":"test-plan","start":3573},{"end":4546,"slug":"regressions","start":4226},{"end":5118,"slug":"acceptance-criteria","start":4572},{"end":5878,"slug":"decomposition-dependencies-and-boundaries","start":5166}],"version":"v1"} -->

<!-- pairallel-refinement:v1 {"base_sha":"270d298f1b037548e0ab4808b2c123c12ddcc4e6","decision":"DECOMPOSE","output_count":12,"output_index":8,"policy_fingerprint":"sha256:v1:c7d698432d5df80cd4d3ca6eb810711f4ef9f7909cfc0008b8ee5c8be3c70e6a","refinement_hash":"sha256:v1:1110fb91593dc8018b0995c64550b059ec199396e78fcb1a95b46e5dfa4bdbec","run_id":"plan-1e6c8c83858cb79fbd34a36a","source":{"host":"github.com","issue_number":1,"repository":"bateriusz/nodewerk"},"source_hash":"sha256:v1:7bbf325efe402077d68a5b5776d28e8be75e4d5da74ccc097c9aecf2783a616f","version":"v1"} -->
