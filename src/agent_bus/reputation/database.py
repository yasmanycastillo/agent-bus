from __future__ import annotations

import aiosqlite
import secrets
import re
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
CREATE TABLE IF NOT EXISTS project_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    project_id TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS inbox_delivery_state (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    failed_at TEXT,
    UNIQUE(message_id, to_agent)
);
CREATE INDEX IF NOT EXISTS idx_delivery_agent_sequence ON inbox_delivery_state(to_agent, sequence);

CREATE TABLE IF NOT EXISTS event_log (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(message_id, to_agent)
);
CREATE INDEX IF NOT EXISTS idx_events_recipient_id ON event_log(to_agent, event_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON event_log(created_at);

CREATE TABLE IF NOT EXISTS event_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    floor INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO event_state(singleton, floor) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS message_idempotency (
    from_agent TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    recipients_json TEXT NOT NULL,
    PRIMARY KEY(from_agent, idempotency_key)
);

CREATE TABLE IF NOT EXISTS message_state (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('agent', 'admin')),
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    task_id TEXT,
    actor_agent_id TEXT NOT NULL,
    actor_session_id TEXT NOT NULL,
    previous_owner TEXT,
    new_owner TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
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
CREATE INDEX IF NOT EXISTS idx_tasks_operation_key ON tasks(operation_key);

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
    reason TEXT,
    session_id TEXT,
    acquisition_id TEXT,
    expires_at REAL
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


class ProjectMismatchError(ValueError):
    """A database belongs to a different project; no cross-project adoption."""


class Database:
    def __init__(self, db_path: str, project_id: str | None = None) -> None:
        self.db_path = db_path
        self.project_id = project_id
        self._connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        try:
            await self._connection.executescript(SCHEMA)
            if self.project_id is not None:
                await self.bind_project(self.project_id)
            await self._migrate_inbox_deliveries()
            await self._migrate_lock_leases()
            await self._migrate_tasks()
        except BaseException:
            await self.close()
            raise

    async def bind_project(self, project_id: str) -> None:
        """Adopt an unbound legacy database once, or verify its durable owner.

        The entire check/write runs in one aiosqlite worker callback. Independent
        connections serialize first adoption with BEGIN IMMEDIATE. A first bind
        never commits another caller's pending transaction.
        """
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project_id):
            raise ValueError("Invalid project identity")

        def binding(connection):
            def existing():
                row = connection.execute("SELECT project_id FROM project_metadata WHERE singleton = 1").fetchone()
                if row is not None and row[0] != project_id:
                    raise ProjectMismatchError("Database belongs to another project")
                return row is not None

            if existing():
                return
            if connection.in_transaction:
                raise RuntimeError("First project binding requires a committed database")
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not existing():
                    if connection.execute("SELECT 1 FROM sessions WHERE project_id != ? LIMIT 1", (project_id,)).fetchone():
                        raise ProjectMismatchError("Existing sessions belong to another project")
                    connection.execute("INSERT INTO project_metadata(singleton, project_id) VALUES (1, ?)", (project_id,))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        # Private driver access is confined here to keep transaction ownership
        # indivisible with respect to other coroutines on this shared connection.
        await self.conn._execute(binding, self.conn._conn)

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
            # Keep the original 13-column schema for the earlier composite-key
            # migration, then extend both old and current databases in place.
            columns = await self.conn.execute_fetchall("PRAGMA table_info(inbox)")
            if "conversation_id" not in {row["name"] for row in columns}:
                await self.conn.execute("ALTER TABLE inbox ADD COLUMN conversation_id TEXT")
                await self.conn.execute(
                    "UPDATE inbox SET conversation_id = COALESCE(correlation_id, message_id)"
                )
            await self.conn.execute(
                "INSERT OR IGNORE INTO inbox_delivery_state(message_id, to_agent) "
                "SELECT message_id, to_agent FROM inbox ORDER BY timestamp, message_id, to_agent"
            )
            await self.conn.execute(
                "INSERT OR IGNORE INTO message_state(name, value) VALUES ('cursor_secret', ?)",
                (secrets.token_hex(32),),
            )
            delivery_columns = await self.conn.execute_fetchall("PRAGMA table_info(inbox_delivery_state)")
            if "event_recorded" not in {row["name"] for row in delivery_columns}:
                await self.conn.execute(
                    "ALTER TABLE inbox_delivery_state ADD COLUMN event_recorded INTEGER NOT NULL DEFAULT 0"
                )
            # Runs within this schema migration transaction and only once.
            from agent_bus.core.events import backfill_events
            await self.conn._execute(backfill_events, self.conn._conn)
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    async def _migrate_lock_leases(self) -> None:
        """Legacy four-column locks expire; restart never expires valid leases."""
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row["name"] for row in await self.conn.execute_fetchall("PRAGMA table_info(locks)")}
            for name, kind in (("session_id", "TEXT"), ("acquisition_id", "TEXT"), ("expires_at", "REAL")):
                if name not in columns:
                    await self.conn.execute(f"ALTER TABLE locks ADD COLUMN {name} {kind}")
            await self.conn.execute(
                "UPDATE locks SET expires_at=0 WHERE session_id IS NULL OR acquisition_id IS NULL OR expires_at IS NULL"
            )
            await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_locks_expiry ON locks(expires_at)")
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    async def _migrate_tasks(self) -> None:
        """Ensure tasks table has acceptance_criteria, test_cmd, depends_on, operation_key columns."""
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row["name"] for row in await self.conn.execute_fetchall("PRAGMA table_info(tasks)")}
            new_cols = [
                ("acceptance_criteria", "TEXT DEFAULT '[]'"),
                ("test_cmd", "TEXT"),
                ("depends_on", "TEXT DEFAULT '[]'"),
                ("operation_key", "TEXT"),
            ]
            for name, col_def in new_cols:
                if name not in columns:
                    await self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {col_def}")
            await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_operation_key ON tasks(operation_key)")
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
