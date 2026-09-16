"""Real SQLite/HTTP/MCP workflows: isolation, rollback, retries and lease fencing."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import Client

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.mcp.server import McpServer
from agent_bus.reputation.database import Database
from agent_bus.types import Envelope, MessageType


@pytest.fixture
async def flow(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_BUS_ALLOW_UNSIGNED', '0')
    db = Database(str(tmp_path / 'flow.db'))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id='workflow', project_root=tmp_path)
    alice = await bus.sessions.create('alice')
    bob = await bus.sessions.create('bob')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bus.app), base_url='http://test') as client:
        yield bus, client, alice, bob
    await db.close()


def headers(session):
    return {'Authorization': 'Bearer ' + session['token']}


async def prepare(client, actor, key='edit', paths=None):
    return await client.post('/coordination/prepare-edit', headers=headers(actor), json={
        'paths': paths or ['a.py', 'b.py'], 'operation_key': key,
    })


async def make_task(bus, actor='alice', task_id='T1'):
    await bus.tasks.create(task_id, 'Implement feature', owner=actor)


async def handoff(client, actor, locks=(), **extra):
    return await client.post('/coordination/handoff', headers=headers(actor), json={
        'task_id': 'T1', 'to_agent': 'bob', 'summary': 'Implementation ready', 'operation_key': 'handoff',
        'validation_commands': ['pytest -q'], 'validation_summary': 'Passed in isolated environment',
        'release_locks': [{'file_path': lock['file_path'], 'acquisition_id': lock['acquisition_id'],
                           'scope': lock['scope']} for lock in locks], **extra,
    })


async def test_bootstrap_and_pending_preserve_identity_unread_and_pagination(flow):
    bus, client, alice, bob = flow
    for index in range(3):
        await make_task(bus, task_id=f'T{index}')
        await bus.inbox.deliver(Envelope(from_agent='bob', to_agent='alice', message_type=MessageType.INBOX,
                                        body={'text': f'Question {index}'}, reply_needed=True))
    await make_task(bus, 'bob', 'B1')
    await bus.tasks.create('FREE', 'Available work')
    response = await client.post('/coordination/bootstrap', headers=headers(alice), json={'limit': 2})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['agent_id'] == 'alice' and result['session_id'] == alice['session_id']
    assert result['available_task_count'] == 1 and result['available_tasks'][0]['task_id'] == 'FREE'
    assert alice['token'] not in response.text
    pending = result['pending']
    assert len(pending['messages']) == len(pending['tasks']) == 2
    assert pending['pending_message_count'] == pending['reply_needed_count'] == 3
    assert pending['next_cursor'] and pending['next_task_offset'] == 2
    page = await client.get('/coordination/pending', headers=headers(alice), params={
        'limit': 2, 'cursor': pending['next_cursor'], 'task_offset': 2,
    })
    assert len(page.json()['messages']) == len(page.json()['tasks']) == 1
    assert await bus.inbox.pending_count('alice') == 3
    assert 'B1' not in json.dumps(pending['tasks'])
    second = await client.post('/coordination/bootstrap', headers=headers(alice), json={})
    assert second.status_code == 200 and second.json()['agent_count'] == 1
    assert (await client.get('/coordination/pending', headers=headers(bob),
                             params={'cursor': pending['next_cursor']})).status_code == 422


async def test_prepare_all_or_nothing_and_no_other_session_tokens(flow):
    bus, client, alice, bob = flow
    held = (await prepare(client, bob, paths=['b.py'])).json()['locks'][0]
    response = await prepare(client, alice)
    assert response.status_code == 409
    assert len(await bus.locks.list_locks()) == 1
    assert held['acquisition_id'] not in response.text
    assert not await bus.db.conn.execute_fetchall("SELECT * FROM coordination_operations WHERE session_id=?", (alice['session_id'],))
    assert (await prepare(client, alice, paths=['../outside'], key='invalid')).status_code == 200  # checkout scope permits physical external files
    invalid = await client.post('/coordination/prepare-edit', headers=headers(alice), json={
        'paths': ['../outside'], 'scope': 'project', 'operation_key': 'escape',
    })
    assert invalid.status_code == 422


async def test_prepare_replay_expiry_and_competing_sessions(flow):
    bus, client, alice, bob = flow
    responses = await asyncio.gather(prepare(client, alice), prepare(client, bob))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = alice if responses[0].status_code == 200 else bob
    original = next(response.json() for response in responses if response.status_code == 200)
    replay = await prepare(client, winner)
    assert replay.json()['replayed'] and replay.json()['locks'] == original['locks']
    assert (await prepare(client, winner, paths=['changed.py'])).status_code == 409
    session2 = await bus.sessions.create(winner['agent_id'])
    assert (await prepare(client, session2)).status_code == 409
    clock = bus.locks._clock()
    bus.locks._clock = lambda: clock + 301
    assert (await prepare(client, winner)).status_code == 409
    successor = await prepare(client, winner, key='new-generation')
    assert successor.status_code == 200
    assert successor.json()['locks'][0]['acquisition_id'] != original['locks'][0]['acquisition_id']


async def test_handoff_atomic_durable_replay_and_evidence(flow):
    bus, client, alice, bob = flow
    await make_task(bus)
    original = Envelope(from_agent='bob', to_agent='alice', message_type=MessageType.INBOX, body={'text': 'Do it'})
    await bus.inbox.deliver(original)
    locks = (await prepare(client, alice)).json()['locks']
    response = await handoff(client, alice, locks, acknowledge_message_ids=[original.message_id])
    assert response.status_code == 200, response.text
    first = response.json()
    assert first['task_status'] == 'in_review' and first['released_lock_count'] == 2
    assert await bus.inbox.pending_count('alice') == 0
    assert (await bus.tasks.get('T1')).status.value == 'in_review'
    messages = await bus.inbox.get_inbox('bob')
    assert len(messages) == 1 and messages[0].body['validation_source'] == 'reported_by_agent'
    assert messages[0].body['validation_commands'] == ['pytest -q']
    assert not await bus.locks.list_locks()
    # Persisted operation survives a new manager/SQLite connection; a replay must
    # not release reservations subsequently acquired for another task.
    successor = (await prepare(client, alice, key='next-edit')).json()['locks']
    other_db = Database(bus.db.db_path)
    await other_db.initialize()
    other_bus = MessageBus(other_db, AgentRegistry(), InboxManager(other_db), project_id='workflow', project_root=bus.project_root)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other_bus.app), base_url='http://test') as other:
            replay = await handoff(other, alice, locks, acknowledge_message_ids=[original.message_id])
            assert replay.status_code == 200 and replay.json()['replayed']
            assert replay.json()['message_id'] == first['message_id']
    finally:
        await other_db.close()
    assert len(await bus.inbox.get_inbox('bob')) == 1
    assert {lock.acquisition_id for lock in await bus.locks.list_locks()} == {lock['acquisition_id'] for lock in successor}
    assert (await handoff(client, alice, locks, summary='Different request')).status_code == 409


async def test_handoff_invalid_ack_or_lock_rolls_back_every_effect(flow):
    bus, client, alice, bob = flow
    await make_task(bus)
    locks = (await prepare(client, alice)).json()['locks']
    denied = await handoff(client, bob, locks)
    assert denied.status_code == 403
    bad = await handoff(client, alice, locks, acknowledge_message_ids=['missing'])
    assert bad.status_code == 404, bad.text
    assert (await bus.tasks.get('T1')).status.value == 'in_progress'
    assert len(await bus.locks.list_locks()) == 2 and not await bus.inbox.get_inbox('bob')
    stale = [{**lock, 'acquisition_id': 'stale'} for lock in locks]
    assert (await handoff(client, alice, stale)).status_code == 409
    assert (await bus.tasks.get('T1')).status.value == 'in_progress'
    assert len(await bus.locks.list_locks()) == 2


async def test_handoff_done_unblocks_only_actual_dependents(flow):
    bus, client, alice, bob = flow
    await make_task(bus)
    await bus.tasks.create('T2', 'dependent', depends_on=['T1'])
    await bus.tasks.create('manual', 'manual blocker')
    await bus.tasks.block('manual')
    result = await handoff(client, alice, task_status='done')
    assert result.status_code == 200, result.text
    assert (await bus.tasks.get('T2')).status.value == 'pending'
    assert (await bus.tasks.get('manual')).status.value == 'blocked'


async def test_revoked_unsigned_and_forged_identities_rejected(flow, monkeypatch):
    bus, client, alice, bob = flow
    assert (await client.post('/coordination/bootstrap', headers=headers(alice), json={'agent_id': 'bob'})).status_code == 422
    monkeypatch.setenv('AGENT_BUS_ALLOW_UNSIGNED', '1')
    assert (await client.post('/coordination/bootstrap', json={})).status_code == 401
    await bus.sessions.revoke(alice['session_id'])
    assert (await prepare(client, alice)).status_code == 401


async def test_mcp_workflow_through_sdk_and_bound_hub(flow, monkeypatch):
    bus, client, alice, bob = flow
    monkeypatch.setattr('agent_bus.mcp.server.load_session', lambda agent: alice)
    server = McpServer(agent_id='alice')
    server._lock_cwd = bus.project_root
    server._lock_project_root = str(bus.project_root)
    @asynccontextmanager
    async def bound_client(*args, **kwargs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bus.app), base_url='http://test', headers=headers(alice)) as bound:
            yield bound
    monkeypatch.setattr(server, '_client', bound_client)
    await make_task(bus)
    async with Client(server.sdk_server()) as mcp:
        response = await mcp.call_tool('bootstrap_agent', {})
        assert not response.is_error, response
        response = await mcp.call_tool('prepare_edit', {'paths': ['a.py'], 'operation_key': 'mcp-edit'})
        assert not response.is_error, response
        lock = response.structured_content['locks'][0]
        response = await mcp.call_tool('complete_handoff', {
            'task_id': 'T1', 'to_agent': 'bob', 'summary': 'Ready', 'operation_key': 'mcp-handoff',
            'release_locks': [{key: lock[key] for key in ('file_path', 'scope', 'acquisition_id')}],
        })
        assert not response.is_error, response
        assert response.structured_content['task_status'] == 'in_review'
        pending = await mcp.call_tool('my_pending_items', {})
        assert not pending.is_error and pending.structured_content['active_lock_count'] == 0


async def test_competing_connections_and_duplicate_handoff(flow):
    bus, client, alice, bob = flow
    other_db = Database(bus.db.db_path)
    await other_db.initialize()
    other_bus = MessageBus(other_db, AgentRegistry(), InboxManager(other_db), project_id='workflow', project_root=bus.project_root)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other_bus.app), base_url='http://test') as other:
            results = await asyncio.gather(prepare(client, alice), prepare(other, bob))
            assert sorted(result.status_code for result in results) == [200, 409]
            winner = alice if results[0].status_code == 200 else bob
            locks = next(result.json()['locks'] for result in results if result.status_code == 200)
            await make_task(bus, winner['agent_id'])
            results = await asyncio.gather(handoff(client, winner, locks), handoff(other, winner, locks))
            assert all(result.status_code == 200 for result in results), [result.text for result in results]
            assert sum(result.json()['replayed'] for result in results) == 1
            assert len({result.json()['message_id'] for result in results}) == 1
            audit = await bus.db.conn.execute_fetchall("SELECT * FROM audit_log WHERE action='complete_handoff'")
            assert len(audit) == 1 and audit[0]['actor_session_id'] == winner['session_id']
    finally:
        await other_db.close()


async def test_pending_transaction_not_committed_and_expired_session_not_replayed(flow):
    bus, client, alice, bob = flow
    await bus.db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('uncommitted')")
    result = await prepare(client, alice)
    assert result.status_code == 503
    await bus.db.conn.rollback()
    assert not await bus.db.conn.execute_fetchall("SELECT * FROM reputation WHERE agent_id='uncommitted'")
    assert (await prepare(client, alice)).status_code == 200
    bus.locks._clock = lambda: alice['expires_at'] + 1
    assert (await prepare(client, alice)).status_code == 401
