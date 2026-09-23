# Landscape and Adoption Review

## Purpose

Before implementing Agent Bus V2, evaluate adjacent multi-agent coding systems and explicitly decide what to adopt, adapt, test, or reject.

This document is a decision aid, not a feature wishlist.

Decision labels:

- **ADOPT**: use the pattern directly where compatible.
- **ADAPT**: keep the idea but implement it in Agent Bus terms.
- **SPIKE**: build a bounded proof of concept before deciding.
- **REJECT**: intentionally do not pursue the pattern now.

---

## Evaluation Criteria

Each external pattern should be judged against:

1. Does it reduce code Agent Bus must maintain?
2. Does it preserve Agent Bus as the durable source of truth?
3. Does it keep providers/runtimes replaceable?
4. Does it improve observability or safety?
5. Can it be tested without paid model access?
6. Does it avoid leaking provider-specific concepts into core?
7. Does it preserve reviewed-SHA == integrated-SHA?
8. Can it work with local-first SQLite deployments?
9. Does it make Odoo-oriented workflows easier to compose?
10. Can the integration be removed later without rewriting core?

---

## Orca

### What to study

- runtime/session lifecycle,
- worktree-oriented execution,
- separation between task state and coding agent process,
- single-binary operator experience,
- external runtime control.

### Decision

- External execution backend: **SPIKE**
- Reuse as primary runtime where useful: **SPIKE**
- Replace Agent Bus durable task/message layer: **REJECT**
- Copy Orca-specific concepts into core domain objects: **REJECT**
- Learn from operational simplicity: **ADOPT**

### Spike question

> Can Agent Bus delegate execution to Orca while retaining task ownership, routing, evidence, decisions, review state and audit history?

### Success criteria

A task must:

1. originate in Agent Bus,
2. launch through Orca,
3. expose runtime status,
4. return artifacts/evidence,
5. enter Agent Bus Gatekeeper review,
6. preserve Agent Bus as source of truth,
7. support cancellation,
8. leave no Orca-specific object in core models.

### Kill criterion

Do not adopt Orca runtime integration if it requires Agent Bus to surrender task state, workflow semantics or review ownership.

---

## OpenHands

### What to study

- sandbox abstraction,
- remote execution,
- long-running agent sessions,
- environment lifecycle,
- tooling isolation.

### Decision

- Sandbox/runtime backend: **SPIKE**
- Remote workspace ideas: **ADAPT**
- Rebuild comparable sandbox infrastructure ourselves: **REJECT**
- Replace Agent Bus workflow/governance layer: **REJECT**

### Key lesson

Agent Bus should avoid becoming a VM/container orchestration platform when a backend can provide that responsibility.

---

## Pact

### What to study

- parallel execution in isolated Git worktrees,
- task fan-out,
- result collection,
- merge-oriented workflow.

### Decision

- Parallel worktree pattern: **ADAPT**
- Treat worktree as universal execution model: **REJECT**
- Merge orchestration lessons: **ADAPT**

### Key lesson

Parallelism is useful, but worktrees should remain a workspace backend, not an architectural assumption.

---

## Hydra

### What to study

- routing between heterogeneous coding agents,
- multi-model deliberation,
- assigning different roles to different models.

### Decision

- Capability-oriented routing: **ADAPT**
- Deterministic routing first: **ADOPT**
- LLM-based opaque routing in V2 Alpha: **REJECT**
- Multi-agent deliberation as optional workflow pattern: **SPIKE**

### Key lesson

Routing should initially be explainable and policy-driven. Model-assisted routing may be added later as a recommendation layer, not as the source of truth.

---

## Orka

### What to study

- implementation -> review -> security review -> merge pipelines,
- independent review roles,
- merge policies,
- explicit approval gates.

### Decision

- Independent review roles: **ADAPT**
- Policy-driven review chains: **ADAPT**
- Replace current Gatekeeper: **REJECT**
- Add specialized reviewers behind Gatekeeper policy: **ADOPT**

### Key lesson

Agent Bus already has a strong integration invariant. Extend the current Gatekeeper rather than replacing it.

---

## multiagents

### What to study

- MCP-based inter-agent communication,
- agent discovery,
- cross-client messaging,
- heterogeneous participants.

### Decision

- MCP as interoperability layer: **ADOPT**
- Direct agent-to-agent communication patterns: **ADAPT**
- Coordination that depends only on prompt compliance: **REJECT**

### Key lesson

Agent communication should remain durable and contract-driven through the bus.

---

## Comparative Matrix

| Area | Agent Bus direction | External reference | Decision |
|---|---|---|---|
| Durable state | Keep in Agent Bus | Orca | ADOPT concept, keep ownership |
| Agent execution | Adapter-based | Orca/OpenHands | SPIKE |
| Worktrees | Workspace backend | Orca/Pact | ADAPT |
| Remote sandbox | Delegate | OpenHands | SPIKE |
| Routing | Capability-based | Hydra | ADAPT |
| Routing policy | Deterministic first | Hydra | ADOPT |
| Inter-agent messaging | Durable MCP contracts | multiagents | ADAPT |
| Review gates | Extend Gatekeeper | Orka | ADAPT |
| Security review | Optional specialized gate | Orka | ADOPT |
| Planner | Provider-neutral interface | internal + model backends | ADAPT |
| Workflow state | Agent Bus source of truth | all | ADOPT |
| Usage/cost | Agent Bus ledger | runtime-reported | ADAPT |

---

## Planner Evaluation

Hermes must not be the fixed planner of the system.

Target layers:

```text
PlanningRequest
      |
      v
   Planner
      |
      +---- HermesPlanner
      +---- StaticPlanner
      +---- WorkflowPlanner
      +---- Future planner
      |
      v
PlanningBackend
      |
      +---- OpenAI-compatible
      +---- Kimi
      +---- Anthropic
      +---- GLM
      +---- Local
      +---- Fake
```

Two distinct concepts must remain separate:

### Planner

Defines **how an objective becomes a plan**.

Examples:

- LLM task decomposition,
- deterministic workflow template,
- hybrid template + LLM filling,
- human-authored DAG.

### PlanningBackend

Defines **which inference system the planner uses**.

Hermes may continue to exist as a planner strategy, but it is not a system singleton and it must not be embedded in core.

### Required spike

Run the same planning request through at least:

1. Hermes with Backend A,
2. Hermes with Backend B,
3. a deterministic/static planner.

All outputs must converge to the same validated `TaskPlan` contract.

---

## Runtime Evaluation

The runtime layer must be tested before expanding the current native worker implementation.

Candidate runtime categories:

```text
AgentRuntime
├── NativeRuntime
├── ExternalCommandRuntime
├── OrcaRuntime
├── OpenHandsRuntime
└── ExternalMcpRuntime
```

### Runtime scorecard

Each spike should record:

- startup latency,
- cancellation support,
- resumability,
- status visibility,
- worktree/sandbox ownership,
- artifact capture,
- usage reporting,
- authentication complexity,
- failure recovery,
- provider portability,
- code maintenance required in Agent Bus.

Do not select a runtime only because it runs more agents. Select it if it reduces Agent Bus maintenance while preserving governance.

---

## Build vs Adopt Rules

### Build in Agent Bus when

The feature primarily:

- coordinates,
- routes,
- governs,
- records,
- verifies,
- preserves state,
- expresses policy.

### Adopt behind an adapter when

The feature primarily:

- launches a coding CLI,
- manages model sessions,
- provisions a sandbox,
- executes remote commands,
- provides provider-specific authentication,
- owns terminal/process mechanics.

### Reject for now when

The feature:

- duplicates mature external infrastructure,
- introduces opaque routing before deterministic routing exists,
- requires replacing SQLite without demonstrated need,
- introduces provider-specific types into core,
- weakens auditability.

---

## Phase -1 Deliverables

Before Phase 1 implementation:

1. complete Orca runtime spike,
2. complete planner abstraction spike,
3. document OpenHands integration feasibility,
4. record decisions in ADRs,
5. update the roadmap based on evidence,
6. decide the minimum scope of `NativeRuntime`,
7. identify code that can be deleted or frozen if an external runtime is adopted.

---

## Exit Criteria

Phase -1 is complete when the team can answer, with evidence:

- Should Orca be supported as a first-class runtime adapter?
- Which execution responsibilities remain native?
- Is OpenHands useful now, later, or not at all?
- Can Hermes use multiple inference backends?
- Can a non-Hermes planner produce the same `TaskPlan`?
- Which patterns from Pact, Hydra, Orka and multiagents will be incorporated?
- Which patterns are explicitly rejected?
- What code should **not** be written in Phase 1?
