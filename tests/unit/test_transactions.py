from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

import pytest

from agent_bus.core.inbox import InboxManager
from agent_bus.core.reviews import ReviewLog
from agent_bus.core.tasks import TaskManager
from agent_bus.reputation.database import Database
from agent_bus.types import Envelope, MessageType
from agent_bus.worker.gatekeeper import ReviewDecision, Verdict


@pytest.fixture(params=['file', 'memory', 'temporary', 'uri', 'anonymous_uri', 'named_uri', 'encoded_uri'])
async def db(request, tmp_path):
    paths = {'file': str(tmp_path / 'transactions.db'), 'memory': ':memory:',
             'temporary': '', 'uri': f'file:{tmp_path}/named?mode=memory&cache=shared',
             'anonymous_uri': 'file::memory:', 'named_uri': f'file:{tmp_path}/named?mode=memory',
             'encoded_uri': f'file:{tmp_path}/encoded?mode=mem%6fry'}
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
async def test_begin_error_codes(db, monkeypatch, code):
    error = sqlite3.OperationalError('injected')
    if code is not None:
        error.sqlite_errorcode = code
    connect = sqlite3.connect
    attempts = []

    class FailingBegin(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == 'BEGIN IMMEDIATE':
                attempts.append(sql)
                if len(attempts) == 1:
                    raise error
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, **kw, factory=FailingBegin))
    def operation(conn):
        return conn.execute('SELECT 42').fetchone()[0]
    if isinstance(code, int) and (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        assert await db.transaction(operation) == 42
        assert len(attempts) == 2
    else:
        with pytest.raises(sqlite3.OperationalError) as caught:
            await db.transaction(operation)
        assert caught.value is error
        assert len(attempts) == 1
