"""HTTP ownership and lease guarantees, with isolated SQLite and controlled time."""
from datetime import datetime
import time

import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.locks import LockManager
from agent_bus.core.registry import AgentRegistry


@pytest.fixture
async def leased(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_BUS_ALLOW_UNSIGNED', '0')
    root = tmp_path / 'project'
    root.mkdir()
    bus = MessageBus(tmp_db, AgentRegistry(), InboxManager(tmp_db), project_id='leases', project_root=root)
    sessions = [await bus.sessions.create('alice') for _ in range(2)]
    admin = await bus.sessions.create('human', role='admin')
    clock = [time.time()]
    bus.locks = LockManager(tmp_db, clock=lambda: clock[0])
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url='http://test') as client:
        yield bus, client, sessions, admin, clock, root


def auth(session):
    return {'Authorization': f"Bearer {session['token']}"}


async def test_same_agent_other_session_cannot_mutate_lease(leased):
    bus, client, sessions, admin, clock, root = leased
    first, second = sessions
    response = await client.post('/locks/acquire', headers=auth(first), json={'file_path': 'a.py'})
    assert response.status_code == 200
    lock = response.json()
    assert lock['file_path'] == str(root / 'a.py')
    assert lock['session_id'] == first['session_id']
    payload = {'file_path': './a.py', 'acquisition_id': lock['acquisition_id']}
    assert (await client.post('/locks/renew', headers=auth(second), json=payload)).status_code == 409
    assert (await client.post('/locks/release', headers=auth(second), json=payload)).status_code == 403
    assert (await client.post('/locks/release', headers=auth(admin), json=payload)).status_code == 403
    assert (await client.post('/locks/release', headers=auth(first), json={'file_path': 'a.py'})).status_code == 422
    for url, session in [('/locks', second), ('/room/api/overview', admin)]:
        response = await client.get(url, headers=auth(session))
        assert response.status_code == 200
        assert lock['acquisition_id'] not in response.text
        assert 'acquisition_id' not in response.text
    clock[0] += 20
    renewed = await client.post('/locks/renew', headers=auth(first), json={**payload, 'ttl_seconds': 600})
    assert renewed.status_code == 200
    assert renewed.json()['acquisition_id'] == lock['acquisition_id']
    assert datetime.fromisoformat(renewed.json()['expires_at']) > datetime.fromisoformat(lock['expires_at'])
    assert (await client.post('/locks/release', headers=auth(first), json=payload)).status_code == 200
    assert (await client.get('/locks', headers=auth(first))).json() == []


async def test_expired_generation_cannot_change_successor_same_session(leased):
    bus, client, sessions, admin, clock, root = leased
    session = sessions[0]
    original = (await client.post('/locks/acquire', headers=auth(session), json={'file_path': 'a.py', 'ttl_seconds': 2})).json()
    clock[0] += 3
    assert (await client.get('/locks', headers=auth(session))).json() == []
    successor_response = await client.post('/locks/acquire', headers=auth(session), json={'file_path': 'a.py'})
    assert successor_response.status_code == 200
    successor = successor_response.json()
    assert successor['acquisition_id'] != original['acquisition_id']
    stale = {'file_path': 'a.py', 'acquisition_id': original['acquisition_id']}
    assert (await client.post('/locks/renew', headers=auth(session), json=stale)).status_code == 409
    assert (await client.post('/locks/release', headers=auth(session), json=stale)).status_code == 403
    current = await bus.locks.get_lock(str(root / 'a.py'))
    assert current.acquisition_id == successor['acquisition_id']


async def test_lease_capped_by_session_and_revocation_denies_renewal(leased):
    bus, client, sessions, admin, clock, root = leased
    short = await bus.sessions.create('short', ttl_seconds=60)
    response = await client.post('/locks/acquire', headers=auth(short), json={'file_path': 'a.py', 'ttl_seconds': 3600})
    assert response.status_code == 200
    lock = response.json()
    assert datetime.fromisoformat(lock['expires_at']).timestamp() <= short['expires_at'] + 0.000001
    await bus.sessions.revoke(short['session_id'])
    assert (await client.post('/locks/renew', headers=auth(short), json={
        'file_path': 'a.py', 'acquisition_id': lock['acquisition_id'],
    })).status_code == 401
    clock[0] = short['expires_at'] + 1
    assert (await client.post('/locks/acquire', headers=auth(sessions[0]), json={'file_path': 'a.py'})).status_code == 200


async def test_server_canonicalizes_aliases_and_shared_scope(leased):
    bus, client, sessions, admin, clock, root = leased
    (root / 'src').mkdir()
    (root / 'alias').symlink_to(root / 'src', target_is_directory=True)
    first = await client.post('/locks/acquire', headers=auth(sessions[0]), json={'file_path': 'src/a.py', 'scope': 'project'})
    assert first.status_code == 200
    collision = await client.post('/locks/acquire', headers=auth(sessions[1]), json={'file_path': str(root / 'alias' / 'a.py')})
    assert collision.status_code == 409
    isolated = await client.post('/locks/acquire', headers=auth(sessions[1]), json={'file_path': str(root.parent / 'worktree' / 'src' / 'a.py')})
    assert isolated.status_code == 200
    for path in ('../escape.py', str(root / 'absolute.py')):
        rejected = await client.post('/locks/acquire', headers=auth(sessions[0]), json={'file_path': path, 'scope': 'project'})
        assert rejected.status_code == 422


@pytest.mark.parametrize('payload', [
    {'ttl_seconds': 0}, {'ttl_seconds': 3601}, {'ttl_seconds': True}, {'ttl_seconds': 1.5},
    {'scope': 'unknown'}, {'file_path': ''}, {'file_path': '\x00bad'}, {'session_id': 'forged'},
])
async def test_invalid_lease_payload_rejected(leased, payload):
    _, client, sessions, _, _, _ = leased
    response = await client.post('/locks/acquire', headers=auth(sessions[0]), json={'file_path': 'a.py', **payload})
    assert response.status_code == 422


async def test_pending_database_transaction_returns_retryable_error_without_committing(leased):
    bus, client, sessions, _, _, _ = leased
    await bus.db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('pending')")
    response = await client.post('/locks/acquire', headers=auth(sessions[0]), json={'file_path': 'a.py'})
    assert response.status_code == 503
    assert response.headers['retry-after'] == '1'
    await bus.db.conn.rollback()
    assert not await bus.db.conn.execute_fetchall('SELECT * FROM reputation')
    assert not await bus.db.conn.execute_fetchall('SELECT * FROM locks')
    assert (await client.post('/locks/acquire', headers=auth(sessions[0]), json={'file_path': 'a.py'})).status_code == 200
