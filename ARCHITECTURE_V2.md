# Agent Bus Architecture V2

## Status

Proposal for the post-0.1.x evolution of `agent-bus`.

## Product Positioning

`agent-bus` should evolve into a **durable coordination and governance layer for heterogeneous AI software-engineering agents**.

It already provides valuable coordination primitives: durable tasks/messages, MCP coordination, explicit ACKs, task claiming, file-lock leases, agent identities/sessions, DAGs, workers, worktrees, evidence-aware handoffs, Gatekeeper review, human decisions, event streaming and auditability.

V2 should preserve those strengths while avoiding duplication of mature runtime projects such as Orca and OpenHands.

## Core Principle

Agent Bus owns:

- tasks and dependencies,
- messages and events,
- claims and locks,
- capabilities and routing,
- artifacts and evidence,
- decisions and reviews,
- usage/cost accounting,
- audit history.

Agent Bus should not own every provider-specific concern:

- model inference implementation,
- provider authentication,
- terminal multiplexing,
- all sandbox/VM lifecycle,
- every coding CLI integration detail.

Those belong behind adapters.

## Target Architecture

```text
                         +------------------+
                         |      Human       |
                         +---------+--------+
                                   |
                                   v
                         +------------------+
                         |     Planner      |
                         | provider-neutral |
                         +---------+--------+
                                   |
                                Task DAG
                                   |
          +------------------------v-------------------------+
          |                    AGENT BUS                     |
          |                                                  |
          | Tasks / Messages / Events / Claims / Locks       |
          | Capabilities / Artifacts / Evidence / Decisions  |
          | Reviews / Usage / Audit                          |
          +------------------------+-------------------------+
                                   |
                         Capability Router
                                   |
             +---------------------+---------------------+
             |                     |                     |
             v                     v                     v
          Runtime A             Runtime B             Runtime C
          Codex                 Kimi                  Claude
             |                     |                     |
             +---------------------+---------------------+
                                   |
                          Workspace Backend
                                   |
             +---------------------+---------------------+
             |                     |                     |
             v                     v                     v
          Worktree              Docker                Remote
                                   |
                                   v
                           +---------------+
                           |  Gatekeeper   |
                           +-------+-------+
                                   |
                                  Merge
```

## Pre-Implementation Evaluation

Before V2 implementation, complete the evaluation defined in [LANDSCAPE_AND_ADOPTION.md](LANDSCAPE_AND_ADOPTION.md).

This evaluation is mandatory for runtime and planning decisions. In particular:

- Orca must be tested as an external runtime candidate before expanding native runtime responsibilities.
- OpenHands must be evaluated as a sandbox/remote execution backend before building equivalent infrastructure.
- Hermes is treated as one planner implementation, not as the fixed planner of Agent Bus.
- Patterns from Pact, Hydra, Orka and multiagents must be explicitly classified as ADOPT, ADAPT, SPIKE or REJECT.
- Decisions that affect core boundaries must be recorded as ADRs.

See [ADR-001](docs/adr/001-pluggable-planning-and-runtime.md).

## Architectural Boundaries

### Agent Bus Core

Recommended domain objects:

- `Agent`
- `AgentCapability`
- `Task`
- `TaskRequirement`
- `TaskDependency`
- `Claim`
- `Message`
- `Event`
- `LockLease`
- `Artifact`
- `Evidence`
- `DecisionRequest`
- `Review`
- `UsageRecord`
- `WorkflowRun`

Core code must remain independent from Codex, Claude, Kimi, GLM, Orca, OpenHands and provider SDKs.

### Planner

The current `HermesOrchestrator` is a useful V1, but V2 should split planning from provider inference.

```python
class Planner(Protocol):
    async def plan(self, request: PlanningRequest) -> TaskPlan:
        ...

class PlanningBackend(Protocol):
    async def complete(self, request: InferenceRequest) -> InferenceResult:
        ...
```

`HermesPlanner` should consume a `PlanningBackend`.

Hermes is optional and replaceable. A deterministic workflow planner, a human-authored planner or another planning strategy must be able to emit the same validated `TaskPlan` contract.

### Runtime Adapter

```python
class AgentRuntime(Protocol):
    async def start(self, request: RuntimeStartRequest) -> RuntimeSession:
        ...

    async def send(self, session: RuntimeSession, message: str) -> RuntimeMessage:
        ...

    async def cancel(self, session: RuntimeSession) -> None:
        ...

    async def status(self, session: RuntimeSession) -> RuntimeStatus:
        ...
```

Possible adapters:

- `NativeRuntime`
- `CodexRuntime`
- `ClaudeRuntime`
- `KimiRuntime`
- `GlmRuntime`
- `ExternalMcpRuntime`
- `OrcaRuntime`
- `OpenHandsRuntime`

### Workspace Backend

Git worktrees remain useful, but should become one implementation of a workspace abstraction.

```python
class WorkspaceBackend(Protocol):
    async def create(self, request: WorkspaceRequest) -> Workspace:
        ...

    async def cleanup(self, workspace: Workspace) -> None:
        ...

    async def snapshot(self, workspace: Workspace) -> WorkspaceSnapshot:
        ...
```

Backends can include direct checkout, Git worktree, Docker, Dev Container and remote sandbox.

## Capability-Based Routing

Workflows should request capabilities instead of naming models.

Bad:

```yaml
agent: kimi
```

Preferred:

```yaml
requires:
  - repository-analysis
  - long-context
```

Example agent:

```yaml
agent:
  id: kimi-analysis-01
  runtime: kimi
  capabilities:
    - repository-analysis
    - long-context
    - architecture
  models:
    - kimi-k3
  context_window: 1000000
  constraints:
    can_edit: false
    can_merge: false
  cost:
    class: low
  status: ready
```

Initial router policy should be deterministic:

1. filter missing required capabilities,
2. filter unavailable agents,
3. filter policy-incompatible agents,
4. rank by explicit priority,
5. use load and cost as tie-breakers.

Avoid opaque AI-based routing initially.

## Artifacts and Context Transfer

Messages are not enough for large multi-agent workflows. V2 needs first-class artifacts.

Example:

```json
{
  "artifact_id": "art_123",
  "task_id": "discover-accounting",
  "producer": "kimi-analysis-01",
  "kind": "repository-analysis",
  "media_type": "application/json",
  "uri": "file://...",
  "sha256": "...",
  "summary": "Mapped account.move fiscal-number flow"
}
```

Tasks should reference artifacts instead of duplicating large content.

Evidence remains distinct from artifacts. An artifact is produced output; evidence supports an acceptance criterion.

## Usage and Cost Accounting

Add a provider-neutral usage ledger.

```json
{
  "agent_id": "kimi-analysis-01",
  "task_id": "discover-accounting",
  "provider": "moonshot",
  "model": "kimi-k3",
  "input_tokens": 523000,
  "cached_input_tokens": 480000,
  "output_tokens": 12000,
  "wall_seconds": 184,
  "estimated_cost_usd": 1.92
}
```

Fields must allow unknown values because not every runtime exposes complete usage.

## Declarative Workflows

Reusable workflows should compile to the existing task DAG.

```yaml
workflow: feature-development
version: 1

steps:
  - id: discovery
    requires: [repository-analysis, long-context]

  - id: design
    requires: [architecture]
    depends_on: [discovery]

  - id: implementation
    requires: [implementation, tests]
    depends_on: [design]

  - id: review
    requires: [code-review]
    depends_on: [implementation]
    policy:
      independent_from: [implementation]

  - id: integration
    depends_on: [review]
    gate:
      tests: passed
      review: approved
```

LLMs may generate workflow instances, but validation remains deterministic.

## Review and Integration

The current `BranchIntegrator` and Gatekeeper design should remain a core strength.

Critical invariant:

> The commit that was reviewed must be the commit that is integrated.

Preserve:

- candidate SHA snapshots,
- target SHA snapshots,
- immutable validation window,
- test evidence,
- review evidence,
- bounded retries,
- explicit blocked states.

## Orca and OpenHands Strategy

Do not reimplement their runtime strengths.

### Orca

Treat Orca as an optional execution backend. Agent Bus should own workflow state, routing, evidence, human decisions and governance.

### OpenHands

Use OpenHands where useful for sandbox lifecycle, remote execution and long-running coding sessions. Agent Bus remains the source of truth for task state and policy.

## MCP Role

Keep MCP as the interoperability surface for direct participants.

Current flow is strong:

```text
bootstrap_agent
  -> my_pending_items
  -> claim_task
  -> prepare_edit
  -> complete_handoff
```

Potential V2 tools:

- `register_capabilities`
- `list_capabilities`
- `publish_artifact`
- `get_artifact`
- `attach_evidence`
- `report_usage`
- `request_runtime`
- `route_task`

## Security

Separate permissions by action:

- `task:read`
- `task:claim`
- `message:send`
- `lock:acquire`
- `artifact:publish`
- `review:submit`
- `runtime:start`
- `merge:approve`
- `admin:route`

An agent that can edit code must not automatically be allowed to approve or merge it.

## Non-Goals

V2 should not become:

- a new LLM SDK,
- a replacement for every coding CLI,
- a terminal multiplexer,
- a universal VM provider,
- a general-purpose workflow engine,
- a GitHub Actions replacement,
- an IDE.

## Proposed Package Direction

```text
src/agent_bus/
├── core/
│   ├── tasks.py
│   ├── messages.py
│   ├── events.py
│   ├── artifacts.py
│   ├── evidence.py
│   ├── capabilities.py
│   ├── usage.py
│   └── decisions.py
├── planning/
├── routing/
├── runtimes/
├── workspaces/
├── workflows/
├── worker/
└── mcp/
```

This is an incremental target, not a big-bang rewrite.

## Migration Strategy

1. add new domain models without moving existing code,
2. introduce adapter protocols,
3. wrap current implementations,
4. add new backends,
5. deprecate provider-specific paths only after compatibility tests pass.

## V2 Success Criteria

V2 is successful when:

1. a workflow can request capabilities without naming a model,
2. at least two runtimes can satisfy the same requirement,
3. planner inference can be swapped without changing task models,
4. worktree execution is one workspace backend instead of a global assumption,
5. artifacts can move between agents without being embedded in messages,
6. task-level usage/cost is inspectable,
7. a third-party runtime can execute work while Agent Bus retains governance,
8. Gatekeeper preserves reviewed-SHA == integrated-SHA,
9. current MCP coordination remains backward compatible,
10. V2 primitives are testable without paid model access.

## Guiding Test

When deciding whether a feature belongs in Agent Bus, ask:

> Does it coordinate, govern, route, preserve or verify work performed by agents?

If yes, it probably belongs in Agent Bus.

If it primarily executes one model, CLI, sandbox or provider-specific workflow, it should probably live behind an adapter.
