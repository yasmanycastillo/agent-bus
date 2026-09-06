from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from agent_bus.core.inbox import InboxManager
from agent_bus.core.reviews import ReviewLog
from agent_bus.core.tasks import TaskManager
from agent_bus.reputation.database import Database
from agent_bus.types import Envelope, MessageType
from agent_bus.worker.gatekeeper import ReviewDecision, Verdict


@pytest.fixture(params=['file', 'memory', 'temporary', 'uri'])
async def db(request, tmp_path):
    paths = {'file': str(tmp_path / 'transactions.db'), 'memory': ':memory:',
             'temporary': '', 'uri': f'file:{tmp_path}/named?mode=memory&cache=shared'}
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
