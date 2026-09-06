"""Acquisition tokens are explicit and paths retain each client's scope."""
import json

import httpx
import pytest
from click.testing import CliRunner
from mcp import Client

from agent_bus.cli import main
from agent_bus.mcp.server import McpServer


@pytest.mark.parametrize('command', ['unlock', 'renew-lock'])
def test_cli_requires_acquisition_before_http(command, monkeypatch):
    monkeypatch.setattr(main, '_client', lambda: pytest.fail('must validate before HTTP'))
    result = CliRunner().invoke(main.work, [command, 'file.py'])
    assert result.exit_code == 2
    assert '--acquisition-id' in result.output


def test_cli_acquire_renew_release_tokens(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, '_require_agent', lambda: 'alice')
    requests = []
    def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'acquisition_id': 'acquisition-A', 'expires_at': '2099-01-01'})
    monkeypatch.setattr(main, '_client', lambda: httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(handler)))
    runner = CliRunner()
    acquired = runner.invoke(main.work, ['lock', 'file.py', '--ttl', '45'])
    assert acquired.exit_code == 0, acquired.output
    assert 'acquisition_id: acquisition-A' in acquired.output
    assert 'expires_at:' in acquired.output
    for command in ('renew-lock', 'unlock'):
        result = runner.invoke(main.work, [command, 'file.py', '--acquisition-id', 'acquisition-A'])
        assert result.exit_code == 0, result.output
    assert [path for path, _ in requests] == ['/locks/acquire', '/locks/renew', '/locks/release']
    assert all(body['file_path'] == str(tmp_path / 'file.py') for _, body in requests)
    assert requests[0][1]['ttl_seconds'] == 45
    assert all(body['acquisition_id'] == 'acquisition-A' for _, body in requests[1:])


@pytest.mark.asyncio
async def test_mcp_schema_requires_tokens_before_http(monkeypatch):
    server = McpServer(agent_id='alice')
    monkeypatch.setattr(server, '_client', lambda *args, **kwargs: pytest.fail('must validate before HTTP'))
    async with Client(server.sdk_server()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ('release_lock', 'renew_lock'):
            assert 'acquisition_id' in tools[name].input_schema['required']
            result = await client.call_tool(name, {'file_path': 'f.py', 'agent_id': 'alice'})
            assert result.is_error
        result = await client.call_tool('acquire_lock', {'file_path': 'f.py', 'agent_id': 'alice', 'ttl_seconds': 0})
        assert result.is_error


@pytest.mark.asyncio
async def test_mcp_pins_checkout_cwd_and_propagates_token(tmp_path, monkeypatch):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    server = McpServer(agent_id='alice')
    monkeypatch.chdir(second)
    requests = []
    def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'acquisition_id': 'exact-token', 'expires_at': 'later'})
    monkeypatch.setattr(server, '_client', lambda *args, **kwargs: httpx.AsyncClient(
        base_url='http://127.0.0.1', transport=httpx.MockTransport(handler)))
    args = {'file_path': 'file.py', 'agent_id': 'alice'}
    await server.execute_tool('acquire_lock', {**args, 'ttl_seconds': 17})
    await server.execute_tool('renew_lock', {**args, 'acquisition_id': 'exact-token', 'ttl_seconds': 19})
    await server.execute_tool('release_lock', {**args, 'acquisition_id': 'exact-token'})
    assert all(body['file_path'] == str(first / 'file.py') for _, body in requests)
    assert requests[1][1]['ttl_seconds'] == 19
    assert requests[2][1]['acquisition_id'] == 'exact-token'


@pytest.mark.asyncio
async def test_mcp_pins_project_root_and_preserves_logical_path(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setenv('AGENT_BUS_PROJECT_ROOT', str(project))
    monkeypatch.chdir(project)
    server = McpServer(agent_id='alice')
    monkeypatch.setenv('AGENT_BUS_PROJECT_ROOT', str(tmp_path / 'other'))
    monkeypatch.chdir(tmp_path)
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'released'})
    monkeypatch.setattr(server, '_client', lambda *args, **kwargs: httpx.AsyncClient(
        base_url='http://127.0.0.1', transport=httpx.MockTransport(handler)))
    await server.execute_tool('release_lock', {'file_path': 'src/file.py', 'scope': 'project',
                                             'agent_id': 'alice', 'acquisition_id': 'old-token'})
    assert captured[0]['file_path'] == 'src/file.py'
    assert captured[0]['scope'] == 'project'
    assert captured[0]['acquisition_id'] == 'old-token'


@pytest.mark.asyncio
async def test_mcp_without_initial_project_does_not_adopt_later_environment(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENT_BUS_PROJECT_ROOT', raising=False)
    monkeypatch.setenv('AGENT_BUS_CONFIG_DIR', str(tmp_path / 'isolated'))
    server = McpServer(agent_id='alice')
    assert server._lock_project_root is None
    monkeypatch.setenv('AGENT_BUS_PROJECT_ROOT', str(tmp_path))
    monkeypatch.setattr(server, '_client', lambda *args, **kwargs: httpx.AsyncClient(
        base_url='http://127.0.0.1', transport=httpx.MockTransport(lambda request: pytest.fail('no HTTP'))))
    with pytest.raises(ValueError, match='when the MCP starts'):
        await server.execute_tool('acquire_lock', {'file_path': 'file.py', 'scope': 'project', 'agent_id': 'alice'})
