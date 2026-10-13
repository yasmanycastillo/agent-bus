from __future__ import annotations

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
    try:
        yield bus
    finally:
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

        # Reply and acknowledgement are committed together.
        pending = await test_bus.inbox.get_inbox("worker_bob")
        assert len(pending) == 0
        replies = await test_bus.inbox.get_inbox("alice")
        assert len(replies) == 1
        assert replies[0].correlation_id == env.message_id
        assert replies[0].body["text"] == "I am online!"
        await daemon._handle_urgent_message(env.model_dump(mode="json"))
        assert len(executed_prompts) == 1


@pytest.mark.asyncio
async def test_worker_prompt_includes_runtime_send(test_bus, tmp_path):
    await test_bus.registry.register(AgentInfo(agent_id="worker_bob", display_name="Bob"))
    await test_bus.tasks.create("T-run", "Implement the helper")
    await test_bus.tasks.claim("T-run", "worker_bob")
    transport = ASGITransport(app=test_bus.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.post("/runtime/native/start", json={
            "attempt_id": "att-run", "task_id": "T-run", "idempotency_key": "native:T-run", "agent_id": "worker_bob",
        })
        assert started.status_code == 200
        sent = await client.post("/runtime/native/att-run/send", json={"text": "use the new helper"})
        assert sent.status_code == 200
        prompts = []

        async def mock_exec(prompt: str, session_id: str | None) -> RunnerResult:
            prompts.append(prompt)
            return RunnerResult(success=True, output="done")

        runner = AgentRunner(agent_id="worker_bob", custom_executor=mock_exec, worktree_dir=tmp_path)
        daemon = WorkerDaemon(agent_id="worker_bob", runner=runner, bus_url="http://test")
        daemon._client = client
        daemon._running = True
        await daemon._check_and_process_pending()
    assert prompts and "use the new helper" in prompts[0]


@pytest.mark.asyncio
async def test_worker_daemon_claims_and_runs_task(test_bus, tmp_path):
    # Register agent
    await test_bus.registry.register(AgentInfo(agent_id="worker_bob", display_name="Bob"))

    # Create a free task that this worker was assigned. Unassigned work stays put.
    await test_bus.tasks.create(task_id="T500", title="Build feature X")
    from agent_bus.core.instructions import InstructionLog
    await InstructionLog(test_bus.db).add_assignment(
        "instruction-1", "worker_bob", "codex", "implement", "Build feature X", "T500",
    )

    executed_tasks = []

    async def mock_exec(prompt: str, session_id: str | None) -> RunnerResult:
        executed_tasks.append(prompt)
        return RunnerResult(success=True, output="Feature X built.")

    # worktree_dir aísla el auto-commit de _handle_active_task del checkout real
    runner = AgentRunner(agent_id="worker_bob", custom_executor=mock_exec, worktree_dir=tmp_path)

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


@pytest.mark.asyncio
async def test_worker_logs_why_a_message_turn_failed(test_bus, caplog):
    await test_bus.registry.register(AgentInfo(agent_id="worker_bob", display_name="Bob"))
    env = Envelope(from_agent="alice", to_agent="worker_bob", message_type=MessageType.INBOX,
                   body={"text": "review?"}, reply_needed=True)
    await test_bus.inbox.deliver(env)

    async def failing_exec(prompt: str, session_id: str | None) -> RunnerResult:
        return RunnerResult(success=False, output="", error="denied actions: command (RunCommand)" + "x" * 2000)

    runner = AgentRunner(agent_id="worker_bob", custom_executor=failing_exec)
    async with AsyncClient(transport=ASGITransport(app=test_bus.app), base_url="http://test") as client:
        daemon = WorkerDaemon(agent_id="worker_bob", runner=runner, bus_url="http://test")
        daemon._client = client
        with caplog.at_level("WARNING", logger="agent_bus.worker.daemon"):
            await daemon._handle_urgent_message(env.model_dump(mode="json"))
    record = next(r for r in caplog.records if env.message_id in r.getMessage())
    assert "denied actions: command (RunCommand)" in record.getMessage()
    assert len(record.getMessage()) < 800


@pytest.mark.asyncio
async def test_worker_logs_why_a_task_turn_failed(test_bus, tmp_path, caplog):
    await test_bus.registry.register(AgentInfo(agent_id="worker_bob", display_name="Bob"))
    await test_bus.tasks.create("T-fail", "Implement")
    await test_bus.tasks.claim("T-fail", "worker_bob")

    async def failing_exec(prompt: str, session_id: str | None) -> RunnerResult:
        return RunnerResult(success=False, output="", error="provider exploded")

    async def allow(task_id: str) -> bool:
        return True

    runner = AgentRunner(agent_id="worker_bob", custom_executor=failing_exec, worktree_dir=tmp_path)
    async with AsyncClient(transport=ASGITransport(app=test_bus.app), base_url="http://test") as client:
        daemon = WorkerDaemon(agent_id="worker_bob", runner=runner, bus_url="http://test")
        daemon._client = client
        daemon._runtime_allows_execution = allow
        with caplog.at_level("WARNING", logger="agent_bus.worker.daemon"):
            await daemon._handle_active_task({"task_id": "T-fail", "title": "Implement"})
    assert any("T-fail" in r.getMessage() and "provider exploded" in r.getMessage() for r in caplog.records)


async def _review_ready(bus, implementations):
    """A review assigned over finished implementations that each delivered a candidate."""
    await bus.registry.register(AgentInfo(agent_id="worker_bob", display_name="Bob"))
    for task_id in implementations:
        await bus.tasks.create(task_id, "Implement", owner="alice")
    for index, task_id in enumerate(implementations):
        await bus.db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at,
                outcome, candidate_sha, log_refs)
               VALUES (?, ?, ?, 'completed', 'external:1', '', '2026-01-01T00:00:00+00:00', 'completed', ?, '[]')""",
            (f"att-{task_id}", task_id, f"dispatch:{task_id}", f"{index + 1:040x}"),
        )
        await bus.db.conn.execute("UPDATE tasks SET status = 'in_review' WHERE task_id = ?", (task_id,))
    await bus.db.conn.commit()
    await bus.tasks.create("R1", "Review", "Papel: review", owner="worker_bob")
    await bus.tasks.hold_for_review("R1", implementations)
    from agent_bus.core.instructions import InstructionLog
    await InstructionLog(bus.db).add_assignment("ins-1", "worker_bob", "codex", "review", "Review", "R1")
    return await bus.tasks.get("R1")


async def _run_review(bus, tmp_path, output):
    prompts = []

    async def review_exec(prompt: str, session_id: str | None) -> RunnerResult:
        prompts.append(prompt)
        return RunnerResult(success=True, output=output)

    async def allow(task_id: str) -> bool:
        return True

    runner = AgentRunner(agent_id="worker_bob", custom_executor=review_exec, worktree_dir=tmp_path)
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        daemon = WorkerDaemon(agent_id="worker_bob", runner=runner, bus_url="http://test")
        daemon._client = client
        daemon._runtime_allows_execution = allow
        task = (await client.get("/tasks/R1")).json()
        result = await daemon._handle_active_task(task)
        reviews = (await client.get("/tasks/R1/reviews")).json()
    return prompts, result, reviews


@pytest.mark.asyncio
async def test_worker_review_records_one_verdict_per_implementation(test_bus, tmp_path):
    review = await _review_ready(test_bus, ["I1", "I2"])
    assert review.status.value == "in_progress"
    prompts, result, reviews = await _run_review(test_bus, tmp_path, (
        "Revisé ambos cambios.\n"
        # changes_requested holds the review, so the approval must still be recorded
        "VERDICT: changes_requested IMPLEMENTATION: I2 REASON: falta manejar el caso vacío\n"
        "VERDICT: approve IMPLEMENTATION: I1 REASON: tests pasan y el diff es correcto\n"
    ))
    assert "VERDICT:" in prompts[0] and "I1" in prompts[0] and "I2" in prompts[0]
    assert result.success
    judged = {r["evidence"]["implementation_task_id"]: (r["verdict"], r["reason"]) for r in reviews}
    assert judged == {
        "I1": ("approve", "tests pasan y el diff es correcto"),
        "I2": ("changes_requested", "falta manejar el caso vacío"),
    }


@pytest.mark.asyncio
async def test_worker_review_without_verdict_line_records_nothing_and_logs(test_bus, tmp_path, caplog):
    await _review_ready(test_bus, ["I1"])
    with caplog.at_level("ERROR", logger="agent_bus.worker.daemon"):
        _, result, reviews = await _run_review(test_bus, tmp_path, "Me parece bien, apruebo.")
    assert reviews == []
    assert not result.success and "VERDICT" in result.error
    assert any("R1" in r.getMessage() and "I1" in r.getMessage() for r in caplog.records)
    assert (await test_bus.tasks.get("R1")).status.value == "in_progress"


@pytest.mark.asyncio
async def test_worker_review_ignores_verdicts_for_tasks_outside_the_review(test_bus, tmp_path):
    await _review_ready(test_bus, ["I1"])
    _, result, reviews = await _run_review(
        test_bus, tmp_path, "VERDICT: approve IMPLEMENTATION: OTHER REASON: no es mía",
    )
    assert reviews == [] and not result.success
