from __future__ import annotations

import json
from datetime import datetime, timezone

from agent_bus.reputation.database import Database
from agent_bus.types import KICKOFF_STEPS, KickoffStep


class KickoffManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self) -> list[KickoffStep]:
        # Clear any previous kickoff
        await self._db.conn.execute("DELETE FROM kickoff")
        await self._db.conn.commit()

        for step, name in KICKOFF_STEPS:
            await self._db.conn.execute_insert(
                "INSERT INTO kickoff (step, name, status) VALUES (?, ?, 'pending')",
                (step, name),
            )
        await self._db.conn.commit()
        return await self.get_progress()

    async def complete_step(
        self,
        step: int,
        result: dict | None = None,
        completed_by: str | None = None,
    ) -> KickoffStep | None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """UPDATE kickoff SET status = 'done', result = ?, completed_by = ?, completed_at = ?
               WHERE step = ?""",
            (json.dumps(result) if result else None, completed_by, now, step),
        )
        await self._db.conn.commit()
        return await self._get_step(step)

    async def get_progress(self) -> list[KickoffStep]:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM kickoff ORDER BY step"
        )
        return [self._row_to_step(row) for row in cursor]

    async def is_complete(self) -> bool:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT COUNT(*) FROM kickoff WHERE status != 'done'"
        )
        return cursor[0][0] == 0

    async def _get_step(self, step: int) -> KickoffStep | None:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM kickoff WHERE step = ?", (step,)
        )
        if not cursor:
            return None
        return self._row_to_step(cursor[0])

    @staticmethod
    def _row_to_step(row: tuple) -> KickoffStep:
        return KickoffStep(
            step=row[0],
            name=row[1],
            status=row[2],
            result=json.loads(row[3]) if row[3] else None,
            completed_by=row[4],
            completed_at=datetime.fromisoformat(row[5]) if row[5] else None,
        )
