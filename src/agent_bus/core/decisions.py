from __future__ import annotations

import json
from datetime import datetime, timezone

from agent_bus.reputation.database import Database
from agent_bus.types import Decision


class DecisionLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        decision_id: str,
        title: str,
        context: str,
        decision: str,
        decided_by: str,
        alternatives: list[str] | None = None,
        consequences: str | None = None,
        supersedes: str | None = None,
    ) -> Decision:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute_insert(
            """INSERT OR IGNORE INTO decisions
               (decision_id, title, context, decision, alternatives, consequences,
                decided_by, supersedes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                title,
                context,
                decision,
                json.dumps(alternatives or []),
                consequences,
                decided_by,
                supersedes,
                now,
            ),
        )
        await self._db.conn.commit()
        return (await self.get(decision_id))  # type: ignore[return-value]

    async def get(self, decision_id: str) -> Decision | None:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        )
        if not cursor:
            return None
        return self._row_to_decision(cursor[0])

    async def list_all(self) -> list[Decision]:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM decisions ORDER BY created_at"
        )
        return [self._row_to_decision(row) for row in cursor]

    async def supersede(self, old_id: str, new_id: str) -> None:
        await self._db.conn.execute(
            "UPDATE decisions SET supersedes = ? WHERE decision_id = ?",
            (old_id, new_id),
        )
        await self._db.conn.commit()

    @staticmethod
    def _row_to_decision(row: tuple) -> Decision:
        return Decision(
            decision_id=row[0],
            title=row[1],
            context=row[2],
            decision=row[3],
            alternatives=json.loads(row[4]),
            consequences=row[5],
            decided_by=row[6],
            supersedes=row[7],
            created_at=datetime.fromisoformat(row[8]),
        )
