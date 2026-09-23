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

Result: **PASS for existing schema compatibility only**. A `Planner`
interface, two live inference backends, contract versioning and publication
through the same bus path remain unproven.

## OpenHands source feasibility

The [OpenHands Software Agent SDK at commit 5b36cac](https://github.com/OpenHands/software-agent-sdk/tree/5b36cacccc2bbe6f8fbce9e1d3ff4b0a3dcddadb) exposes an Agent Server
API for conversations/events and separate remote workspace implementations.
These are plausible adapter surfaces, but this review did not launch a sandbox
or measure lifecycle, recovery, artifact export or credential requirements.
Decision: **DEFER runtime adoption**; keep Agent Server and workspace choices
separate in a later executable spike.

## Remaining exit gates

1. Define a versioned plan contract compatible with `TaskBreakdownPlan` and
   run the same request through two actual inference backends plus a static
   planner, including publication to Agent Bus.
2. Run a generic external-command attempt with bus-owned task state, restart
   reconciliation, cancellation, immutable candidate and Gatekeeper review.
3. Record measured OpenHands sandbox feasibility and the remaining pattern
   decisions with source revisions and the [adoption scorecard](../../LANDSCAPE_AND_ADOPTION.md).

Until those gates pass, Phase 0 and Phase 1 remain proposed work rather than
validated follow-on implementation.
