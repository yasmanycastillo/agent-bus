from __future__ import annotations

from agent_bus.core.tasks import TaskManager
from agent_bus.reputation.database import Database
from agent_bus.types import TaskStatus


async def test_create_and_get(tmp_db: Database):
    tm = TaskManager(tmp_db)
    task = await tm.create("T1", "Setup CI", description="Configure pipeline")
    assert task.task_id == "T1"
    assert task.title == "Setup CI"
    assert task.owner == "free"
    assert task.status == TaskStatus.PENDING

    fetched = await tm.get("T1")
    assert fetched is not None
    assert fetched.title == "Setup CI"


async def test_claim(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("T1", "Task 1")
    claimed = await tm.claim("T1", "claude")
    assert claimed is not None
    assert claimed.owner == "claude"
    assert claimed.status == TaskStatus.IN_PROGRESS


async def test_claim_already_owned(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("T1", "Task 1", owner="codex")
    claimed = await tm.claim("T1", "claude")
    # Should not claim since owner != 'free'
    assert claimed is not None
    assert claimed.owner == "codex"


async def test_complete(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("T1", "Task 1")
    completed = await tm.complete("T1")
    assert completed is not None
    assert completed.status == TaskStatus.DONE


async def test_list_with_filters(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("T1", "Task 1", owner="claude")
    await tm.create("T2", "Task 2", owner="codex")
    await tm.create("T3", "Task 3")

    all_tasks = await tm.list_all()
    assert len(all_tasks) == 3

    claude_tasks = await tm.list_all(owner="claude")
    assert len(claude_tasks) == 1
    assert claude_tasks[0].task_id == "T1"


async def test_lock_and_unlock_files(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("T1", "Task 1")
    locked = await tm.lock_files("T1", ["src/main.py", "src/utils.py"])
    assert locked is not None
    assert len(locked.locked_files) == 2

    unlocked = await tm.unlock_files("T1")
    assert unlocked is not None
    assert len(unlocked.locked_files) == 0


async def test_get_nonexistent(tmp_db: Database):
    tm = TaskManager(tmp_db)
    assert await tm.get("NOPE") is None


async def test_reassign(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("T1", "Task 1", owner="claude")
    reassigned = await tm.reassign("T1", "antigravity")
    assert reassigned is not None
    assert reassigned.owner == "antigravity"
