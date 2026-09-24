"""Attempt record for the existing local worker. It does not start a second process."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from agent_bus.reputation.database import Database
from agent_bus.runtimes.protocol import (
    AttemptConflict,
    RuntimeMessage,
    RuntimeSession,
    RuntimeStartRequest,
)

TERMINAL = {"completed", "failed", "cancelled"}


class NativeRuntime:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self, request: RuntimeStartRequest) -> RuntimeSession:
        existing = await self._by_key(request.idempotency_key)
        if existing is not None:
            return existing
        unknown = await self._open_unknown(request.task_id)
        if unknown is not None:
            raise AttemptConflict(f"reconcile attempt {unknown.attempt_id} before starting another")
        now = _now()
        external = f"native:{request.agent_id}"
        await self._db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at)
               VALUES (?, ?, ?, 'started', ?, ?, ?)""",
            (request.attempt_id, request.task_id, request.idempotency_key, external, request.workspace_ref, now),
        )
        await self._db.conn.commit()
        return RuntimeSession(request.attempt_id, request.task_id, external, request.workspace_ref, "started")

    async def send(self, session: RuntimeSession, message: str) -> RuntimeMessage:
        message_id = f"rtm-{uuid.uuid4().hex[:12]}"
        await self._db.conn.execute(
            """INSERT INTO runtime_messages (message_id, attempt_id, text, created_at, consumed)
               VALUES (?, ?, ?, ?, 0)""",
            (message_id, session.attempt_id, message, _now()),
        )
        await self._db.conn.commit()
        return RuntimeMessage(session.attempt_id, message)

    async def consume_messages(self, task_id: str) -> list[str]:
        rows = await self._db.conn.execute_fetchall(
            """SELECT runtime_messages.message_id, runtime_messages.text
               FROM runtime_messages
               JOIN runtime_attempts ON runtime_attempts.attempt_id = runtime_messages.attempt_id
               WHERE runtime_attempts.task_id = ? AND runtime_messages.consumed = 0
               ORDER BY runtime_messages.created_at""",
            (task_id,),
        )
        if not rows:
            return []
        ids = [row["message_id"] for row in rows]
        placeholders = ",".join("?" * len(ids))
        await self._db.conn.execute(
            f"UPDATE runtime_messages SET consumed = 1 WHERE message_id IN ({placeholders})",
            tuple(ids),
        )
        await self._db.conn.commit()
        return [row["text"] for row in rows]

    async def cancel(self, session: RuntimeSession) -> None:
        await self._set_state(session.attempt_id, "cancelled")

    async def status(self, attempt_id: str) -> RuntimeSession:
        row = await self._db.conn.execute_fetchall(
            "SELECT * FROM runtime_attempts WHERE attempt_id = ?", (attempt_id,),
        )
        if not row:
            raise KeyError(attempt_id)
        return _session(row[0])

    async def reconcile(self, attempt_id: str, outcome: str) -> RuntimeSession:
        if outcome not in TERMINAL:
            raise ValueError(f"outcome must be one of {sorted(TERMINAL)}")
        current = await self.status(attempt_id)
        if current.state != "unknown" and current.state not in TERMINAL:
            raise AttemptConflict(f"attempt {attempt_id} is {current.state}")
        if current.state in TERMINAL:
            return current
        await self._set_state(attempt_id, outcome)
        return await self.status(attempt_id)

    async def mark_unknown(self, attempt_id: str) -> None:
        await self._set_state(attempt_id, "unknown")

    async def _by_key(self, key: str) -> RuntimeSession | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM runtime_attempts WHERE idempotency_key = ?", (key,),
        )
        return _session(rows[0]) if rows else None

    async def _open_unknown(self, task_id: str) -> RuntimeSession | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM runtime_attempts WHERE task_id = ? AND state = 'unknown' ORDER BY updated_at DESC LIMIT 1",
            (task_id,),
        )
        return _session(rows[0]) if rows else None

    async def _set_state(self, attempt_id: str, state: str) -> None:
        await self._db.conn.execute(
            "UPDATE runtime_attempts SET state = ?, updated_at = ? WHERE attempt_id = ?",
            (state, _now(), attempt_id),
        )
        await self._db.conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session(row) -> RuntimeSession:
    return RuntimeSession(
        row["attempt_id"], row["task_id"], row["external_ref"], row["workspace_ref"], row["state"],
    )
