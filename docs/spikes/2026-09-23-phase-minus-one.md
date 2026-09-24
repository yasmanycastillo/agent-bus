# V2 phase -1 evidence, 2026-09-23

Phase -1 is **open**. This record separates source preflight from executable
integration and mocked contract checks from live provider checks.

## Orca source preflight

- Candidate: [orca-cli/orca at commit 5beeefc](https://github.com/orca-cli/orca/tree/5beeefcb57555962bb93facc54b5f82484731802)
  (2026-05-08).
- Why this candidate: its README describes the single Go binary, SQLite state,
  Git worktrees and MCP surface named in the V2 proposal.
- Reproduction (full clone, pinned revision; stop if the checkout differs):

  ```sh
  gh repo clone orca-cli/orca /tmp/agent-bus-orca-spike
  git -C /tmp/agent-bus-orca-spike checkout --detach 5beeefcb57555962bb93facc54b5f82484731802
  test "$(git -C /tmp/agent-bus-orca-spike rev-parse HEAD)" = 5beeefcb57555962bb93facc54b5f82484731802 || exit 1
  git -C /tmp/agent-bus-orca-spike ls-tree -r --name-only HEAD internal/cli internal/mcp internal/runner
  rg -n 'rootCmd\.AddCommand|&cobra\.Command|Use:' /tmp/agent-bus-orca-spike/internal
  ```
- Observed: the CLI registers only its root and `version` command;
  `internal/mcp` and `internal/runner` contain only `doc.go`. The README lists
  `orca run`, `orca kill` and `orca mcp serve`, but this commit has no command
  implementations for them. The GitHub repository had no published release
  returned by `gh api repos/orca-cli/orca/releases` at the time of review.
- Decision: **REJECT this revision as the first runtime adapter**. It cannot
  launch or cancel an Agent Bus task, so no lifecycle, recovery or governance
  integration test was possible. This says nothing about other projects named
  Orca or a future release of this repository. Use a generic external-command
  probe for the first executable runtime comparison.

## Planner contract probe

The existing `HermesOrchestrator` accepted two differently configured
`InferenceClient` instances backed by separate `httpx.MockTransport` endpoints.
Both returned the same valid JSON. A static path passed the same data directly
to `validate_breakdown_dict`. All three yielded equal `TaskBreakdownPlan`
objects with one task. No paid model, live provider or task publisher was used.

Reproduce from this checkout with `uv run python -` and the following input:

```python
import asyncio
import json
import httpx
from agent_bus.orchestrator.client import InferenceClient
from agent_bus.orchestrator.config import OrchestratorConfig
from agent_bus.orchestrator.hermes import HermesOrchestrator
from agent_bus.orchestrator.schema import validate_breakdown_dict

data = {"objective": "Document a module", "tasks": [
    {"task_id": "inspect", "title": "Inspect module",
     "acceptance_criteria": ["Map imports"], "depends_on": []}
]}
payload = {"choices": [{"message": {"content": json.dumps(data)}}]}
calls = []

def respond(request):
    calls.append(json.loads(request.content)["model"])
    return httpx.Response(200, json=payload)

async def main():
    plans = []
    for provider, model in (("openai", "backend-a"), ("ollama", "backend-b")):
        config = OrchestratorConfig(provider=provider, model=model,
                                    base_url=f"https://{model}.invalid/v1")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = InferenceClient(config=config, http_client=http)
            result = await HermesOrchestrator(config=config, client=client).breakdown_objective(
                "Document a module")
            assert result.success and result.plan is not None
            plans.append(result.plan.model_dump(mode="json"))
    plans.append(validate_breakdown_dict(data).model_dump(mode="json"))
    assert plans[0] == plans[1] == plans[2]
    assert calls == ["backend-a", "backend-b"]
    print("PASS: two mock HTTP backends and one static path share TaskBreakdownPlan")

asyncio.run(main())
```

Result: **PASS for existing schema compatibility only**. Two live inference
backends remain unproven.

## Plan contract version 1

`TaskBreakdownPlan.plan_version` is `"1"`. Payloads that omit it stay valid and
default to version 1. Any other version fails validation. `StaticPlanner` has
no inference client. Two mock Hermes backends and that static planner produce
equal plans, and the static plan publishes through `publish_breakdown` with an
idempotent `operation_key`.

Command:

```sh
uv run pytest tests/unit/test_plan_contract.py tests/unit/test_orchestrator.py::test_schema_valid_breakdown -q
```

Result: **PASS for contract version 1, static planner, and bus publication**.

Live backends, measured 2026-09-24 with both servers on CPU because Docker
reported `no known GPU vendor found`:

```sh
HERMES_LIVE_A_URL=http://127.0.0.1:11434/v1 HERMES_LIVE_A_MODEL=qwen2.5:1.5b \
HERMES_LIVE_B_URL=http://127.0.0.1:11435/v1 HERMES_LIVE_B_MODEL=smollm2:135m \
uv run pytest tests/unit/test_plan_contract.py::test_live_backends_publish_plan_version_1 -q
```

Result: **FAIL**.

- `qwen2.5:1.5b` returned malformed JSON (`Unterminated string` around line 423).
- `smollm2:135m` copied the prompt placeholders. The bus rejected the batch:
  task `<unique-kebab-case-id>` depends on `<prerequisite-task-id>`.

The same test skips when those four variables are unset, so the mock contract
checks stay green. These two small CPU models do not establish interchangeable
live planners.

A later pair did. On 2026-09-24 Claude provisioned two vLLM servers on one
Runpod H100 80GB (`k5wdcmxdszmb4t`, image `vllm/vllm-openai:latest`). A local
forwarder published them on the loopback ports the test allows. Grok reran the
same test against those servers and observed `1 passed in 8.89s`.

```sh
HERMES_LIVE_A_URL=http://127.0.0.1:11434/v1 \
HERMES_LIVE_A_MODEL=Qwen/Qwen2.5-7B-Instruct-AWQ \
HERMES_LIVE_B_URL=http://127.0.0.1:11435/v1 \
HERMES_LIVE_B_MODEL=Qwen/Qwen2.5-Coder-7B-Instruct-AWQ \
uv run pytest tests/unit/test_plan_contract.py::test_live_backends_publish_plan_version_1 -q
```

Both models reported `max_model_len` 16384. The public proxies were
`https://k5wdcmxdszmb4t-8000.proxy.runpod.net/v1` and
`https://k5wdcmxdszmb4t-8001.proxy.runpod.net/v1`, without authentication.
The pod billed $3.49/h and is not required for the remaining OpenHands gate,
so it should be stopped after this record. The two plans were valid version 1
publications. They were not byte-identical, and this run does not make Hermes
the only planner.

## Generic external-command probe

The probe lives in `tests/spikes/external_command_probe.py`. It is a disposable
measurement, not a runtime adapter and not `NativeRuntime`. Attempt state is a
bus message. The child process environment drops `AGENT_BUS*` variables.

Command:

```sh
uv run pytest tests/spikes/test_external_command_probe.py -q
```

Observed on 2026-09-23:

| Gate | Result |
|---|---|
| Hub restart does not execute a completed attempt again | **PASS** |
| Timeout stays `unknown` and blocks another launch until reconcile | **PASS** |
| Cancel ends in one `cancelled` state and the process is dead | **PASS** |
| Candidate SHA is the Gatekeeper input; the command does not move `main` or mark the task done | **PASS** |
| Unsigned `POST /tasks/{id}/done` is refused | **PASS** after the actor requirement |

The command itself has no bus credential. A later change requires `agent_id`
for unsigned `done`, `review` and `block`. An empty `POST /done` returns 422.
A stranger receives 409, and the task stays not done. Authenticated non-owner
completion remains covered by `tests/integration/test_authorization.py`.
Maintenance cost of this probe is one test module. It does not replace the
worker. Orca at `5beeefc` remains rejected and was not given this role.

## OpenHands source feasibility

The [OpenHands Software Agent SDK at commit 5b36cac](https://github.com/OpenHands/software-agent-sdk/tree/5b36cacccc2bbe6f8fbce9e1d3ff4b0a3dcddadb)
is release v1.49.5. It exposes an Agent Server API and separate workspace
implementations. A local SDK conversation is not this measurement.

## OpenHands Docker workspace, 2026-09-24

Package `openhands-workspace==1.49.5` and image
`ghcr.io/openhands/agent-server:1.49.5-python`. No LLM call. The probe lived
in `/tmp` and was not added to the repository.

| Gate | Result |
|---|---|
| Container start and health | **PASS** |
| Command execution | **PASS** (`echo hello-from-sandbox`) |
| Client-side command timeout | **PASS** (`sleep 30` returned exit `-1` within the 2s timeout) |
| File upload and download | **PASS** |
| `docker pause` / `unpause` keeps the file | **PASS** |
| A new container after `docker stop` | **PASS** as isolation; the marker is gone |
| Credentials for this local workspace | **PASS**; none were required |
| Kubernetes `AgentSandboxWorkspace` | **UNKNOWN**; this host has no kubeconfig or warm pool |
| `OpenHandsCloudWorkspace` with no arguments | **FAIL** closed; validation requires `cloud_api_url` and `cloud_api_key` before any request |
| `APIRemoteWorkspace` with no arguments | **FAIL** closed; validation requires `runtime_api_url`, `runtime_api_key` and `server_image` |

The image logs a warning when it binds `0.0.0.0` without `SESSION_API_KEY`.
`OH_SECRET_KEY` was unset, so the server said secrets do not survive its own
restart. This run does not adopt an adapter. Agent Bus would still own the
task, the review, and the candidate SHA. The Docker workspace is a later
adapter candidate. The Kubernetes and cloud workspaces stay unmeasured.

## Remaining exit gates

1. The version 1 plan has now been published by two live 7B AWQ backends on
   2026-09-24. The earlier `qwen2.5:1.5b` and `smollm2:135m` CPU run remains a
   recorded failure. Mock backends and the static planner also pass.
2. Keep the external-command probe disposable. Do not promote it to an adapter
   before the runtime contract exists. Unsigned `done`, `review` and `block`
   now require `agent_id`, and that agent must own the task.
3. The local Docker workspace is measured above. Cloud and remote API
   workspaces fail closed without credentials. The Kubernetes sandbox remains
   unknown. Pact `demo`, Hydra `init`, multiagents `status` and the Orka CLI
   `version`/`status` were run on 2026-09-24. No Orka task was submitted.

Until those gates pass, Phase 0 and Phase 1 remain proposed work rather than
validated follow-on implementation.
