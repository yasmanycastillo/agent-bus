from __future__ import annotations

import json

from agent_bus.reputation.database import Database
from agent_bus.types import AgentSkill


class SkillRegistry:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def assign_role(
        self,
        agent_id: str,
        role: str,
        responsibilities: list[str] | None = None,
        audit_questions: list[str] | None = None,
    ) -> AgentSkill:
        await self._db.conn.execute(
            """INSERT OR REPLACE INTO agent_skills
               (agent_id, role, responsibilities, audit_questions)
               VALUES (?, ?, ?, ?)""",
            (
                agent_id,
                role,
                json.dumps(responsibilities or []),
                json.dumps(audit_questions or []),
            ),
        )
        await self._db.conn.commit()
        return AgentSkill(
            agent_id=agent_id,
            role=role,
            responsibilities=responsibilities or [],
            audit_questions=audit_questions or [],
        )

    async def get_roles(self, agent_id: str) -> list[AgentSkill]:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM agent_skills WHERE agent_id = ?", (agent_id,)
        )
        return [self._row_to_skill(row) for row in cursor]

    async def get_audit_questions(self, agent_id: str) -> list[str]:
        roles = await self.get_roles(agent_id)
        questions: list[str] = []
        for role in roles:
            questions.extend(role.audit_questions)
        return questions

    @staticmethod
    def _row_to_skill(row: tuple) -> AgentSkill:
        return AgentSkill(
            agent_id=row[0],
            role=row[1],
            responsibilities=json.loads(row[2]),
            audit_questions=json.loads(row[3]),
        )
