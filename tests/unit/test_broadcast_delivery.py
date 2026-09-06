"""Durable broadcast deliveries and migration from the single-message primary key."""
from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.types import Envelope


async def test_broadcast_persists_for_three_offline_recipients_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    path = str(tmp_path / "bus.db")
    db = Database(path)
    await db.initialize()
    try:
        bus = MessageBus(db, AgentRegistry(), InboxManager(db))
        async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
            for agent in ("sender", "one", "two", "three"):
                response = await client.post("/register", json={"agent_id": agent, "display_name": agent})
                assert response.status_code == 201
            # No SSE/websocket subscribers: every recipient relies on persistence.
            response = await client.post("/messages", json={
                "from_agent": "sender", "message_type": "broadcast", "body": {"text": "hello"},
            })
            assert response.status_code == 200
            ids = response.json()["message_ids"]
            assert len(ids) == 3
            assert len(set(ids)) == 1
    finally:
        await db.close()

    reopened = Database(path)
    await reopened.initialize()
    try:
        inbox = InboxManager(reopened)
        assert await inbox.get_inbox("sender") == []
        for agent in ("one", "two", "three"):
            messages = await inbox.get_inbox(agent)
            assert len(messages) == 1
            assert messages[0].message_id == ids[0]
            assert messages[0].body == {"text": "hello"}
        await inbox.archive("one", ids[0])
        assert await inbox.pending_count("one") == 0
        assert len(await inbox.get_archived("one")) == 1
        for agent in ("two", "three"):
            assert await inbox.pending_count(agent) == 1
            assert await inbox.get_archived(agent) == []
        # Retrying delivery must neither duplicate nor resurrect archived rows.
        for agent in ("one", "two", "three"):
            envelope = await inbox.get_message(agent, ids[0])
            await inbox.deliver(envelope)
        assert await inbox.pending_count("one") == 0
        assert await inbox.pending_count("two") == 1
        assert await inbox.pending_count("three") == 1
        rows = await reopened.conn.execute_fetchall("SELECT COUNT(*) FROM inbox")
        assert rows[0][0] == 3
    finally:
        await reopened.close()


def create_legacy_database(path):
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE inbox (
            message_id TEXT PRIMARY KEY, from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL, message_type TEXT NOT NULL,
            correlation_id TEXT, reply_needed INTEGER DEFAULT 0, related_task TEXT,
            body TEXT, metadata TEXT, signature TEXT, timestamp TEXT NOT NULL,
            archived INTEGER DEFAULT 0, archived_at TEXT)""")
        conn.execute("CREATE INDEX idx_inbox_to_agent ON inbox(to_agent, archived)")
        conn.execute("CREATE INDEX idx_inbox_timestamp ON inbox(timestamp)")
        for archived in (0, 1):
            conn.execute("INSERT INTO inbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                f"legacy-{archived}", "sender", "one", "inbox", "thread", 1, "task-1",
                '{"text":"preserved"}', '{"key":42}', "signature", "2026-09-01T00:00:00+00:00",
                archived, "2026-09-02T00:00:00+00:00" if archived else None,
            ))
        return conn.execute("SELECT * FROM inbox ORDER BY message_id").fetchall()


async def test_legacy_migration_preserves_all_columns_and_indexes(tmp_path):
    path = str(tmp_path / "legacy.db")
    original = create_legacy_database(path)
    for _ in range(2):  # Restart is safe and must not rerun a destructive migration.
        db = Database(path)
        await db.initialize()
        try:
            rows = await db.conn.execute_fetchall("SELECT * FROM inbox ORDER BY message_id")
            assert [tuple(row)[:13] for row in rows] == original
            indexes = await db.conn.execute_fetchall("PRAGMA index_list(inbox)")
            assert {"idx_inbox_to_agent", "idx_inbox_timestamp"} <= {row[1] for row in indexes}
        finally:
            await db.close()
    db = Database(path)
    await db.initialize()
    try:
        inbox = InboxManager(db)
        original_message = await inbox.get_message("one", "legacy-0")
        await inbox.deliver(original_message.model_copy(update={"to_agent": "two"}))
        assert await inbox.pending_count("one") == 1
        assert await inbox.pending_count("two") == 1
        assert len(await inbox.get_archived("one")) == 1
    finally:
        await db.close()


async def test_migration_failure_rolls_back_schema_and_data(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    original = create_legacy_database(path)
    import aiosqlite

    execute = aiosqlite.Connection.execute

    def fail_copy(self, sql, parameters=None):
        if sql == "INSERT INTO inbox SELECT * FROM inbox_legacy":
            raise RuntimeError("injected migration failure")
        return execute(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_copy)
    db = Database(path)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        await db.initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM inbox ORDER BY message_id").fetchall() == original
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='inbox_legacy'").fetchall() == []
        assert [row[1] for row in conn.execute("PRAGMA table_info(inbox)") if row[5]] == ["message_id"]
