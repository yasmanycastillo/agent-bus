# Landscape and Adoption Review

## Purpose

Before implementing Agent Bus V2, evaluate adjacent multi-agent coding systems and explicitly decide what to adopt, adapt, test, or reject.

This document is a decision aid, not a feature wishlist.

As of 2026-09-23, the labels below are **provisional pattern decisions** based
on source review. They do not mean an integration has passed a spike. Every
runtime adoption decision must include a tested release/commit, commands,
results, limitations and a dated ADR. Recheck the license and API surface at
the exact revision tested.

Decision labels:

- **ADOPT**: use the pattern directly where compatible.
- **ADAPT**: keep the idea but implement it in Agent Bus terms.
- **SPIKE**: build a bounded proof of concept before deciding.
- **REJECT**: intentionally do not pursue the pattern now.

## Reference projects

| Name used here | Primary source | Surface to assess |
|---|---|---|
| Orca | [orca-cli/orca](https://github.com/orca-cli/orca) | Advertised CLI/MCP lifecycle; first source preflight failed |
| OpenHands | [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk) | Agent Server API and workspace/sandbox implementations separately |
| Pact | [zekariasasaminew/pact](https://github.com/zekariasasaminew/pact) | Worktree and merge workflow |
| Hydra | [krowxx/hydra](https://github.com/krowxx/hydra) | Heuristic routing and deliberation |
| Orka | [orka-agents/orka](https://github.com/orka-agents/orka) | Kubernetes task, review and security workflows |
| multiagents | [zetbrush/multiagents](https://github.com/zetbrush/multiagents) | MCP peer discovery and messaging |

The generic names Orca, Hydra and Orka identify multiple unrelated projects;
spike reports must name the exact repository and revision. A pattern listed as
ADOPT below still needs a local compatibility test before code is adopted.

---

## Evaluation Criteria

Each external pattern should be judged against:

1. Does it reduce code Agent Bus must maintain?
2. Does it preserve Agent Bus as the durable source of truth?
3. Does it keep providers/runtimes replaceable?
4. Does it improve observability or safety?
5. Can it be tested without paid model access?
6. Does it avoid leaking provider-specific concepts into core?
7. Does it preserve the reviewed candidate SHA as the merge input, verify its
   parentage and target baseline, and require approval when review is strict?
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

- External execution backend at commit `5beeefc`: **REJECT for now**; the
  command preflight failed ([evidence](docs/spikes/2026-09-23-phase-minus-one.md))
- Reuse as primary runtime at that revision: **REJECT for now**
- Replace Agent Bus durable task/message layer: **REJECT**
- Copy Orca-specific concepts into core domain objects: **REJECT**
- Learn from operational simplicity: **ADOPT**

### Spike question

> Can Agent Bus delegate execution to Orca while retaining task ownership, routing, evidence, decisions, review state and audit history?

The Orca README describes its own run state, worktrees, DAG and review flows.
If a future revision implements these, the probe must use it only as an
executor and record which side owns each transition. A generic
external-command runtime is the first executable probe; any later Orca
adapter would be compared against it.

At the tested revision, only `orca version` is registered. The advertised
launch/MCP/cancel commands are absent, so the executable governance spike
cannot run. A future revision or a different Orca project requires a new
preflight; this finding does not evaluate those candidates.

### Success criteria for a future Orca revision

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

Evaluate Agent Server and the selected workspace implementation as separate
surfaces. A successful local SDK conversation is not evidence that remote
sandbox provisioning, recovery or artifact export works.

On 2026-09-24 the Docker workspace at SDK `5b36cac` / package `1.49.5` started
`ghcr.io/openhands/agent-server:1.49.5-python`, ran a command, transferred a
file, survived `docker pause`, and lost that file in a new container. No API
key was required, and the server warned that it was listening on `0.0.0.0`
without `SESSION_API_KEY`. `AgentSandboxWorkspace` was not run: this host has
no kubeconfig. That measurement does not adopt an adapter.

---

## Pact

Cited source: [zekariasasaminew/pact](https://github.com/zekariasasaminew/pact) `0b3d882b79ea7fa0a8be14b6ae44820f51c6852d` (2026-08-31, MIT). The README describes per-agent git worktrees and a risk-sequenced `merge-all`. File claims are advisory. On 2026-09-24 `cargo run -p pact-cli --bin pact -- --help` produced pact 0.5.0, and `pact demo` merged two simulated workspaces onto a disposable branch with no agent CLI and no model call. That confirms the worktree loop. It does not adopt Pact.

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

Cited source: [krowxx/hydra](https://github.com/krowxx/hydra) `c4377f499ad6d6b41a51e49824e96fe24140b595` (2026-03-07, MIT, branch `master`). The README describes heuristic routing across Claude, Gemini and Codex, plus optional multi-round deliberation. The README badge names PrimeLocus/Hydra; this citation is the `krowxx/hydra` repository. On 2026-09-24 `node bin/hydra-cli.mjs --help` listed setup, init and prompt modes. After `npm ci`, `hydra init` wrote `HYDRA.md` and synced `CLAUDE.md`, `GEMINI.md` and `AGENTS.md` in a throwaway directory. The daemon and any model route were not started.

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

Cited source: [orka-agents/orka](https://github.com/orka-agents/orka) `80bfc20b17c68e82a8a763881d4b0ff65f7baa2a` (2026-09-24, MIT). The README describes Kubernetes tasks for model calls, coding agents and commands, and says the project is experimental. The CLI source at `cmd/cli` talks to a server and a kubeconfig. This host has no Go toolchain and no kubeconfig, so the binary was not executed. The published `v0.2.0` release is older than this commit and was not substituted for it.

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

Cited source: [zetbrush/multiagents](https://github.com/zetbrush/multiagents) `03fcf6e875804a21650ed4e45eb9b6624460f7fb` (2026-04-26, release v0.5.0, no SPDX license in the repository metadata). The README describes MCP peer discovery, a local broker, and review loops. Node cannot run the CLI because it uses TypeScript parameter properties. On 2026-09-24 `bun ./cli.ts --help` listed setup, session, broker and MCP commands, and `bun ./cli.ts status` printed `Broker is not running.` The broker was not started and no MCP client was configured.

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
| Durable state | Keep in Agent Bus | Orca | Existing core; compare recovery patterns |
| Agent execution | Adapter-based | Generic command/OpenHands | SPIKE; tested Orca revision rejected |
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
      +---- HermesPlanner ----> PlanningBackend
      |                            +---- OpenAI-compatible
      |                            +---- Anthropic / Kimi / GLM
      |                            +---- Local / Fake
      +---- StaticPlanner (no inference backend)
      +---- WorkflowPlanner (no inference backend required)
      +---- Future planner
      |
      v
Validated TaskPlan
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

The current implementation calls this contract `TaskBreakdownPlan`. The spike
must define versioning and compatibility before claiming interchangeability.

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

Also record the tested revision, exact commands and observed results for:

1. the same task surviving a hub restart without duplicate execution,
2. a timed-out launch reconciled before retry,
3. cancellation and cleanup with an unambiguous terminal state,
4. the candidate commit and evidence reaching Gatekeeper without the external
   runtime changing task or review authority,
5. a comparison with the generic external-command probe on maintenance cost.

Any loss of bus-owned task/review state, ambiguous retry that duplicates work,
or inability to bind the reviewed candidate to the runtime attempt fails the
runtime adoption gate. Latency and usage numbers inform the trade-off but do
not override those correctness gates.

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

## Phase -1 scorecard, 2026-09-23

Commands and raw gates are in [docs/spikes/2026-09-23-phase-minus-one.md](docs/spikes/2026-09-23-phase-minus-one.md).
`pytest` for the new checks: 9 passed.

| Gate | Revision / command | Result |
|---|---|---|
| Orca launch, status, cancel | `orca-cli/orca` `5beeefc`, pinned checkout | **FAIL** preflight; not the first adapter |
| External command, restart without duplicate execution | `tests/spikes/test_external_command_probe.py` | **PASS** |
| External command, timeout reconciled before retry | same | **PASS** |
| External command, cancellation terminal state | same | **PASS** |
| Candidate SHA reaches Gatekeeper; command does not merge or complete | same | **PASS** |
| Hub refuses completion by a non-owner | unsigned `POST /tasks/{id}/done` without `agent_id` returns 422; a stranger returns 409. Authenticated coverage remains `tests/integration/test_authorization.py` | **PASS** |
| Maintenance versus keeping the probe | one test module, no core runtime type | **PASS** as a spike; do not adopt it as an adapter yet |
| Plan contract version 1 and static planner | `tests/unit/test_plan_contract.py` | **PASS** |
| Two live inference backends | Runpod pod `k5wdcmxdszmb4t`, H100 80GB, `Qwen2.5-7B-Instruct-AWQ` and `Qwen2.5-Coder-7B-Instruct-AWQ`, 2026-09-24. Earlier CPU `qwen2.5:1.5b` / `smollm2:135m` run failed. | **PASS** |
| OpenHands sandbox lifecycle | SDK `5b36cac`, `DockerWorkspace` plus image `1.49.5-python` on local Docker, 2026-09-24 | **PASS** for local Docker; Kubernetes/cloud **unknown**; do not adopt an adapter yet |
| Pact local loop | `0b3d882`, `pact demo` | **PASS** for the simulated worktree merge; not adopted |
| Hydra CLI | `c4377f4`, `--help` and `init` | **PASS** for file generation; daemon and model routing not run |
| Orka CLI | `80bfc20`, source only | **UNKNOWN**; no Go toolchain and no kubeconfig |
| multiagents CLI | `03fcf6e`, Bun `--help` and `status` | **PASS** for the CLI; broker stayed down |

`NativeRuntime` remains the current worker and watch path. Nothing in the worker
is deleted or frozen. Phase 1 still must not add an Orca adapter, an OpenHands
adapter, or a second task owner inside a terminal UI.

## Phase -1 Deliverables

Before Phase 1 implementation:

1. record the Orca preflight rejection and complete an executable generic
   external-command runtime spike,
2. complete planner abstraction spike,
3. document OpenHands integration feasibility,
4. record decisions in ADRs,
5. update the roadmap based on evidence,
6. decide the minimum scope of `NativeRuntime`,
7. identify code that can be deleted or frozen if an external runtime is adopted.
8. publish a scorecard with source revision, commands, raw results and each
   correctness gate marked pass/fail/unknown.

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
