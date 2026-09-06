from __future__ import annotations

from datetime import datetime, timezone

from agent_bus.reputation.database import Database
from agent_bus.types import Lock


class LockError(Exception):
    pass


class LockManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def acquire(self, file_path: str, agent_id: str, reason: str | None = None) -> Lock:
        """Acquire once; retries conflict even when the agent already owns the lock."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.conn.execute(
            "INSERT INTO locks (file_path, locked_by, locked_at, reason) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(file_path) DO NOTHING",
            (file_path, agent_id, now, reason),
        )
        acquired = cursor.rowcount == 1
        await cursor.close()
        await self._db.conn.commit()
        if not acquired:
            existing = await self.get_lock(file_path)
            owner = existing.locked_by if existing else "another acquisition"
            raise LockError(f"File '{file_path}' is locked by '{owner}'; retry acquisition")
        return Lock(
            file_path=file_path,
            locked_by=agent_id,
            locked_at=datetime.fromisoformat(now),
            reason=reason,
        )

    async def release(self, file_path: str, agent_id: str) -> None:
        """Release only the caller's lock; an absent lock is an idempotent success."""
        cursor = await self._db.conn.execute(
            "DELETE FROM locks WHERE file_path = ? AND locked_by = ?", (file_path, agent_id)
        )
        released = cursor.rowcount == 1
        await cursor.close()
        await self._db.conn.commit()
        if released:
            return
        existing = await self.get_lock(file_path)
        if existing:
            raise LockError(
                f"Only '{existing.locked_by}' can release lock on '{file_path}'"
            )

    async def get_lock(self, file_path: str) -> Lock | None:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM locks WHERE file_path = ?", (file_path,)
        )
        if not cursor:
            return None
        return Lock(
            file_path=cursor[0][0],
            locked_by=cursor[0][1],
            locked_at=datetime.fromisoformat(cursor[0][2]),
            reason=cursor[0][3],
        )

    async def list_locks(self) -> list[Lock]:
        cursor = await self._db.conn.execute_fetchall("SELECT * FROM locks ORDER BY locked_at")
        return [
            Lock(
                file_path=row[0],
                locked_by=row[1],
                locked_at=datetime.fromisoformat(row[2]),
                reason=row[3],
            )
            for row in cursor
        ]
