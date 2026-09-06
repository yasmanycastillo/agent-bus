"""Local execution exclusion and project-bound subprocess configuration."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from agent_bus.worker.execution import ExecutionBusy, ExecutionGuard
from agent_bus.worker.client import worker_environment


def test_guard_independent_process_and_process_death(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_BUS_DATABASE_PATH', str(tmp_path / 'bus.db'))
    monkeypatch.setenv('AGENT_BUS_PROJECT_ID', 'project-a')
    code = '''
from agent_bus.worker.execution import ExecutionGuard
import sys
with ExecutionGuard('alice'):
    print('ready', flush=True)
    sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, env=os.environ.copy())
    try:
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(ExecutionBusy):
            with ExecutionGuard('alice', kind='watcher'):
                pytest.fail('duplicate executor')
        with ExecutionGuard('bob'):
            pass
        monkeypatch.setenv('AGENT_BUS_PROJECT_ID', 'project-b')
        with ExecutionGuard('alice'):
            pass
        monkeypatch.setenv('AGENT_BUS_PROJECT_ID', 'project-a')
        child.kill()
        child.wait(timeout=3)
        guard = ExecutionGuard('alice')
        with guard:
            inode = guard.path.stat().st_ino
        assert guard.path.stat().st_ino == inode
        with ExecutionGuard('alice'):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        child.stdin.close()
        child.stdout.close()


def test_guard_shared_database_with_distinct_config_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_BUS_DATABASE_PATH', str(tmp_path / 'bus.db'))
    monkeypatch.setenv('AGENT_BUS_PROJECT_ID', 'same-project')
    with ExecutionGuard('alice'):
        monkeypatch.setenv('AGENT_BUS_CONFIG_DIR', str(tmp_path / 'other-runtime'))
        with pytest.raises(ExecutionBusy):
            with ExecutionGuard('alice'):
                pass


def test_guard_rejects_symlink_and_insecure_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_BUS_DATABASE_PATH', str(tmp_path / 'bus.db'))
    guard = ExecutionGuard('alice')
    guard.path.parent.mkdir(parents=True)
    target = tmp_path / 'target'
    target.write_text('untouched')
    guard.path.symlink_to(target)
    with pytest.raises(OSError):
        with guard:
            pass
    assert target.read_text() == 'untouched'
    guard.path.unlink()
    guard.path.write_text('')
    guard.path.chmod(0o644)
    with pytest.raises(RuntimeError, match='0600'):
        with guard:
            pass


@pytest.mark.asyncio
async def test_worker_and_watcher_share_guard_but_observers_coexist(tmp_path, monkeypatch):
    from agent_bus.worker.daemon import WorkerDaemon
    from agent_bus.worker.runner import AgentRunner
    from agent_bus.cli.watch_cmds import PendingMessageWatcher
    monkeypatch.setenv('AGENT_BUS_DATABASE_PATH', str(tmp_path / 'bus.db'))
    daemon = WorkerDaemon('alice', AgentRunner('alice', provider='mock'), bus_url='http://127.0.0.1:12345')
    watcher = PendingMessageWatcher('alice', bus_url='http://127.0.0.1:12345')
    observer = PendingMessageWatcher('alice', bus_url='http://127.0.0.1:12345', dry_run=True)
    daemon._run = AsyncMock()
    watcher._run = AsyncMock()
    observer._run = AsyncMock()
    with ExecutionGuard('alice'):
        with pytest.raises(ExecutionBusy):
            await daemon.start()
        with pytest.raises(ExecutionBusy):
            await watcher.run()
        await observer.run()
    daemon._run.assert_not_awaited()
    watcher._run.assert_not_awaited()
    observer._run.assert_awaited_once()
    await daemon.start()
    await watcher.run()
    assert daemon.runner.bus_url == 'http://127.0.0.1:12345'


def test_environment_freezes_project_before_child_cwd(tmp_path, monkeypatch):
    parent = tmp_path / 'parent'
    parent.mkdir()
    monkeypatch.chdir(parent)
    monkeypatch.setenv('AGENT_BUS_CONFIG_DIR', 'runtime')
    monkeypatch.setenv('AGENT_BUS_DATABASE_PATH', 'state/bus.db')
    monkeypatch.setenv('AGENT_BUS_PROJECT_ID', 'sample')
    monkeypatch.setenv('AGENT_BUS_URL', 'http://127.0.0.1:12345')
    monkeypatch.delenv('AGENT_BUS_PROJECT_ROOT', raising=False)
    env = worker_environment('alice', bus_url='http://127.0.0.1:23456')
    assert Path(env['AGENT_BUS_CONFIG_DIR']).is_absolute()
    assert Path(env['AGENT_BUS_DATABASE_PATH']).is_absolute()
    assert env['AGENT_BUS_PROJECT_ID'] == 'sample'
    assert env['AGENT_BUS_URL'] == 'http://127.0.0.1:23456'
    assert 'AGENT_BUS_PROJECT_ROOT' not in env
    monkeypatch.chdir(tmp_path)
    assert Path(env['AGENT_BUS_CONFIG_DIR']) == parent / 'runtime'


def test_dynamic_worker_and_session_paths(tmp_path, monkeypatch):
    from agent_bus.cli.worker_cmds import _pid_file
    from agent_bus.cli.watch_cmds import _session_file
    monkeypatch.setenv('AGENT_BUS_CONFIG_DIR', str(tmp_path / 'a'))
    assert _pid_file('alice') == tmp_path / 'a/workers/alice.pid'
    assert _session_file('alice') != _session_file('bob')
    monkeypatch.setenv('AGENT_BUS_CONFIG_DIR', str(tmp_path / 'b'))
    assert _pid_file('alice') == tmp_path / 'b/workers/alice.pid'
    assert _session_file('alice') == tmp_path / 'b/watch/alice/sessions.json'


def test_generated_agent_uses_provider_metadata(monkeypatch):
    from agent_bus.cli import worker_cmds
    monkeypatch.setattr(worker_cmds, 'load_session', lambda *args, **kwargs: {'provider': 'codex'})
    assert worker_cmds._worker_provider('codex-deadbeef', None, {'AGENT_BUS_SESSION_FILE': '/fake'}) == 'codex'
    assert worker_cmds._worker_provider('codex-deadbeef', 'mock', {}) == 'mock'
    monkeypatch.setattr(worker_cmds, 'load_session', lambda *args, **kwargs: {'provider': 'unknown'})
    import click
    with pytest.raises(click.ClickException, match='Proveedor'):
        worker_cmds._worker_provider('codex-deadbeef', None, {'AGENT_BUS_SESSION_FILE': '/fake'})


def test_worker_start_reports_guard_failure_without_pid(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from agent_bus.cli.worker_cmds import worker, _pid_file
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AGENT_BUS_DATABASE_PATH', str(tmp_path / 'bus.db'))
    with ExecutionGuard('alice', kind='watcher'):
        result = CliRunner().invoke(worker, ['start', '--agent', 'alice', '--provider', 'mock',
                                             '--bus-url', 'http://127.0.0.1:12345'])
    assert result.exit_code != 0
    assert 'no inició' in result.output
    assert not _pid_file('alice').exists()


def test_run_team_uses_checkout_root_and_absolute_worktree(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from types import SimpleNamespace
    from agent_bus.cli import worker_cmds
    from agent_bus import project
    from agent_bus.worker import worktrees
    checkout = tmp_path / 'checkout'
    child = checkout / 'subdir'
    child.mkdir(parents=True)
    target = checkout / '.worktrees' / 'alice'
    target.mkdir(parents=True)
    monkeypatch.chdir(child)
    monkeypatch.setattr(project, 'get_checkout_root', lambda: checkout)
    seen = {}
    class Manager:
        def __init__(self, repo_root):
            seen['root'] = repo_root
        def is_repo(self):
            return True
        def create(self, agent, base_ref):
            return SimpleNamespace(path=target, branch='agent/alice')
    monkeypatch.setattr(worktrees, 'WorktreeManager', Manager)
    monkeypatch.setattr(worker_cmds, '_spawn_worker', lambda *args: seen.update(spawn=args))
    result = CliRunner().invoke(worker_cmds.run_team, ['--agents', 'alice', '--mock',
                                                     '--bus-url', 'http://127.0.0.1:12345'])
    assert result.exit_code == 0, result.output
    assert seen['root'] == checkout
    assert seen['spawn'][3] == str(target)
    assert seen['spawn'][5]['AGENT_BUS_URL'] == 'http://127.0.0.1:12345'
