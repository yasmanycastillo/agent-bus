"""Confirmed human instructions and the assignments a coordinator makes from them."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from agent_bus.reputation.database import Database

PROVIDERS = frozenset({"hermes", "grok", "claude", "codex", "agy"})
ROLES = frozenset({"plan", "implement", "review"})
ROLE_REQUIREMENTS = {
    "plan": ["architecture"],
    "implement": ["implementation"],
    "review": ["code-review"],
}


class InstructionError(ValueError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


class InstructionLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def ensure_schema(self) -> None:
        await self._db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS instructions (
                instruction_id TEXT PRIMARY KEY,
                coordinator_agent_id TEXT NOT NULL,
                body TEXT NOT NULL,
                agents_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_assignments (
                assignment_id TEXT PRIMARY KEY,
                instruction_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                role TEXT NOT NULL,
                title TEXT NOT NULL
            );
            """
        )
        await self._db.conn.commit()

    async def submit(self, coordinator: str, text: str, confirmed: bool, agents: list[dict]) -> dict:
        cleaned = text.strip()
        if not confirmed:
            raise InstructionError("the human has not confirmed this instruction")
        if not cleaned:
            raise InstructionError("the instruction is empty")
        roster = _roster(agents)
        instruction_id = f"ins-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """INSERT INTO instructions (instruction_id, coordinator_agent_id, body, agents_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (instruction_id, coordinator, cleaned, json.dumps(roster), now),
        )
        await self._db.conn.commit()
        return {"instruction_id": instruction_id, "coordinator": coordinator, "agents": roster}

    async def get(self, instruction_id: str) -> dict | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM instructions WHERE instruction_id = ?", (instruction_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "instruction_id": row["instruction_id"],
            "coordinator": row["coordinator_agent_id"],
            "body": row["body"],
            "agents": json.loads(row["agents_json"]),
        }

    async def assignments(self, instruction_id: str) -> list[dict]:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM work_assignments WHERE instruction_id = ? ORDER BY assignment_id",
            (instruction_id,),
        )
        return [dict(row) for row in rows]

    async def add_assignment(self, instruction_id: str, agent_id: str, provider: str, role: str, title: str, task_id: str) -> None:
        await self._db.conn.execute(
            """INSERT INTO work_assignments
               (assignment_id, instruction_id, task_id, agent_id, provider, role, title)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (f"asg-{uuid.uuid4().hex[:12]}", instruction_id, task_id, agent_id, provider, role, title),
        )
        await self._db.conn.commit()


def _roster(agents: list[dict]) -> list[dict]:
    if not isinstance(agents, list) or not agents:
        raise InstructionError("name the agents who can do this work")
    roster = []
    seen = set()
    for item in agents:
        if not isinstance(item, dict):
            raise InstructionError("each agent needs an agent_id and a provider")
        agent_id = item.get("agent_id")
        provider = item.get("provider")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise InstructionError("each agent needs an agent_id")
        if provider not in PROVIDERS:
            raise InstructionError("provider must be hermes, grok, claude, codex, or agy")
        if agent_id in seen:
            raise InstructionError(f"{agent_id} is listed twice")
        seen.add(agent_id)
        roster.append({"agent_id": agent_id, "provider": provider})
    return roster


async def assign_work(bus, instruction_id: str, assignee: str, role: str, title: str) -> dict:
    from agent_bus.types import Envelope, MessageType

    log = InstructionLog(bus.db)
    stored = await log.get(instruction_id)
    if stored is None:
        raise InstructionError("instruction not found", 404)
    roster = {item["agent_id"]: item["provider"] for item in stored["agents"]}
    if assignee not in roster:
        raise InstructionError(f"{assignee} is not available for this instruction", 409)
    if role not in ROLES:
        raise InstructionError("role must be plan, implement, or review")
    name = title.strip()
    if not name:
        raise InstructionError("the coordinator must name this part of the work")
    existing = await log.assignments(instruction_id)
    conflict = role_conflict(existing, assignee, role)
    if conflict:
        raise InstructionError(conflict, 409)
    implement_ids = [row["task_id"] for row in existing if row["role"] == "implement"]
    task_id = f"{instruction_id}-{role}-{assignee}"
    description = (
        f"{stored['body']}\n\nParte: {name}\nPapel: {role}\nAgente: {assignee} ({roster[assignee]})"
    )
    await bus.tasks.create(
        task_id, name, description, owner=assignee,
        requirements=ROLE_REQUIREMENTS[role],
        independent_from=implement_ids if role == "review" else [],
    )
    if role == "implement":
        for row in existing:
            if row["role"] != "review":
                continue
            task = await bus.tasks.get(row["task_id"])
            if task is None or task_id in task.independent_from:
                continue
            await bus.db.conn.execute(
                "UPDATE tasks SET independent_from = ? WHERE task_id = ?",
                (json.dumps([*task.independent_from, task_id]), row["task_id"]),
            )
        await bus.db.conn.commit()
    await log.add_assignment(instruction_id, assignee, roster[assignee], role, name, task_id)
    await bus.inbox.send(Envelope(
        from_agent=stored["coordinator"],
        to_agent=assignee,
        message_type=MessageType.INBOX,
        reply_needed=True,
        related_task=task_id,
        body={"text": stored["body"], "title": name, "role": role},
    ), [assignee])
    return {"task_id": task_id, "owner": assignee, "role": role, "provider": roster[assignee], "instruction_id": instruction_id}


def role_conflict(existing: list[dict], agent_id: str, role: str) -> str | None:
    roles = {row["role"] for row in existing if row["agent_id"] == agent_id}
    if role == "review" and "implement" in roles:
        return f"{agent_id} writes this work and cannot review it"
    if role == "implement" and "review" in roles:
        return f"{agent_id} reviews this work and cannot write it"
    return None
