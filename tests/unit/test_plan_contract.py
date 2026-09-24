from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from agent_bus.orchestrator.config import OrchestratorConfig
from agent_bus.orchestrator.client import InferenceClient
from agent_bus.orchestrator.hermes import HermesOrchestrator
from agent_bus.orchestrator.schema import PlanValidationError, validate_breakdown_dict
from agent_bus.orchestrator.static import StaticPlanner


TASKS = [{
    "task_id": "inspect",
    "title": "Inspect module",
    "acceptance_criteria": ["Map imports"],
    "depends_on": [],
}]


def _data() -> dict:
    return {"objective": "Document a module", "tasks": TASKS}


def test_missing_plan_version_defaults_to_v1():
    plan = validate_breakdown_dict(_data())
    assert plan.plan_version == "1"


def test_reject_unknown_plan_version():
    with pytest.raises(PlanValidationError):
        validate_breakdown_dict({**_data(), "plan_version": "2"})


async def _hermes_plan(model: str) -> dict:
    payload = {"choices": [{"message": {"content": json.dumps(_data())}}]}

    def respond(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == model
        return httpx.Response(200, json=payload)

    config = OrchestratorConfig(provider="ollama", model=model, base_url=f"https://{model}.invalid/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = InferenceClient(config=config, http_client=http)
        result = await HermesOrchestrator(config=config, client=client).breakdown_objective("Document a module")
    assert result.success and result.plan is not None
    return result.plan.model_dump(mode="json")


def test_static_planner_matches_mock_hermes_contract():
    first, second = asyncio.run(_hermes_plan("backend-a")), asyncio.run(_hermes_plan("backend-b"))
    static = StaticPlanner(TASKS).breakdown("Document a module").model_dump(mode="json")
    assert first == second == static
    assert static["plan_version"] == "1"


@pytest.mark.asyncio
async def test_static_plan_publishes_on_the_bus(live_bus_url: str):
    plan = StaticPlanner(TASKS).breakdown("Document a module", operation_key="plan-v1-static")
    config = OrchestratorConfig(provider="ollama", model="unused", base_url="https://unused.invalid/v1", api_key="unused")
    publisher = HermesOrchestrator(config=config, client=InferenceClient(config=config, http_client=httpx.AsyncClient()))
    async with httpx.AsyncClient(base_url=live_bus_url, trust_env=False) as client:
        created = await publisher.publish_breakdown(plan, client)
        again = await publisher.publish_breakdown(plan, client)
        stored = (await client.get("/tasks/inspect")).json()
    assert created[0]["task_id"] == again[0]["task_id"] == "inspect"
    assert stored["title"] == "Inspect module"
    assert stored["status"] == "pending"


def _live_backend(prefix: str) -> tuple[str, str]:
    url = os.environ.get(f"HERMES_LIVE_{prefix}_URL")
    model = os.environ.get(f"HERMES_LIVE_{prefix}_MODEL")
    if not url or not model:
        pytest.skip(f"HERMES_LIVE_{prefix}_URL and HERMES_LIVE_{prefix}_MODEL are required")
    return url, model


@pytest.mark.asyncio
async def test_live_backends_publish_plan_version_1(live_bus_url: str, allowed_test_ports: set[int]):
    """Two real inference servers must validate and publish contract version 1."""
    backends = (_live_backend("A"), _live_backend("B"))
    for url, _model in backends:
        allowed_test_ports.add(httpx.URL(url).port)
    objective = "Document a module"
    failures: list[str] = []
    async with httpx.AsyncClient(base_url=live_bus_url, trust_env=False) as bus:
        for index, (url, model) in enumerate(backends):
            config = OrchestratorConfig(
                provider="ollama",
                model=model,
                base_url=url,
                timeout=300,
                max_tokens=4096,
                temperature=0,
            )
            async with httpx.AsyncClient(timeout=300, trust_env=False) as http:
                client = InferenceClient(config=config, http_client=http)
                result = await HermesOrchestrator(config=config, client=client).breakdown_objective(
                    objective, operation_key=f"live-plan-{index}"
                )
            if not result.success or result.plan is None:
                reason = result.failure.reason if result.failure else "no plan"
                failures.append(f"{model}: {reason}")
                continue
            if result.plan.plan_version != "1" or not result.plan.tasks:
                failures.append(f"{model}: plan_version={result.plan.plan_version} tasks={len(result.plan.tasks)}")
                continue
            publisher = HermesOrchestrator(config=config, client=client)
            try:
                created = await publisher.publish_breakdown(result.plan, bus)
            except RuntimeError as exc:
                failures.append(f"{model}: {exc}")
                continue
            stored = (await bus.get(f"/tasks/{created[0]['task_id']}")).json()
            if stored["task_id"] != created[0]["task_id"]:
                failures.append(f"{model}: published task was not stored")
    assert not failures, "\n".join(failures)
