from __future__ import annotations

import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.types import AgentInfo, Envelope, MessageType
from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner, RunnerResult


@pytest.fixture
async def test_bus(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    await db.initialize()
    registry = AgentRegistry()
    inbox = InboxManager(db)
    bus = MessageBus(db=db, registry=registry, inbox=inbox)
    yield bus
    await db.close()


@pytest.mark.asyncio
async def test_worker_daemon_processes_urgent_message(test_bus):
    # Register agent
    await test_bus.registry.register(AgentInfo(agent_id="worker_bob", display_name="Bob"))

    # Deliver message requiring reply
    env = Envelope(
        from_agent="alice",
        to_agent="worker_bob",
        message_type=MessageType.INBOX,
        body={"text": "Are you online?"},
        reply_needed=True,
    )
    await test_bus.inbox.deliver(env)

    executed_prompts = []

    async def mock_exec(prompt: str, session_id: str | None) -> RunnerResult:
        executed_prompts.append(prompt)
        return RunnerResult(success=True, output="I am online!")

    runner = AgentRunner(agent_id="worker_bob", custom_executor=mock_exec)

    transport = ASGITransport(app=test_bus.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        daemon = WorkerDaemon(agent_id="worker_bob", runner=runner, bus_url="http://test")
        daemon._client = client
        daemon._running = True

        # Run one processing iteration
        await daemon._check_and_process_pending()

        assert len(executed_prompts) == 1
        assert "Are you online?" in executed_prompts[0]

        # Verify message was archived
        pending = await test_bus.inbox.get_inbox("worker_bob")
        assert len(pending) == 0


@pytest.mark.asyncio
async def test_worker_daemon_claims_and_runs_task(test_bus):
    # Register agent
    await test_bus.registry.register(AgentInfo(agent_id="worker_bob", display_name="Bob"))

    # Create free pending task
    await test_bus.tasks.create(task_id="T500", title="Build feature X")

    executed_tasks = []

    async def mock_exec(prompt: str, session_id: str | None) -> RunnerResult:
        executed_tasks.append(prompt)
        return RunnerResult(success=True, output="Feature X built.")

    runner = AgentRunner(agent_id="worker_bob", custom_executor=mock_exec)

    transport = ASGITransport(app=test_bus.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        daemon = WorkerDaemon(agent_id="worker_bob", runner=runner, bus_url="http://test")
        daemon._client = client
        daemon._running = True

        # Run one processing iteration
        await daemon._check_and_process_pending()

        assert len(executed_tasks) == 1
        assert "Build feature X" in executed_tasks[0]

        # Verify task is now in_progress and owned by worker_bob
        task = await test_bus.tasks.get("T500")
        assert task is not None
        assert task.owner == "worker_bob"
        assert task.status.value == "in_progress"
