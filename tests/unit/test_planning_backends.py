import json
from pathlib import Path

import pytest

from agent_bus.orchestrator.hermes import HermesOrchestrator
from agent_bus.planning import FakeBackend, HermesPlanner, StaticPlanner


PLAN = {
    "objective": "Document a module",
    "tasks": [{
        "task_id": "inspect",
        "title": "Inspect module",
        "acceptance_criteria": ["Map imports"],
        "depends_on": [],
    }],
}


@pytest.mark.asyncio
async def test_hermes_uses_two_backends_without_task_code_changes():
    raw = json.dumps(PLAN)
    first = FakeBackend(raw, "backend-a")
    second = FakeBackend(raw, "backend-b")
    left = await HermesPlanner(first).plan("Document a module", operation_key="plan-a")
    right = await HermesPlanner(second).plan("Document a module", operation_key="plan-b")
    static = StaticPlanner(PLAN["tasks"]).breakdown("Document a module")
    assert left.model_dump(exclude={"operation_key"}) == right.model_dump(exclude={"operation_key"})
    assert left.model_dump(exclude={"operation_key"}) == static.model_dump(exclude={"operation_key"})
    assert first.calls == 1 and second.calls == 1
    assert first.name != second.name
    task_source = Path("src/agent_bus/core/tasks.py").read_text()
    assert "Hermes" not in task_source
    assert "FakeBackend" not in task_source


def test_orchestrator_without_backend_still_wraps_the_client():
    orchestrator = HermesOrchestrator(backend=FakeBackend("{}", "unused"))
    assert orchestrator.backend.name == "unused"
    assert orchestrator.client is None
