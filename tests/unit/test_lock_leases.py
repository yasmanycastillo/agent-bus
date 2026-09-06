"""Session fencing, deterministic expiry, contention, and legacy lock migration."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from agent_bus.core.locks import LockBusyError, LockError, LockManager
from agent_bus.reputation.database import Database


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


async def test_session_cap_renewal_and_active_only_reads(tmp_db, clock):
    manager = LockManager(tmp_db, clock=clock)
    lock = await manager.acquire("a.py", "alice", session_id="session-a", ttl_seconds=60,
                                 session_expires_at=clock.now + 30)
    assert lock.expires_at.timestamp() == clock.now + 30
    assert lock.locked_at.timestamp() == clock.now
    clock.now += 10
    renewed = await manager.renew("a.py", "alice", session_id="session-a", acquisition_id=lock.acquisition_id,
                                  ttl_seconds=60, session_expires_at=clock.now + 30)
    assert renewed.acquisition_id == lock.acquisition_id
    assert renewed.locked_at == lock.locked_at
    assert renewed.expires_at.timestamp() == clock.now + 30
    clock.now += 30
    assert await manager.get_lock("a.py") is None
    assert await manager.list_locks() == []
    for operation in (manager.renew, manager.release):
        with pytest.raises(LockError):
            await operation("a.py", "alice", session_id="session-a", acquisition_id=lock.acquisition_id)


@pytest.mark.parametrize("ttl", [0, -1, 3601, True, 1.5, "60"])
async def test_invalid_ttl_does_not_mutate(tmp_db, clock, ttl):
    manager = LockManager(tmp_db, clock=clock)
    with pytest.raises(LockError, match="TTL"):
        await manager.acquire("a.py", "alice", ttl_seconds=ttl)
    assert await manager.list_locks() == []
    lock = await manager.acquire("a.py", "alice")
    with pytest.raises(LockError, match="TTL"):
        await manager.renew("a.py", "alice", acquisition_id=lock.acquisition_id, ttl_seconds=ttl)
    assert await manager.get_lock("a.py") == lock


@pytest.mark.parametrize("expiry", [0, float("inf"), float("nan"), True])
async def test_expired_or_invalid_session_rejects_acquire_and_renew(tmp_db, clock, expiry):
    manager = LockManager(tmp_db, clock=clock)
    with pytest.raises(LockError, match="session"):
        await manager.acquire("a.py", "alice", session_expires_at=expiry)
    lock = await manager.acquire("a.py", "alice")
    with pytest.raises(LockError, match="session"):
        await manager.renew("a.py", "alice", acquisition_id=lock.acquisition_id, session_expires_at=expiry)
    assert await manager.get_lock("a.py") == lock


@pytest.mark.parametrize("actor,session,token", [
    ("bob", "session-a", None), ("alice", "session-b", None), ("alice", "session-a", "wrong-token"),
])
async def test_every_ownership_component_is_required(tmp_db, clock, actor, session, token):
    manager = LockManager(tmp_db, clock=clock)
    lock = await manager.acquire("a.py", "alice", session_id="session-a")
    for operation in (manager.release, manager.renew):
        with pytest.raises(LockError):
            await operation("a.py", actor, session_id=session, acquisition_id=token or lock.acquisition_id)
        assert await manager.get_lock("a.py") == lock


async def test_same_agent_other_session_cannot_reacquire_active_lease(tmp_db, clock):
    manager = LockManager(tmp_db, clock=clock)
    lock = await manager.acquire("a.py", "alice", session_id="session-a")
    with pytest.raises(LockError):
        await manager.acquire("a.py", "alice", session_id="session-b")
    assert await manager.get_lock("a.py") == lock


@pytest.mark.parametrize("successor_session", ["session-a", "session-b"])
async def test_expired_acquisition_cannot_touch_successor_even_same_agent_session(tmp_db, clock, successor_session):
    manager = LockManager(tmp_db, clock=clock)
    old = await manager.acquire("a.py", "alice", session_id="session-a", ttl_seconds=1)
    clock.now += 1
    successor = await manager.acquire("a.py", "alice", "new work", session_id=successor_session)
    assert successor.acquisition_id != old.acquisition_id
    for operation in (manager.release, manager.renew):
        with pytest.raises(LockError):
            await operation("a.py", "alice", session_id="session-a", acquisition_id=old.acquisition_id)
    assert await manager.get_lock("a.py") == successor


@pytest.mark.parametrize("expired", [False, True])
async def test_independent_connections_race_for_one_acquisition(tmp_db, clock, expired):
    other = Database(tmp_db.db_path)
    await other.initialize()
    managers = [LockManager(tmp_db, clock=clock), LockManager(other, clock=clock)]
    try:
        if expired:
            await managers[0].acquire("race.py", "previous", ttl_seconds=1)
            clock.now += 1
        start = asyncio.Event()
        async def contender(manager, session):
            await start.wait()
            return await manager.acquire("race.py", "alice", session_id=session)
        pending = [asyncio.create_task(contender(manager, f"session-{index}")) for index, manager in enumerate(managers)]
        start.set()
        results = await asyncio.gather(*pending, return_exceptions=True)
        winners = [result for result in results if not isinstance(result, Exception)]
        assert len(winners) == 1
        assert sum(isinstance(result, LockError) for result in results) == 1
        assert await managers[0].get_lock("race.py") == winners[0]
        assert await managers[1].get_lock("race.py") == winners[0]
    finally:
        await other.close()


async def test_queue_delay_cannot_renew_expired_lease(tmp_db, clock, monkeypatch):
    manager = LockManager(tmp_db, clock=clock)
    lock = await manager.acquire("a.py", "alice", session_id="s", ttl_seconds=1)
    execute = tmp_db.conn._execute
    delayed = False
    async def delay_callback(function, *args, **kwargs):
        nonlocal delayed
        if not delayed and function.__name__ == "transaction":
            delayed = True
            clock.now += 2
        return await execute(function, *args, **kwargs)
    monkeypatch.setattr(tmp_db.conn, "_execute", delay_callback)
    with pytest.raises(LockError):
        await manager.renew("a.py", "alice", session_id="s", acquisition_id=lock.acquisition_id)
    assert delayed
    assert await manager.get_lock("a.py") is None


async def test_shared_connection_write_during_acquisition_has_no_pending_returning_cursor(tmp_db, clock):
    manager = LockManager(tmp_db, clock=clock)
    async def unrelated_write():
        await tmp_db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('bob')")
        await tmp_db.conn.commit()
    lock, _ = await asyncio.gather(manager.acquire("a.py", "alice"), unrelated_write())
    assert await manager.get_lock("a.py") == lock
    assert (await tmp_db.conn.execute_fetchall("SELECT agent_id FROM reputation"))[0][0] == "bob"


async def test_legacy_four_column_locks_expire_once_and_migration_preserves_other_data(tmp_path, clock):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE locks(file_path TEXT PRIMARY KEY, locked_by TEXT NOT NULL, locked_at TEXT NOT NULL, reason TEXT)")
        connection.execute("INSERT INTO locks VALUES ('a.py', 'legacy', '2020-01-01T00:00:00+00:00', 'old work')")
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
    db = Database(path)
    await db.initialize()
    try:
        manager = LockManager(db, clock=clock)
        assert await manager.list_locks() == []
        assert await manager.get_lock("a.py") is None
        lock = await manager.acquire("a.py", "alice", session_id="s")
    finally:
        await db.close()
    await db.initialize()
    try:
        assert await LockManager(db, clock=clock).get_lock("a.py") == lock
        assert (await db.conn.execute_fetchall("SELECT value FROM unrelated"))[0][0] == "keep"
        columns = [row["name"] for row in await db.conn.execute_fetchall("PRAGMA table_info(locks)")]
        assert len(columns) == len(set(columns)) == 7
    finally:
        await db.close()


async def test_pending_transaction_is_neither_committed_nor_modified(tmp_db, clock):
    manager = LockManager(tmp_db, clock=clock)
    await tmp_db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('uncommitted')")
    with pytest.raises(LockBusyError, match="committed"):
        await manager.acquire("a.py", "alice")
    assert not await tmp_db.conn.execute_fetchall("SELECT * FROM locks")
    await tmp_db.conn.rollback()
    assert not await tmp_db.conn.execute_fetchall("SELECT * FROM reputation")
    lock = await manager.acquire("a.py", "alice")
    for operation in (manager.release, manager.renew):
        await tmp_db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('uncommitted')")
        with pytest.raises(LockBusyError, match="committed"):
            await operation("a.py", "alice", acquisition_id=lock.acquisition_id)
        await tmp_db.conn.rollback()
        assert await manager.get_lock("a.py") == lock
        assert not await tmp_db.conn.execute_fetchall("SELECT * FROM reputation")


async def test_clock_is_sampled_only_after_sqlite_write_ownership(tmp_db, clock):
    # Sampling inside the callback alone is insufficient if its first write then
    # blocks behind another connection until after the lease has expired.
    other = sqlite3.connect(tmp_db.db_path, timeout=0, check_same_thread=False)
    sampled = False
    def reserved_clock():
        nonlocal sampled
        try:
            other.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc)
            sampled = True
        else:
            other.rollback()
            pytest.fail("Clock was sampled without SQLite write ownership")
        return clock.now
    try:
        lease = await LockManager(tmp_db, clock=reserved_clock).acquire("a.py", "alice")
        assert sampled
        assert lease.expires_at.timestamp() == clock.now + 300
    finally:
        other.close()
