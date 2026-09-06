from __future__ import annotations

import aiosqlite
from pathlib import Path


INBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
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
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS reputation (
    agent_id TEXT PRIMARY KEY,
    score REAL DEFAULT 0.5,
    accuracy REAL DEFAULT 0.5,
    honesty REAL DEFAULT 0.5,
    energy REAL DEFAULT 0.5,
    last_updated REAL
);

CREATE TABLE IF NOT EXISTS endorsements (
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    timestamp REAL NOT NULL,
    PRIMARY KEY (from_agent, to_agent)
);

""" + INBOX_SCHEMA + """

CREATE INDEX IF NOT EXISTS idx_inbox_to_agent ON inbox(to_agent, archived);
CREATE INDEX IF NOT EXISTS idx_inbox_timestamp ON inbox(timestamp);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    owner TEXT DEFAULT 'free',
    status TEXT DEFAULT 'pending',
    locked_files TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    context TEXT NOT NULL,
    decision TEXT NOT NULL,
    alternatives TEXT DEFAULT '[]',
    consequences TEXT,
    decided_by TEXT NOT NULL,
    supersedes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    responsibilities TEXT DEFAULT '[]',
    audit_questions TEXT DEFAULT '[]',
    PRIMARY KEY (agent_id, role)
);

CREATE TABLE IF NOT EXISTS locks (
    file_path TEXT PRIMARY KEY,
    locked_by TEXT NOT NULL,
    locked_at TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS kickoff (
    step INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    result TEXT,
    completed_by TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        try:
            await self._connection.executescript(SCHEMA)
            await self._migrate_inbox_deliveries()
        except BaseException:
            await self.close()
            raise

    async def _migrate_inbox_deliveries(self) -> None:
        # Serialize schema inspection and migration across hub initializations.
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            columns = await self.conn.execute_fetchall("PRAGMA table_info(inbox)")
            primary_key = [
                row["name"]
                for row in sorted(columns, key=lambda row: row["pk"])
                if row["pk"]
            ]
            if primary_key == ["message_id"]:
                await self.conn.execute("ALTER TABLE inbox RENAME TO inbox_legacy")
                await self.conn.execute(INBOX_SCHEMA)
                await self.conn.execute("INSERT INTO inbox SELECT * FROM inbox_legacy")
                await self.conn.execute("DROP TABLE inbox_legacy")
                # The old indexes followed the renamed table and were dropped with it.
                await self.conn.execute(
                    "CREATE INDEX idx_inbox_to_agent ON inbox(to_agent, archived)"
                )
                await self.conn.execute(
                    "CREATE INDEX idx_inbox_timestamp ON inbox(timestamp)"
                )
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._connection

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()
            self._connection = None
