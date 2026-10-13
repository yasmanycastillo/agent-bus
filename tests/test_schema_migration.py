import sqlite3
from pathlib import Path

import pytest

from agent_bus.reputation.database import Database


OLD_SCHEMA = """
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    owner TEXT DEFAULT 'free',
    status TEXT DEFAULT 'pending',
    locked_files TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    acceptance_criteria TEXT DEFAULT '[]',
    test_cmd TEXT,
    depends_on TEXT DEFAULT '[]',
    operation_key TEXT
);
CREATE TABLE inbox (
    message_id TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    message_type TEXT NOT NULL,
    correlation_id TEXT,
    reply_needed INTEGER DEFAULT 0,
    related_task TEXT,
    body TEXT,
    metadata TEXT,
    signature TEXT,
    timestamp TEXT NOT NULL,
    archived INTEGER DEFAULT 0,
    archived_at TEXT,
    PRIMARY KEY (message_id, to_agent)
);
CREATE TABLE reviews (
    review_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    reviewer_agent_id TEXT NOT NULL,
    reviewer_session_id TEXT NOT NULL,
    sha TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence TEXT DEFAULT '{}',
    test_results TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def _legacy(path):
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.execute(
        """INSERT INTO tasks (task_id, title, description, owner, status, locked_files, created_at, updated_at)
           VALUES ('T-old', 'Keep me', 'legacy', 'alice', 'pending', '[]', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"""
    )
    connection.execute(
        """INSERT INTO inbox
           (message_id, from_agent, to_agent, message_type, body, timestamp)
           VALUES ('m-old', 'alice', 'bob', 'inbox', '{\"text\": \"keep this message\"}', '2026-01-01T00:00:00+00:00')"""
    )
    connection.execute(
        """INSERT INTO reviews
           (review_id, task_id, reviewer_agent_id, reviewer_session_id, sha, verdict, reason, created_at)
           VALUES ('r-old', 'T-old', 'reviewer', 'sess', 'abc', 'approve', 'keep this review', '2026-01-01T00:00:00+00:00')"""
    )
    connection.commit()
    connection.close()


def _columns(path, table):
    connection = sqlite3.connect(path)
    names = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    connection.close()
    return names


def _cell(path, sql):
    connection = sqlite3.connect(path)
    value = connection.execute(sql).fetchone()[0]
    connection.close()
    return value


@pytest.mark.asyncio
async def test_legacy_database_keeps_rows_and_gains_task_columns(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy(path)
    for _ in range(2):
        db = Database(str(path), project_id="alpha")
        await db.initialize()
        await db.close()
    columns = _columns(path, "tasks")
    assert "requirements" in columns
    assert "independent_from" in columns
    assert "blocked_reason" in columns
    assert columns.count("requirements") == 1
    assert _cell(path, "SELECT title FROM tasks WHERE task_id = 'T-old'") == "Keep me"
    assert _cell(path, "SELECT requirements FROM tasks WHERE task_id = 'T-old'") == "[]"
    assert _cell(path, "SELECT independent_from FROM tasks WHERE task_id = 'T-old'") == "[]"
    assert _cell(path, "SELECT blocked_reason FROM tasks WHERE task_id = 'T-old'") is None
    assert _cell(path, "SELECT COUNT(*) FROM tasks") == 1
    assert "keep this message" in _cell(path, "SELECT body FROM inbox WHERE message_id = 'm-old'")
    assert _cell(path, "SELECT COUNT(*) FROM inbox") == 1
    assert _cell(path, "SELECT reason FROM reviews WHERE review_id = 'r-old'") == "keep this review"
    assert _cell(path, "SELECT COUNT(*) FROM reviews") == 1
    assert not Path(str(path) + ".migration-snapshot").exists()


@pytest.mark.asyncio
async def test_failed_migration_leaves_the_legacy_database_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    _legacy(path)

    async def fail(self):
        await self.conn.execute("ALTER TABLE tasks ADD COLUMN requirements TEXT DEFAULT '[]'")
        raise RuntimeError("migration failed halfway")

    monkeypatch.setattr(Database, "_migrate_tasks", fail)
    db = Database(str(path), project_id="alpha")
    with pytest.raises(RuntimeError, match="migration failed halfway"):
        await db.initialize()
    assert "requirements" not in _columns(path, "tasks")
    assert "independent_from" not in _columns(path, "tasks")
    assert "blocked_reason" not in _columns(path, "tasks")
    assert _cell(path, "SELECT title FROM tasks WHERE task_id = 'T-old'") == "Keep me"
    assert "keep this message" in _cell(path, "SELECT body FROM inbox WHERE message_id = 'm-old'")
    assert _cell(path, "SELECT reason FROM reviews WHERE review_id = 'r-old'") == "keep this review"
    assert not Path(str(path) + ".migration-snapshot").exists()
