from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from agent_bus.core.tasks import TaskManager


@pytest.mark.asyncio
async def test_create_never_returns_uncommitted_shared_connection_state(tmp_db):
    manager = TaskManager(tmp_db)
    now = "2026-01-01T00:00:00+00:00"
    await tmp_db.conn.execute(
        """INSERT INTO tasks
           (task_id, title, owner, status, locked_files, created_at, updated_at,
            acceptance_criteria, depends_on)
           VALUES ('ghost', 'foreign', 'free', 'pending', '[]', ?, ?, '[]', '[]')""",
        (now, now),
    )

    creation = asyncio.create_task(manager.create("ghost", "durable"))
    await asyncio.sleep(0.05)
    assert not creation.done()

    await tmp_db.conn.rollback()
    created = await asyncio.wait_for(creation, timeout=3)
    assert created.title == "durable"
    assert (await manager.get("ghost")) == created


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "create",
        "claim",
        "reassign",
        "complete",
        "submit_review",
        "block",
        "lock_files",
        "unlock_files",
    ],
)
async def test_task_mutations_never_commit_the_shared_connection(
    tmp_db, monkeypatch, mutation: str
):
    """Every task write owns a dedicated transaction instead of committing foreign work."""
    manager = TaskManager(tmp_db)
    task_id = f"TASK-{mutation}"

    if mutation != "create":
        owner = "free" if mutation == "claim" else "alice"
        await manager.create(task_id, mutation, owner=owner)
        if mutation == "unlock_files":
            await manager.lock_files(task_id, ["before.py"])

    async def forbidden_commit() -> None:
        raise AssertionError("task mutation committed the shared connection")

    monkeypatch.setattr(tmp_db.conn, "commit", forbidden_commit)

    operations: dict[str, Callable[[], Awaitable[object]]] = {
        "create": lambda: manager.create(task_id, mutation),
        "claim": lambda: manager.claim(task_id, "alice"),
        "reassign": lambda: manager.reassign(task_id, "bob"),
        "complete": lambda: manager.complete(task_id, actor="alice"),
        "submit_review": lambda: manager.submit_review(task_id, actor="alice"),
        "block": lambda: manager.block(task_id, actor="alice"),
        "lock_files": lambda: manager.lock_files(task_id, ["after.py"], actor="alice"),
        "unlock_files": lambda: manager.unlock_files(task_id),
    }

    result = await operations[mutation]()
    assert result is not None


@pytest.mark.asyncio
async def test_create_on_an_existing_id_returns_it_without_revalidating_dependencies(tmp_db):
    """Re-creating a task stays idempotent: the stored task wins over a conflicting request."""
    manager = TaskManager(tmp_db)
    first = await manager.create("A", "first")
    await manager.create("B", "second", depends_on=["A"])
    assert await manager.create("A", "again", depends_on=["B"]) == first  # would be a cycle
    assert await manager.create("A", "again", depends_on=["ZZ"]) == first  # missing dependency
