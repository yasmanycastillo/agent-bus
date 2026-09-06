from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent_bus.core.inbox import IdempotencyConflict, InboxManager, InvalidCursor, MessageNotFound
from agent_bus.reputation.database import Database
from agent_bus.types import Envelope, MessageType


def message(**changes):
    return Envelope(**(dict(from_agent="alice", to_agent="bob", message_type=MessageType.INBOX,
                           body={"text": "Hello"}) | changes))


async def test_idempotent_send_survives_restart_and_conflicts(tmp_db):
    inbox = InboxManager(tmp_db)
    first = await inbox.send(message(), ["bob"], idempotency_key="operation-1")
    assert not first.replayed
    assert first.envelope.conversation_id == first.envelope.message_id
    await inbox.archive("bob", first.envelope.message_id)
    state = await inbox.delivery_state("bob", first.envelope.message_id)
    reopened = Database(tmp_db.db_path)
    await reopened.initialize()
    try:
        second = InboxManager(reopened)
        retry = await second.send(message(), ["bob"], idempotency_key="operation-1")
        assert retry.replayed and retry.envelope == first.envelope
        assert retry.recipients == ["bob"]
        assert await second.pending_count("bob") == 0
        assert await second.delivery_state("bob", first.envelope.message_id) == state
        with pytest.raises(IdempotencyConflict):
            await second.send(message(body={"text": "Changed"}), ["bob"], idempotency_key="operation-1")
        with pytest.raises(IdempotencyConflict):
            await second.send(message(to_agent="carol"), ["carol"], idempotency_key="operation-1")
        independent = await second.send(message(from_agent="carol"), ["bob"], idempotency_key="operation-1")
        assert not independent.replayed
    finally:
        await reopened.close()


async def test_broadcast_retry_freezes_recipients(tmp_db):
    inbox = InboxManager(tmp_db)
    first = await inbox.send(message(to_agent=None, message_type=MessageType.BROADCAST),
                             ["bob", "carol", "dave"], idempotency_key="broadcast-1")
    await inbox.archive("bob", first.envelope.message_id)
    retry = await inbox.send(message(to_agent=None, message_type=MessageType.BROADCAST),
                             ["carol", "dave", "eve"], idempotency_key="broadcast-1")
    assert retry.replayed
    assert retry.recipients == ["bob", "carol", "dave"]
    assert await inbox.pending_count("bob") == 0
    assert await inbox.pending_count("eve") == 0
    assert await inbox.pending_count("carol") == await inbox.pending_count("dave") == 1


async def test_concurrent_retries_have_one_commit(tmp_db):
    other = Database(tmp_db.db_path)
    await other.initialize()
    try:
        first, second = InboxManager(tmp_db), InboxManager(other)
        for index in range(10):
            results = await asyncio.gather(
                first.send(message(), ["bob"], idempotency_key=f"key-{index}"),
                second.send(message(), ["bob"], idempotency_key=f"key-{index}"),
            )
            assert sorted(result.replayed for result in results) == [False, True]
            assert results[0].envelope == results[1].envelope
        assert await first.pending_count("bob") == 10
        results = await asyncio.gather(
            first.send(message(body={"text": "A"}), ["bob"], idempotency_key="conflict"),
            second.send(message(body={"text": "B"}), ["bob"], idempotency_key="conflict"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, IdempotencyConflict) for result in results) == 1
        assert await first.pending_count("bob") == 11
    finally:
        await other.close()


async def test_broadcast_failure_rolls_back_deliveries_and_key(tmp_db):
    inbox = InboxManager(tmp_db)
    await tmp_db.conn.execute("""CREATE TRIGGER fail_delivery BEFORE INSERT ON inbox
        WHEN NEW.to_agent = 'carol' BEGIN SELECT RAISE(ABORT, 'delivery unavailable'); END""")
    await tmp_db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="delivery unavailable"):
        await inbox.send(message(to_agent=None, message_type=MessageType.BROADCAST),
                         ["bob", "carol"], idempotency_key="retry-after-failure")
    assert await inbox.pending_count("bob") == 0
    assert await tmp_db.conn.execute_fetchall("SELECT * FROM message_idempotency") == []
    assert await tmp_db.conn.execute_fetchall("SELECT * FROM inbox_delivery_state") == []
    await tmp_db.conn.execute("DROP TRIGGER fail_delivery")
    await tmp_db.conn.commit()
    sent = await inbox.send(message(to_agent=None, message_type=MessageType.BROADCAST),
                            ["bob", "carol"], idempotency_key="retry-after-failure")
    assert not sent.replayed
    assert await inbox.pending_count("bob") == await inbox.pending_count("carol") == 1


async def test_custom_message_id_cannot_impersonate_or_change_existing_content(tmp_db):
    inbox = InboxManager(tmp_db)
    original = message(message_id="chosen-id")
    await inbox.deliver(original)
    await inbox.deliver(original)
    for conflicting in (original.model_copy(update={"from_agent": "mallory"}),
                        original.model_copy(update={"body": {"text": "Tampered"}}),
                        original.model_copy(update={"to_agent": "carol", "from_agent": "mallory"})):
        with pytest.raises(IdempotencyConflict):
            await inbox.deliver(conflicting)
    assert (await inbox.get_message("bob", "chosen-id")).body == original.body
    assert await inbox.pending_count("carol") == 0


async def test_reply_derives_thread_and_acknowledges_atomically(tmp_db):
    inbox = InboxManager(tmp_db)
    parent = message(related_task="T1", reply_needed=True)
    await inbox.deliver(parent)
    reply = await inbox.reply("bob", parent.message_id, {"text": "Answer"},
                              idempotency_key="reply-1", acknowledge=True)
    assert reply.envelope.from_agent == "bob"
    assert reply.envelope.to_agent == "alice"
    assert reply.envelope.correlation_id == parent.message_id
    assert reply.envelope.conversation_id == parent.message_id
    assert reply.envelope.related_task == "T1"
    assert await inbox.pending_count("bob") == 0
    retry = await inbox.reply("bob", parent.message_id, {"text": "Answer"},
                              idempotency_key="reply-1", acknowledge=True)
    assert retry.replayed and retry.envelope == reply.envelope
    assert await inbox.pending_count("alice") == 1
    next_reply = await inbox.reply("alice", reply.envelope.message_id, {"text": "Follow-up"}, idempotency_key="follow-up")
    assert next_reply.envelope.correlation_id == reply.envelope.message_id
    assert next_reply.envelope.conversation_id == parent.message_id
    with pytest.raises(MessageNotFound):
        await inbox.reply("carol", parent.message_id, {}, idempotency_key="not-owner")
    with pytest.raises(IdempotencyConflict):
        await inbox.reply("bob", parent.message_id, {"text": "Changed"}, idempotency_key="reply-1", acknowledge=True)


async def test_reply_ack_failure_rolls_back_reply_and_idempotency(tmp_db):
    inbox = InboxManager(tmp_db)
    parent = message()
    await inbox.deliver(parent)
    await tmp_db.conn.execute("""CREATE TRIGGER fail_ack BEFORE UPDATE OF archived ON inbox
        BEGIN SELECT RAISE(ABORT, 'ack unavailable'); END""")
    await tmp_db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="ack unavailable"):
        await inbox.reply("bob", parent.message_id, {}, idempotency_key="retry", acknowledge=True)
    assert await inbox.pending_count("alice") == 0
    assert await inbox.pending_count("bob") == 1
    assert await tmp_db.conn.execute_fetchall("SELECT * FROM message_idempotency") == []
    await tmp_db.conn.execute("DROP TRIGGER fail_ack")
    await tmp_db.conn.commit()
    result = await inbox.reply("bob", parent.message_id, {}, idempotency_key="retry", acknowledge=True)
    assert not result.replayed


async def test_admin_reply_keeps_real_actor_and_source_scope(tmp_db):
    inbox = InboxManager(tmp_db)
    first = await inbox.send(message(to_agent=None, message_type=MessageType.BROADCAST), ["human", "bob"])
    reply = await inbox.reply("human", first.envelope.message_id, {"approved": True},
                              idempotency_key="approval", acknowledge=True, actor_id="operator")
    assert reply.envelope.from_agent == "operator"
    assert (await inbox.delivery_state("human", first.envelope.message_id))["acknowledged"]
    assert not (await inbox.delivery_state("bob", first.envelope.message_id))["acknowledged"]
    with pytest.raises(IdempotencyConflict):
        await inbox.reply("bob", first.envelope.message_id, {"approved": True},
                          idempotency_key="approval", acknowledge=True, actor_id="operator")


async def test_ack_batch_has_no_partial_success_and_keeps_first_timestamp(tmp_db):
    inbox = InboxManager(tmp_db)
    first, second = message(), message(to_agent="carol")
    await inbox.deliver(first)
    await inbox.deliver(second)
    with pytest.raises(MessageNotFound):
        await inbox.acknowledgments("bob", [first.message_id, second.message_id])
    assert await inbox.pending_count("bob") == 1
    result = await inbox.acknowledgments("bob", [first.message_id, first.message_id])
    assert result == {"acknowledged": [first.message_id]}
    state = await inbox.delivery_state("bob", first.message_id)
    await inbox.archive("bob", first.message_id)
    assert await inbox.delivery_state("bob", first.message_id) == state
    assert await inbox.delivery_state("carol", first.message_id) is None


async def test_failure_is_durable_and_does_not_ack(tmp_db):
    inbox = InboxManager(tmp_db)
    parent = message()
    await inbox.deliver(parent)
    assert (await inbox.record_failure("bob", parent.message_id, "Runner failed"))["attempts"] == 1
    state = await inbox.record_failure("bob", parent.message_id, "Reply delivery failed")
    assert state["attempts"] == 2 and not state["acknowledged"]
    assert state["last_error"] == "Reply delivery failed"
    other = Database(tmp_db.db_path)
    await other.initialize()
    try:
        assert await InboxManager(other).delivery_state("bob", parent.message_id) == state
    finally:
        await other.close()
    with pytest.raises(MessageNotFound):
        await inbox.record_failure("carol", parent.message_id, "Unauthorized")
    await inbox.acknowledgments("bob", [parent.message_id])
    with pytest.raises(ValueError):
        await inbox.record_failure("bob", parent.message_id, "Late failure")


async def test_page_watermark_acknowledgments_and_new_arrivals(tmp_db):
    inbox = InboxManager(tmp_db)
    ids = []
    for index in range(7):
        envelope = message(body={"index": index}, reply_needed=index % 2 == 0)
        ids.append(await inbox.deliver(envelope))
    page = await inbox.page("bob", limit=2)
    seen = [m["message_id"] for m in page["messages"]]
    assert seen == ids[:2]
    await inbox.acknowledgments("bob", seen)
    late = await inbox.deliver(message())
    while page["next_cursor"]:
        page = await inbox.page("bob", limit=2, cursor=page["next_cursor"])
        batch = [m["message_id"] for m in page["messages"]]
        seen.extend(batch)
        if batch:
            await inbox.acknowledgments("bob", batch)
    assert seen == ids
    assert [m["message_id"] for m in (await inbox.page("bob"))["messages"]] == [late]


async def test_cursor_scope_tampering_restart_and_filter(tmp_db):
    inbox = InboxManager(tmp_db)
    for index in range(6):
        await inbox.deliver(message(reply_needed=index % 2 == 0))
    page = await inbox.page("bob", reply_needed=True, limit=1)
    cursor = page["next_cursor"]
    for agent, filtering, value in (("alice", True, cursor), ("bob", None, cursor),
                                    ("bob", False, cursor), ("bob", True, cursor[:-1] + ("0" if cursor[-1] != "0" else "1")),
                                    ("bob", True, "not-a-cursor")):
        with pytest.raises(InvalidCursor):
            await inbox.page(agent, reply_needed=filtering, cursor=value)
    other = Database(tmp_db.db_path)
    await other.initialize()
    try:
        next_page = await InboxManager(other).page("bob", reply_needed=True, limit=100, cursor=cursor)
        assert len(next_page["messages"]) == 2
        assert all(row["reply_needed"] for row in next_page["messages"])
        assert next_page["next_cursor"] is None
    finally:
        await other.close()


async def test_retention_cannot_recycle_sequence_into_old_cursor(tmp_db):
    inbox = InboxManager(tmp_db)
    for index in range(3):
        await inbox.deliver(message())
    first = await inbox.page("bob", limit=1)
    ids = [row.message_id for row in await inbox.get_inbox("bob")]
    old_sequences = [(await inbox.delivery_state("bob", value))["sequence"] for value in ids]
    await inbox.acknowledgments("bob", ids)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    await tmp_db.conn.execute("UPDATE inbox SET archived_at = ?", (old,))
    await tmp_db.conn.commit()
    assert await inbox.cleanup_expired() == 3
    fresh = await inbox.deliver(message())
    assert (await inbox.delivery_state("bob", fresh))["sequence"] > max(old_sequences)
    assert (await inbox.page("bob", cursor=first["next_cursor"]))["messages"] == []


async def test_pending_summary_counts_without_full_inbox(tmp_db, monkeypatch):
    inbox = InboxManager(tmp_db)
    for index in range(8):
        await inbox.deliver(message(reply_needed=index % 2 == 0))
    async def forbidden(*args):
        pytest.fail("Summary must not load full inbox")
    monkeypatch.setattr(inbox, "get_inbox", forbidden)
    summary = await inbox.pending_summary("bob")
    assert summary["count"] == 8 and summary["reply_needed"] == 4
    assert len(summary["latest_summary"]) == 3
    assert summary["latest_senders"] == ["alice"]


@pytest.mark.parametrize("limit", [0, 101, True, "10"])
async def test_page_rejects_invalid_limits(tmp_db, limit):
    with pytest.raises(ValueError):
        await InboxManager(tmp_db).page("bob", limit=limit)


async def test_migration_from_composite_inbox_backfills_thread_and_sequence(tmp_path):
    from agent_bus.reputation.database import INBOX_SCHEMA
    path = str(tmp_path / "pre-lifecycle.db")
    with sqlite3.connect(path) as connection:
        connection.execute(INBOX_SCHEMA)
        connection.execute("INSERT INTO inbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           ("old", "alice", "bob", "inbox", "old-thread", 1, "T1", '{}', '{}', None,
                            "2026-01-01T00:00:00+00:00", 1, "2026-01-02T00:00:00+00:00"))
    previous_state = None
    previous_secret = None
    for _ in range(2):
        db = Database(path)
        await db.initialize()
        try:
            inbox = InboxManager(db)
            envelope = await inbox.get_message("bob", "old")
            assert envelope.conversation_id == "old-thread"
            state = await inbox.delivery_state("bob", "old")
            assert state["acknowledged"] and state["attempts"] == 0
            assert state["acknowledged_at"] == "2026-01-02T00:00:00+00:00"
            secret = (await db.conn.execute_fetchall("SELECT value FROM message_state"))[0][0]
            if previous_state:
                assert state == previous_state and secret == previous_secret
            previous_state, previous_secret = state, secret
        finally:
            await db.close()
