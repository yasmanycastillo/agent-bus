"""Waking a live agent pane through `muxel ctl` instead of a headless turn."""
import json
import subprocess

import pytest

from agent_bus.cli import watch_cmds as watch


@pytest.fixture
def hub(monkeypatch):
    from tests.test_message_workers import DeliveryHub
    instance = DeliveryHub()
    monkeypatch.setattr(watch, 'async_bus_client', lambda *a, **kw: instance.client(**kw))
    monkeypatch.setattr(watch.shutil, 'which', lambda _: '/usr/bin/muxel')
    return instance


class FakeMuxel:
    """Answers `muxel ctl` the way muxel prints it: one JSON object per command."""

    def __init__(self, status='idle', outcome='finished', reply='Respuesta desde la TUI', asked=None):
        self.status = status
        self.outcome = outcome
        self.reply = reply
        self.asked = asked
        self.calls = []

    async def __call__(self, cmd, agent_id, bus_url=None, input_text=None):
        verb = cmd[2]
        self.calls.append((verb, input_text))
        if verb == 'status':
            out = {'host': 'pc', 'status': self.status}
        elif verb == 'send':
            self.asked = self.asked or input_text
            out = {'host': 'pc', 'sent': True}
        elif verb == 'wait':
            out = {'host': 'pc', 'last_prompt': {'text': self.asked, 'source': 'transcript'} if self.asked else None,
                   'last_reply': {'text': self.reply, 'source': 'transcript'} if self.reply else None,
                   'wait': {'outcome': self.outcome, 'waited_secs': 4}}
        else:
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), '')


async def test_idle_pane_gets_the_prompt_and_its_reply_is_delivered(hub, monkeypatch, tmp_path):
    muxel = FakeMuxel()
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude',
                         sessions_file=tmp_path / 'sessions.json')
    assert [verb for verb, _ in muxel.calls] == ['status', 'send', 'wait']
    assert 'agent-bus' in muxel.calls[1][1]
    assert hub.message['acknowledged']
    assert hub.replies[0]['body']['text'] == 'Respuesta desde la TUI'
    assert not list(tmp_path.glob('outbox/*.sent'))


@pytest.mark.parametrize('status', ['working', 'blocked', 'starting'])
async def test_busy_pane_defers_without_spending_an_attempt(hub, monkeypatch, tmp_path, status):
    muxel = FakeMuxel(status=status)
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude',
                         sessions_file=tmp_path / 'sessions.json')
    assert [verb for verb, _ in muxel.calls] == ['status']
    assert not hub.failures and not hub.message['acknowledged']


@pytest.mark.parametrize('outcome', ['blocked', 'timed_out'])
async def test_pending_turn_is_awaited_not_failed_nor_sent_twice(hub, monkeypatch, tmp_path, outcome):
    muxel = FakeMuxel(outcome=outcome)
    monkeypatch.setattr(watch, '_run_cli', muxel)
    sessions = tmp_path / 'sessions.json'
    for _ in range(6):  # more retries than the attempt limit
        await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude', sessions_file=sessions)
    assert not hub.failures and not hub.message['acknowledged']
    muxel.outcome, muxel.status = 'finished', 'done'
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude', sessions_file=sessions)
    assert [verb for verb, _ in muxel.calls].count('send') == 1
    assert hub.message['acknowledged']


async def test_exited_agent_is_a_failure(hub, monkeypatch, tmp_path):
    monkeypatch.setattr(watch, '_run_cli', FakeMuxel(outcome='exited'))
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude',
                         sessions_file=tmp_path / 'sessions.json')
    assert hub.failures and not hub.message['acknowledged']


async def test_finished_turn_without_reply_is_not_acknowledged(hub, monkeypatch, tmp_path):
    monkeypatch.setattr(watch, '_run_cli', FakeMuxel(reply=None))
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude',
                         sessions_file=tmp_path / 'sessions.json')
    assert hub.failures and not hub.message['acknowledged']


def test_watch_requires_a_muxel_agent():
    from click.testing import CliRunner
    result = CliRunner().invoke(watch.watch, ['--agent', 'bob', '--cli', 'muxel'])
    assert result.exit_code != 0
    assert '--muxel-agent' in result.output


async def test_failed_turn_types_the_prompt_again_next_time(hub, monkeypatch, tmp_path):
    muxel = FakeMuxel(outcome='exited')
    monkeypatch.setattr(watch, '_run_cli', muxel)
    sessions = tmp_path / 'sessions.json'
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude', sessions_file=sessions)
    assert not list(tmp_path.glob('outbox/*.sent'))
    muxel.outcome = 'finished'
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude', sessions_file=sessions)
    assert [verb for verb, _ in muxel.calls].count('send') == 2
    assert hub.message['acknowledged']


async def test_reply_to_someone_elses_prompt_is_not_delivered(hub, monkeypatch, tmp_path):
    muxel = FakeMuxel(asked='lo que escribió el usuario en el panel')
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude',
                         sessions_file=tmp_path / 'sessions.json')
    assert hub.failures and not hub.message['acknowledged'] and not hub.replies
