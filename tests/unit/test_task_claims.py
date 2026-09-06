from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.core.tasks import TaskManager
from agent_bus.reputation.database import Database
from agent_bus.types import TaskStatus
from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner, RunnerResult


@pytest.fixture
async def claim_buses(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    databases = [Database(str(tmp_path / "claims.db")) for _ in range(2)]
    try:
        for db in databases:
            await db.initialize()
        yield [
            MessageBus(db=db, registry=AgentRegistry(), inbox=InboxManager(db))
            for db in databases
        ]
    finally:
        for db in databases:
            await db.close()


async def test_competing_claims_independent_connections(claim_buses):
    first, second = claim_buses
    # Repeat on fresh rows to exercise competing SQLite writers.
    for index in range(20):
        task_id = f"T{index}"
        await first.tasks.create(task_id, "Contended task")
        results = await asyncio.gather(
            first.tasks.claim(task_id, "alice"),
            second.tasks.claim(task_id, "bob"),
        )
        winners = [task for task in results if task is not None]
        assert len(winners) == 1
        persisted = await second.tasks.get(task_id)
        assert persisted.owner == winners[0].owner
        assert persisted.status == TaskStatus.IN_PROGRESS


@pytest.mark.parametrize("status", [status for status in TaskStatus if status != TaskStatus.PENDING])
async def test_claim_rejects_non_pending_tasks(tmp_db, status):
    manager = TaskManager(tmp_db)
    original = await manager.create("T1", "Not claimable")
    await tmp_db.conn.execute("UPDATE tasks SET status = ? WHERE task_id = 'T1'", (status.value,))
    await tmp_db.conn.commit()
    assert await manager.claim("T1", "alice") is None
    persisted = await manager.get("T1")
    assert persisted.status == status
    assert persisted.owner == "free"
    assert persisted.updated_at == original.updated_at


async def test_claim_retry_and_missing_task(tmp_db):
    manager = TaskManager(tmp_db)
    await manager.create("T1", "Claim once")
    first = await manager.claim("T1", "alice")
    assert first is not None
    assert await manager.claim("T1", "alice") is None
    assert await manager.claim("missing", "alice") is None
    assert await manager.get("T1") == first


async def test_http_claim_has_one_winner(claim_buses):
    first, second = claim_buses
    await first.tasks.create("T1", "Claim once")
    async with (
        AsyncClient(transport=ASGITransport(app=first.app), base_url="http://test") as alice,
        AsyncClient(transport=ASGITransport(app=second.app), base_url="http://test") as bob,
    ):
        responses = await asyncio.gather(
            alice.post("/tasks/T1/claim", json={"agent_id": "alice"}),
            bob.post("/tasks/T1/claim", json={"agent_id": "bob"}),
        )
        assert sorted(response.status_code for response in responses) == [200, 409]
        winner = next(response.json()["owner"] for response in responses if response.status_code == 200)
        assert (await alice.post("/tasks/T1/claim", json={"agent_id": winner})).status_code == 409
        assert (await bob.post("/tasks/missing/claim", json={"agent_id": "bob"})).status_code == 409
        await first.tasks.create("done", "Finished")
        await first.tasks.complete("done")
        assert (await alice.post("/tasks/done/claim", json={"agent_id": "alice"})).status_code == 409


async def test_worker_does_not_execute_after_losing_claim(claim_buses):
    first, second = claim_buses
    await first.tasks.create("T1", "Claim race")
    executions = []

    async def execute(prompt, session_id):
        executions.append(prompt)
        return RunnerResult(success=True, output="Unexpected execution")

    # Let the worker observe a free task, then a different connection wins
    # immediately before its claim reaches the real HTTP endpoint.
    async def competitor_claim(request):
        if request.url.path == "/tasks/T1/claim":
            assert await second.tasks.claim("T1", "alice") is not None

    async with AsyncClient(
        transport=ASGITransport(app=first.app),
        base_url="http://test",
        event_hooks={"request": [competitor_claim]},
    ) as client:
        worker = WorkerDaemon("bob", AgentRunner("bob", custom_executor=execute))
        worker._client = client
        await worker._check_and_process_pending()

    assert executions == []
    assert (await first.tasks.get("T1")).owner == "alice"
