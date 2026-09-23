# ADR-001: Pluggable Planning and Runtime Boundaries

- Status: Accepted for V2 exploration
- Date: 2026-09-23

## Context

Agent Bus currently contains a concrete `HermesOrchestrator`, native workers/listeners, Git worktree handling and provider/client-specific integration paths.

As the ecosystem grows, projects such as Orca, OpenHands, Pact, Hydra, Orka and MCP-oriented multi-agent systems increasingly provide overlapping execution capabilities.

If Agent Bus hard-codes Hermes as the planner or continues expanding native runtime behavior without evaluating existing runtimes, the project risks becoming a monolithic agent launcher rather than a durable coordination layer.

## Decision

### 1. Hermes is not the system planner

Hermes becomes one implementation of a `Planner` contract.

The core must depend on the planner contract and validated `TaskPlan` output, not on `HermesOrchestrator`.

### 2. Planning strategy and inference backend are separate

A `Planner` decides how an objective is transformed into tasks.

A `PlanningBackend` supplies inference when required.

This allows:

- Hermes + Kimi,
- Hermes + OpenAI-compatible backend,
- Hermes + GLM,
- deterministic workflow planners,
- static/fake planners for tests,
- future planners without changing core.

### 3. Runtime execution is pluggable

Agent execution must be represented by an `AgentRuntime` contract.

The current worker/watch implementation becomes a candidate `NativeRuntime`.

External systems such as Orca and OpenHands may be implemented as runtime adapters.

### 4. Orca must be evaluated before expanding NativeRuntime

Before substantial V2 runtime work, implement a bounded Orca integration spike.

The purpose is to determine whether spawning, session lifecycle, worktree/process management or related responsibilities can be delegated.

### 5. Agent Bus remains the durable source of truth

External runtimes may execute work, but Agent Bus retains ownership of:

- tasks,
- dependencies,
- routing,
- claims,
- messages,
- decisions,
- evidence,
- review state,
- audit history,
- usage records.

### 6. External runtime objects do not leak into core

Provider/runtime-specific IDs may be stored as adapter metadata, but core domain models must not require Orca, OpenHands, Codex, Claude, Kimi or similar types.

## Consequences

### Positive

- providers can be changed without rewriting workflows,
- execution infrastructure can be adopted instead of rebuilt,
- core stays smaller,
- Agent Bus can compare runtimes empirically,
- tests can use fake planners/runtimes,
- Odoo workflows remain provider-neutral.

### Negative

- an adapter layer adds interfaces and translation code,
- external runtimes may expose incomplete state/usage data,
- some native functionality may temporarily overlap with adapters,
- compatibility testing becomes important.

## Alternatives Considered

### Keep Hermes fixed

Rejected because it couples planning strategy to one implementation and makes experimentation unnecessarily invasive.

### Build all runtimes natively

Rejected because it duplicates fast-moving CLI/sandbox ecosystems.

### Replace Agent Bus with Orca/OpenHands

Rejected because Agent Bus has a different architectural goal: durable coordination, governance, messaging, evidence and review policy across heterogeneous runtimes.

### Let an LLM dynamically choose everything

Rejected for V2 Alpha because routing and planning decisions must remain observable and deterministic where possible.

## Validation

This ADR is validated when:

1. two inference backends can drive the same planner,
2. a static planner can produce the same `TaskPlan` contract,
3. a task executes through an external runtime adapter,
4. Agent Bus still owns task/review history,
5. no external runtime type is required by core.
