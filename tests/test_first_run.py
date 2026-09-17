"""First-use CLI exercised against its own authenticated hub and real MCP pipes."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import click
import httpx
import pytest
from mcp import Client, StdioServerParameters, stdio_client

import agent_bus
from agent_bus.cli.first_run import parse_agents, probe_hub


@pytest.mark.parametrize('value', ['', '../bad:claude', 'one:claude,one:codex', 'free', 'one:'])
def test_invalid_identities(value):
    with pytest.raises(click.ClickException):
        parse_agents(value)


@pytest.mark.parametrize('body', [{'project_id': 'elsewhere'}, [], {'status': 'ok'}])
def test_wrong_hub_is_rejected_before_credentials(monkeypatch, body):
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: httpx.Response(200, json=body))
    with pytest.raises(click.ClickException, match='otro servicio o proyecto'):
        probe_hub('http://127.0.0.1:8421', 'expected')


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_BUS_') and k != 'AGENT_ID'}
    env['PYTHONPATH'] = str(Path(agent_bus.__file__).resolve().parent.parent)
    return env


def invoke(root, env, *args, input=None):
    return subprocess.run([sys.executable, '-m', 'agent_bus.cli.main', '--project', str(root), *args],
                          env=env, cwd=root, text=True, capture_output=True, input=input, timeout=30)


def test_decline_and_bad_identity_leave_no_project(tmp_path):
    env = clean_env()
    bad = invoke(tmp_path, env, 'onboard', '--mcp-only', '--agents', '../bad', '--yes')
    assert bad.returncode != 0
    assert not (tmp_path / '.agent-bus').exists()
    declined = invoke(tmp_path, env, 'onboard', '--mcp-only', input='n\n')
    assert declined.returncode != 0
    assert not (tmp_path / '.agent-bus').exists()


def test_mcp_onboarding_real_hub_repeat_and_revocation(tmp_path):
    env = clean_env()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    args = ['onboard', '--mcp-only', '--yes', '--port', str(port), '--agents', 'backend:grok,qa:codex']
    runtime = tmp_path / '.agent-bus' / 'runtime'
    try:
        first = invoke(tmp_path, env, *args)
        assert first.returncode == 0, first.stdout + first.stderr
        assert 'sesiones verificados' in first.stdout
        credentials = {p.name: p.read_bytes() for p in (runtime / 'credentials').glob('*.json')}
        for raw in credentials.values():
            assert json.loads(raw)['token'] not in first.stdout
        assert len(credentials) == 3
        assert not (tmp_path / '.worktrees').exists()
        assert not (runtime / 'workers').exists()
        launcher = runtime / 'watch' / 'backend' / 'start.sh'
        assert launcher.stat().st_mode & 0o777 == 0o700
        script = launcher.read_text()
        assert '--cli grok' in script
        assert all(json.loads(raw)['token'] not in script for raw in credentials.values())
        status = subprocess.run([str(launcher), '--status'], cwd='/', env=env,
                                text=True, capture_output=True, timeout=10)
        assert status.returncode == 0, status.stderr
        assert json.loads(status.stdout)['state'] == 'stopped'
        second = invoke(tmp_path, env, 'onboard', '--mcp-only', '--yes', '--agents', 'backend:grok,qa:codex')
        assert second.returncode == 0, second.stdout + second.stderr
        assert credentials == {p.name: p.read_bytes() for p in (runtime / 'credentials').glob('*.json')}
        assert launcher.read_text() == script

        async def bootstrap_both():
            for name in ('backend', 'qa'):
                snippet = json.loads((runtime / 'mcp' / f'{name}.json').read_text())['mcpServers']['agent-bus']
                params = StdioServerParameters(command=snippet['command'], args=snippet['args'],
                                               env={**env, **snippet['env']}, cwd=str(tmp_path))
                async with Client(stdio_client(params)) as client:
                    result = await client.call_tool('bootstrap_agent', {})
                    assert not result.is_error, result
                    assert name in str(result.structured_content)
        asyncio.run(bootstrap_both())

        session = json.loads(credentials['backend.json'])
        revoked = invoke(tmp_path, env, 'auth', 'revoke', '--session', session['session_id'])
        assert revoked.returncode == 0
        rejected = invoke(tmp_path, env, *args)
        assert rejected.returncode != 0
        assert 'rechazó la sesión de backend' in rejected.stderr
        assert session['token'] not in rejected.stdout + rejected.stderr
        assert (runtime / 'credentials' / 'backend.json').read_bytes() == credentials['backend.json']
    finally:
        invoke(tmp_path, env, 'serve', '--stop')


def test_occupied_port_leaves_new_project_untouched(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from agent_bus.cli.main import app
    for key in list(os.environ):
        if key.startswith('AGENT_BUS_'):
            monkeypatch.delenv(key)
    monkeypatch.setattr(httpx, 'get', lambda *a, **kw: httpx.Response(200, json={'project_id': 'other'}))
    result = CliRunner().invoke(app, ['--project', str(tmp_path), 'onboard', '--mcp-only', '--yes'])
    assert result.exit_code != 0
    assert 'otro servicio o proyecto' in result.output
    assert not (tmp_path / '.agent-bus').exists()


def test_inherited_database_override_is_rejected(tmp_path):
    env = clean_env()
    env['AGENT_BUS_DATABASE_PATH'] = str(tmp_path / 'other.db')
    result = invoke(tmp_path, env, 'onboard', '--mcp-only', '--yes')
    assert result.returncode != 0
    assert 'sin overrides' in result.stderr
    assert not (tmp_path / '.agent-bus').exists()
    assert not (tmp_path / 'other.db').exists()
