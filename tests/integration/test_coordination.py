from __future__ import annotations

import os
import tempfile

import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database


@pytest.fixture
async def bus_app():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, "test.db"))
        await db.initialize()
        registry = AgentRegistry()
        inbox = InboxManager(db)
        bus = MessageBus(db=db, registry=registry, inbox=inbox)
        yield bus
        await db.close()


@pytest.fixture
async def client(bus_app: MessageBus):
    transport = ASGITransport(app=bus_app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- Tasks ---


async def test_task_create_and_list(client: AsyncClient):
    resp = await client.post("/tasks", json={"task_id": "T1", "title": "Setup CI"})
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "T1"

    resp = await client.get("/tasks")
    assert len(resp.json()) == 1


async def test_task_claim(client: AsyncClient):
    await client.post("/tasks", json={"task_id": "T1", "title": "Setup CI"})
    resp = await client.post("/tasks/T1/claim", json={"agent_id": "claude"})
    assert resp.status_code == 200
    assert resp.json()["owner"] == "claude"
    assert resp.json()["status"] == "in_progress"


async def test_task_complete(client: AsyncClient):
    await client.post("/tasks", json={"task_id": "T1", "title": "Setup CI"})
    resp = await client.post("/tasks/T1/done")
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


async def test_task_not_found(client: AsyncClient):
    resp = await client.get("/tasks/NOPE")
    assert resp.status_code == 404


async def test_task_lock_files(client: AsyncClient):
    await client.post("/tasks", json={"task_id": "T1", "title": "Task"})
    resp = await client.post("/tasks/T1/lock-files", json={"files": ["a.py", "b.py"]})
    assert resp.status_code == 200
    assert len(resp.json()["locked_files"]) == 2


# --- Decisions ---


async def test_decision_add_and_list(client: AsyncClient):
    resp = await client.post(
        "/decisions",
        json={
            "decision_id": "D1",
            "title": "Use FastAPI",
            "context": "Need web framework",
            "decision": "FastAPI it is",
            "decided_by": "consensus",
        },
    )
    assert resp.status_code == 200

    resp = await client.get("/decisions")
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "Use FastAPI"


async def test_decision_not_found(client: AsyncClient):
    resp = await client.get("/decisions/NOPE")
    assert resp.status_code == 404


# --- Locks ---


async def test_lock_acquire_and_list(client: AsyncClient):
    resp = await client.post(
        "/locks/acquire",
        json={"file_path": "src/main.py", "agent_id": "claude", "reason": "refactor"},
    )
    assert resp.status_code == 200
    assert resp.json()["locked_by"] == "claude"

    resp = await client.get("/locks")
    assert len(resp.json()) == 1


async def test_lock_conflict(client: AsyncClient):
    await client.post("/locks/acquire", json={"file_path": "a.py", "agent_id": "claude"})
    resp = await client.post("/locks/acquire", json={"file_path": "a.py", "agent_id": "codex"})
    assert resp.status_code == 409


async def test_lock_release(client: AsyncClient):
    await client.post("/locks/acquire", json={"file_path": "a.py", "agent_id": "claude"})
    resp = await client.post("/locks/release", json={"file_path": "a.py", "agent_id": "claude"})
    assert resp.status_code == 200

    resp = await client.get("/locks")
    assert len(resp.json()) == 0


async def test_lock_release_wrong_owner(client: AsyncClient):
    await client.post("/locks/acquire", json={"file_path": "a.py", "agent_id": "claude"})
    resp = await client.post("/locks/release", json={"file_path": "a.py", "agent_id": "codex"})
    assert resp.status_code == 403


# --- Skills ---


async def test_skill_assign_and_get(client: AsyncClient):
    resp = await client.post(
        "/skills/claude",
        json={
            "role": "developer",
            "responsibilities": ["write code"],
            "audit_questions": ["Tests pass?"],
        },
    )
    assert resp.status_code == 200

    resp = await client.get("/skills/claude")
    assert len(resp.json()) == 1
    assert resp.json()[0]["role"] == "developer"


# --- Kickoff ---


async def test_kickoff_start_and_progress(client: AsyncClient):
    resp = await client.post("/kickoff/start")
    assert resp.status_code == 200
    steps = resp.json()
    assert len(steps) == 9
    assert steps[0]["name"] == "project_brief"

    resp = await client.get("/kickoff/progress")
    progress = resp.json()
    assert all(s["status"] == "pending" for s in progress)


async def test_kickoff_complete_step(client: AsyncClient):
    await client.post("/kickoff/start")
    resp = await client.post(
        "/kickoff/step/0",
        json={"result": {"brief": "Build agent-bus"}, "completed_by": "human"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"

    progress = (await client.get("/kickoff/progress")).json()
    assert progress[0]["status"] == "done"
    assert progress[1]["status"] == "pending"
