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
        existing = await self.get_lock(file_path)
        if existing:
            raise LockError(
                f"File '{file_path}' is locked by '{existing.locked_by}'"
            )
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute_insert(
            "INSERT OR IGNORE INTO locks (file_path, locked_by, locked_at, reason) VALUES (?, ?, ?, ?)",
            (file_path, agent_id, now, reason),
        )
        await self._db.conn.commit()
        return (await self.get_lock(file_path))  # type: ignore[return-value]

    async def release(self, file_path: str, agent_id: str) -> None:
        existing = await self.get_lock(file_path)
        if not existing:
            return
        if existing.locked_by != agent_id:
            raise LockError(
                f"Only '{existing.locked_by}' can release lock on '{file_path}'"
            )
        await self._db.conn.execute("DELETE FROM locks WHERE file_path = ?", (file_path,))
        await self._db.conn.commit()

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
