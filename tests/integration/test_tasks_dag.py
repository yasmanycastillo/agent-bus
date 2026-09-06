from __future__ import annotations

import os
import tempfile
import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.cli.display import generate_dashboard_renderable, print_tasks_table
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


async def test_http_dag_lifecycle(client: AsyncClient):
    # 1. Create task A with acceptance criteria and test command
    resp_a = await client.post("/tasks", json={
        "task_id": "dag-a",
        "title": "Build backend",
        "acceptance_criteria": ["All endpoints functional", "100% test pass"],
        "test_cmd": ["pytest", "tests/unit"],
        "operation_key": "op-backend-1",
    })
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["task_id"] == "dag-a"
    assert data_a["status"] == "pending"
    assert data_a["acceptance_criteria"] == ["All endpoints functional", "100% test pass"]
    assert data_a["test_cmd"] == ["pytest", "tests/unit"]
    assert data_a["operation_key"] == "op-backend-1"

    # 2. Create task B depending on A
    resp_b = await client.post("/tasks", json={
        "task_id": "dag-b",
        "title": "Build frontend",
        "depends_on": ["dag-a"],
    })
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert data_b["status"] == "blocked"
    assert data_b["depends_on"] == ["dag-a"]

    # 3. Worker queries ready free pending tasks
    resp_ready = await client.get("/tasks", params={"owner": "free", "status": "pending", "ready_only": "true"})
    assert resp_ready.status_code == 200
    ready_tasks = resp_ready.json()
    assert len(ready_tasks) == 1
    assert ready_tasks[0]["task_id"] == "dag-a"

    # 4. Worker claiming B fails with 409 Conflict because dependencies are not met
    claim_b = await client.post("/tasks/dag-b/claim", json={"agent_id": "worker-1"})
    assert claim_b.status_code == 409
    assert "blocked by unmet dependencies" in claim_b.json()["error"]

    # 5. Worker claims and completes A
    claim_a = await client.post("/tasks/dag-a/claim", json={"agent_id": "worker-1"})
    assert claim_a.status_code == 200
    assert claim_a.json()["owner"] == "worker-1"

    done_a = await client.post("/tasks/dag-a/done")
    assert done_a.status_code == 200
    assert done_a.json()["status"] == "done"

    # 6. Task B is now automatically unblocked to pending
    get_b = await client.get("/tasks/dag-b")
    assert get_b.status_code == 200
    assert get_b.json()["status"] == "pending"

    # 7. Now worker claiming B succeeds
    claim_b_ok = await client.post("/tasks/dag-b/claim", json={"agent_id": "worker-1"})
    assert claim_b_ok.status_code == 200
    assert claim_b_ok.json()["owner"] == "worker-1"
    assert claim_b_ok.json()["status"] == "in_progress"


async def test_http_cycle_detection_rejection(client: AsyncClient):
    # Direct self dependency
    resp = await client.post("/tasks", json={
        "task_id": "cycle-self",
        "title": "Self loop",
        "depends_on": ["cycle-self"],
    })
    assert resp.status_code == 400
    assert "cannot depend on itself" in resp.json()["error"]

    # Batch cycle
    batch_resp = await client.post("/tasks/batch", json={
        "tasks": [
            {"task_id": "node-1", "title": "Node 1", "depends_on": ["node-2"]},
            {"task_id": "node-2", "title": "Node 2", "depends_on": ["node-1"]},
        ]
    })
    assert batch_resp.status_code == 400
    assert "Cyclic dependency detected" in batch_resp.json()["error"]


async def test_http_batch_breakdown_idempotency(client: AsyncClient):
    payload = {
        "operation_key": "breakdown-user-auth",
        "tasks": [
            {
                "task_id": "auth-db",
                "title": "Auth database schema",
                "acceptance_criteria": ["Users table created"],
                "depends_on": [],
            },
            {
                "task_id": "auth-api",
                "title": "Auth API endpoints",
                "acceptance_criteria": ["/login returns JWT"],
                "depends_on": ["auth-db"],
            },
            {
                "task_id": "auth-ui",
                "title": "Auth login form",
                "acceptance_criteria": ["LoginForm component rendered"],
                "depends_on": ["auth-api"],
            },
        ],
    }

    resp1 = await client.post("/tasks/breakdown", json=payload)
    assert resp1.status_code == 200
    tasks1 = resp1.json()
    assert len(tasks1) == 3
    assert tasks1[0]["status"] == "pending"
    assert tasks1[1]["status"] == "blocked"
    assert tasks1[2]["status"] == "blocked"

    # Retrying with the same operation_key is idempotent
    resp2 = await client.post("/tasks/breakdown", json=payload)
    assert resp2.status_code == 200
    tasks2 = resp2.json()
    assert len(tasks2) == 3
    assert [t["task_id"] for t in tasks2] == [t["task_id"] for t in tasks1]

    # Verify no duplicated tasks exist
    all_tasks = (await client.get("/tasks")).json()
    assert len(all_tasks) == 3


def test_dashboard_and_table_display_with_dag():
    tasks = [
        {
            "task_id": "T1",
            "title": "Database setup",
            "owner": "worker-1",
            "status": "done",
            "depends_on": [],
            "locked_files": ["db.py"],
        },
        {
            "task_id": "T2",
            "title": "API implementation",
            "owner": "free",
            "status": "blocked",
            "depends_on": ["T1"],
            "locked_files": [],
        },
    ]

    # Test dashboard renderable generation
    renderable = generate_dashboard_renderable(
        status={"timestamp": "2026-09-06T12:00:00"},
        tasks=tasks,
        inbox=[],
        locks=[],
        decisions=[],
        agents=[{"agent_id": "worker-1", "status": "online"}],
        current_agent="worker-1",
    )
    assert renderable is not None

    # Test print_tasks_table without exception
    print_tasks_table(tasks)
