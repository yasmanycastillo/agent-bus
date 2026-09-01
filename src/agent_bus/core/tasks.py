from __future__ import annotations

import json
from datetime import datetime, timezone

from agent_bus.reputation.database import Database
from agent_bus.types import Task, TaskStatus


class TaskManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        task_id: str,
        title: str,
        description: str | None = None,
        owner: str = "free",
    ) -> Task:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute_insert(
            """INSERT OR IGNORE INTO tasks
               (task_id, title, description, owner, status, locked_files, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', '[]', ?, ?)""",
            (task_id, title, description, owner, now, now),
        )
        await self._db.conn.commit()
        return (await self.get(task_id))  # type: ignore[return-value]

    async def get(self, task_id: str) -> Task | None:
        cursor = await self._db.conn.execute_fetchall(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        )
        if not cursor:
            return None
        return self._row_to_task(cursor[0])

    async def list_all(
        self,
        status: TaskStatus | None = None,
        owner: str | None = None,
    ) -> list[Task]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[str] = []
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if owner:
            query += " AND owner = ?"
            params.append(owner)
        query += " ORDER BY task_id"
        cursor = await self._db.conn.execute_fetchall(query, params)
        return [self._row_to_task(row) for row in cursor]

    async def claim(self, task_id: str, agent_id: str) -> Task | None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """UPDATE tasks SET owner = ?, status = 'in_progress', updated_at = ?
               WHERE task_id = ? AND owner = 'free'""",
            (agent_id, now, task_id),
        )
        await self._db.conn.commit()
        return await self.get(task_id)

    async def reassign(self, task_id: str, new_owner: str) -> Task | None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """UPDATE tasks SET owner = ?, updated_at = ?
               WHERE task_id = ?""",
            (new_owner, now, task_id),
        )
        await self._db.conn.commit()
        return await self.get(task_id)

    async def complete(self, task_id: str) -> Task | None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """UPDATE tasks SET status = 'done', updated_at = ?
               WHERE task_id = ?""",
            (now, task_id),
        )
        await self._db.conn.commit()
        return await self.get(task_id)

    async def lock_files(self, task_id: str, paths: list[str]) -> Task | None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """UPDATE tasks SET locked_files = ?, updated_at = ?
               WHERE task_id = ?""",
            (json.dumps(paths), now, task_id),
        )
        await self._db.conn.commit()
        return await self.get(task_id)

    async def unlock_files(self, task_id: str) -> Task | None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """UPDATE tasks SET locked_files = '[]', updated_at = ?
               WHERE task_id = ?""",
            (now, task_id),
        )
        await self._db.conn.commit()
        return await self.get(task_id)

    @staticmethod
    def _row_to_task(row: tuple) -> Task:
        return Task(
            task_id=row[0],
            title=row[1],
            description=row[2],
            owner=row[3],
            status=TaskStatus(row[4]),
            locked_files=json.loads(row[5]),
            created_at=datetime.fromisoformat(row[6]),
            updated_at=datetime.fromisoformat(row[7]),
        )
