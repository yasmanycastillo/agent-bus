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
                title TEXT NOT NULL,
                covers TEXT
            );
            """
        )
        columns = {row["name"] for row in await self._db.conn.execute_fetchall("PRAGMA table_info(work_assignments)")}
        if "covers" not in columns:
            await self._db.conn.execute("ALTER TABLE work_assignments ADD COLUMN covers TEXT")
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

    async def for_agent(self, agent_id: str) -> list[dict]:
        await self.ensure_schema()
        rows = await self._db.conn.execute_fetchall(
            """SELECT task_id, role, agent_id, title FROM work_assignments
               WHERE agent_id = ? ORDER BY assignment_id""",
            (agent_id,),
        )
        return [dict(row) for row in rows]

    async def holder(self, task_id: str) -> dict | None:
        await self.ensure_schema()
        rows = await self._db.conn.execute_fetchall(
            "SELECT agent_id, role FROM work_assignments WHERE task_id = ? ORDER BY assignment_id",
            (task_id,),
        )
        return dict(rows[0]) if rows else None

    async def add_assignment(
        self, instruction_id: str, agent_id: str, provider: str, role: str, title: str, task_id: str,
        covers: list[str] | None = None,
    ) -> None:
        """covers: implementations a review is limited to; None means every implementation."""
        await self._db.conn.execute(
            """INSERT INTO work_assignments
               (assignment_id, instruction_id, task_id, agent_id, provider, role, title, covers)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"asg-{uuid.uuid4().hex[:12]}", instruction_id, task_id, agent_id, provider, role, title,
             None if covers is None else json.dumps(covers)),
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


async def assign_work(
    bus, instruction_id: str, assignee: str, role: str, title: str, reviews: list[str] | None = None,
    actor: str | None = None,
) -> dict:
    """reviews limits a review to these implementation task_ids; None covers every implementation.

    actor is the authenticated caller; only the instruction's coordinator may hand out its work.
    None means an administrator or an unsigned development hub.
    """
    from agent_bus.types import Envelope, MessageType

    log = InstructionLog(bus.db)
    stored = await log.get(instruction_id)
    if stored is None:
        raise InstructionError(f"instruction {instruction_id} does not exist in project {bus.project_id}", 404)
    if actor is not None and actor != stored["coordinator"]:
        raise InstructionError(f"only {stored['coordinator']}, who coordinates {instruction_id}, can assign its work", 403)
    roster = {item["agent_id"]: item["provider"] for item in stored["agents"]}
    if assignee not in roster:
        raise InstructionError(f"{assignee} is not available for this instruction", 409)
    if role not in ROLES:
        raise InstructionError("role must be plan, implement, or review")
    name = title.strip()
    if not name:
        raise InstructionError("the coordinator must name this part of the work")
    existing = await log.assignments(instruction_id)
    implement_ids = [row["task_id"] for row in existing if row["role"] == "implement"]
    if reviews is not None:
        if role != "review":
            raise InstructionError("reviews applies only to role review")
        if not reviews:
            raise InstructionError("reviews must name at least one implementation task_id")
        unknown = [item for item in reviews if item not in implement_ids]
        if unknown:
            raise InstructionError(
                f"{', '.join(unknown)} is not an implementation of instruction {instruction_id}; "
                f"implementations: {', '.join(implement_ids) or 'none yet'}"
            )
        implement_ids = list(dict.fromkeys(reviews))
    conflict = role_conflict(existing, assignee, role, implement_ids if reviews is not None else None)
    if conflict:
        raise InstructionError(conflict, 409)
    task_id = f"{instruction_id}-{role}-{assignee}"
    description = (
        f"{stored['body']}\n\nParte: {name}\nPapel: {role}\nAgente: {assignee} ({roster[assignee]})"
    )
    await bus.tasks.create(
        task_id, name, description, owner=assignee,
        requirements=ROLE_REQUIREMENTS[role],
    )
    # A review covers (independent_from) and waits for (depends_on) the implementations
    # it names, or every implementation of the instruction whichever was assigned first.
    if role == "review":
        await bus.tasks.hold_for_review(task_id, implement_ids)
    elif role == "implement":
        for row in existing:
            if row["role"] == "review" and row["covers"] is None:
                await bus.tasks.hold_for_review(row["task_id"], [task_id])
    await log.add_assignment(instruction_id, assignee, roster[assignee], role, name, task_id,
                             implement_ids if reviews is not None else None)
    # A coordinator assigning itself already sees the task in my_pending_items;
    # a message to itself would only sit there unacknowledged.
    if assignee != stored["coordinator"]:
        await bus.inbox.send(Envelope(
            from_agent=stored["coordinator"],
            to_agent=assignee,
            message_type=MessageType.INBOX,
            reply_needed=True,
            related_task=task_id,
            body={"text": stored["body"], "title": name, "role": role},
        ), [assignee])
    return {"task_id": task_id, "owner": assignee, "role": role, "provider": roster[assignee], "instruction_id": instruction_id}


def role_conflict(existing: list[dict], agent_id: str, role: str, covers: list[str] | None = None) -> str | None:
    """Reviewer and implementer stay apart on the implementations a review covers.

    covers None means the review covers the whole instruction, as do reviews stored without covers.
    """
    own = [row for row in existing if row["agent_id"] == agent_id]
    if role == "review" and covers is not None:
        mine = [row["task_id"] for row in own if row["role"] == "implement" and row["task_id"] in covers]
        if mine:
            return (f"{agent_id} implements {', '.join(mine)} and cannot review it: "
                    "the reviewer must not implement the work under review; assign the review to another agent")
        return None
    roles = {row["role"] for row in own if row["role"] != "review" or row.get("covers") is None}
    if role == "review" and "implement" in roles:
        return (f"{agent_id} implements part of this instruction and cannot review it: "
                "the reviewer must not implement any part of the same instruction; assign the review to another agent")
    if role == "implement" and "review" in roles:
        return (f"{agent_id} reviews this instruction and cannot implement part of it: "
                "the reviewer must not implement any part of the same instruction; assign it to another agent")
    return None
