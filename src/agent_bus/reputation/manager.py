from __future__ import annotations

import time
from dataclasses import dataclass

from agent_bus.reputation.database import Database


@dataclass
class MetricsUpdate:
    accuracy_delta: float = 0.0
    honesty_delta: float = 0.0
    energy_delta: float = 0.0


@dataclass
class ReputationRecord:
    agent_id: str
    score: float
    accuracy: float
    honesty: float
    energy: float
    last_updated: float


class ReputationManager:
    def __init__(
        self,
        db: Database,
        decay_factor: float = 0.95,
        initial_score: float = 0.5,
        accuracy_weight: float = 0.5,
        honesty_weight: float = 0.3,
        energy_weight: float = 0.2,
    ) -> None:
        self._db = db
        self.decay_factor = decay_factor
        self.initial_score = initial_score
        self.accuracy_weight = accuracy_weight
        self.honesty_weight = honesty_weight
        self.energy_weight = energy_weight

    async def get_score(self, agent_id: str) -> float:
        record = await self._get_record(agent_id)
        if not record:
            return self.initial_score
        age_seconds = time.time() - record.last_updated
        decayed_score = record.score * (self.decay_factor ** (age_seconds / 3600))
        return max(0.0, min(1.0, decayed_score))

    async def update(self, agent_id: str, metrics: MetricsUpdate) -> None:
        record = await self._get_record(agent_id)
        if record:
            accuracy = max(0.0, min(1.0, record.accuracy + metrics.accuracy_delta))
            honesty = max(0.0, min(1.0, record.honesty + metrics.honesty_delta))
            energy = max(0.0, min(1.0, record.energy + metrics.energy_delta))
        else:
            accuracy = max(0.0, min(1.0, self.initial_score + metrics.accuracy_delta))
            honesty = max(0.0, min(1.0, self.initial_score + metrics.honesty_delta))
            energy = max(0.0, min(1.0, self.initial_score + metrics.energy_delta))

        score = (
            accuracy * self.accuracy_weight
            + honesty * self.honesty_weight
            + energy * self.energy_weight
        )
        now = time.time()

        await self._db.conn.execute(
            """INSERT OR REPLACE INTO reputation (agent_id, score, accuracy, honesty, energy, last_updated)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent_id, score, accuracy, honesty, energy, now),
        )
        await self._db.conn.commit()

    async def get_all_scores(self) -> dict[str, float]:
        cursor = await self._db.conn.execute_fetchall("SELECT agent_id, score FROM reputation")
        result = {}
        now = time.time()
        for row in cursor:
            agent_id = row[0]
            base_score = row[1]
            # Get full record for proper decay
            record = await self._get_record(agent_id)
            if record:
                age = now - record.last_updated
                result[agent_id] = max(0.0, min(1.0, base_score * (self.decay_factor ** (age / 3600))))
            else:
                result[agent_id] = base_score
        return result

    async def get_leaderboard(self, limit: int = 10) -> list[ReputationRecord]:
        scores = await self.get_all_scores()
        cursor = await self._db.conn.execute_fetchall(
            "SELECT agent_id, score, accuracy, honesty, energy, last_updated FROM reputation"
        )
        records = []
        for row in cursor:
            records.append(
                ReputationRecord(
                    agent_id=row[0],
                    score=scores.get(row[0], row[1]),
                    accuracy=row[2],
                    honesty=row[3],
                    energy=row[4],
                    last_updated=row[5],
                )
            )
        records.sort(key=lambda r: r.score, reverse=True)
        return records[:limit]

    async def _get_record(self, agent_id: str) -> ReputationRecord | None:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT agent_id, score, accuracy, honesty, energy, last_updated FROM reputation WHERE agent_id = ?",
            (agent_id,),
        )
        if not cursor:
            return None
        row = cursor[0]
        return ReputationRecord(
            agent_id=row[0],
            score=row[1],
            accuracy=row[2],
            honesty=row[3],
            energy=row[4],
            last_updated=row[5],
        )
