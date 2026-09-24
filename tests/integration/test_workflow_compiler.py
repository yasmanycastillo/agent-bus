import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.workflows import WorkflowError, load_workflow


@pytest.mark.asyncio
async def test_feature_development_compiles_to_the_bus_and_rejects_advisory(tmp_path):
    with pytest.raises(WorkflowError, match="advisory"):
        load_workflow("", name="feature-development", advisory=True)
    with pytest.raises(WorkflowError, match="depends on"):
        load_workflow("""
workflow: cycle
version: 1
steps:
  - id: a
    depends_on: [b]
  - id: b
    depends_on: [a]
""")

    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        refused = await client.post("/workflows/compile", json={"name": "feature-development", "instance_id": "run-1", "advisory": True})
        assert refused.status_code == 422
        compiled = await client.post("/workflows/compile", json={"name": "feature-development", "instance_id": "run-1"})
        assert compiled.status_code == 200
        tasks = {item["task_id"]: item for item in compiled.json()["tasks"]}
        assert set(tasks) == {
            "feature-development-run-1-discovery",
            "feature-development-run-1-design",
            "feature-development-run-1-implementation",
            "feature-development-run-1-review",
            "feature-development-run-1-integration",
        }
        discovery = tasks["feature-development-run-1-discovery"]
        assert discovery["requirements"] == ["repository-analysis", "long-context"]
        implementation = tasks["feature-development-run-1-implementation"]
        assert implementation["depends_on"] == ["feature-development-run-1-design"]
        policy = await client.get("/tasks/feature-development-run-1-integration/evidence-policy")
        assert policy.status_code == 200
        assert policy.json()["strict"] is True
        assert policy.json()["require_review"] is True
    await db.close()
