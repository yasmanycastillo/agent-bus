from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timezone

from agent_bus.reputation.database import Database
from agent_bus.types import Lock


class LockError(Exception):
    pass


class LockBusyError(LockError):
    """No mutation occurred: retry after another database transaction completes."""


class LockManager:
    """Leases on caller-canonicalized paths, fenced by a unique acquisition ID."""

    def __init__(self, db: Database, *, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock

    @staticmethod
    def _session(agent_id: str, session_id: str | None) -> str:
        # Only internal legacy callers may omit a session. HTTP requires one.
        return session_id if session_id is not None else f"legacy:{agent_id}"

    @staticmethod
    def _expiry(now: float, ttl_seconds: int, session_expires_at: float | None) -> float:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 3600:
            raise LockError("Lock TTL must be an integer between 1 and 3600 seconds")
        expiry = now + ttl_seconds
        if session_expires_at is not None:
            if (isinstance(session_expires_at, bool)
                    or not isinstance(session_expires_at, (int, float))
                    or not math.isfinite(session_expires_at)
                    or session_expires_at <= now):
                raise LockError("Cannot acquire or renew a lock with an expired session")
            expiry = min(expiry, session_expires_at)
        return expiry

    async def _mutate(self, operation):
        """Consume SQL cursors and commit in one queued driver operation.

        aiosqlite exposes no public multi-statement callback. Keeping this private
        API at one boundary prevents another coroutine's commit from interleaving
        a conditional mutation and its follow-up ownership check.
        """
        def transaction(connection):
            if connection.in_transaction:
                raise LockBusyError("Lock mutation requires a committed database; retry shortly")
            connection.execute("SAVEPOINT lock_lease")
            try:
                # Reserve SQLite write ownership before sampling the clock: a
                # blocked writer must not compare expiry against a stale time.
                connection.execute("UPDATE locks SET expires_at=expires_at WHERE 0")
                result = operation(connection)
            except BaseException:
                connection.execute("ROLLBACK TO lock_lease")
                connection.execute("RELEASE lock_lease")
                raise
            # With no outer transaction, releasing the savepoint commits the
            # acquisition before any other queued connection operation can run.
            connection.execute("RELEASE lock_lease")
            return result
        return await self._db.conn._execute(transaction, self._db.conn._conn)

    async def acquire(
        self, file_path: str, agent_id: str, reason: str | None = None, *,
        session_id: str | None = None, ttl_seconds: int = 300,
        session_expires_at: float | None = None,
    ) -> Lock:
        session = self._session(agent_id, session_id)
        acquisition = secrets.token_urlsafe(24)

        def operation(connection):
            now = self._clock()
            expiry = self._expiry(now, ttl_seconds, session_expires_at)
            rows = connection.execute(
                "INSERT INTO locks (file_path, locked_by, locked_at, reason, session_id, acquisition_id, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(file_path) DO UPDATE SET "
                "locked_by=excluded.locked_by, locked_at=excluded.locked_at, reason=excluded.reason, "
                "session_id=excluded.session_id, acquisition_id=excluded.acquisition_id, expires_at=excluded.expires_at "
                "WHERE COALESCE(locks.expires_at, 0) <= ? RETURNING *",
                (file_path, agent_id, datetime.fromtimestamp(now, timezone.utc).isoformat(), reason,
                 session, acquisition, expiry, now),
            ).fetchall()
            if not rows:
                raise LockError(f"File '{file_path}' is locked by another acquisition")
            return rows[0]
        return self._row_to_lock(await self._mutate(operation))

    async def release(
        self, file_path: str, agent_id: str, *, session_id: str | None = None,
        acquisition_id: str,
    ) -> None:
        if not acquisition_id:
            raise LockError("An acquisition ID is required")
        session = self._session(agent_id, session_id)

        def operation(connection):
            now = self._clock()
            rows = connection.execute(
                "DELETE FROM locks WHERE file_path=? AND locked_by=? AND session_id=? "
                "AND acquisition_id=? AND expires_at>? RETURNING file_path",
                (file_path, agent_id, session, acquisition_id, now),
            ).fetchall()
            if not rows and connection.execute("SELECT 1 FROM locks WHERE file_path=?", (file_path,)).fetchone():
                raise LockError("Only the current, unexpired acquisition can release this lock")
        await self._mutate(operation)

    async def renew(
        self, file_path: str, agent_id: str, *, session_id: str | None = None,
        acquisition_id: str, ttl_seconds: int = 300, session_expires_at: float | None = None,
    ) -> Lock:
        if not acquisition_id:
            raise LockError("An acquisition ID is required")
        session = self._session(agent_id, session_id)

        def operation(connection):
            now = self._clock()
            expiry = self._expiry(now, ttl_seconds, session_expires_at)
            rows = connection.execute(
                "UPDATE locks SET expires_at=? WHERE file_path=? AND locked_by=? AND session_id=? "
                "AND acquisition_id=? AND expires_at>? RETURNING *",
                (expiry, file_path, agent_id, session, acquisition_id, now),
            ).fetchall()
            if not rows:
                raise LockError("Only the current, unexpired acquisition can renew this lock")
            return rows[0]
        return self._row_to_lock(await self._mutate(operation))

    async def get_lock(self, file_path: str) -> Lock | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM locks WHERE file_path=? AND expires_at>? "
            "AND session_id IS NOT NULL AND acquisition_id IS NOT NULL", (file_path, self._clock()),
        )
        return self._row_to_lock(rows[0]) if rows else None

    async def list_locks(self) -> list[Lock]:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM locks WHERE expires_at>? AND session_id IS NOT NULL "
            "AND acquisition_id IS NOT NULL ORDER BY locked_at, file_path", (self._clock(),),
        )
        return [self._row_to_lock(row) for row in rows]

    @staticmethod
    def _row_to_lock(row) -> Lock:
        return Lock(
            file_path=row["file_path"], locked_by=row["locked_by"],
            locked_at=datetime.fromisoformat(row["locked_at"]), reason=row["reason"],
            session_id=row["session_id"], acquisition_id=row["acquisition_id"],
            expires_at=datetime.fromtimestamp(row["expires_at"], timezone.utc),
        )
