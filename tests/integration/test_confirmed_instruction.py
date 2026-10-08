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
        assert "claude-01 implements part of this instruction" in same.json()["error"]
        assert "reviewer must not implement" in same.json()["error"]
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


async def _status(client, task_id):
    return (await client.get(f"/tasks/{task_id}")).json()["status"]


@pytest.mark.asyncio
async def test_review_waits_for_every_implementation_assigned_before_or_after(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        async def submit(text):
            saved = await client.post("/instructions", json={
                "agent_id": "coordinator", "instruction": text, "confirmed": True, "agents": AGENTS,
            })
            return saved.json()["instruction_id"]

        async def assign(instruction_id, agent_id, role):
            response = await client.post(f"/instructions/{instruction_id}/assignments", json={
                "agent_id": agent_id, "role": role, "title": f"{role} {agent_id}",
            })
            assert response.status_code == 200, response.text
            return response.json()["task_id"]

        instruction_id = await submit("Corregir impuestos")
        # Review assigned before any implementation: nothing to review yet.
        review = await assign(instruction_id, "codex-01", "review")
        assert await _status(client, review) == "blocked"
        plan = await assign(instruction_id, "hermes-01", "plan")
        assert (await client.post(f"/tasks/{plan}/done", json={"agent_id": "hermes-01"})).status_code == 200
        assert await _status(client, review) == "blocked"
        # The worker only executes its own in_progress or pending tasks.
        for status in ("in_progress", "pending"):
            owned = (await client.get("/tasks", params={"owner": "codex-01", "status": status})).json()
            assert review not in {task["task_id"] for task in owned}

        first = await assign(instruction_id, "claude-01", "implement")
        second = await assign(instruction_id, "grok-01", "implement")
        task = (await client.get(f"/tasks/{review}")).json()
        assert task["status"] == "blocked"
        assert set(task["depends_on"]) == {first, second}
        assert set(task["independent_from"]) == {first, second}

        assert (await client.post(f"/tasks/{first}/review", json={"agent_id": "claude-01"})).status_code == 200
        assert await _status(client, review) == "blocked"
        assert (await client.post(f"/tasks/{second}/done", json={"agent_id": "grok-01"})).status_code == 200
        assert await _status(client, review) == "in_progress"

        # A new implementation after the review started: it waits again.
        third = await assign(instruction_id, "agy-01", "implement")
        assert await _status(client, review) == "blocked"
        assert (await client.post(f"/tasks/{third}/review", json={"agent_id": "agy-01"})).status_code == 200
        assert await _status(client, review) == "in_progress"
        # Reviewer and implementer stay separate.
        refused = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "claude-01", "role": "review", "title": "self review",
        })
        assert refused.status_code == 409

        # Review assigned after its implementation is already in review starts at once.
        other = await submit("Otro encargo")
        written = await assign(other, "claude-01", "implement")
        assert (await client.post(f"/tasks/{written}/review", json={"agent_id": "claude-01"})).status_code == 200
        late = await assign(other, "codex-01", "review")
        assert await _status(client, late) == "in_progress"
    await db.close()


@pytest.mark.asyncio
async def test_cross_review_covers_only_the_named_implementations(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        saved = await client.post("/instructions", json={
            "agent_id": "coordinator", "instruction": "Backend y frontend", "confirmed": True, "agents": AGENTS,
        })
        instruction_id = saved.json()["instruction_id"]

        async def assign(agent_id, role, **extra):
            return await client.post(f"/instructions/{instruction_id}/assignments", json={
                "agent_id": agent_id, "role": role, "title": f"{role} {agent_id}", **extra,
            })

        backend = (await assign("codex-01", "implement")).json()["task_id"]
        frontend = (await assign("grok-01", "implement")).json()["task_id"]
        # Without a scope the review still covers every implementation.
        assert (await assign("codex-01", "review")).status_code == 409
        foreign = await assign("codex-01", "review", reviews=["ins-x-implement-y"])
        assert foreign.status_code == 422
        assert "ins-x-implement-y" in foreign.json()["error"]
        own = await assign("codex-01", "review", reviews=[backend])
        assert own.status_code == 409
        assert backend in own.json()["error"]
        assert (await assign("codex-01", "review", reviews=[])).status_code == 422

        crossed = await assign("codex-01", "review", reviews=[frontend])
        assert crossed.status_code == 200, crossed.text
        review = crossed.json()["task_id"]
        task = (await client.get(f"/tasks/{review}")).json()
        assert task["status"] == "blocked"
        assert task["independent_from"] == [frontend]
        assert task["depends_on"] == [frontend]
        assert (await assign("grok-01", "review", reviews=[backend])).status_code == 200

        # A scoped reviewer may implement elsewhere; a later implementation joins
        # only reviews without an explicit scope.
        scoped = (await assign("agy-01", "review", reviews=[frontend])).json()["task_id"]
        later = await assign("agy-01", "implement")
        assert later.status_code == 200, later.text
        assert later.json()["task_id"] not in (await client.get(f"/tasks/{review}")).json()["independent_from"]
        assert (await client.get(f"/tasks/{scoped}")).json()["independent_from"] == [frontend]

        # The review waits only for grok's handoff and judges only grok's candidate.
        assert (await client.post(f"/tasks/{frontend}/review", json={"agent_id": "grok-01"})).status_code == 200
        assert await _status(client, review) == "in_progress"
        await db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at,
                outcome, candidate_sha, log_refs)
               VALUES ('att-front', ?, 'dispatch:front', 'completed', 'external:1', '.', ?, 'completed', 'abc123', '[]')""",
            (frontend, "2026-01-01T00:00:00+00:00"),
        )
        await db.conn.commit()
        judged = await client.post(f"/tasks/{review}/verdict", json={
            "agent_id": "codex-01", "verdict": "approve", "reason": "ok",
        })
        assert judged.status_code == 200, judged.text
        assert judged.json()["evidence"]["implementation_task_id"] == frontend
        outside = await client.post(f"/tasks/{review}/verdict", json={
            "agent_id": "codex-01", "verdict": "approve", "implementation_task_id": backend,
        })
        assert outside.status_code == 409
    await db.close()


@pytest.mark.asyncio
async def test_missing_instruction_explains_the_404(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        missing = await client.post("/instructions/ins-nope/assignments", json={
            "agent_id": "codex-01", "role": "implement", "title": "x",
        })
        assert missing.status_code == 404
        detail = missing.json()["error"]
        assert "ins-nope" in detail and "alpha" in detail
    await db.close()
