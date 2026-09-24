import asyncio

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


async def _bus(tmp_path, name):
    db = Database(str(tmp_path / name))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db))
    return db, bus


@pytest.mark.asyncio
async def test_revoked_capability_cannot_claim(tmp_path):
    db, bus = await _bus(tmp_path, "bus.db")
    await bus.registry.register(AgentInfo(agent_id="codex-01", display_name="Codex"))
    transport = ASGITransport(app=bus.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/tasks", json={"task_id": "T1", "title": "T1"})
        await client.post("/tasks/T1/requirements", json={"requires": ["python"]})
        await client.post("/agents/codex-01/route-profile", json={"declared": ["python"], "approved": ["python"]})
        assert (await client.post("/tasks/T1/claim", json={"agent_id": "codex-01"})).status_code == 200
    await db.close()

    db, bus = await _bus(tmp_path, "revoked.db")
    await bus.registry.register(AgentInfo(agent_id="codex-01", display_name="Codex"))
    transport = ASGITransport(app=bus.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/tasks", json={"task_id": "T1", "title": "T1"})
        await client.post("/tasks/T1/requirements", json={"requires": ["python"]})
        await client.post("/agents/codex-01/route-profile", json={"declared": ["python"], "approved": ["python"]})
        await client.post("/agents/codex-01/route-profile", json={"declared": ["python"], "approved": []})
        denied = await client.post("/tasks/T1/claim", json={"agent_id": "codex-01"})
        assert denied.status_code == 409
    await db.close()


@pytest.mark.asyncio
async def test_concurrent_claims_leave_one_owner(tmp_path):
    db, bus = await _bus(tmp_path, "bus.db")
    transport = ASGITransport(app=bus.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/tasks", json={"task_id": "T1", "title": "T1"})

        async def claim(agent_id: str) -> int:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as claimer:
                response = await claimer.post("/tasks/T1/claim", json={"agent_id": agent_id})
                return response.status_code

        codes = await asyncio.gather(claim("alice"), claim("bob"))
        stored = (await client.get("/tasks/T1")).json()
    assert sorted(codes) == [200, 409]
    assert stored["owner"] in {"alice", "bob"}
    assert stored["status"] == "in_progress"
    await db.close()


@pytest.mark.asyncio
async def test_route_stays_inside_its_project(tmp_path):
    first_db, first = await _bus(tmp_path, "one.db")
    second_db, second = await _bus(tmp_path, "two.db")
    await first.registry.register(AgentInfo(agent_id="codex-01", display_name="Codex"))
    async with httpx.AsyncClient(transport=ASGITransport(app=first.app), base_url="http://test") as client:
        await client.post("/tasks", json={"task_id": "SAME", "title": "one"})
        await client.post("/tasks/SAME/requirements", json={"requires": ["python"]})
        await client.post("/agents/codex-01/route-profile", json={"declared": ["python"], "approved": ["python"]})
        selected = (await client.post("/tasks/SAME/route")).json()["selected_agent"]
    async with httpx.AsyncClient(transport=ASGITransport(app=second.app), base_url="http://test") as client:
        await client.post("/tasks", json={"task_id": "SAME", "title": "two"})
        await client.post("/tasks/SAME/requirements", json={"requires": ["python"]})
        other = (await client.post("/tasks/SAME/route")).json()
    assert selected == "codex-01"
    assert other["selected_agent"] is None
    await first_db.close()
    await second_db.close()
