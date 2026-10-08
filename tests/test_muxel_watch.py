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

    def __init__(self, status='idle', outcome='finished', reply='Respuesta desde la TUI', asked=None,
                 awaiting_reply=False, cwd=None, screen=''):
        self.status = status
        self.screen = screen
        self.cwd = cwd
        self.awaiting_reply = awaiting_reply
        self.outcome = outcome
        self.reply = reply
        self.asked = asked
        self.calls = []

    async def __call__(self, cmd, agent_id, bus_url=None, input_text=None):
        verb = cmd[2]
        self.calls.append((verb, input_text))
        if verb == 'status':
            out = {'host': 'pc', 'status': self.status, 'awaiting_reply': self.awaiting_reply, 'cwd': self.cwd, 'remote': False}
        elif verb == 'screen':
            if self.screen is None:
                return subprocess.CompletedProcess(cmd, 1, json.dumps({'ok': False, 'error': 'no pane'}), '')
            out = {'host': 'pc', 'text': self.screen}
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
    assert [verb for verb, _ in muxel.calls] == ['status', 'screen', 'send', 'wait']
    assert 'agent-bus' in muxel.calls[2][1]
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


async def test_pane_that_just_got_a_prompt_counts_as_busy(hub, monkeypatch, tmp_path):
    # Seen with muxel 0.2.8: a pane reports "done" with awaiting_reply until the new turn shows.
    muxel = FakeMuxel(status='done', awaiting_reply=True)
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude',
                         sessions_file=tmp_path / 'sessions.json')
    assert [verb for verb, _ in muxel.calls] == ['status']
    assert not hub.failures and not hub.message['acknowledged']


async def test_claude_working_in_the_pane_folder_counts_as_busy(hub, monkeypatch, tmp_path):
    # muxel can report "done" while Claude Code itself says it is busy.
    muxel = FakeMuxel(status='done', cwd=str(tmp_path / 'project'))
    monkeypatch.setattr(watch, '_run_cli', muxel)
    from agent_bus.observe import sessions
    seen = []
    monkeypatch.setattr(sessions, 'claude_live_status', lambda root, home: seen.append(root) or 'busy')
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Claude',
                         sessions_file=tmp_path / 'sessions.json')
    assert seen == [tmp_path / 'project']
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


@pytest.mark.parametrize('footer', [
    '• Working (1m 07s • esc to interrupt)',  # Codex
    '✻ Cogitating… (12s · ↑ 1.2k tokens · esc to interrupt)',  # Claude Code
    '⠋ Thinking…',  # Grok
    'grok-4.6 · [stop]',  # Grok
])
async def test_pane_screen_showing_work_counts_as_busy(hub, monkeypatch, tmp_path, footer):
    # muxel 0.2.8 reported idle/done while Codex and Claude were visibly working.
    muxel = FakeMuxel(status='idle', screen=f'respuesta anterior\n\n{footer}\n\n› \n  ? for shortcuts')
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Codex',
                         sessions_file=tmp_path / 'sessions.json')
    assert [verb for verb, _ in muxel.calls] == ['status', 'screen']
    assert not hub.failures and not hub.message['acknowledged']


async def test_work_marker_scrolled_far_above_the_footer_is_ignored(hub, monkeypatch, tmp_path):
    old = 'el usuario preguntó por "esc to interrupt"\n' + 'línea\n' * 30 + '› '
    muxel = FakeMuxel(status='idle', screen=old)
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Codex',
                         sessions_file=tmp_path / 'sessions.json')
    assert 'send' in [verb for verb, _ in muxel.calls]
    assert hub.message['acknowledged']


async def test_unreadable_screen_does_not_block_the_turn(hub, monkeypatch, tmp_path):
    muxel = FakeMuxel(status='idle', screen=None)
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await watch.run_turn('bob', hub.message, {}, cli='muxel', muxel_agent='Codex',
                         sessions_file=tmp_path / 'sessions.json')
    assert [verb for verb, _ in muxel.calls] == ['status', 'screen', 'send', 'wait']
    assert hub.message['acknowledged']


# --- Task nudges: wake a live pane when its tasks change, without a reply turn ---

def test_task_nudge_plan_follows_status_transitions():
    plan = watch.plan_task_nudges
    assert set(plan({}, {'T1': 'pending', 'R1': 'blocked'})) == {'T1'}
    assert 'liberada' in plan({'R1': 'blocked'}, {'R1': 'in_progress'})['R1']
    assert plan({'T1': 'pending'}, {'T1': 'in_progress'}) == {}  # the agent claimed it itself
    assert plan({'T1': 'in_progress'}, {'T1': 'in_review'}) == {}


@pytest.fixture
def task_hub(hub):
    import httpx
    hub.message['acknowledged'] = True
    hub.tasks = [{'task_id': 'T1', 'owner': 'bob', 'status': 'pending'}]
    hub.task_queries = []
    original = hub.handle

    def handle(request):
        path = request.url.path
        if path == '/tasks':
            hub.task_queries.append(dict(request.url.params))
            owner = request.url.params.get('owner')
            return httpx.Response(200, json=[t for t in hub.tasks if t['owner'] == owner])
        if path.startswith('/tasks/'):
            task = next(t for t in hub.tasks if t['task_id'] == path.rsplit('/', 1)[1])
            return httpx.Response(200, json=task)
        return original(request)
    hub.handle = handle
    return hub


def _watcher(tmp_path, **kw):
    return watch.PendingMessageWatcher('bob', cli='muxel', muxel_agent='Codex',
                                       sessions_file=tmp_path / 'sessions.json', **kw)


async def test_assigned_task_is_nudged_once_without_waiting_or_replying(task_hub, monkeypatch, tmp_path):
    muxel = FakeMuxel()
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await _watcher(tmp_path).tick()
    assert [verb for verb, _ in muxel.calls] == ['status', 'screen', 'send']
    text = muxel.calls[2][1]
    assert 'T1' in text and 'my_pending_items' in text
    assert not task_hub.replies and task_hub.task_queries == [{'owner': 'bob'}]
    await _watcher(tmp_path).tick()  # a restarted watcher remembers the delivered nudge
    assert [verb for verb, _ in muxel.calls].count('send') == 1


async def test_busy_pane_keeps_the_nudge_for_later(task_hub, monkeypatch, tmp_path):
    muxel = FakeMuxel(status='working')
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await _watcher(tmp_path).tick()
    assert 'send' not in [verb for verb, _ in muxel.calls]
    muxel.status = 'idle'
    await _watcher(tmp_path).tick()
    assert [verb for verb, _ in muxel.calls].count('send') == 1


async def test_released_review_and_reopened_implementation_are_nudged(task_hub, monkeypatch, tmp_path):
    muxel = FakeMuxel()
    monkeypatch.setattr(watch, '_run_cli', muxel)
    task_hub.tasks = [{'task_id': 'R1', 'owner': 'bob', 'status': 'blocked'},
                      {'task_id': 'I1', 'owner': 'bob', 'status': 'in_review'}]
    watcher = _watcher(tmp_path)
    await watcher.tick()
    assert 'send' not in [verb for verb, _ in muxel.calls]
    task_hub.tasks = [{'task_id': 'R1', 'owner': 'bob', 'status': 'in_progress'},
                      {'task_id': 'I1', 'owner': 'free', 'status': 'pending'}]
    await watcher.tick()
    sent = [text for verb, text in muxel.calls if verb == 'send']
    assert len(sent) == 1 and 'R1' in sent[0] and 'I1' in sent[0] and 'reclám' in sent[0]
    task_hub.tasks[1].update(owner='bob', status='in_progress')  # reclaimed by the agent itself
    await watcher.tick()
    assert [verb for verb, _ in muxel.calls].count('send') == 1


async def test_nudge_goes_stale_when_the_task_is_no_longer_actionable(task_hub, monkeypatch, tmp_path):
    muxel = FakeMuxel(status='working')
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await _watcher(tmp_path).tick()
    task_hub.tasks[0]['status'] = 'in_review'
    muxel.status = 'idle'
    await _watcher(tmp_path).tick()
    assert 'send' not in [verb for verb, _ in muxel.calls]


async def test_reply_needed_message_goes_before_task_nudges(task_hub, monkeypatch, tmp_path):
    task_hub.message['acknowledged'] = False
    muxel = FakeMuxel()
    monkeypatch.setattr(watch, '_run_cli', muxel)
    watcher = _watcher(tmp_path)
    await watcher.tick()
    assert [verb for verb, _ in muxel.calls] == ['status', 'screen', 'send', 'wait']
    assert task_hub.message['acknowledged'] and not task_hub.task_queries
    await watcher.tick()
    assert [verb for verb, _ in muxel.calls].count('send') == 2


async def test_task_nudges_can_be_disabled(task_hub, monkeypatch, tmp_path):
    muxel = FakeMuxel()
    monkeypatch.setattr(watch, '_run_cli', muxel)
    await _watcher(tmp_path, task_nudges=False).tick()
    assert not muxel.calls and not task_hub.task_queries
    from click.testing import CliRunner
    assert '--no-task-nudges' in CliRunner().invoke(watch.watch, ['--help']).output
