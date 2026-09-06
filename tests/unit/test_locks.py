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
    assert lock.session_id == "legacy:claude"
    assert lock.acquisition_id
    assert await lm.get_lock("src/main.py") == lock


async def test_acquire_already_locked(tmp_db: Database):
    lm = LockManager(tmp_db)
    await lm.acquire("src/main.py", "claude")
    with pytest.raises(LockError, match="locked by"):
        await lm.acquire("src/main.py", "codex")


async def test_release(tmp_db: Database):
    lm = LockManager(tmp_db)
    lock = await lm.acquire("src/main.py", "claude")
    await lm.release("src/main.py", "claude", acquisition_id=lock.acquisition_id)
    assert await lm.get_lock("src/main.py") is None


async def test_release_wrong_owner(tmp_db: Database):
    lm = LockManager(tmp_db)
    lock = await lm.acquire("src/main.py", "claude")
    with pytest.raises(LockError, match="Only"):
        await lm.release("src/main.py", "codex", acquisition_id=lock.acquisition_id)
    assert await lm.get_lock("src/main.py") == lock


async def test_release_nonexistent(tmp_db: Database):
    lm = LockManager(tmp_db)
    await lm.release("nonexistent.py", "claude", acquisition_id="previous-acquisition")


async def test_list_locks(tmp_db: Database):
    lm = LockManager(tmp_db)
    await lm.acquire("a.py", "claude")
    await lm.acquire("b.py", "codex")
    assert len(await lm.list_locks()) == 2


async def test_acquire_retry_by_owner_conflicts(tmp_db: Database):
    lm = LockManager(tmp_db)
    original = await lm.acquire("src/main.py", "claude", "original")
    with pytest.raises(LockError, match="locked by"):
        await lm.acquire("src/main.py", "claude", "retry")
    assert await lm.get_lock("src/main.py") == original


async def test_release_retry_is_idempotent(tmp_db: Database):
    lm = LockManager(tmp_db)
    lock = await lm.acquire("src/main.py", "claude")
    await lm.release("src/main.py", "claude", acquisition_id=lock.acquisition_id)
    await lm.release("src/main.py", "claude", acquisition_id=lock.acquisition_id)
    assert await lm.get_lock("src/main.py") is None
