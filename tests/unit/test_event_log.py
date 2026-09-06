from __future__ import annotations

import asyncio
import sqlite3

import pytest

from agent_bus.core.events import CursorExpired, CursorInvalid, EventLog, append_event
from agent_bus.core.inbox import IdempotencyConflict, InboxManager
from agent_bus.reputation.database import Database, INBOX_SCHEMA
from agent_bus.types import Envelope, MessageType


def message(**changes):
    return Envelope(**(dict(from_agent="alice", to_agent="bob", message_type=MessageType.INBOX,
                           body={"text": "Hello"}) | changes))


async def test_events_resume_with_new_process_and_stable_cursors(tmp_db):
    inbox, log = InboxManager(tmp_db), EventLog(tmp_db)
    before = await log.checkpoint("bob")
    first = await inbox.send(message(), ["bob"], idempotency_key="first")
    first_page = await log.read("bob", before)
    event = first_page["events"][0]
    assert event["event"] == "message"
    assert event["data"] == first.envelope.model_dump(mode="json")
    assert first_page["next_cursor"] == event["id"]
    assert not first_page["has_more"]
    other = Database(tmp_db.db_path)
    await other.initialize()
    try:
        resumed = EventLog(other)
        assert (await resumed.read("bob", before))["events"] == [event]
        assert await resumed.checkpoint("bob") == await log.checkpoint("bob")
        second = await InboxManager(other).send(message(body={"text": "After restart"}), ["bob"])
        result = await resumed.read("bob", event["id"])
        assert [row["data"]["message_id"] for row in result["events"]] == [second.envelope.message_id]
    finally:
        await other.close()


async def test_broadcast_has_one_event_per_recipient_and_retries_do_not_append(tmp_db):
    inbox, log = InboxManager(tmp_db), EventLog(tmp_db)
    checkpoints = {scope: await log.checkpoint(scope) for scope in (None, "bob", "carol")}
    first = await inbox.send(message(to_agent=None, message_type=MessageType.BROADCAST),
                             ["bob", "carol"], idempotency_key="broadcast")
    await inbox.send(message(to_agent=None, message_type=MessageType.BROADCAST),
                     ["bob", "carol", "dave"], idempotency_key="broadcast")
    await inbox.deliver(first.envelope.model_copy(update={"to_agent": "bob"}))
    await inbox.archive("bob", first.envelope.message_id)
    all_events = (await log.read(None, checkpoints[None]))["events"]
    assert len(all_events) == 2
    assert {event["data"]["to_agent"] for event in all_events} == {"bob", "carol"}
    assert {event["data"]["message_id"] for event in all_events} == {first.envelope.message_id}
    for agent in ("bob", "carol"):
        events = (await log.read(agent, checkpoints[agent]))["events"]
        assert len(events) == 1 and events[0]["data"]["to_agent"] == agent
    assert await inbox.pending_count("bob") == 0  # event history is not the inbox


async def test_cursor_scope_and_global_all_are_distinct(tmp_db):
    inbox, log = InboxManager(tmp_db), EventLog(tmp_db)
    all_agent = await log.checkpoint("all")
    global_cursor = await log.checkpoint(None)
    bob_cursor = await log.checkpoint("bob")
    assert all_agent != global_cursor
    await inbox.deliver(message(to_agent="all"))
    await inbox.deliver(message(to_agent="bob"))
    assert len((await log.read("all", all_agent))["events"]) == 1
    assert len((await log.read(None, global_cursor))["events"]) == 2
    for scope, cursor in ((None, all_agent), ("all", global_cursor), ("bob", all_agent), ("all", bob_cursor)):
        with pytest.raises(CursorInvalid):
            await log.read(scope, cursor)


async def test_event_cursor_rejects_tampering_future_and_inbox_cursor(tmp_db):
    inbox, log = InboxManager(tmp_db), EventLog(tmp_db)
    before = await log.checkpoint("bob")
    for _ in range(2):
        await inbox.deliver(message())
    inbox_cursor = (await inbox.page("bob", limit=1))["next_cursor"]
    secret = (await tmp_db.conn.execute_fetchall("SELECT value FROM message_state WHERE name='cursor_secret'"))[0][0]
    future = log._encode("bob", 999999, secret)
    altered = before[:-1] + ("0" if before[-1] != "0" else "1")
    for cursor in (altered, "bad", "a.b.c", inbox_cursor, future, None):
        with pytest.raises(CursorInvalid):
            await log.read("bob", cursor)


async def test_scoped_page_advances_over_global_gaps_and_has_more(tmp_db):
    inbox, log = InboxManager(tmp_db), EventLog(tmp_db)
    cursor = await log.checkpoint("bob")
    bob_ids = []
    for index in range(5):
        envelope = message(to_agent="bob" if index % 2 == 0 else "carol")
        await inbox.deliver(envelope)
        if envelope.to_agent == "bob":
            bob_ids.append(envelope.message_id)
    await inbox.deliver(message(to_agent="carol"))
    seen = []
    while True:
        page = await log.read("bob", cursor, limit=1)
        seen.extend(event["data"]["message_id"] for event in page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert seen == bob_ids
    assert cursor == await log.checkpoint("bob")
    assert (await log.read("bob", cursor))["events"] == []
    await inbox.deliver(message(to_agent="carol"))
    gap_page = await log.read("bob", cursor)
    assert not gap_page["events"] and not gap_page["has_more"]
    assert gap_page["next_cursor"] != cursor
    assert gap_page["next_cursor"] == await log.checkpoint("bob")


async def test_count_retention_floor_survives_restart_and_preserves_inbox(tmp_db):
    inbox, original = InboxManager(tmp_db), EventLog(tmp_db)
    before = await original.checkpoint("bob")
    for _ in range(4):
        await inbox.deliver(message())
    old_events = (await original.read("bob", before))["events"]
    bounded = EventLog(tmp_db, max_events=2)
    with pytest.raises(CursorExpired):
        await bounded.read("bob", before)
    assert (await tmp_db.conn.execute_fetchall("SELECT floor FROM event_state"))[0][0] == 2
    assert (await tmp_db.conn.execute_fetchall("SELECT COUNT(*) FROM event_log"))[0][0] == 2
    assert await inbox.pending_count("bob") == 4
    at_floor = await bounded.read("bob", old_events[1]["id"])
    assert [row["id"] for row in at_floor["events"]] == [row["id"] for row in old_events[2:]]
    other = Database(tmp_db.db_path)
    await other.initialize()
    try:
        with pytest.raises(CursorExpired):
            await EventLog(other).read("bob", before)
        assert (await other.conn.execute_fetchall("SELECT COUNT(*) FROM event_log"))[0][0] == 2
        checkpoint = await EventLog(other).checkpoint("bob")
        assert checkpoint == await bounded.checkpoint("bob")
    finally:
        await other.close()


async def test_ttl_pruning_also_runs_on_checkpoint(tmp_db, monkeypatch):
    current = [1000000]
    monkeypatch.setattr("agent_bus.core.events.time.time", lambda: current[0])
    inbox, log = InboxManager(tmp_db), EventLog(tmp_db, retention_seconds=10)
    before = await log.checkpoint("bob")
    await inbox.deliver(message())
    first_event = (await log.read("bob", before))["events"][0]
    current[0] += 11
    await inbox.deliver(message())
    await log.checkpoint("bob")
    with pytest.raises(CursorExpired):
        await log.read("bob", before)
    assert len((await log.read("bob", first_event["id"]))["events"]) == 1
    current[0] += 11
    latest = await log.checkpoint("bob")
    assert (await log.read("bob", latest))["events"] == []
    assert (await tmp_db.conn.execute_fetchall("SELECT COUNT(*) FROM event_log"))[0][0] == 0
    assert await inbox.pending_count("bob") == 2


async def test_append_enforces_runtime_retention_in_same_transaction(tmp_db, monkeypatch):
    current = [1000000]
    monkeypatch.setattr("agent_bus.core.events.time.time", lambda: current[0])
    inbox = InboxManager(tmp_db)
    await inbox.deliver(message())
    current[0] += 604801
    await inbox.deliver(message())
    assert (await tmp_db.conn.execute_fetchall("SELECT COUNT(*) FROM event_log"))[0][0] == 1
    assert (await tmp_db.conn.execute_fetchall("SELECT floor FROM event_state"))[0][0] == 1


async def test_failed_delivery_reply_or_event_insert_leaves_no_orphan(tmp_db):
    inbox, log = InboxManager(tmp_db), EventLog(tmp_db)
    start = await log.checkpoint(None)
    await tmp_db.conn.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON event_log
        WHEN NEW.to_agent = 'carol' BEGIN SELECT RAISE(ABORT, 'event unavailable'); END""")
    await tmp_db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="event unavailable"):
        await inbox.send(message(to_agent=None, message_type=MessageType.BROADCAST),
                         ["bob", "carol"], idempotency_key="first-attempt")
    assert await inbox.pending_count("bob") == 0
    assert (await log.read(None, start))["events"] == []
    assert await tmp_db.conn.execute_fetchall("SELECT * FROM message_idempotency") == []
    await tmp_db.conn.execute("DROP TRIGGER fail_event")
    await tmp_db.conn.commit()
    parent = message()
    await inbox.deliver(parent)
    before_reply = await log.checkpoint(None)
    await tmp_db.conn.execute("""CREATE TRIGGER fail_ack BEFORE UPDATE OF archived ON inbox
        BEGIN SELECT RAISE(ABORT, 'ack unavailable'); END""")
    await tmp_db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="ack unavailable"):
        await inbox.reply("bob", parent.message_id, {}, idempotency_key="reply", acknowledge=True)
    assert (await log.read(None, before_reply))["events"] == []
    assert await inbox.pending_count("alice") == 0
    assert await inbox.pending_count("bob") == 1


async def test_successful_reply_event_has_derived_conversation(tmp_db):
    inbox, log = InboxManager(tmp_db), EventLog(tmp_db)
    parent = message()
    await inbox.deliver(parent)
    checkpoint = await log.checkpoint("alice")
    reply = await inbox.reply("bob", parent.message_id, {}, idempotency_key="reply", acknowledge=True)
    await inbox.reply("bob", parent.message_id, {}, idempotency_key="reply", acknowledge=True)
    events = (await log.read("alice", checkpoint))["events"]
    assert len(events) == 1
    assert events[0]["data"]["correlation_id"] == parent.message_id
    assert events[0]["data"]["conversation_id"] == parent.message_id
    assert events[0]["data"]["message_id"] == reply.envelope.message_id


async def test_concurrent_connections_retry_one_event_and_snapshot(tmp_db):
    other = Database(tmp_db.db_path)
    await other.initialize()
    try:
        inbox, second = InboxManager(tmp_db), InboxManager(other)
        log, other_log = EventLog(tmp_db), EventLog(other)
        checkpoint = await log.checkpoint("bob")
        for index in range(5):
            results = await asyncio.gather(
                inbox.send(message(), ["bob"], idempotency_key=f"key-{index}"),
                second.send(message(), ["bob"], idempotency_key=f"key-{index}"),
                other_log.read("bob", checkpoint),
            )
            assert sorted(result.replayed for result in results[:2]) == [False, True]
        events = (await log.read("bob", checkpoint))["events"]
        assert len(events) == 5
        assert len({event["data"]["message_id"] for event in events}) == 5
    finally:
        await other.close()


async def test_migration_backfills_pending_once_and_never_rebuilds_pruned_events(tmp_path):
    path = str(tmp_path / "pre-events.db")
    with sqlite3.connect(path) as connection:
        connection.execute(INBOX_SCHEMA)
        for archived in (0, 1):
            connection.execute("INSERT INTO inbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (f"old-{archived}", "alice", "bob", "inbox", "old-thread", 1, "T1", '{}', '{}', None,
                                "2026-01-01T00:00:00+00:00", archived, "2026-01-02T00:00:00+00:00" if archived else None))
    for iteration in range(2):
        db = Database(path)
        await db.initialize()
        try:
            rows = await db.conn.execute_fetchall("SELECT envelope_json FROM event_log")
            if iteration == 0:
                import json
                assert len(rows) == 1
                assert json.loads(rows[0][0])["message_id"] == "old-0"
                assert json.loads(rows[0][0])["conversation_id"] == "old-thread"
                await db.conn.execute("UPDATE event_log SET created_at = 0")
                await db.conn.commit()
                await EventLog(db).checkpoint("bob")
            else:
                assert rows == []
                assert (await db.conn.execute_fetchall("SELECT floor FROM event_state"))[0][0] == 1
            assert await InboxManager(db).pending_count("bob") == 1
        finally:
            await db.close()


async def test_tombstone_prevents_regeneration_after_event_and_inbox_retention(tmp_db):
    inbox = InboxManager(tmp_db)
    envelope = message()
    await inbox.deliver(envelope)
    await inbox.archive("bob", envelope.message_id)
    await tmp_db.conn.execute("UPDATE event_log SET created_at = 0")
    await tmp_db.conn.execute("UPDATE inbox SET archived_at = '2000-01-01T00:00:00+00:00'")
    await tmp_db.conn.commit()
    checkpoint = await EventLog(tmp_db).checkpoint("bob")
    await inbox.cleanup_expired()
    with pytest.raises(IdempotencyConflict, match="expired"):
        await inbox.deliver(envelope)
    assert await inbox.get_message("bob", envelope.message_id) is None
    assert (await EventLog(tmp_db).read("bob", checkpoint))["events"] == []
    assert (await tmp_db.conn.execute_fetchall("SELECT COUNT(*) FROM event_log"))[0][0] == 0


@pytest.mark.parametrize("limit", [0, 101, True, "2"])
async def test_read_limit_validation(tmp_db, limit):
    log = EventLog(tmp_db)
    checkpoint = await log.checkpoint("bob")
    with pytest.raises(ValueError):
        await log.read("bob", checkpoint, limit=limit)
