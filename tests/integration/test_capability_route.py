import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.types import AgentInfo


@pytest.mark.asyncio
async def test_route_selects_approved_agent_and_claim_cannot_bypass(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    registry = AgentRegistry()
    bus = MessageBus(db, registry, InboxManager(db))
    await registry.register(AgentInfo(agent_id="codex-01", display_name="Codex", capabilities=["implementation", "python"]))
    await registry.register(AgentInfo(agent_id="reader-01", display_name="Reader", capabilities=["python"]))
    transport = ASGITransport(app=bus.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/tasks", json={"task_id": "implement", "title": "Implement"})
        assert created.status_code == 200
        requirements = await client.post("/tasks/implement/requirements", json={"requires": ["implementation", "python"]})
        assert requirements.json()["requirements"] == ["implementation", "python"]
        for agent_id, approved, priority, can_edit in (
            ("codex-01", ["implementation", "python"], 1, True),
            ("reader-01", ["python"], 9, False),
        ):
            profile = await client.post(f"/agents/{agent_id}/route-profile", json={
                "declared": ["implementation", "python"],
                "approved": approved,
                "priority": priority,
                "cost_class": "low",
                "can_edit": can_edit,
            })
            assert profile.status_code == 200
        routed = await client.post("/tasks/implement/route")
        body = routed.json()
        assert body["selected_agent"] == "codex-01"
        assert body["eligible"] == ["codex-01"]
        denied = await client.post("/tasks/implement/claim", json={"agent_id": "reader-01"})
        assert denied.status_code == 409
        claimed = await client.post("/tasks/implement/claim", json={"agent_id": "codex-01"})
        assert claimed.status_code == 200
        assert claimed.json()["owner"] == "codex-01"
    await db.close()
