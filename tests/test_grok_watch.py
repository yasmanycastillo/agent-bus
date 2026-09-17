"""Native Grok turns, durable delivery and observable executor health."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

from agent_bus.cli import watch_cmds as watch
from agent_bus.cli.watch_state import record_status, reply_file, watcher_status
from agent_bus.worker.execution import ExecutionGuard


@pytest.fixture
def hub(monkeypatch):
    from tests.test_message_workers import DeliveryHub
    instance = DeliveryHub()
    monkeypatch.setattr(watch, 'async_bus_client', lambda *a, **kw: instance.client(**kw))
    monkeypatch.setattr(watch.shutil, 'which', lambda _: '/bin/grok')
    return instance


async def test_native_grok_arguments_and_resume(hub, monkeypatch, tmp_path):
    calls = []
    async def run(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({
            'text': 'Answer', 'sessionId': 'native-session', 'stopReason': 'end_turn',
            'thought': 'must not be delivered'}), '')
    monkeypatch.setattr(watch, '_run_cli', run)
    sessions = tmp_path / 'sessions.json'
    await watch.run_turn('bob', hub.message, {}, cli='grok', model='grok-4.6', sessions_file=sessions)
    restored = watch.load_session_map(sessions)
    assert restored == {'conversation-1': 'native-session'}
    hub.message['acknowledged'] = False
    await watch.run_turn('bob', hub.message, restored, cli='grok', sessions_file=sessions)
    assert calls[0].count('--output-format') == 1
    assert calls[0][calls[0].index('--model') + 1] == 'grok-4.6'
    assert calls[0][calls[0].index('--tools') + 1] == ''
    assert '--no-subagents' in calls[0] and '--disable-web-search' in calls[0]
    assert calls[1][-2:] == ['--resume', 'native-session']
    assert all(x['body']['text'] == 'Answer' for x in hub.replies)


@pytest.mark.parametrize('output', [
    'not-json', '{}', '{"text":{}}',
    '{"text":"partial","stopReason":"max_turns"}',
    '{"text":"partial","stopReason":"error"}',
    '{"text":"Answer","stopReason":"end_turn","is_error":true}',
])
async def test_grok_incomplete_never_acknowledges(hub, monkeypatch, tmp_path, output):
    async def run(*a, **kw):
        return subprocess.CompletedProcess([], 0, output, '')
    monkeypatch.setattr(watch, '_run_cli', run)
    await watch.run_turn('bob', hub.message, {}, cli='grok', sessions_file=tmp_path / 'sessions.json')
    assert not hub.message['acknowledged'] and not hub.replies
    assert not list(tmp_path.glob('outbox/*.json'))


async def test_lost_post_response_does_not_invoke_model_twice(hub, monkeypatch, tmp_path):
    calls = []
    async def run(*a, **kw):
        calls.append(1)
        return subprocess.CompletedProcess([], 0, '{"text":"Answer","stopReason":"end_turn"}', '')
    monkeypatch.setattr(watch, '_run_cli', run)
    hub.lose_reply_response = True
    path = tmp_path / 'sessions.json'
    await watch.run_turn('bob', hub.message, {}, cli='grok', sessions_file=path)
    assert reply_file(path, 'source').exists()
    await watch.run_turn('bob', hub.message, {}, cli='grok', sessions_file=path)
    assert calls == [1] and len(hub.replies) == 1
    assert not reply_file(path, 'source').exists()


def test_process_crash_after_preparing_reply_recovers_without_model(tmp_path):
    # Kill the process inside POST, after fsync of the reply and before commit.
    # No provider or network involved: only the crash/restart boundary is real.
    code = '''
import asyncio,json,os,subprocess,sys
from pathlib import Path
import httpx
from agent_bus.cli import watch_cmds as w
root=Path(sys.argv[1])
def handler(request):
 if request.method=='GET':
  return httpx.Response(200,json={'message_id':'m','conversation_id':'thread'})
 if sys.argv[2]=='crash': os._exit(17)
 (root/'delivered.json').write_bytes(request.content)
 return httpx.Response(200,json={})
w.async_bus_client=lambda *a,**kw: httpx.AsyncClient(transport=httpx.MockTransport(handler),**kw)
w.shutil.which=lambda _: '/fake-grok'
async def run(*a,**kw):
 with (root/'calls').open('a') as f: f.write('model\\n')
 return subprocess.CompletedProcess([],0,'{"text":"saved answer","sessionId":"s","stopReason":"end_turn"}','')
w._run_cli=run
asyncio.run(w.run_turn('bob',{'message_id':'m'},w.load_session_map(root/'sessions.json'),cli='grok',sessions_file=root/'sessions.json'))
'''
    env = dict(os.environ, PYTHONPATH=str(Path(watch.__file__).parents[2]))
    crashed = subprocess.run([sys.executable, '-c', code, str(tmp_path), 'crash'], env=env, capture_output=True)
    assert crashed.returncode == 17, crashed.stderr
    prepared = reply_file(tmp_path / 'sessions.json', 'm')
    assert prepared.exists() and prepared.stat().st_mode & 0o777 == 0o600
    recovered = subprocess.run([sys.executable, '-c', code, str(tmp_path), 'recover'], env=env, capture_output=True)
    assert recovered.returncode == 0, recovered.stderr
    assert (tmp_path / 'calls').read_text() == 'model\n'
    payload = json.loads((tmp_path / 'delivered.json').read_text())
    assert payload['body']['text'] == 'saved answer' and payload['acknowledge']
    assert payload['idempotency_key'] == 'watch-reply:m'


def test_status_uses_lock_not_surviving_pid_file(tmp_path):
    path = tmp_path / 'status.json'
    record_status(path, 'grok', 'waiting')
    assert not watcher_status('bob', path)['can_dispatch']
    with ExecutionGuard('bob', kind='watcher'):
        assert watcher_status('bob', path)['can_dispatch']
        record_status(path, 'grok', 'auth_error')
        assert watcher_status('bob', path)['state'] == 'auth_error'
        assert not watcher_status('bob', path)['can_dispatch']
        record_status(path, 'grok', 'waiting')
        value = json.loads(path.read_text()); value['updated_at'] -= 60
        path.write_text(json.dumps(value))
        assert watcher_status('bob', path)['state'] == 'unknown'
    assert watcher_status('bob', path)['state'] == 'stopped'
    with ExecutionGuard('bob', kind='worker'):
        assert watcher_status('bob', path)['state'] == 'other_executor'


async def test_expired_session_recovers_after_replacement(secure_bus, monkeypatch, tmp_path):
    # Real signed HTTP and SQLite; only the provider response is simulated.
    from agent_bus.security import async_bus_client
    async with async_bus_client('alice', base_url=secure_bus.url) as c:
        sent = await c.post('/messages', json={'to_agent': 'bob', 'body': {'text': 'Answer'}, 'reply_needed': True})
        sent.raise_for_status()
    original = secure_bus.paths['bob'].read_text()
    expired = json.loads(original); expired['expires_at'] = time.time() - 1
    secure_bus.paths['bob'].write_text(json.dumps(expired))
    invoked = []
    async def run(*a, **kw):
        invoked.append(1)
        return subprocess.CompletedProcess([], 0, '{"text":"Recovered","stopReason":"end_turn"}', '')
    monkeypatch.setattr(watch, '_run_cli', run)
    monkeypatch.setattr(watch.shutil, 'which', lambda _: '/fake-grok')
    worker = watch.PendingMessageWatcher('bob', cli='grok', bus_url=secure_bus.url, sessions_file=tmp_path / 'sessions.json')
    await worker._run(once=True)
    assert json.loads((tmp_path / 'status.json').read_text())['state'] == 'auth_error'
    assert invoked == []
    secure_bus.paths['bob'].write_text(original)
    await worker.poll_once()
    assert invoked == [1]
    async with async_bus_client('bob', base_url=secure_bus.url) as c:
        delivery = await c.get('/inbox/bob/' + sent.json()['message_id'])
        assert delivery.json()['acknowledged']


def test_default_cli_from_credential(monkeypatch):
    monkeypatch.setenv('AGENT_BUS_ALLOW_UNSIGNED', '0')
    monkeypatch.setattr(watch, 'load_session', lambda *a: {'provider': 'grok'})
    monkeypatch.setattr(watch, 'worker_environment', lambda *a, **kw: {})
    monkeypatch.setattr(watch.shutil, 'which', lambda _: '/bin/grok')
    selected = []
    async def run(self, **kw):
        selected.append(self.cli)
    monkeypatch.setattr(watch.PendingMessageWatcher, 'run', run)
    result = CliRunner().invoke(watch.watch, ['--agent', 'grok-generated', '--once'])
    assert result.exit_code == 0, result.output
    assert selected == ['grok']
