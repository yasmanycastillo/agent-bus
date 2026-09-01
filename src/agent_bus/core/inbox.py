from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from agent_bus.reputation.database import Database
from agent_bus.types import Envelope, MessageType


class InboxManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def deliver(self, envelope: Envelope) -> str:
        if not envelope.to_agent:
            raise ValueError("Cannot deliver message without to_agent")

        await self._db.conn.execute_insert(
            """INSERT OR IGNORE INTO inbox
               (message_id, from_agent, to_agent, message_type, correlation_id,
                reply_needed, related_task, body, metadata, signature, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                envelope.message_id,
                envelope.from_agent,
                envelope.to_agent,
                envelope.message_type.value,
                envelope.correlation_id,
                int(envelope.reply_needed),
                envelope.related_task,
                json.dumps(envelope.body) if envelope.body else None,
                json.dumps(envelope.metadata) if envelope.metadata else None,
                envelope.signature,
                envelope.timestamp.isoformat(),
            ),
        )
        await self._db.conn.commit()
        return envelope.message_id

    async def get_inbox(self, agent_id: str) -> list[Envelope]:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM inbox WHERE to_agent = ? AND archived = 0 ORDER BY timestamp",
            (agent_id,),
        )
        return [self._row_to_envelope(row) for row in cursor]

    async def get_message(self, agent_id: str, msg_id: str) -> Envelope | None:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM inbox WHERE to_agent = ? AND message_id = ?",
            (agent_id, msg_id),
        )
        if not cursor:
            return None
        return self._row_to_envelope(cursor[0])

    async def archive(self, agent_id: str, msg_id: str) -> None:
        await self._db.conn.execute(
            """UPDATE inbox SET archived = 1, archived_at = ?
               WHERE to_agent = ? AND message_id = ?""",
            (datetime.now(timezone.utc).isoformat(), agent_id, msg_id),
        )
        await self._db.conn.commit()

    async def get_archived(self, agent_id: str) -> list[Envelope]:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM inbox WHERE to_agent = ? AND archived = 1 ORDER BY timestamp DESC",
            (agent_id,),
        )
        return [self._row_to_envelope(row) for row in cursor]

    async def pending_count(self, agent_id: str) -> int:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT COUNT(*) FROM inbox WHERE to_agent = ? AND archived = 0",
            (agent_id,),
        )
        return cursor[0][0]

    async def cleanup_expired(self, max_age_days: int = 30) -> int:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT COUNT(*) FROM inbox WHERE archived = 1 AND archived_at < datetime('now', ?)",
            (f"-{max_age_days} days",),
        )
        count = cursor[0][0]
        await self._db.conn.execute(
            "DELETE FROM inbox WHERE archived = 1 AND archived_at < datetime('now', ?)",
            (f"-{max_age_days} days",),
        )
        await self._db.conn.commit()
        return count

    @staticmethod
    def _row_to_envelope(row: aiosqlite.Row | tuple) -> Envelope:
        if isinstance(row, aiosqlite.Row):
            values = tuple(row)
        else:
            values = row
        return Envelope(
            message_id=values[0],
            from_agent=values[1],
            to_agent=values[2],
            message_type=MessageType(values[3]),
            correlation_id=values[4],
            reply_needed=bool(values[5]),
            related_task=values[6],
            body=json.loads(values[7]) if values[7] else None,
            metadata=json.loads(values[8]) if values[8] else {},
            signature=values[9],
            timestamp=datetime.fromisoformat(values[10]),
        )
