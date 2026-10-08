"""The watcher writes into an `agent-bus panes` tmux pane; the agent there replies itself."""
import shutil
import subprocess
import time
import uuid

import pytest
from click.testing import CliRunner

from agent_bus import panes
from agent_bus.cli import watch_cmds as watch
from tests import test_muxel_watch

hub, task_hub = test_muxel_watch.hub, test_muxel_watch.task_hub  # shared fixtures


class FakePane:
    def __init__(self, busy=False):
        self.busy = busy
        self.sent = []

    def __call__(self, name, text, force=False):
        if self.busy:
            raise panes.PaneBusy(f"pane '{name}' is working")
        self.sent.append((name, text))


def _watcher(tmp_path, **kw):
    return watch.PendingMessageWatcher('bob', cli='tmux', tmux_agent='bob-pane',
                                       sessions_file=tmp_path / 'sessions.json', **kw)


async def test_question_is_typed_once_and_left_for_the_agent_to_answer(task_hub, monkeypatch, tmp_path):
    task_hub.message['acknowledged'] = False
    task_hub.tasks = []
    pane = FakePane()
    monkeypatch.setattr(panes, 'send', pane)
    await _watcher(tmp_path).tick()
    assert len(pane.sent) == 1
    name, text = pane.sent[0]
    assert name == 'bob-pane' and 'Review please' in text and 'reply_message(message_id="source"' in text
    # The watcher neither answers nor acknowledges: the agent does, over MCP.
    assert not task_hub.replies and not task_hub.failures and not task_hub.acks
    await _watcher(tmp_path).tick()  # a restarted watcher remembers it was asked
    assert len(pane.sent) == 1


async def test_busy_pane_defers_questions_and_nudges_without_failing(task_hub, monkeypatch, tmp_path):
    task_hub.message['acknowledged'] = False
    pane = FakePane(busy=True)
    monkeypatch.setattr(panes, 'send', pane)
    for _ in range(6):  # more than the attempt limit of reply turns
        await _watcher(tmp_path).tick()
    assert not pane.sent and not task_hub.failures
    pane.busy = False
    await _watcher(tmp_path).tick()
    assert len(pane.sent) == 1
    text = pane.sent[0][1]
    assert 'T1' in text and 'my_pending_items' in text and 'reply_message' in text


async def test_task_notice_is_typed_and_acknowledged(task_hub, monkeypatch, tmp_path):
    task_hub.message.update(acknowledged=False, related_task='T1',
                            body={'text': 'Implementa A', 'title': 'A', 'role': 'implement'})
    pane = FakePane()
    monkeypatch.setattr(panes, 'send', pane)
    await _watcher(tmp_path).tick()
    assert len(pane.sent) == 1 and 'reply_message' not in pane.sent[0][1]
    assert task_hub.acks == [['source']]


def test_watch_requires_a_tmux_pane():
    result = CliRunner().invoke(watch.watch, ['--agent', 'bob', '--cli', 'tmux'])
    assert result.exit_code != 0 and '--tmux-agent' in result.output


@pytest.mark.skipif(not shutil.which('tmux'), reason='tmux not installed')
async def test_question_reaches_a_real_tmux_pane(task_hub, monkeypatch, tmp_path):
    socket = f'agent-bus-test-{uuid.uuid4().hex[:8]}'
    monkeypatch.setattr(panes, 'TMUX', ['tmux', '-L', socket])
    # `cat` echoes what is typed, as an agent's input box would show it.
    monkeypatch.setattr(panes, 'PRESETS', {'cat': panes.Preset('cat', None, r'^WORKING$', r'^CONFIRM\?$')})
    task_hub.message['acknowledged'] = False
    task_hub.tasks = []
    try:
        panes.spawn('bob-pane', 'cat', cwd=str(tmp_path))
        await _watcher(tmp_path).tick()
        deadline = time.monotonic() + 5
        while 'reply_message' not in panes.screen('bob-pane', 40):
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        subprocess.run(['tmux', '-L', socket, 'kill-server'], capture_output=True)
