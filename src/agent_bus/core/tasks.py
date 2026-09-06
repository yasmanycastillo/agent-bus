from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from agent_bus.reputation.database import Database
from agent_bus.types import Task, TaskStatus


class TaskDependencyError(ValueError):
    """Base exception for task dependency errors."""


class CycleDetectedError(TaskDependencyError):
    """Raised when a dependency cycle is detected."""


class MissingDependencyError(TaskDependencyError):
    """Raised when a task depends on a nonexistent task."""


def _detect_cycles(graph: dict[str, list[str]]) -> list[str] | None:
    """Check for cycles in a directed graph using 3-color DFS.
    
    Returns the cycle path as a list of task IDs if found, or None.
    """
    state: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=visited
    cycle_path: list[str] = []

    def dfs(node: str, path: list[str]) -> bool:
        state[node] = 1
        path.append(node)
        for neighbor in graph.get(node, []):
            if state.get(neighbor, 0) == 1:
                try:
                    cycle_start = path.index(neighbor)
                    cycle_path.extend(path[cycle_start:] + [neighbor])
                except ValueError:
                    cycle_path.extend(path + [neighbor])
                return True
            elif state.get(neighbor, 0) == 0:
                if dfs(neighbor, path):
                    return True
        path.pop()
        state[node] = 2
        return False

    for node in sorted(graph.keys()):
        if state.get(node, 0) == 0:
            if dfs(node, []):
                return cycle_path
    return None


async def _validate_dag(
    conn: aiosqlite.Connection,
    new_tasks: list[dict[str, Any]],
) -> None:
    """Validate that all dependencies exist and do not form cycles."""
    rows = await conn.execute_fetchall("SELECT task_id, depends_on FROM tasks")
    graph: dict[str, list[str]] = {}
    for row in rows:
        t_id = row["task_id"] if hasattr(row, "keys") and not isinstance(row, (tuple, list)) else row[0]
        raw_deps = row["depends_on"] if hasattr(row, "keys") and not isinstance(row, (tuple, list)) else (row[1] if len(row) > 1 else "[]")
        deps = json.loads(raw_deps) if isinstance(raw_deps, str) else (raw_deps or [])
        graph[t_id] = list(deps)

    for t in new_tasks:
        graph[t["task_id"]] = list(t.get("depends_on") or [])

    for t in new_tasks:
        t_id = t["task_id"]
        deps = t.get("depends_on") or []
        for dep in deps:
            if dep == t_id:
                raise CycleDetectedError(f"Task '{t_id}' cannot depend on itself")
            if dep not in graph:
                raise MissingDependencyError(f"Task '{t_id}' depends on non-existent task '{dep}'")

    cycle = _detect_cycles(graph)
    if cycle:
        raise CycleDetectedError(f"Cyclic dependency detected: {' -> '.join(cycle)}")


class TaskManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        task_id: str,
        title: str,
        description: str | None = None,
        owner: str = "free",
        acceptance_criteria: list[str] | None = None,
        test_cmd: list[str] | None = None,
        depends_on: list[str] | None = None,
        operation_key: str | None = None,
    ) -> Task:
        if operation_key:
            existing = await self._db.conn.execute_fetchall(
                "SELECT * FROM tasks WHERE operation_key = ? LIMIT 1",
                (operation_key,),
            )
            if existing:
                return self._row_to_task(existing[0])

        existing_task = await self.get(task_id)
        if existing_task is not None:
            return existing_task

        task_dict = {
            "task_id": task_id,
            "title": title,
            "description": description,
            "owner": owner,
            "acceptance_criteria": acceptance_criteria or [],
            "test_cmd": test_cmd,
            "depends_on": depends_on or [],
            "operation_key": operation_key,
        }
        res = await self.create_batch([task_dict], operation_key=operation_key)
        return res[0]

    async def create_batch(
        self,
        tasks: list[dict[str, Any]],
        operation_key: str | None = None,
    ) -> list[Task]:
        if not tasks:
            return []

        if operation_key:
            existing = await self._db.conn.execute_fetchall(
                "SELECT * FROM tasks WHERE operation_key = ? ORDER BY rowid",
                (operation_key,),
            )
            if existing:
                return [self._row_to_task(row) for row in existing]

        batch_ids = [t["task_id"] for t in tasks]
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("Duplicate task_id within batch")

        await _validate_dag(self._db.conn, tasks)

        all_deps: set[str] = set()
        for t in tasks:
            all_deps.update(t.get("depends_on") or [])

        done_task_ids: set[str] = set()
        if all_deps:
            placeholders = ",".join("?" * len(all_deps))
            rows = await self._db.conn.execute_fetchall(
                f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) AND status = 'done'",
                tuple(all_deps),
            )
            done_task_ids = {r["task_id"] if hasattr(r, "keys") and not isinstance(r, (tuple, list)) else r[0] for r in rows}

        now = datetime.now(timezone.utc).isoformat()
        created_tasks: list[Task] = []

        await self._db.conn.execute("BEGIN IMMEDIATE")
        try:
            for t in tasks:
                task_id = t["task_id"]
                title = t["title"]
                description = t.get("description")
                owner = t.get("owner", "free")
                acceptance_criteria = t.get("acceptance_criteria") or []
                test_cmd = t.get("test_cmd")
                depends_on = t.get("depends_on") or []
                op_key = operation_key or t.get("operation_key")

                deps_satisfied = all(dep in done_task_ids for dep in depends_on)
                if owner == "free":
                    initial_status = "pending" if (not depends_on or deps_satisfied) else "blocked"
                else:
                    initial_status = "in_progress"

                test_cmd_json = json.dumps(test_cmd) if test_cmd is not None else None

                await self._db.conn.execute(
                    """INSERT OR IGNORE INTO tasks
                       (task_id, title, description, owner, status, locked_files, created_at, updated_at,
                        acceptance_criteria, test_cmd, depends_on, operation_key)
                       VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)""",
                    (
                        task_id,
                        title,
                        description,
                        owner,
                        initial_status,
                        now,
                        now,
                        json.dumps(acceptance_criteria),
                        test_cmd_json,
                        json.dumps(depends_on),
                        op_key,
                    ),
                )
            await self._db.conn.commit()
        except BaseException:
            await self._db.conn.rollback()
            raise

        for t in tasks:
            task = await self.get(t["task_id"])
            if task:
                created_tasks.append(task)
        return created_tasks

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
        ready_only: bool = False,
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
        tasks = [self._row_to_task(row) for row in cursor]

        if ready_only:
            all_dep_ids: set[str] = set()
            for t in tasks:
                all_dep_ids.update(t.depends_on)
            done_map: set[str] = set()
            if all_dep_ids:
                placeholders = ",".join("?" * len(all_dep_ids))
                rows = await self._db.conn.execute_fetchall(
                    f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) AND status = 'done'",
                    tuple(all_dep_ids),
                )
                done_map = {r["task_id"] if hasattr(r, "keys") and not isinstance(r, (tuple, list)) else r[0] for r in rows}
            tasks = [t for t in tasks if t.status != TaskStatus.BLOCKED and all(d in done_map for d in t.depends_on)]

        return tasks

    async def claim(self, task_id: str, agent_id: str) -> Task | None:
        """Claim a free pending task once; retries, competing claims, and unmet dependencies conflict."""
        task = await self.get(task_id)
        if not task:
            return None
        if task.owner != "free" or task.status != TaskStatus.PENDING:
            return None

        if task.depends_on:
            placeholders = ",".join("?" * len(task.depends_on))
            rows = await self._db.conn.execute_fetchall(
                f"SELECT COUNT(*) FROM tasks WHERE task_id IN ({placeholders}) AND status = 'done'",
                tuple(task.depends_on),
            )
            done_count = rows[0]["COUNT(*)"] if hasattr(rows[0], "keys") and not isinstance(rows[0], (tuple, list)) else rows[0][0]
            if done_count < len(task.depends_on):
                return None

        now = datetime.now(timezone.utc).isoformat()
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
        if new_owner == "free":
            task = await self.get(task_id)
            is_blocked = False
            if task and task.depends_on:
                placeholders = ",".join("?" * len(task.depends_on))
                rows = await self._db.conn.execute_fetchall(
                    f"SELECT COUNT(*) FROM tasks WHERE task_id IN ({placeholders}) AND status = 'done'",
                    tuple(task.depends_on),
                )
                done_count = rows[0]["COUNT(*)"] if hasattr(rows[0], "keys") and not isinstance(rows[0], (tuple, list)) else rows[0][0]
                if done_count < len(task.depends_on):
                    is_blocked = True
            status_sql = ", status = 'blocked'" if is_blocked else ", status = 'pending'"
        else:
            status_sql = ", status = 'in_progress'"

        await self._db.conn.execute(
            "UPDATE tasks SET owner = ?, updated_at = ?" + status_sql
            + " WHERE task_id = ?",
            (new_owner, now, task_id),
        )
        await self._db.conn.commit()
        return await self.get(task_id)

    async def complete(self, task_id: str, actor: str | None = None) -> Task | None:
        now = datetime.now(timezone.utc).isoformat()
        condition = " AND owner = ? AND status IN ('in_progress', 'in_review')" if actor else " AND status != 'done'"
        params = (now, task_id, actor) if actor else (now, task_id)
        rows = await self._db.conn.execute_fetchall(
            "UPDATE tasks SET status = 'done', updated_at = ? WHERE task_id = ?"
            + condition + " RETURNING *", params,
        )
        if not rows:
            await self._db.conn.commit()
            return None

        # Unblock any blocked tasks whose dependencies are now all 'done'
        blocked_rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM tasks WHERE status = 'blocked'"
        )
        for b_row in blocked_rows:
            b_task = self._row_to_task(b_row)
            if not b_task.depends_on:
                await self._db.conn.execute(
                    "UPDATE tasks SET status = 'pending', updated_at = ? WHERE task_id = ? AND status = 'blocked'",
                    (now, b_task.task_id),
                )
            else:
                placeholders = ",".join("?" * len(b_task.depends_on))
                done_rows = await self._db.conn.execute_fetchall(
                    f"SELECT COUNT(*) FROM tasks WHERE task_id IN ({placeholders}) AND status = 'done'",
                    tuple(b_task.depends_on),
                )
                done_count = done_rows[0]["COUNT(*)"] if hasattr(done_rows[0], "keys") and not isinstance(done_rows[0], (tuple, list)) else done_rows[0][0]
                if done_count == len(b_task.depends_on):
                    await self._db.conn.execute(
                        "UPDATE tasks SET status = 'pending', updated_at = ? WHERE task_id = ? AND status = 'blocked'",
                        (now, b_task.task_id),
                    )

        await self._db.conn.commit()
        return self._row_to_task(rows[0])

    async def submit_review(self, task_id: str, actor: str | None = None) -> Task | None:
        """Move owned work to the serialized integration queue."""
        now = datetime.now(timezone.utc).isoformat()
        condition = " AND owner = ?" if actor else ""
        params = (now, task_id, actor) if actor else (now, task_id)
        rows = await self._db.conn.execute_fetchall(
            "UPDATE tasks SET status = 'in_review', updated_at = ? "
            "WHERE task_id = ? AND status = 'in_progress'" + condition + " RETURNING *", params,
        )
        await self._db.conn.commit()
        return self._row_to_task(rows[0]) if rows else None

    async def block(self, task_id: str, reason: str | None = None) -> Task | None:
        """Mark a task as blocked."""
        now = datetime.now(timezone.utc).isoformat()
        rows = await self._db.conn.execute_fetchall(
            "UPDATE tasks SET status = 'blocked', updated_at = ? WHERE task_id = ? AND status != 'done' RETURNING *",
            (now, task_id),
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

                if new_owner == "free":
                    dep_rows = connection.execute("SELECT depends_on FROM tasks WHERE task_id = ?", (task_id,)).fetchall()
                    is_blocked = False
                    if dep_rows and dep_rows[0][0]:
                        deps = json.loads(dep_rows[0][0])
                        if deps:
                            placeholders = ",".join("?" * len(deps))
                            c = connection.execute(
                                f"SELECT COUNT(*) FROM tasks WHERE task_id IN ({placeholders}) AND status = 'done'",
                                tuple(deps),
                            ).fetchone()[0]
                            if c < len(deps):
                                is_blocked = True
                    status = ", status = 'blocked'" if is_blocked else ", status = 'pending'"
                else:
                    status = ", status = 'in_progress'"

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
    def _row_to_task(row: tuple | aiosqlite.Row | dict) -> Task:
        if hasattr(row, "keys") and not isinstance(row, (tuple, list)):
            keys = row.keys()
            task_id = row["task_id"]
            title = row["title"]
            description = row["description"]
            owner = row["owner"]
            status = row["status"]
            locked_files = row["locked_files"]
            created_at = row["created_at"]
            updated_at = row["updated_at"]
            acceptance_raw = row["acceptance_criteria"] if "acceptance_criteria" in keys else "[]"
            test_cmd_raw = row["test_cmd"] if "test_cmd" in keys else None
            depends_on_raw = row["depends_on"] if "depends_on" in keys else "[]"
            operation_key = row["operation_key"] if "operation_key" in keys else None
        else:
            task_id = row[0]
            title = row[1]
            description = row[2]
            owner = row[3]
            status = row[4]
            locked_files = row[5]
            created_at = row[6]
            updated_at = row[7]
            acceptance_raw = row[8] if len(row) > 8 else "[]"
            test_cmd_raw = row[9] if len(row) > 9 else None
            depends_on_raw = row[10] if len(row) > 10 else "[]"
            operation_key = row[11] if len(row) > 11 else None

        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)

        acceptance_criteria = json.loads(acceptance_raw) if isinstance(acceptance_raw, str) else (acceptance_raw or [])
        if isinstance(test_cmd_raw, str):
            try:
                parsed = json.loads(test_cmd_raw)
                test_cmd = parsed if isinstance(parsed, list) else [str(parsed)]
            except Exception:
                test_cmd = [test_cmd_raw]
        else:
            test_cmd = test_cmd_raw

        depends_on = json.loads(depends_on_raw) if isinstance(depends_on_raw, str) else (depends_on_raw or [])
        locked = json.loads(locked_files) if isinstance(locked_files, str) else (locked_files or [])

        return Task(
            task_id=task_id,
            title=title,
            description=description,
            owner=owner,
            status=TaskStatus(status),
            locked_files=locked,
            acceptance_criteria=acceptance_criteria,
            test_cmd=test_cmd,
            depends_on=depends_on,
            operation_key=operation_key,
            created_at=created_at,
            updated_at=updated_at,
        )
