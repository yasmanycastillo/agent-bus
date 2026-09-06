from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from agent_bus.cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


def test_init_command(runner: CliRunner, tmp_path, monkeypatch):
    tmpdir = tmp_path
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "inicializado" in result.output.lower()
    assert (Path(tmpdir) / ".agent-bus" / "config.yaml").exists()
    assert (Path(tmpdir) / ".agent-bus" / "agents").is_dir()


def test_init_idempotent(runner: CliRunner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "inicializado" in result.output.lower()


def test_commands_require_server(runner: CliRunner, unavailable_bus_url, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_URL", unavailable_bus_url)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1


def test_init_generates_protocols(runner: CliRunner, tmp_path, monkeypatch):
    tmpdir = tmp_path
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    # No agents yet — no protocols generated
    assert not (Path(tmpdir) / "CLAUDE.md").exists()

    # Create agent config and re-init
    from agent_bus.project import create_agent_config

    create_agent_config("claude", {"display_name": "Claude"}, cwd=Path(tmpdir))
    create_agent_config("codex", {"display_name": "Codex"}, cwd=Path(tmpdir))
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (Path(tmpdir) / "CLAUDE.md").exists()
    assert (Path(tmpdir) / "CODEX.md").exists()
    content = (Path(tmpdir) / "CLAUDE.md").read_text()
    assert "agent-bus work as claude" in content


def test_cli_selects_worktree_project_from_external_cwd(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    import yaml
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', *args], check=True, capture_output=True)
    git('init', '-q')
    git('commit', '--allow-empty', '-m', 'initial')
    checkout = tmp_path / 'linked'
    git('worktree', 'add', '-b', 'other', str(checkout))
    nested = checkout / 'src'
    nested.mkdir()
    env = os.environ.copy()
    for name in ('AGENT_BUS_CONFIG_DIR', 'AGENT_BUS_PROJECT_ID', 'AGENT_BUS_PROJECT_ROOT', 'AGENT_BUS_URL'):
        env.pop(name, None)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2] / 'src')
    def cli(*args):
        result = subprocess.run([sys.executable, '-c', 'from agent_bus.cli.main import app; app()',
                                 '--project', str(nested), *args], cwd=tmp_path,
                                env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return result
    cli('init', '--bus-url', 'http://127.0.0.1:18422')
    assert not (checkout / '.agent-bus').exists()
    marker = repo / '.agent-bus'
    assert yaml.safe_load((marker / 'config.yaml').read_text())['bus_url'].endswith(':18422')
    cli('auth', 'create', '--provider', 'claude')
    cli('auth', 'create', '--provider', 'claude')
    sessions = [json.loads(path.read_text()) for path in (marker / 'runtime' / 'credentials').glob('*.json')]
    assert len(sessions) == 2
    assert sessions[0]['agent_id'] != sessions[1]['agent_id']
    assert sessions[0]['project_id'] == sessions[1]['project_id']


def test_project_option_preserves_invocation_relative_runtime(tmp_path, monkeypatch):
    import os
    from agent_bus.config import get_config_dir, load_config
    from agent_bus.cli import main
    selected = tmp_path / 'selected'
    selected.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AGENT_BUS_CONFIG_DIR', 'runtime')
    monkeypatch.setenv('AGENT_BUS_DATABASE_PATH', 'data/hub.db')
    monkeypatch.setenv('AGENT_BUS_SESSION_FILE', 'credentials/alice.json')
    observed = []
    def check():
        observed.append((get_config_dir(), load_config().database_path, os.environ['AGENT_BUS_SESSION_FILE']))
    monkeypatch.setattr(main.status, 'callback', check)
    result = CliRunner().invoke(app, ['--project', str(selected), 'status'])
    assert result.exit_code == 0, result.output
    assert observed == [(tmp_path / 'runtime', str(tmp_path / 'data/hub.db'), str(tmp_path / 'credentials/alice.json'))]
    assert Path.cwd() == tmp_path
    assert os.environ['AGENT_BUS_CONFIG_DIR'] == 'runtime'
