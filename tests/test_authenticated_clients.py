"""Client identity and credential propagation without a personal bus or subprocess."""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from agent_bus import security
from agent_bus.mcp.server import McpServer
from agent_bus.worker.client import BusEventClient, worker_environment
from agent_bus.worker.runner import AgentRunner


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "clients-test")
    monkeypatch.delenv("AGENT_BUS_SESSION_FILE", raising=False)
    monkeypatch.delenv("AGENT_BUS_AGENT_ID", raising=False)
    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.delenv("AGENT_BUS_ALLOW_UNSIGNED", raising=False)
    result = {}
    for agent, role in (("alice", "agent"), ("bob", "agent"), ("operator", "admin")):
        session = dict(token=(agent + "_") * 12, agent_id=agent, session_id=agent + "-session",
                       project_id="clients-test", role=role, expires_at=time.time() + 3600)
        path = tmp_path / "config" / "credentials" / f"{agent}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session))
        path.chmod(0o600)
        result[agent] = (session, path)
    return result


def install_transport(monkeypatch, module, handler, *, synchronous=False):
    original = security.sync_bus_client if synchronous else security.async_bus_client
    def factory(*args, **kwargs):
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(module, "sync_bus_client" if synchronous else "async_bus_client", factory)


async def test_mcp_binds_actor_and_token_at_startup(credentials, monkeypatch):
    import agent_bus.mcp.server as mcp
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(credentials["alice"][1]))
    server = McpServer(bus_url="http://127.0.0.1:8420")
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(credentials["bob"][1]))
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=json.loads(request.content))
    install_transport(monkeypatch, mcp, handle)
    message = await server.execute_tool("post_message", {"to_agent": "bob", "text": "Review"})
    assert message["from_agent"] == "alice"
    assert requests[0].headers["Authorization"] == f"Bearer {credentials['alice'][0]['token']}"
    with pytest.raises(ValueError, match="authenticated MCP session"):
        await server.execute_tool("post_message", {"from_agent": "bob", "to_agent": "alice", "text": "Spoof"})
    assert len(requests) == 1
    tools = (await server.handle_request({"id": 1, "method": "tools/list"}))["result"]["tools"]
    for tool in tools:
        assert not {"agent_id", "from_agent", "decided_by"} & tool["inputSchema"]["properties"].keys()
        assert not {"agent_id", "from_agent", "decided_by"} & set(tool["inputSchema"].get("required", []))


async def test_mcp_decision_uses_session_actor(credentials, monkeypatch):
    import agent_bus.mcp.server as mcp
    server = McpServer(agent_id="alice")
    install_transport(monkeypatch, mcp, lambda request: httpx.Response(200, json=json.loads(request.content)))
    decision = await server.execute_tool("record_decision", {"title": "ADR", "what": "Use SQLite"})
    assert decision["decided_by"] == "alice"
    assert decision["decision"] == "Use SQLite"


async def test_mcp_wait_does_not_mask_unauthorized_as_empty(credentials, monkeypatch):
    import agent_bus.mcp.server as mcp
    requests = []
    def reject(request):
        requests.append(request)
        return httpx.Response(401, json={"detail": "Invalid session"})
    install_transport(monkeypatch, mcp, reject)
    result = await McpServer(agent_id="alice").execute_tool("wait_for_updates", {"timeout": 1})
    assert result["status"] == "error"
    assert len(requests) == 1


async def test_sse_client_sends_bound_session(credentials, monkeypatch):
    import agent_bus.worker.client as clients
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, text='event: message\ndata: {"from_agent":"bob"}\n\n')
    install_transport(monkeypatch, clients, handle)
    events = []
    async def on_event(event):
        events.append(event)
    client = BusEventClient("alice", on_event=on_event)
    client._running = True
    await client._consume_sse()
    assert events[0]["from_agent"] == "bob"
    assert requests[0].url.path == "/events/alice"
    assert requests[0].headers["Authorization"] == f"Bearer {credentials['alice'][0]['token']}"


def test_team_selects_agent_file_instead_of_admin_override(credentials, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(credentials["operator"][1]))
    env = worker_environment("alice", per_agent=True)
    assert env["AGENT_BUS_AGENT_ID"] == "alice"
    assert env["AGENT_BUS_SESSION_FILE"] == str(credentials["alice"][1])
    assert credentials["operator"][0]["token"] not in env.values()
    with pytest.raises(security.AuthenticationError, match="identity"):
        worker_environment("alice")


async def test_runner_rejects_inherited_admin_session(credentials, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(credentials["operator"][1]))
    async def forbidden(*args, **kwargs):
        pytest.fail("Must reject mismatched credentials before spawning")
    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    result = await AgentRunner("alice")._run_subprocess(["unused"], 1)
    assert not result.success
    assert "identity" in result.error


async def test_runner_passes_own_session_path(credentials, monkeypatch):
    captured = {}
    class Process:
        returncode = 0
        async def communicate(self):
            return b"ok", b""
    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        return Process()
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    result = await AgentRunner("alice")._run_subprocess(["unused"], 1)
    assert result.success
    assert captured["env"]["AGENT_BUS_AGENT_ID"] == "alice"
    assert captured["env"]["AGENT_BUS_SESSION_FILE"] == str(credentials["alice"][1])


def test_submit_uses_actual_operator_identity(credentials, monkeypatch):
    import agent_bus.cli.worker_cmds as commands
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(credentials["operator"][1]))
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "operator")
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"message_id": "sent"})
    install_transport(monkeypatch, commands, handle, synchronous=True)
    result = CliRunner().invoke(commands.submit_goal, ["Review project"])
    assert result.exit_code == 0, result.output
    assert json.loads(requests[0].content)["from_agent"] == "operator"
    assert requests[0].headers["Authorization"] == f"Bearer {credentials['operator'][0]['token']}"


def test_cli_unauthorized_inbox_is_not_reported_empty(credentials, monkeypatch):
    import agent_bus.cli.main as commands
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "alice")
    install_transport(monkeypatch, commands,
                      lambda request: httpx.Response(401, json={"detail": "Expired"}), synchronous=True)
    result = CliRunner().invoke(commands.app, ["work", "check"])
    assert result.exit_code != 0
    assert "Inbox vacio" not in result.output
    assert "Sesión inválida" in result.output


async def test_watcher_response_and_child_share_session(credentials, monkeypatch, tmp_path):
    import agent_bus.cli.watch_cmds as watch
    captured = {}
    class Result:
        returncode = 0
        stdout = '{"session_id":"thread-session","result":"Reviewed"}'
        stderr = ''
    def run(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return Result()
    def handle(request):
        captured["request"] = request
        return httpx.Response(200, json={"message_id": "reply"})
    monkeypatch.setattr(watch.shutil, "which", lambda _: "/unused")
    monkeypatch.setattr(watch.subprocess, "run", run)
    install_transport(monkeypatch, watch, handle)
    await watch.run_turn("alice", {"message_id": "incoming", "from_agent": "bob", "body": {"text": "Review"}},
                         {}, sessions_file=tmp_path / "watch.json")
    assert captured["env"]["AGENT_BUS_SESSION_FILE"] == str(credentials["alice"][1])
    assert captured["request"].headers["Authorization"] == f"Bearer {credentials['alice'][0]['token']}"
    assert json.loads(captured["request"].content)["from_agent"] == "alice"


def test_team_explicit_admin_never_falls_back_to_unsigned(credentials, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(credentials["operator"][1]))
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    with pytest.raises(security.AuthenticationError):
        worker_environment("missing", per_agent=True)


async def test_daemon_registers_only_its_authenticated_identity(credentials, monkeypatch):
    import agent_bus.worker.daemon as daemons
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(201 if request.url.path == "/register" else 200, json={})
    install_transport(monkeypatch, daemons, handle)
    daemon = daemons.WorkerDaemon("alice", AgentRunner("alice", provider="mock"))
    async def finish(*args):
        return None
    monkeypatch.setattr(daemon, "_heartbeat_loop", finish)
    monkeypatch.setattr(daemon, "_event_poll_loop", finish)
    monkeypatch.setattr(daemons.BusEventClient, "start", finish)
    await daemon.start()
    assert json.loads(requests[0].content)["agent_id"] == "alice"
    assert requests[0].headers["Authorization"] == f"Bearer {credentials['alice'][0]['token']}"
    assert daemon._client is None


async def test_daemon_registration_failure_closes_client(credentials, monkeypatch):
    import agent_bus.worker.daemon as daemons
    install_transport(monkeypatch, daemons, lambda request: httpx.Response(403, json={}))
    daemon = daemons.WorkerDaemon("alice", AgentRunner("alice", provider="mock"))
    with pytest.raises(httpx.HTTPStatusError):
        await daemon.start()
    assert daemon._client is None
    assert not daemon._running
