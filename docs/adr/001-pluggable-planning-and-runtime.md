# ADR-001: Pluggable Planning and Runtime Boundaries

- Status: Accepted boundary; external integrations remain unproven
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

Static and human-authored planners do not require an inference backend. The
first spike defines the versioned plan contract against today's
`TaskBreakdownPlan` before a provider-specific adapter is selected.

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

### 4. An external runtime must pass preflight before expanding NativeRuntime

Before substantial V2 runtime work, evaluate named external candidates and
implement a bounded executable integration spike.

The purpose is to determine whether spawning, session lifecycle,
worktree/process management or related responsibilities can be delegated. The
first `orca-cli/orca` candidate at commit `5beeefc` failed command preflight:
it provides no task launch or cancellation command. It is not the first
adapter; use a generic external-command probe and reconsider Orca only with a
viable revision. See the [Phase -1 evidence](../spikes/2026-09-23-phase-minus-one.md).

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

### 7. Agent Bus owns eligibility and execution identity

An agent's declared capabilities are not authorization. The bus records
project-approved capabilities and task requirements, checks them during
routing and at atomic claim, and creates an idempotent runtime-attempt ID.
External session IDs remain opaque adapter metadata. Unknown outcomes are
reconciled before another attempt starts.

### 8. Approval and integration identify different commits

The reviewed candidate SHA must be the candidate input to integration. A
normal non-fast-forward merge creates a new integration commit whose second
parent is that candidate. The target baseline must still match the one
validated for the review. Gatekeeper approval is the default merge policy;
explicit advisory review may merge `CHANGES_REQUESTED` with passing tests,
but never `BLOCKED`. No workflow compiler enforces `review: approved` today;
Phase 8 must reject advisory mode for that gate.

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
5. no external runtime type is required by core,
6. a restart reconciles an external attempt without duplicate execution,
7. direct claims cannot bypass required capabilities,
8. a strict workflow never merges `CHANGES_REQUESTED`, and the integration
   commit records the approved candidate as its second parent.
