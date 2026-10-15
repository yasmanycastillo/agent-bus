from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_bus.core.inbox import InboxManager
from agent_bus.core.reviews import ReviewLog
from agent_bus.core.tasks import TaskManager
import agent_bus.reputation.database as database_module
from agent_bus.reputation.database import Database
from agent_bus.types import Envelope, MessageType
from agent_bus.worker.gatekeeper import ReviewDecision, Verdict


@pytest.mark.asyncio
async def test_file_database_is_private_without_changing_existing_parent(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    path = parent / "bus.db"
    database = Database(str(path))
    await database.initialize()
    try:
        assert os.stat(parent).st_mode & 0o777 == 0o755
        assert os.stat(path).st_mode & 0o777 == 0o600
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_file_database_is_private_before_sqlite_opens_it(tmp_path, monkeypatch):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    path = parent / "bus.db"
    real_connect = database_module.aiosqlite.connect
    observed_modes = []

    def observed_connect(db_path, *args, **kwargs):
        observed_modes.append(os.stat(path).st_mode & 0o777)
        return real_connect(db_path, *args, **kwargs)

    monkeypatch.setattr(database_module.aiosqlite, "connect", observed_connect)
    database = Database(str(path))
    await database.initialize()
    try:
        assert observed_modes == [0o600]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_file_database_allows_shared_parent_below_private_ancestor(tmp_path):
    parent = tmp_path / "team-runtime"
    parent.mkdir(mode=0o775)
    parent.chmod(0o775)
    database = Database(str(parent / "bus.db"))
    await database.initialize()
    await database.close()


@pytest.mark.asyncio
async def test_file_database_rejects_reachable_world_writable_parent():
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        parent = Path(directory)
        parent.chmod(0o777)
        database = Database(str(parent / "bus.db"))
        with pytest.raises(PermissionError, match="writable by other users"):
            await database.initialize()


@pytest.mark.asyncio
async def test_file_uri_mode_rw_does_not_create_missing_database(tmp_path):
    path = tmp_path / "missing.db"
    database = Database(f"file:{path}?mode=rw")
    with pytest.raises(FileNotFoundError):
        await database.initialize()
    assert not path.exists()


@pytest.mark.asyncio
async def test_database_rejects_path_swapped_for_symlink_before_sqlite_open(tmp_path, monkeypatch):
    path = tmp_path / "bus.db"
    target = tmp_path / "target.db"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.commit()
    real_connect = database_module.aiosqlite.connect

    orphan = tmp_path / "orphan.db"

    def swap_path_before_connect(db_path, *args, **kwargs):
        path.rename(orphan)
        path.symlink_to(target)
        return real_connect(db_path, *args, **kwargs)

    monkeypatch.setattr(database_module.aiosqlite, "connect", swap_path_before_connect)
    database = Database(str(path))
    with pytest.raises(RuntimeError, match="changed during secure open"):
        await database.initialize()
    with closing(sqlite3.connect(target)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert tables == {"sentinel"}


@pytest.mark.asyncio
async def test_transaction_rejects_path_swapped_for_symlink_after_initialize(tmp_path):
    path = tmp_path / "bus.db"
    original = tmp_path / "original.db"
    target = tmp_path / "target.db"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.commit()
    database = Database(str(path))
    await database.initialize()
    path.rename(original)
    path.symlink_to(target)
    try:
        with pytest.raises(RuntimeError, match="changed after initialization"):
            await database.transaction(lambda connection: connection.execute(
                "CREATE TABLE redirected(value TEXT)"
            ))
    finally:
        await database.close()
    with closing(sqlite3.connect(target)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert tables == {"sentinel"}


@pytest.mark.asyncio
async def test_database_rejects_symlinked_ancestor(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    database = Database(str(alias / "bus.db"))
    with pytest.raises(OSError):
        await database.initialize()
    assert not (real_parent / "bus.db").exists()


def test_database_rejects_file_uri_authority():
    with pytest.raises(ValueError, match="authority"):
        Database("file://evil.invalid/tmp/authority.db?mode=rw")


@pytest.mark.parametrize("uri", [
    "file:named?mode=memory&mode=rw",
    "file:named?mode=rw&mode=memory",
])
def test_database_rejects_duplicate_uri_modes(uri):
    with pytest.raises(ValueError, match="mode"):
        Database(uri)


@pytest.mark.asyncio
async def test_database_rejects_second_initialize_while_active(tmp_path):
    database = Database(str(tmp_path / "bus.db"))
    await database.initialize()
    try:
        with pytest.raises(RuntimeError, match="already initialized"):
            await database.initialize()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_database_serializes_concurrent_initialize(tmp_path, monkeypatch):
    real_connect = database_module.aiosqlite.connect

    async def delayed_connect(*args, **kwargs):
        await asyncio.sleep(0.01)
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(database_module.aiosqlite, "connect", delayed_connect)
    database = Database(str(tmp_path / "bus.db"))
    results = await asyncio.gather(database.initialize(), database.initialize(), return_exceptions=True)
    assert sum(result is None for result in results) == 1
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "already initialized" in str(errors[0])
    descriptor = database._anchor_fd
    assert descriptor is not None
    await database.close()
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.asyncio
async def test_close_waits_for_inflight_initialize_and_releases_resources(tmp_path, monkeypatch):
    real_connect = database_module.aiosqlite.connect
    entered_connect = asyncio.Event()
    release_connect = asyncio.Event()

    async def delayed_connect(*args, **kwargs):
        entered_connect.set()
        await release_connect.wait()
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(database_module.aiosqlite, "connect", delayed_connect)
    database = Database(str(tmp_path / "bus.db"))
    initializing = asyncio.create_task(database.initialize())
    await entered_connect.wait()
    closing = asyncio.create_task(database.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release_connect.set()
    await initializing
    descriptor = database._anchor_fd
    assert descriptor is not None
    await closing
    assert database._connection is None
    assert database._anchor_fd is None
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.asyncio
async def test_database_rejects_direct_sticky_world_writable_parent():
    descriptor, raw_path = tempfile.mkstemp(prefix="agent-bus-permissions-", suffix=".db", dir="/tmp")
    os.close(descriptor)
    path = Path(raw_path)
    path.unlink()
    try:
        database = Database(str(path))
        with pytest.raises(PermissionError, match="writable by other users"):
            await database.initialize()
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_file_database_creates_private_parent_directory(tmp_path):
    parent = tmp_path / "private"
    path = parent / "bus.db"
    database = Database(str(path))
    await database.initialize()
    try:
        assert os.stat(parent).st_mode & 0o777 == 0o700
        assert os.stat(path).st_mode & 0o777 == 0o600
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_file_uri_database_is_private_without_changing_existing_parent(tmp_path):
    parent = tmp_path / "uri-shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    path = parent / "bus.db"
    database = Database(f"file:{path}?mode=rwc")
    await database.initialize()
    try:
        assert os.stat(parent).st_mode & 0o777 == 0o755
        assert os.stat(path).st_mode & 0o777 == 0o600
    finally:
        await database.close()


@pytest.fixture(params=['file', 'memory', 'temporary', 'uri', 'anonymous_uri', 'named_uri', 'encoded_uri',
                        'shared_anonymous_uri', 'encoded_anonymous_uri', 'fragment_uri'])
async def db(request, tmp_path):
    paths = {'file': str(tmp_path / 'transactions.db'), 'memory': ':memory:',
             'temporary': '', 'uri': f'file:{tmp_path}/named?mode=memory&cache=shared',
             'anonymous_uri': 'file::memory:', 'named_uri': f'file:{tmp_path}/named?mode=memory',
             'encoded_uri': f'file:{tmp_path}/encoded?mode=mem%6fry',
             'shared_anonymous_uri': 'file::memory:?cache=shared',
             'encoded_anonymous_uri': 'file:%3Amemory%3A',
             'fragment_uri': f'file:{tmp_path}/fragment?mode=memory#fragment'}
    database = Database(paths[request.param])
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


def envelope():
    return Envelope(from_agent='alice', to_agent='bob', message_type=MessageType.INBOX,
                    timestamp=datetime.now(timezone.utc))


def review():
    return ReviewDecision(review_id='rev-1', task_id='TASK-1', reviewer_agent_id='charlie',
                          reviewer_session_id='sess-charlie', sha='abcdef1234567890',
                          verdict=Verdict.APPROVE, reason='Looks solid', evidence={},
                          test_results={}, created_at=datetime.now(timezone.utc))


async def after_writer_rollback(db, operation, monkeypatch):
    """A holds the writer lock until B reaches BEGIN, without timing sleeps."""
    await db.conn.execute('BEGIN IMMEDIATE')
    await db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('dirty-agent')")
    begin_attempted = asyncio.Event()
    loop = asyncio.get_running_loop()
    connect = sqlite3.connect

    def observed_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                loop.call_soon_threadsafe(begin_attempted.set)

        connection.set_trace_callback(trace)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', observed_connect)
        pending = asyncio.create_task(operation())
        try:
            await asyncio.wait_for(begin_attempted.wait(), timeout=3)
            assert not pending.done(), 'B must wait for A to release the writer lock'
        finally:
            await db.conn.rollback()
            # Drain the worker even when a barrier assertion fails.
            result = await asyncio.wait_for(pending, timeout=7)
    return result


@pytest.mark.parametrize('kind', ['deliver', 'send', 'archive', 'transfer', 'review'])
async def test_success_survives_other_transaction_rollback(db, kind, monkeypatch):
    inbox = InboxManager(db)
    tasks = TaskManager(db)
    env = envelope()
    await tasks.create('TASK-1', 'Baseline', owner='alice')
    if kind == 'archive':
        await inbox.deliver(env)
    operations = {
        'deliver': lambda: inbox.deliver(env),
        'send': lambda: inbox.send(env, ['bob']),
        'archive': lambda: inbox.archive('bob', env.message_id),
        'transfer': lambda: tasks.transfer('TASK-1', 'bob', actor='alice',
                                          session_id='sess-alice', require_owner=True),
        'review': lambda: ReviewLog(db).add(review()),
    }
    await after_writer_rollback(db, operations[kind], monkeypatch)
    with closing(sqlite3.connect(db.db_path, uri=True)) as fresh:
        assert not fresh.execute("SELECT 1 FROM reputation WHERE agent_id='dirty-agent'").fetchall()
        if kind in ('deliver', 'send', 'archive'):
            assert fresh.execute('SELECT archived FROM inbox WHERE message_id=?',
                                 (env.message_id,)).fetchall() == [(int(kind == 'archive'),)]
            assert fresh.execute('SELECT COUNT(*) FROM event_log WHERE message_id=?',
                                 (env.message_id,)).fetchone() == (1,)
        elif kind == 'transfer':
            assert fresh.execute("SELECT owner FROM tasks WHERE task_id='TASK-1'").fetchone() == ('bob',)
            assert fresh.execute('SELECT action, previous_owner, new_owner FROM audit_log').fetchall() == [
                ('handoff', 'alice', 'bob')]
        else:
            assert fresh.execute('SELECT review_id FROM reviews').fetchall() == [('rev-1',)]
            assert fresh.execute('SELECT action, task_id FROM audit_log').fetchall() == [
                ('gatekeeper_review', 'TASK-1')]


@pytest.mark.parametrize('kind', ['send', 'transfer', 'review', 'archive'])
async def test_partial_operation_rolls_back(db, kind):
    inbox = InboxManager(db)
    env = envelope()
    await TaskManager(db).create('TASK-1', 'Baseline', owner='alice')
    if kind == 'archive':
        await inbox.deliver(env)
    trigger = {
        'send': 'BEFORE INSERT ON event_log',
        'transfer': 'BEFORE UPDATE ON tasks',
        'review': 'BEFORE INSERT ON audit_log',
        'archive': 'AFTER UPDATE ON inbox',
    }[kind]
    await db.conn.execute(f"CREATE TRIGGER fail_operation {trigger} BEGIN SELECT RAISE(ABORT, 'injected'); END")
    await db.conn.commit()
    operations = {
        'send': lambda: inbox.send(env, ['bob'], idempotency_key='key'),
        'transfer': lambda: TaskManager(db).transfer('TASK-1', 'bob', actor='alice', session_id='s'),
        'review': lambda: ReviewLog(db).add(review()),
        'archive': lambda: inbox.archive('bob', env.message_id),
    }
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        await operations[kind]()
    with closing(sqlite3.connect(db.db_path, uri=True)) as fresh:
        assert fresh.execute('SELECT owner FROM tasks').fetchall() == [('alice',)]
        for table in ('audit_log', 'reviews', 'message_idempotency'):
            assert fresh.execute(f'SELECT COUNT(*) FROM {table}').fetchone() == (0,)
        assert fresh.execute('SELECT archived FROM inbox').fetchall() == ([(0,)] if kind == 'archive' else [])
        assert fresh.execute('SELECT COUNT(*) FROM event_log').fetchone() == (int(kind == 'archive'),)
        assert fresh.execute('SELECT COUNT(*) FROM inbox_delivery_state').fetchone() == (int(kind == 'archive'),)


async def test_base_exception_rolls_back_and_closes(db, monkeypatch):
    class Abort(BaseException):
        pass

    closed = []
    connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def close(self):
            super().close()
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                self.execute('SELECT 1')
            closed.append(True)

    monkeypatch.setattr(sqlite3, 'connect',
                        lambda *args, **kwargs: connect(*args, **kwargs, factory=ObservedConnection))

    def failing(connection):
        connection.execute("INSERT INTO reputation(agent_id) VALUES ('aborted')")
        raise Abort()

    with pytest.raises(Abort):
        await db.transaction(failing)
    assert closed == [True]
    with closing(sqlite3.connect(db.db_path, uri=True)) as fresh:
        assert not fresh.execute("SELECT 1 FROM reputation WHERE agent_id='aborted'").fetchall()
    assert await db.transaction(lambda connection: connection.execute('SELECT 42').fetchone()[0]) == 42
    assert closed == [True, True, True]


async def test_memory_databases_are_isolated():
    first, second = Database(':memory:'), Database(':memory:')
    await first.initialize()
    await second.initialize()
    try:
        await first.transaction(lambda conn: conn.execute("INSERT INTO reputation(agent_id) VALUES ('first')").rowcount)
        assert not await second.conn.execute_fetchall('SELECT * FROM reputation')
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize('uri', [
    'file::memory:?cache=private', 'file:named?mode=memory&cache=private',
    'file:named?cache=shared&mode=memory&cache=private',
    'file:%3Amemory%3A?cache=private',
])
def test_private_memory_cache_is_rejected(uri):
    with pytest.raises(ValueError, match='cache=private'):
        Database(uri)


@pytest.mark.parametrize('uri, expected', [
    ('file::memory:', 'file::memory:?cache=shared'),
    ('file::memory:?cache=shared', 'file::memory:?cache=shared'),
    ('file:named?mode=memory', 'file:named?mode=memory&cache=shared'),
    ('file:named?mode=memory#fragment', 'file:named?mode=memory&cache=shared#fragment'),
    ('file:disk?cache=private', 'file:disk?cache=private'),
])
def test_memory_uri_normalization(uri, expected):
    assert Database(uri).db_path == expected


@pytest.mark.parametrize('fail', [False, True])
async def test_cancellation_waits_for_commit_or_rollback_and_close(db, monkeypatch, fail):
    loop = asyncio.get_running_loop()
    started, close_started = asyncio.Event(), asyncio.Event()
    release_operation, release_close = threading.Event(), threading.Event()
    closed = []
    connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def close(self):
            loop.call_soon_threadsafe(close_started.set)
            assert release_close.wait(5)
            super().close()
            closed.append(True)

    monkeypatch.setattr(sqlite3, 'connect',
                        lambda *args, **kwargs: connect(*args, **kwargs, factory=ObservedConnection))

    def operation(connection):
        connection.execute("INSERT INTO reputation(agent_id) VALUES ('cancelled')")
        loop.call_soon_threadsafe(started.set)
        assert release_operation.wait(5)
        if fail:
            raise ValueError('injected failure after cancellation')

    pending = asyncio.create_task(db.transaction(operation))
    try:
        await asyncio.wait_for(started.wait(), 3)
        pending.cancel()
        await asyncio.sleep(0)  # Deliver cancellation while the callback is held.
        assert not pending.done()
        assert len(db._workers) == 1
        release_operation.set()
        await asyncio.wait_for(close_started.wait(), 3)
        pending.cancel()  # Repeated cancellation must not skip connection.close().
        await asyncio.sleep(0)
        assert not pending.done()
        with closing(connect(db.db_path, uri=True)) as fresh:
            assert fresh.execute("SELECT COUNT(*) FROM reputation WHERE agent_id='cancelled'").fetchone() == (
                int(not fail),)
    finally:
        release_operation.set()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
    assert closed == [True]
    assert not db._workers


async def test_concurrent_close_waits_for_every_dispatched_worker(db, monkeypatch):
    loop = asyncio.get_running_loop()
    connecting = [asyncio.Event(), asyncio.Event()]
    release = [threading.Event(), threading.Event()]
    guard = threading.Lock()
    dispatched = []
    closed = []
    connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def close(self):
            super().close()
            closed.append(True)

    def held_connect(*args, **kwargs):
        with guard:
            index = len(dispatched)
            dispatched.append(index)
        loop.call_soon_threadsafe(connecting[index].set)
        assert release[index].wait(5)
        return connect(*args, **kwargs, factory=ObservedConnection)

    monkeypatch.setattr(sqlite3, 'connect', held_connect)
    workers = [asyncio.create_task(db.transaction(
        lambda conn: conn.execute('SELECT COUNT(*) FROM reputation').fetchone()[0], write=False,
    )) for _ in range(2)]
    closers = []
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in connecting)), 3)
        assert len(db._workers) == 2
        anchor = db.conn
        closers = [asyncio.create_task(db.close()) for _ in range(2)]
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match='closing'):
            await db.transaction(lambda conn: None)
        with pytest.raises(RuntimeError, match='closing'):
            await db.initialize()
        closers[0].cancel()
        await asyncio.sleep(0)
        assert all(not task.done() for task in closers)
        release[0].set()
        await asyncio.wait_for(asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED), 3)
        assert len(closed) == 1
        assert all(not task.done() for task in closers)
        assert await anchor.execute_fetchall('SELECT 1')
    finally:
        for event in release:
            event.set()
        results = await asyncio.wait_for(asyncio.gather(*workers, *closers, return_exceptions=True), 3)
    assert results[:2] == [0, 0]
    assert isinstance(results[2], asyncio.CancelledError)
    assert results[3] is None
    assert len(closed) == 2
    assert not db._workers
    assert db._connection is None
    with pytest.raises(ValueError, match='no active connection'):
        await anchor.execute('SELECT 1')
    await db.close()


async def test_reinitialize_after_close_starts_a_new_lifecycle(db):
    await db.close()
    with pytest.raises(RuntimeError, match='closed'):
        await db.transaction(lambda conn: None)
    await db.initialize()
    anchor = db.conn
    assert await db.transaction(lambda conn: conn.execute('SELECT 42').fetchone()[0]) == 42
    await db.close()
    with pytest.raises(ValueError, match='no active connection'):
        await anchor.execute('SELECT 1')


@pytest.mark.parametrize('method', ['page', 'pending_summary'])
async def test_inbox_read_does_not_wait_for_reserved_writer(tmp_path, method):
    database = Database(str(tmp_path / 'reader.db'))
    await database.initialize()
    inbox = InboxManager(database)
    env = envelope()
    await inbox.deliver(env)
    try:
        await database.conn.execute('BEGIN IMMEDIATE')
        await database.conn.execute('UPDATE inbox SET archived=1')
        result = await asyncio.wait_for(getattr(inbox, method)('bob'), 2)
        if method == 'page':
            assert [message['message_id'] for message in result['messages']] == [env.message_id]
        else:
            assert result['count'] == 1
    finally:
        await database.conn.rollback()
        await database.close()


async def test_read_snapshot_does_not_reserve_writer(tmp_path):
    database = Database(str(tmp_path / 'snapshot.db'))
    await database.initialize()
    async with database.conn.execute('PRAGMA journal_mode=WAL') as cursor:
        assert (await cursor.fetchone())[0] == 'wal'
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()

    def read(connection):
        before = connection.execute('SELECT COUNT(*) FROM reputation').fetchone()[0]
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        after = connection.execute('SELECT COUNT(*) FROM reputation').fetchone()[0]
        return before, after

    reader = asyncio.create_task(database.transaction(read, write=False))
    try:
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(database.transaction(
            lambda conn: conn.execute("INSERT INTO reputation(agent_id) VALUES ('writer')").rowcount,
        ), 2)
    finally:
        release.set()
        result = await asyncio.wait_for(reader, 3)
        await database.close()
    assert result == (0, 0)


async def test_read_transaction_rejects_writes(db):
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        await db.transaction(lambda conn: conn.execute("INSERT INTO reputation(agent_id) VALUES ('no')"), write=False)


@pytest.mark.parametrize('code', [None, 'invalid', sqlite3.SQLITE_ERROR,
                                       sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                                       sqlite3.SQLITE_LOCKED | (1 << 8)])
@pytest.mark.parametrize('stage', ['begin', 'callback', 'commit'])
async def test_transaction_error_codes(db, monkeypatch, code, stage):
    error = sqlite3.OperationalError('injected')
    if code is not None:
        error.sqlite_errorcode = code
    connect = sqlite3.connect
    attempts = []

    closed = []

    def inject():
        attempts.append(stage)
        if len(attempts) == 1:
            raise error

    class FailingAttempt(sqlite3.Connection):
        def execute(self, sql, *args):
            if (stage == 'begin' and sql == 'BEGIN IMMEDIATE'
                    or stage == 'callback' and sql == 'SELECT 42'):
                inject()
            return super().execute(sql, *args)

        def commit(self):
            if stage == 'commit':
                inject()
            return super().commit()

        def close(self):
            assert not self.in_transaction
            super().close()
            closed.append(True)

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, **kw, factory=FailingAttempt))

    def operation(conn):
        # A replay must not retain the previous attempt's primary-key insert.
        conn.execute("INSERT INTO reputation(agent_id) VALUES ('retry')")
        return conn.execute('SELECT 42').fetchone()[0]

    retryable = isinstance(code, int) and (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    if retryable:
        assert await db.transaction(operation) == 42
        assert len(attempts) == 2
    else:
        with pytest.raises(sqlite3.OperationalError) as caught:
            await db.transaction(operation)
        assert caught.value is error
        assert len(attempts) == 1
    assert len(closed) == len(attempts)
    rows = await db.conn.execute_fetchall('SELECT agent_id FROM reputation')
    assert [tuple(row) for row in rows] == ([('retry',)] if retryable else [])


@pytest.mark.parametrize('direction', ['reader_writer', 'writer_reader'])
@pytest.mark.parametrize('outcome', ['success', 'exhausted', 'cancelled'])
async def test_entire_attempt_retries_real_contention(db, monkeypatch, direction, outcome):
    """Hold a real table/file lock until the failed attempt has been closed."""
    anchor = db.conn
    await anchor.execute('CREATE TABLE retry_partial (value INTEGER)')
    await anchor.commit()
    write = direction == 'reader_writer'
    memory = not (await anchor.execute_fetchall('PRAGMA database_list'))[0][2]
    if not memory:
        # WAL removes reader/writer contention; rollback journal recreates it.
        await anchor.execute_fetchall('PRAGMA journal_mode=DELETE')
    if write:
        await anchor.execute('BEGIN')
        await anchor.execute_fetchall('SELECT * FROM reputation')
    else:
        # Memory table locks suffice; EXCLUSIVE would lock its schema too,
        # preventing the reader from reaching its callback.
        await anchor.execute('BEGIN IMMEDIATE' if memory else 'BEGIN EXCLUSIVE')
        await anchor.execute("INSERT INTO reputation(agent_id) VALUES ('uncommitted')")

    loop = asyncio.get_running_loop()
    retry_waiting, release_retry = asyncio.Event(), threading.Event()
    connections, closed, failures = [], [], []
    callbacks = []
    clock_offset = 0
    connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            try:
                return super().execute(sql, *args)
            except sqlite3.OperationalError as exc:
                stage = sql if sql.startswith(('BEGIN', 'PRAGMA')) else 'callback'
                failures.append((stage, exc.sqlite_errorcode & 0xff))
                raise

        def commit(self):
            try:
                return super().commit()
            except sqlite3.OperationalError as exc:
                failures.append(('commit', exc.sqlite_errorcode & 0xff))
                raise

        def close(self):
            assert not self.in_transaction
            super().close()
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                self.execute('SELECT 1')
            closed.append(self)

    def observed_connect(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=ObservedConnection)
        connections.append(connection)
        return connection

    def retry_sleep(delay):
        nonlocal clock_offset
        assert 0 < delay <= 0.01
        assert closed == connections  # Cleanup precedes every retry wait.
        loop.call_soon_threadsafe(retry_waiting.set)
        assert release_retry.wait(5)
        if outcome == 'exhausted':
            clock_offset = 6  # Advance only the retry clock, not asyncio timers.

    monkeypatch.setattr(sqlite3, 'connect', observed_connect)
    monkeypatch.setattr(database_module, 'time', SimpleNamespace(
        monotonic=lambda: time.monotonic() + clock_offset, sleep=retry_sleep,
    ))

    def operation(connection):
        callbacks.append(connection)
        if write:
            assert connection.execute('SELECT COUNT(*) FROM retry_partial').fetchone()[0] == 0
            connection.execute('INSERT INTO retry_partial VALUES (1)')
            connection.execute("INSERT INTO reputation(agent_id) VALUES ('committed')")
            return 42
        return connection.execute('SELECT COUNT(*) FROM reputation').fetchone()[0]

    pending = asyncio.create_task(db.transaction(operation, write=write))
    closer = None
    try:
        await asyncio.wait_for(retry_waiting.wait(), 3)
        assert not pending.done()
        assert len(callbacks) == 1  # BEGIN succeeded; the failure happened later.
        assert len(failures) == 1
        assert failures[0][0] == ('commit' if write and not memory else 'callback')
        assert failures[0][1] in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        if outcome != 'exhausted':
            if outcome == 'cancelled':
                pending.cancel()
            closer = asyncio.create_task(db.close())
            await asyncio.sleep(0)
            assert not pending.done()
            assert not closer.done()
            assert db._workers
            await anchor.rollback()
        release_retry.set()
        if outcome == 'exhausted':
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                await asyncio.wait_for(pending, 3)
            assert len(connections) == 1  # No fresh attempt after the deadline.
            await anchor.rollback()
            assert not await anchor.execute_fetchall('SELECT * FROM retry_partial')
            assert not await anchor.execute_fetchall('SELECT * FROM reputation')
        elif outcome == 'cancelled':
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 3)
        else:
            assert await asyncio.wait_for(pending, 3) == (42 if write else 0)
    finally:
        release_retry.set()
        if db._connection is not None and closer is None:
            await anchor.rollback()
        await asyncio.gather(pending, return_exceptions=True)
        await (closer if closer is not None else db.close())
    assert not db._workers
    assert db._connection is None
    assert closed == connections
    assert len(callbacks) == (1 if outcome == 'exhausted' else 2)
    with pytest.raises(ValueError, match='no active connection'):
        await anchor.execute('SELECT 1')
