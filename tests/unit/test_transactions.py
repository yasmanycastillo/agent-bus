from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agent_bus.core.inbox import InboxManager
from agent_bus.core.reviews import ReviewLog
from agent_bus.core.tasks import TaskManager
from agent_bus.types import Envelope, MessageType
from agent_bus.worker.gatekeeper import ReviewDecision, Verdict


async def test_unrelated_pending_write_does_not_survive_rollback_after_message_operation(tmp_db):
    """Deterministic regression test for P0-TRANSACTIONS:
    An unrelated pending write on the shared connection must not survive
    its own rollback merely because a concurrent message operation completes.
    """
    inbox = InboxManager(tmp_db)

    # 1. Coroutine A performs an uncommitted write on the shared connection.
    await tmp_db.conn.execute(
        "INSERT INTO tasks (task_id, title, created_at, updated_at) "
        "VALUES ('unrelated-write-1', 'Uncommitted Work', '2026-01-01', '2026-01-01')"
    )

    # 2. Coroutine B executes a concurrent message delivery operation to completion.
    env = Envelope(
        from_agent="alice",
        to_agent="bob",
        message_type=MessageType.INBOX,
        body={"text": "hello concurrent world"},
        timestamp=datetime.now(timezone.utc),
    )
    delivered_id = await inbox.deliver(env)
    assert delivered_id == env.message_id

    # 3. Coroutine A aborts and rolls back its pending work.
    await tmp_db.conn.rollback()

    # 4. Prove the unrelated pending write did NOT survive its own rollback.
    rows = await tmp_db.conn.execute_fetchall(
        "SELECT * FROM tasks WHERE task_id = 'unrelated-write-1'"
    )
    assert len(rows) == 0, "Unrelated pending write survived its rollback due to message commit interference"


async def test_message_send_and_archive_do_not_commit_unrelated_reputation_write(tmp_db):
    """Prove that send and archive operations do not commit dirty transaction state."""
    inbox = InboxManager(tmp_db)

    # 1. Unrelated write to reputation table
    await tmp_db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('uncommitted-agent')")

    # 2. Send and archive messages
    env = Envelope(
        from_agent="alice",
        to_agent="bob",
        message_type=MessageType.INBOX,
        timestamp=datetime.now(timezone.utc),
    )
    send_result = await inbox.send(env, ["bob"])
    assert not send_result.replayed
    await inbox.archive("bob", env.message_id)

    # 3. Rollback the uncommitted write
    await tmp_db.conn.rollback()

    # 4. Verify uncommitted reputation write was cleanly rolled back
    reputation_rows = await tmp_db.conn.execute_fetchall(
        "SELECT * FROM reputation WHERE agent_id = 'uncommitted-agent'"
    )
    assert len(reputation_rows) == 0


async def test_task_transfer_and_review_add_do_not_commit_unrelated_write(tmp_db):
    """Prove that task transfer and review logging do not commit unrelated pending writes."""
    task_manager = TaskManager(tmp_db)
    review_log = ReviewLog(tmp_db)

    # Create a baseline task
    await task_manager.create("TASK-1", "Baseline Task", owner="alice")

    # 1. Coroutine A has an uncommitted write
    await tmp_db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('dirty-agent')")

    # 2. Coroutine B performs task transfer and review logging
    transferred = await task_manager.transfer(
        "TASK-1", "bob", actor="alice", session_id="sess-alice", require_owner=True
    )
    assert transferred is not None
    assert transferred.owner == "bob"

    review = ReviewDecision(
        review_id="rev-1",
        task_id="TASK-1",
        reviewer_agent_id="charlie",
        reviewer_session_id="sess-charlie",
        sha="abcdef1234567890",
        verdict=Verdict.APPROVE,
        reason="Looks solid",
        evidence={"diff_stat": "+1 -1"},
        test_results={"passed": 1},
        created_at=datetime.now(timezone.utc),
    )
    logged_review = await review_log.add(review)
    assert logged_review.review_id == "rev-1"

    # 3. Rollback the dirty write
    await tmp_db.conn.rollback()

    # 4. Verify dirty write was rolled back and did not survive
    assert not await tmp_db.conn.execute_fetchall(
        "SELECT * FROM reputation WHERE agent_id = 'dirty-agent'"
    )


async def test_concurrent_message_and_failing_coroutine_isolation(tmp_db):
    """Verify concurrent execution where one coroutine fails and rolls back
    while another completes a message operation on the shared connection.
    """
    inbox = InboxManager(tmp_db)
    barrier = asyncio.Event()

    async def failing_worker():
        await tmp_db.conn.execute(
            "INSERT INTO tasks (task_id, title, created_at, updated_at) "
            "VALUES ('failing-task', 'Should Be Rolled Back', '2026-01-01', '2026-01-01')"
        )
        barrier.set()
        await asyncio.sleep(0.05)
        await tmp_db.conn.rollback()

    async def message_worker():
        await barrier.wait()
        env = Envelope(
            from_agent="worker-1",
            to_agent="worker-2",
            message_type=MessageType.INBOX,
            timestamp=datetime.now(timezone.utc),
        )
        return await inbox.deliver(env)

    msg_id, _ = await asyncio.gather(message_worker(), failing_worker())
    assert msg_id is not None

    rows = await tmp_db.conn.execute_fetchall(
        "SELECT * FROM tasks WHERE task_id = 'failing-task'"
    )
    assert len(rows) == 0, "Failing worker's write survived rollback despite concurrent message"
