from __future__ import annotations

import pytest

from agent_bus.core.locks import LockError, LockManager
from agent_bus.reputation.database import Database


async def test_acquire_and_get(tmp_db: Database):
    lm = LockManager(tmp_db)
    lock = await lm.acquire("src/main.py", "claude", "refactoring")
    assert lock.file_path == "src/main.py"
    assert lock.locked_by == "claude"
    assert lock.reason == "refactoring"

    fetched = await lm.get_lock("src/main.py")
    assert fetched is not None
    assert fetched.locked_by == "claude"


async def test_acquire_already_locked(tmp_db: Database):
    lm = LockManager(tmp_db)
    await lm.acquire("src/main.py", "claude")
    with pytest.raises(LockError, match="locked by"):
        await lm.acquire("src/main.py", "codex")


async def test_release(tmp_db: Database):
    lm = LockManager(tmp_db)
    await lm.acquire("src/main.py", "claude")
    await lm.release("src/main.py", "claude")
    assert await lm.get_lock("src/main.py") is None


async def test_release_wrong_owner(tmp_db: Database):
    lm = LockManager(tmp_db)
    await lm.acquire("src/main.py", "claude")
    with pytest.raises(LockError, match="Only"):
        await lm.release("src/main.py", "codex")


async def test_release_nonexistent(tmp_db: Database):
    lm = LockManager(tmp_db)
    # Should not raise, just silently return
    await lm.release("nonexistent.py", "claude")


async def test_list_locks(tmp_db: Database):
    lm = LockManager(tmp_db)
    await lm.acquire("a.py", "claude")
    await lm.acquire("b.py", "codex")
    locks = await lm.list_locks()
    assert len(locks) == 2
