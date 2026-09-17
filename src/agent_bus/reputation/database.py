from __future__ import annotations

import aiosqlite
import asyncio
import os
import secrets
import re
import sqlite3
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit


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
CREATE TABLE IF NOT EXISTS coordination_operations (
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (session_id, kind, operation_key)
);

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

CREATE TABLE IF NOT EXISTS reviews (
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
CREATE INDEX IF NOT EXISTS idx_reviews_task_id ON reviews(task_id);
"""


class ProjectMismatchError(ValueError):
    """A database belongs to a different project; no cross-project adoption."""


class Database:
    def __init__(self, db_path: str, project_id: str | None = None) -> None:
        # Keep an anchor connection alive for temporary databases, so every
        # independent transaction sees the same schema and committed data.
        self.db_path = (
            f"file:agent-bus-{secrets.token_hex(16)}?mode=memory&cache=shared"
            if db_path in ("", ":memory:") else db_path
        )
        self._file_path: Path | None = None
        self._uri_parameters: list[tuple[str, str]] = []
        self._uri_mode = "rwc"
        if self.db_path.startswith("file:"):
            uri = urlsplit(self.db_path)
            if uri.netloc:
                raise ValueError("SQLite file URI authority is not supported")
            parameters = parse_qsl(uri.query, keep_blank_values=True)
            modes = [value for key, value in parameters if key == "mode"]
            if len(modes) > 1 or (modes and modes[0] not in {"memory", "ro", "rw", "rwc"}):
                raise ValueError("Unsupported or duplicate SQLite file URI mode")
            decoded_path = unquote(uri.path)
            if decoded_path == ":memory:" or modes == ["memory"]:
                caches = [value for key, value in parameters if key == "cache"]
                if any(value != "shared" for value in caches):
                    raise ValueError("In-memory transactions require cache=shared; cache=private is unsupported")
                if not caches:
                    base, marker, fragment = self.db_path.partition("#")
                    self.db_path = base + ("&" if "?" in base else "?") + "cache=shared" + marker + fragment
            else:
                self._file_path = Path(decoded_path).absolute()
                self._uri_parameters = parameters
                self._uri_mode = modes[0] if modes else "rwc"
        else:
            self._file_path = Path(self.db_path).absolute()
        self.project_id = project_id
        self._connection: aiosqlite.Connection | None = None
        self._workers: set[asyncio.Task] = set()
        self._closing = False
        self._close_task: asyncio.Task | None = None
        self._anchor_fd: int | None = None
        self._connect_path = self.db_path
        self._expected_identity: tuple[int, int] | None = None
        self._initialize_lock = asyncio.Lock()

    @staticmethod
    def _open_parent_directory(parent: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(parent.anchor or "/", flags)
        opened = [os.fstat(descriptor)]
        try:
            for component in parent.parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                opened.append(os.fstat(descriptor))
            current_uid = os.geteuid()
            private_barrier = any(
                item.st_uid == current_uid and item.st_mode & 0o077 == 0 for item in opened
            )
            writable_component = any(
                item.st_mode & 0o022 and not item.st_mode & stat.S_ISVTX for item in opened
            )
            final = opened[-1]
            final_is_sticky_writable = bool(final.st_mode & stat.S_ISVTX and final.st_mode & 0o022)
            final_is_controlled = final.st_uid == current_uid
            if (not final_is_controlled or final_is_sticky_writable
                    or (writable_component and not private_barrier)):
                raise PermissionError("Database parent directory is writable by other users")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _path_has_identity(path: Path, expected: tuple[int, int]) -> bool:
        try:
            current = path.lstat()
        except OSError:
            return False
        return stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == expected

    async def initialize(self) -> None:
        if self._closing and (self._close_task is None or not self._close_task.done()):
            raise RuntimeError("Database is closing")
        async with self._initialize_lock:
            await self._initialize()

    async def _initialize(self) -> None:
        if self._closing:
            if self._close_task is None or not self._close_task.done():
                raise RuntimeError("Database is closing")
            self._close_task.result()
            self._close_task = None
            self._closing = False
        if self._connection is not None:
            raise RuntimeError("Database is already initialized")
        descriptor: int | None = None
        parent_descriptor: int | None = None
        connect_path = self.db_path
        expected_identity: tuple[int, int] | None = None
        if self._file_path is not None:
            if not self.db_path.startswith("file:"):
                self.db_path = str(self._file_path)
            parent = self._file_path.parent
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if self._uri_mode == "ro":
                raise ValueError("Read-only SQLite databases cannot run writable migrations")
            parent_descriptor = self._open_parent_directory(parent)
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            if self._uri_mode == "rwc":
                flags |= os.O_CREAT
            try:
                descriptor = os.open(self._file_path.name, flags, 0o600, dir_fd=parent_descriptor)
            finally:
                os.close(parent_descriptor)
                parent_descriptor = None
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise ValueError("Database path must be a regular file")
                os.fchmod(descriptor, 0o600)
                expected_identity = (opened.st_dev, opened.st_ino)
                fd_root = next((root for root in (Path("/proc/self/fd"), Path("/dev/fd"))
                                if root.is_dir()), None)
                if fd_root is None:
                    raise RuntimeError("Secure SQLite file opening requires /proc/self/fd or /dev/fd")
                parameters = [(key, value) for key, value in self._uri_parameters if key != "mode"]
                parameters.append(("mode", "rw"))
                connect_path = f"file:{fd_root / str(descriptor)}?{urlencode(parameters)}"
            except BaseException:
                os.close(descriptor)
                raise
        try:
            try:
                self._connection = await aiosqlite.connect(connect_path, uri=True)
            except BaseException as exc:
                if (self._file_path is not None and expected_identity is not None
                        and not self._path_has_identity(self._file_path, expected_identity)):
                    raise RuntimeError("Database path changed during secure open") from exc
                raise
            if self._file_path is not None and expected_identity is not None:
                if not self._path_has_identity(self._file_path, expected_identity):
                    await self._connection.close()
                    self._connection = None
                    raise RuntimeError("Database path changed during secure open")
            self._anchor_fd = descriptor
            self._connect_path = connect_path
            self._expected_identity = expected_identity
            descriptor = None
        finally:
            if parent_descriptor is not None:
                os.close(parent_descriptor)
            if descriptor is not None:
                os.close(descriptor)
        self._connection.row_factory = aiosqlite.Row
        try:
            await self._connection.executescript(SCHEMA)
            if self.project_id is not None:
                await self.bind_project(self.project_id)
            await self._migrate_inbox_deliveries()
            await self._migrate_lock_leases()
            await self._migrate_tasks()
            await self._migrate_reviews()
            await self._migrate_worker_control()
        except BaseException:
            await self._close_locked()
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

    async def _migrate_reviews(self) -> None:
        """Ensure reviews table exists with idx_reviews_task_id."""
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            await self.conn.execute(
                """CREATE TABLE IF NOT EXISTS reviews (
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
                )"""
            )
            await self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reviews_task_id ON reviews(task_id)"
            )
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    async def _migrate_worker_control(self) -> None:
        """Ensure worker_control table exists (paused flag per agent, T-21)."""
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            await self.conn.execute(
                """CREATE TABLE IF NOT EXISTS worker_control (
                    agent_id TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )"""
            )
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    async def set_worker_paused(self, agent_id: str, paused: bool) -> None:
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            await self.conn.execute(
                """INSERT INTO worker_control (agent_id, paused, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(agent_id) DO UPDATE SET paused = excluded.paused, updated_at = excluded.updated_at""",
                (agent_id, int(paused), datetime.now(timezone.utc).isoformat()),
            )
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    async def is_worker_paused(self, agent_id: str) -> bool:
        rows = await self.conn.execute_fetchall(
            "SELECT paused FROM worker_control WHERE agent_id = ?", (agent_id,)
        )
        return bool(rows and rows[0]["paused"])

    async def list_paused_workers(self) -> list[str]:
        rows = await self.conn.execute_fetchall(
            "SELECT agent_id FROM worker_control WHERE paused = 1"
        )
        return [row["agent_id"] for row in rows]

    @staticmethod
    async def _wait_for_worker(worker):
        """Defer even repeated cancellation until the worker has cleaned up."""
        cancelled = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except BaseException:
                break
        if cancelled is not None:
            # Retrieve a possible failure so cancellation does not leak an
            # unobserved task exception. Cancellation takes precedence.
            try:
                worker.result()
            except BaseException:
                pass
            raise cancelled
        return worker.result()

    async def transaction(self, operation, *, write=True):
        """Run a synchronous callback in its own committed transaction.

        Connection creation, writer reservation, callback and cleanup all run
        in one worker job. No pending work on ``conn`` can absorb this commit.
        Cancellation waits for commit/rollback and connection closure before
        propagating. Read callbacks use a deferred, query-only snapshot.
        Callbacks must be DB-only: contention may replay the entire callback
        on a fresh connection after rollback, within a five-second retry budget.
        """
        if self._closing:
            raise RuntimeError("Database is closing or closed")
        self.conn  # Require an initialized database (including its memory anchor).
        if (self._file_path is not None and self._expected_identity is not None
                and not self._path_has_identity(self._file_path, self._expected_identity)):
            raise RuntimeError("Database path changed after initialization")
        connect_path = self._connect_path

        def runner():
            deadline = time.monotonic() + 5
            while True:
                # Own the busy wait here: SQLite's per-statement timeout would
                # otherwise add another five seconds at each attempt/stage.
                connection = sqlite3.connect(connect_path, uri=True, timeout=0)
                connection.row_factory = sqlite3.Row
                try:
                    if not write:
                        connection.execute("PRAGMA query_only = ON")
                    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    result = operation(connection)
                    connection.commit()
                    return result
                except BaseException as exc:
                    connection.rollback()
                    # Shared-cache memory tables can return SQLITE_LOCKED from
                    # the callback after BEGIN succeeds; file DBs can be busy
                    # at COMMIT. Replay only after discarding all partial work.
                    code = getattr(exc, "sqlite_errorcode", None)
                    if (not isinstance(exc, sqlite3.OperationalError)
                            or not isinstance(code, int)
                            or (code & 0xff) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    contention = exc
                finally:
                    connection.close()
                time.sleep(min(0.01, remaining))
                if time.monotonic() >= deadline:
                    raise contention

        worker = asyncio.create_task(asyncio.to_thread(runner))
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        return await self._wait_for_worker(worker)

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._connection

    async def close(self) -> None:
        # Close admission before waiting for initialize(), so new work cannot
        # overtake a scheduled close while the lifecycle lock is contended.
        self._closing = True
        async with self._initialize_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        if self._close_task is None:
            # Admission and worker registration contain no await, so close
            # cannot miss a transaction accepted on this event loop.
            self._closing = True

            async def finish():
                if self._workers:
                    await asyncio.gather(*self._workers, return_exceptions=True)
                if self._connection:
                    await self._connection.close()
                    self._connection = None
                if self._anchor_fd is not None:
                    os.close(self._anchor_fd)
                    self._anchor_fd = None

            self._close_task = asyncio.create_task(finish())
        await self._wait_for_worker(self._close_task)
