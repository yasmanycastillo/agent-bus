import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database


AGENTS = [
    {"agent_id": "hermes-01", "provider": "hermes"},
    {"agent_id": "claude-01", "provider": "claude"},
    {"agent_id": "codex-01", "provider": "codex"},
    {"agent_id": "agy-01", "provider": "agy"},
    {"agent_id": "grok-01", "provider": "grok"},
]


@pytest.mark.asyncio
async def test_coordinator_assigns_only_a_confirmed_instruction(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    text = "Corregir el cálculo del impuesto en la factura de venta."
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        refused = await client.post("/instructions", json={
            "agent_id": "coordinator", "instruction": text, "confirmed": False, "agents": AGENTS,
        })
        assert refused.status_code == 422
        assert "confirmed" in refused.json()["error"]
        saved = await client.post("/instructions", json={
            "agent_id": "coordinator", "instruction": text, "confirmed": True, "agents": AGENTS,
        })
        assert saved.status_code == 200, saved.text
        instruction_id = saved.json()["instruction_id"]
        unknown = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "stranger", "role": "implement", "title": "Calcular",
        })
        assert unknown.status_code == 409
        writing = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "claude-01", "role": "implement", "title": "Corregir el cálculo",
        })
        assert writing.status_code == 200, writing.text
        same = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "claude-01", "role": "review", "title": "Revisar el cálculo",
        })
        assert same.status_code == 409
        planning = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "hermes-01", "role": "plan", "title": "Plan del cálculo",
        })
        review = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "codex-01", "role": "review", "title": "Revisar el cálculo",
        })
        assert planning.status_code == 200, planning.text
        assert review.status_code == 200, review.text
        task = (await client.get(f"/tasks/{writing.json()['task_id']}")).json()
        assert task["owner"] == "claude-01"
        assert text in task["description"]
        assert "Corregir el cálculo" in task["description"]
        review_task = (await client.get(f"/tasks/{review.json()['task_id']}")).json()
        assert writing.json()["task_id"] in review_task["independent_from"]
        inbox = (await client.get("/inbox/claude-01/messages")).json()
        assert any(text in ((message.get("body") or {}).get("text") or "") for message in inbox["messages"])

        profile = await client.post("/agents/grok-01/route-profile", json={
            "declared": ["code-review"], "approved": ["code-review"], "can_edit": False,
        })
        assert profile.status_code == 200, profile.text
        free = await client.post("/tasks", json={
            "task_id": "adapt-ar-aging", "title": "aging", "description": "implementation work",
        })
        assert free.status_code == 200, free.text
        stolen = await client.post("/tasks/adapt-ar-aging/claim", json={"agent_id": "grok-01"})
        assert stolen.status_code == 409
        assert "reviewer" in stolen.json()["error"]
        released = await client.post(f"/tasks/{writing.json()['task_id']}/reassign", json={"new_owner": "free"})
        assert released.status_code == 200, released.text
        taken = await client.post(f"/tasks/{writing.json()['task_id']}/claim", json={"agent_id": "grok-01"})
        assert taken.status_code == 409
        assert "claude-01" in taken.json()["error"]
        listed = (await client.get("/agents/claude-01/assignments")).json()
        assert writing.json()["task_id"] in {row["task_id"] for row in listed["assignments"]}
    await db.close()
