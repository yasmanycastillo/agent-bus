from __future__ import annotations

import time

from agent_bus.reputation.database import Database
from agent_bus.reputation.manager import MetricsUpdate, ReputationManager


class EndorsementTracker:
    def __init__(self, db: Database, reputation: ReputationManager) -> None:
        self._db = db
        self._reputation = reputation

    async def endorse(self, from_agent: str, to_agent: str, weight: float = 1.0) -> None:
        now = time.time()
        await self._db.conn.execute(
            """INSERT OR REPLACE INTO endorsements (from_agent, to_agent, weight, timestamp)
               VALUES (?, ?, ?, ?)""",
            (from_agent, to_agent, weight, now),
        )
        await self._db.conn.commit()

        # Endorsing increases the endorser's honesty (trustworthy judgment)
        await self._reputation.update(from_agent, MetricsUpdate(honesty_delta=0.01))
        # Being endorsed increases the target's accuracy
        await self._reputation.update(to_agent, MetricsUpdate(accuracy_delta=0.02 * weight))

    async def get_endorsements(self, agent_id: str) -> list[dict]:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT from_agent, weight, timestamp FROM endorsements WHERE to_agent = ?",
            (agent_id,),
        )
        return [
            {"from_agent": row[0], "weight": row[1], "timestamp": row[2]}
            for row in cursor
        ]

    async def get_endorsements_given(self, agent_id: str) -> list[dict]:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT to_agent, weight, timestamp FROM endorsements WHERE from_agent = ?",
            (agent_id,),
        )
        return [
            {"to_agent": row[0], "weight": row[1], "timestamp": row[2]}
            for row in cursor
        ]
