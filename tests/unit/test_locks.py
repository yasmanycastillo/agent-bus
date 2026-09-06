from __future__ import annotations

import asyncio

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
    assert (await lm.get_lock("src/main.py")).locked_by == "claude"


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


async def test_acquire_retry_by_owner_conflicts(tmp_db: Database):
    lm = LockManager(tmp_db)
    original = await lm.acquire("src/main.py", "claude", "original")
    with pytest.raises(LockError, match="locked by"):
        await lm.acquire("src/main.py", "claude", "retry")
    assert await lm.get_lock("src/main.py") == original


async def test_release_retry_is_idempotent(tmp_db: Database):
    lm = LockManager(tmp_db)
    await lm.acquire("src/main.py", "claude")
    await lm.release("src/main.py", "claude")
    await lm.release("src/main.py", "claude")
    assert await lm.get_lock("src/main.py") is None


@pytest.mark.parametrize("attempt", range(5))
async def test_concurrent_acquire_has_one_winner(tmp_db: Database, attempt: int, monkeypatch):
    other_db = Database(tmp_db.db_path)
    await other_db.initialize()
    try:
        # Both contenders reach INSERT before either writes, including implementations
        # that pre-read ownership or use execute_insert instead of execute.
        ready = asyncio.Event()
        arrivals = 0

        def gate_insert(execute):
            async def gated(sql, parameters=None):
                nonlocal arrivals
                if sql.startswith("INSERT") and "INTO locks" in sql:
                    arrivals += 1
                    if arrivals == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), timeout=5)
                return await execute(sql, parameters)
            return gated

        for db in (tmp_db, other_db):
            for method in ("execute", "execute_insert"):
                monkeypatch.setattr(db.conn, method, gate_insert(getattr(db.conn, method)))
        managers = [LockManager(tmp_db), LockManager(other_db)]
        path = f"race-{attempt}.py"
        outcomes = await asyncio.gather(
            managers[0].acquire(path, "claude", "first"),
            managers[1].acquire(path, "codex", "second"),
            return_exceptions=True,
        )
        winners = [result for result in outcomes if not isinstance(result, Exception)]
        assert len(winners) == 1, outcomes
        assert sum(isinstance(result, LockError) for result in outcomes) == 1
        assert await managers[0].get_lock(path) == winners[0]
        assert await managers[1].get_lock(path) == winners[0]
    finally:
        await other_db.close()


async def test_stale_release_preserves_successor(tmp_db: Database, monkeypatch):
    """Replace ownership immediately before DELETE, after any old pre-read."""
    other_db = Database(tmp_db.db_path)
    await other_db.initialize()
    lm = LockManager(tmp_db)
    successor_manager = LockManager(other_db)
    await lm.acquire("src/main.py", "claude")
    original_execute = tmp_db.conn.execute
    replaced = False

    async def replace_before_delete(sql, parameters=None):
        nonlocal replaced
        if sql.startswith("DELETE FROM locks") and not replaced:
            replaced = True
            await successor_manager.release("src/main.py", "claude")
            await successor_manager.acquire("src/main.py", "codex", "successor")
        return await original_execute(sql, parameters)

    monkeypatch.setattr(tmp_db.conn, "execute", replace_before_delete)
    try:
        with pytest.raises(LockError, match="Only 'codex'"):
            await lm.release("src/main.py", "claude")
        assert replaced
        successor = await successor_manager.get_lock("src/main.py")
        assert successor.locked_by == "codex"
        assert successor.reason == "successor"
    finally:
        await other_db.close()
