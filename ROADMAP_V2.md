# Agent Bus Roadmap V2

## Goal

Evolve `agent-bus` from a strong local multi-agent coordination prototype into a durable, provider-neutral coordination and governance layer without duplicating mature runtime projects such as Orca or OpenHands.

This roadmap is incremental. Each phase must leave the current product usable.

## Phase -1 - Architecture Evaluation

### Objective

Validate the V2 boundaries before writing capability routing or runtime code.

Use [LANDSCAPE_AND_ADOPTION.md](LANDSCAPE_AND_ADOPTION.md) as the evaluation checklist and [ADR-001](docs/adr/001-pluggable-planning-and-runtime.md) as the initial architectural decision.

### Required spikes

#### Orca runtime spike

Prove whether Agent Bus can delegate execution to Orca while retaining durable ownership of tasks, routing, evidence, review state and audit history.

Evaluate launch/session lifecycle, cancellation, status visibility, worktree ownership, result/artifact capture, failure recovery and maintenance cost.

#### Planner abstraction spike

Run the same planning request through:

1. Hermes with one inference backend,
2. Hermes with a second backend,
3. a deterministic/static planner.

All must produce the same validated `TaskPlan` contract.

#### External ecosystem review

Record what Agent Bus will adopt, adapt, test or reject from Orca, OpenHands, Pact, Hydra, Orka and multiagents.

### Exit criteria

Before Phase 0 begins, answer with evidence:

- whether Orca should become a first-class runtime adapter,
- what remains in `NativeRuntime`,
- whether OpenHands is useful now or deferred,
- whether Hermes can operate with multiple backends,
- whether a non-Hermes planner can produce a valid `TaskPlan`,
- what external patterns are explicitly adopted/rejected,
- what code should not be built.

### Deliverables

- `LANDSCAPE_AND_ADOPTION.md`,
- runtime spike notes/results,
- planner spike notes/results,
- ADR updates if evidence changes the architecture,
- revised Phase 1 scope.

## Phase 0 - Freeze the Boundary

### Objective

Establish the V2 architectural boundary before adding more execution-specific features.

### Deliverables

- adopt `ARCHITECTURE_V2.md`,
- position Agent Bus as a coordination/governance layer,
- classify existing modules into core coordination, planning, runtime, workspace and integration/review,
- identify provider-specific behavior that should move behind adapters.

### Acceptance criteria

- no new provider-specific execution feature goes directly into core,
- runtime-specific changes have an adapter path,
- current tests remain green.

## Phase 1 - Capability Registry

### Objective

Stop assigning work by hard-coded model/agent identity.

### Scope

Add first-class capability metadata.

Suggested entities:

- `AgentCapability`
- `TaskRequirement`
- `AgentAvailability`

Suggested initial capabilities:

- `repository-analysis`
- `long-context`
- `architecture`
- `implementation`
- `debugging`
- `tests`
- `code-review`
- `security-review`
- `git`
- `odoo`
- `sql`
- `frontend`

### CLI ideas

```bash
agent-bus agent capabilities codex
agent-bus agent register-capability codex implementation
agent-bus agent register-capability kimi long-context
agent-bus route TASK_ID
```

### MCP additions

- `register_capabilities`
- `get_agent_capabilities`
- `route_task`

### Routing V1

1. required-capability filter,
2. availability filter,
3. project-policy filter,
4. explicit priority,
5. current load,
6. cost-class tie-break.

### Tests

- exact capability match,
- missing requirement rejection,
- unavailable-agent filtering,
- deterministic tie resolution,
- project isolation.

### Definition of done

A task can be created with:

```json
{
  "requires": ["repository-analysis", "long-context"]
}
```

and routed without naming Kimi, Codex, Claude or another provider.

## Phase 2 - Runtime Adapter Interface

### Objective

Separate agent execution from coordination.

### Deliverables

Create:

```text
src/agent_bus/runtimes/
├── base.py
├── registry.py
└── native.py
```

Minimal stable interface:

```python
class AgentRuntime(Protocol):
    async def start(self, request: RuntimeStartRequest) -> RuntimeSession: ...
    async def send(self, session: RuntimeSession, message: str) -> RuntimeMessage: ...
    async def status(self, session: RuntimeSession) -> RuntimeStatus: ...
    async def cancel(self, session: RuntimeSession) -> None: ...
```

Wrap the current worker/watch execution path as `NativeRuntime`. Do not rewrite it.

### Acceptance criteria

- current listeners/workers operate through the adapter contract,
- a fake runtime executes all runtime tests,
- core imports no provider-specific CLI code.

## Phase 3 - External Runtime Pilot

### Objective

Prove that Agent Bus can govern work it does not execute itself.

### Recommended first adapter

**Orca** or a generic external-command runtime.

### Deliverables

- runtime job/session mapping,
- task -> external session relationship,
- status bridge,
- cancellation,
- completion mapping,
- output capture.

### Definition of done

A task can:

1. originate in Agent Bus,
2. be routed to an external runtime,
3. execute outside Agent Bus,
4. return result/evidence,
5. enter the existing review/integration pipeline,
6. remain fully visible in Agent Bus history.

## Phase 4 - Artifact Store

### Objective

Move large context and results as artifacts instead of messages.

### Data model

Add:

- `Artifact`
- `TaskArtifact`
- `ArtifactReference`

Initial storage can remain filesystem + SQLite metadata.

### Initial kinds

- `repository-analysis`
- `patch`
- `diff`
- `test-report`
- `architecture`
- `structured-data`
- `log`

### MCP

- `publish_artifact`
- `list_task_artifacts`
- `get_artifact_metadata`

Do not stream arbitrarily large binary payloads through MCP in V1.

### Definition of done

A discovery agent can publish a structured repository analysis and an implementation agent can consume it by artifact ID.

## Phase 5 - Evidence V2

### Objective

Make acceptance criteria machine-auditable.

Example:

```json
{
  "criterion": "all tests pass",
  "evidence": [
    {
      "artifact_id": "test-report-42",
      "kind": "test-result",
      "status": "passed"
    }
  ]
}
```

### Gatekeeper integration

Allow policies to require:

- test evidence,
- review evidence,
- exact SHA match,
- optional specialized review.

### Definition of done

Under a strict policy, a task cannot be integrated if required evidence is missing even when the author says it is complete.

## Phase 6 - Planner Decoupling

### Objective

Turn `HermesOrchestrator` into a provider-neutral planner.

### Refactor target

```text
planning/
├── base.py
├── hermes.py
├── schema.py
└── backends/
    ├── openai_compatible.py
    ├── static.py
    └── fake.py
```

The planner may propose tasks. The bus validates task schema, dependencies, capabilities and policy.

### Definition of done

The same planner logic works with two inference backends without changing core task code.

## Phase 7 - Usage and Cost Ledger

### Objective

Make cost and consumption observable per task and workflow.

### Data model

Add `UsageRecord` with:

- agent,
- runtime,
- provider,
- model,
- task,
- input tokens,
- cached tokens,
- output tokens,
- wall time,
- estimated cost,
- source,
- confidence.

### CLI

```bash
agent-bus usage
agent-bus usage --task T123
agent-bus usage --agent codex
agent-bus usage --workflow feature-42
```

### Definition of done

The operator can answer:

- what did this task cost?
- which agent consumed the most?
- which workflow produced the most retries?
- how much cached input was reused?

## Phase 8 - Declarative Workflow Compiler

### Objective

Allow reusable engineering workflows to compile into the existing DAG.

### First workflow

`feature-development`

```yaml
steps:
  - discovery
  - design
  - implementation
  - review
  - integration
```

Other useful workflows:

- bug-fix,
- migration,
- security-review,
- codebase-discovery,
- Odoo-module-development,
- Odoo-version-migration.

### Definition of done

A YAML workflow can be loaded, validated, compiled to tasks and executed using the existing bus.

## Phase 9 - Workspace Backend Interface

### Objective

Remove the assumption that all isolated work happens through Git worktrees.

### Deliverables

```text
workspaces/
├── base.py
├── direct.py
├── worktree.py
└── fake.py
```

Migrate the current worktree implementation behind the interface first.

Then optionally add Docker, Dev Containers or remote sandboxes.

### Definition of done

The integrator receives a workspace abstraction instead of constructing paths from agent identity.

## Phase 10 - OpenHands / Remote Runtime

### Objective

Support one rich external sandbox/runtime.

Delegate:

- sandbox startup,
- remote command execution,
- tool environment,
- long-running session.

Retain in Agent Bus:

- task state,
- routing,
- evidence,
- decisions,
- audit,
- review policy.

# Priority Order

```text
-1. Architecture Evaluation / Spikes
0. Freeze the Boundary
1. Capability Registry
2. Runtime Interface
3. External Runtime Pilot
4. Artifact Store
5. Evidence V2
6. Planner Decoupling
7. Usage / Cost
8. Workflow Compiler
9. Workspace Interface
10. Remote Runtime
```

Phase -1 validates the architectural bet before implementation. The first three implementation phases then validate the selected contracts.

# What Not to Build Yet

Defer:

- distributed message brokers,
- Kubernetes orchestration,
- cloud control plane,
- vector database,
- custom container scheduler,
- proprietary model gateway,
- complex AI-based router,
- automatic billing,
- enterprise RBAC UI,
- plugin marketplace.

SQLite and local-first operation are strengths at the current stage.

# Suggested Epics

## Epic A - Capability Routing

- capability model,
- agent capability registration,
- task requirements,
- deterministic router,
- MCP tools,
- CLI views,
- integration tests.

## Epic B - Runtime Abstraction

- runtime protocol,
- native worker adapter,
- runtime registry,
- fake runtime,
- external command runtime,
- Orca proof of concept.

## Epic C - Artifact and Evidence Layer

- artifact metadata,
- filesystem artifact backend,
- task-artifact references,
- evidence links,
- Gatekeeper policy updates.

## Epic D - Planner V2

- provider backend extraction,
- Hermes migration,
- static/fake backend,
- schema compatibility tests.

## Epic E - Usage

- usage model,
- runtime reporting,
- provider estimates,
- task/workflow aggregation.

## Epic F - Workflow DSL

- schema,
- parser,
- compiler,
- feature-development template,
- Odoo templates.

# Odoo-Specific Validation Workflow

Use a real engineering workflow as the main V2 pilot.

## Objective

Implement or modify an Odoo accounting/localization feature.

## Roles

```text
Discovery
  requires: repository-analysis, long-context, odoo

Architecture
  requires: architecture, odoo

Implementation
  requires: implementation, python, odoo, tests

Review
  requires: code-review, odoo

Integration
  requires: git
  gate:
    tests: passed
    review: approved
```

A practical test deployment may map:

- Kimi -> discovery,
- GPT/Claude -> architecture,
- Codex -> implementation,
- GLM/Claude -> independent review,
- Agent Bus Gatekeeper -> integration policy.

The workflow itself must not depend on those identities.

### Success condition

Replace any one provider with another agent exposing the same capabilities without changing the workflow definition.

# Milestone Exit Criteria

## V2 Alpha

- capability routing,
- runtime abstraction,
- one external runtime,
- artifact references,
- backward-compatible MCP.

## V2 Beta

- evidence V2,
- planner decoupling,
- usage accounting,
- workflow compiler.

## V2 Stable

- workspace abstraction,
- external sandbox runtime,
- hardened migrations,
- operator documentation,
- compatibility tests across multiple agent clients.

# Engineering Rules During Migration

1. No big-bang rewrite.
2. Preserve existing CLI behavior unless explicitly deprecated.
3. Keep MCP contracts backward compatible when possible.
4. New infrastructure must be testable without paid APIs.
5. All external runtimes require fake/test adapters.
6. Routing decisions must be observable.
7. No provider-specific object may leak into core domain models.
8. Keep SQLite as the default persistence backend.
9. Gatekeeper invariants must not regress.
10. Every phase needs an end-to-end acceptance test.

# Immediate Next Sprint

Do **not** start Phase 1 yet.

The next sprint is Phase -1 and should contain only evaluation work:

### 1. Orca runtime spike

Build the smallest adapter/probe necessary to measure whether Orca can execute Agent Bus-owned work without taking over governance.

### 2. Planner abstraction spike

Demonstrate interchangeable planning backends and a non-Hermes planner against the same `TaskPlan` contract.

### 3. Adoption decision record

Update `LANDSCAPE_AND_ADOPTION.md` and ADRs with measured conclusions.

After Phase -1 exits successfully, the first implementation sprint should contain only three technical objectives:

### 1. Capability model

Add capability registration and task requirements.

### 2. Deterministic router

Route unassigned tasks to eligible available agents.

### 3. Runtime protocol

Define the runtime interface and wrap the current native worker.

Do **not** start Orca/OpenHands adapters until these contracts exist.

A good sprint result is:

```text
Task
  requires: [implementation, python]

        |
        v

Capability Router

        |
        +---- codex-01
        +---- claude-02
        +---- glm-01

        |
        v

NativeRuntime
```

with complete unit and integration coverage.

After that foundation is stable, an external runtime adapter becomes a contained integration project instead of another architectural branch.
