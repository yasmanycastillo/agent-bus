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
               VALUES (?, ?, ?, ?, ?, '[]', ?, ?)""",
            (task_id, title, description, owner, "pending" if owner == "free" else "in_progress", now, now),
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
        """Claim a free pending task once; retries and competing claims conflict."""
        now = datetime.now(timezone.utc).isoformat()
        # Drain RETURNING in the same aiosqlite operation so another coroutine
        # sharing this connection can commit without an active SQL statement.
        rows = await self._db.conn.execute_fetchall(
            """UPDATE tasks SET owner = ?, status = 'in_progress', updated_at = ?
               WHERE task_id = ? AND owner = 'free' AND status = 'pending'
               RETURNING *""",
            (agent_id, now, task_id),
        )
        await self._db.conn.commit()
        return self._row_to_task(rows[0]) if rows else None

    async def reassign(self, task_id: str, new_owner: str) -> Task | None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """UPDATE tasks SET owner = ?, updated_at = ?
               WHERE task_id = ?""",
            (new_owner, now, task_id),
        )
        await self._db.conn.commit()
        return await self.get(task_id)

    async def complete(self, task_id: str, actor: str | None = None) -> Task | None:
        now = datetime.now(timezone.utc).isoformat()
        condition = " AND owner = ? AND status = 'in_progress'" if actor else ""
        params = (now, task_id, actor) if actor else (now, task_id)
        rows = await self._db.conn.execute_fetchall(
            "UPDATE tasks SET status = 'done', updated_at = ? WHERE task_id = ?"
            + condition + " RETURNING *", params,
        )
        await self._db.conn.commit()
        return self._row_to_task(rows[0]) if rows else None

    async def lock_files(self, task_id: str, paths: list[str], actor: str | None = None) -> Task | None:
        now = datetime.now(timezone.utc).isoformat()
        condition = " AND owner = ? AND status != 'done'" if actor else ""
        params = (json.dumps(paths), now, task_id)
        if actor:
            params += (actor,)
        rows = await self._db.conn.execute_fetchall(
            "UPDATE tasks SET locked_files = ?, updated_at = ? WHERE task_id = ?"
            + condition + " RETURNING *", params,
        )
        await self._db.conn.commit()
        return self._row_to_task(rows[0]) if rows else None

    async def transfer(
        self, task_id: str, new_owner: str, *, actor: str, session_id: str,
        require_owner: bool = False,
    ) -> Task | None:
        """Transfer a nonterminal task and audit it in one SQLite transaction.

        All statements run in one aiosqlite worker operation: other coroutines
        sharing the connection cannot commit between mutation and audit.
        """
        now = datetime.now(timezone.utc).isoformat()
        condition = "task_id = ? AND status != 'done'"
        params = (task_id,)
        if require_owner:
            condition += " AND owner = ?"
            params += (actor,)
        action = "handoff" if require_owner else "reassign"

        def transaction(connection):
            connection.execute("SAVEPOINT task_transfer")
            try:
                audit = connection.execute(
                    """INSERT INTO audit_log
                    (action, task_id, actor_agent_id, actor_session_id, previous_owner, new_owner, created_at)
                    SELECT ?, task_id, ?, ?, owner, ?, ? FROM tasks WHERE """ + condition,
                    (action, actor, session_id, new_owner, now) + params,
                )
                if audit.rowcount == 0:
                    connection.execute("RELEASE task_transfer")
                    return None
                status = ", status = 'pending'" if new_owner == "free" else ", status = 'in_progress'"
                rows = connection.execute(
                    "UPDATE tasks SET owner = ?, updated_at = ?" + status
                    + " WHERE " + condition + " RETURNING *",
                    (new_owner, now) + params,
                ).fetchall()
                connection.execute("RELEASE task_transfer")
                return rows[0]
            except BaseException:
                connection.execute("ROLLBACK TO task_transfer")
                connection.execute("RELEASE task_transfer")
                raise

        # aiosqlite has no public API for a multi-statement worker callback.
        # Keep this private-API dependency confined to this transaction boundary.
        row = await self._db.conn._execute(transaction, self._db.conn._conn)
        await self._db.conn.commit()
        return self._row_to_task(row) if row is not None else None

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
