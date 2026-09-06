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
    message = await server.execute_tool("post_message", {"to_agent": "bob", "text": "Review", "idempotency_key": "session-bound"})
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
    async def run(cmd, agent_id):
        captured["env"] = watch.worker_environment(agent_id)
        return Result()
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={
                "message_id": "incoming", "from_agent": "bob", "body": {"text": "Review"},
                "conversation_id": "c1", "acknowledged": False,
            })
        captured["request"] = request
        return httpx.Response(200, json={"message_id": "reply"})
    monkeypatch.setattr(watch.shutil, "which", lambda _: "/unused")
    monkeypatch.setattr(watch, "_run_cli", run)
    install_transport(monkeypatch, watch, handle)
    await watch.run_turn("alice", {"message_id": "incoming", "from_agent": "bob", "body": {"text": "Review"}},
                         {}, sessions_file=tmp_path / "watch.json")
    assert captured["env"]["AGENT_BUS_SESSION_FILE"] == str(credentials["alice"][1])
    assert captured["request"].headers["Authorization"] == f"Bearer {credentials['alice'][0]['token']}"
    assert captured["request"].url.path == "/inbox/alice/incoming/reply"
    payload = json.loads(captured["request"].content)
    assert payload["idempotency_key"] == "watch-reply:incoming"
    assert payload["acknowledge"] is True
    assert payload["body"]["text"] == "Reviewed"
    assert "from_agent" not in payload


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


def test_display_switches_configuration_at_use_time(tmp_path, monkeypatch):
    from agent_bus.cli import display
    monkeypatch.delenv("AGENT_BUS_AGENT_ID", raising=False)
    monkeypatch.delenv("AGENT_ID", raising=False)
    first, second = tmp_path / "first", tmp_path / "second"
    # Deliberately leave CURRENT_AGENT_FILE at the fixture's unrelated location:
    # neither lookup nor update may depend on that cached import-time constant.
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(first))
    display.set_current_agent("alice")
    assert display.get_current_agent() == "alice"
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(second))
    assert display.get_current_agent() is None
    display.set_current_agent("bob")
    assert display.get_current_agent() == "bob"
    assert (first / "current_agent").read_text() == "alice"
    assert (second / "current_agent").read_text() == "bob"


def test_worker_paths_remain_valid_after_changing_worktree(credentials, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", "config")
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", "config/credentials/alice.json")
    env = worker_environment("alice")
    assert Path(env["AGENT_BUS_CONFIG_DIR"]).is_absolute()
    assert Path(env["AGENT_BUS_SESSION_FILE"]).is_absolute()
    other_worktree = tmp_path / "worktree"
    other_worktree.mkdir()
    monkeypatch.chdir(other_worktree)
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", env["AGENT_BUS_CONFIG_DIR"])
    assert security.load_session("alice", session_file=env["AGENT_BUS_SESSION_FILE"])["agent_id"] == "alice"


def test_hook_uses_installed_entrypoint_without_system_package(credentials, monkeypatch, tmp_path):
    import os
    import subprocess
    import sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    # This server captures the hook subprocess; it never touches a personal hub.
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            body = json.dumps({"messages": [{"from_agent": "bob", "reply_needed": True, "body": {"text": "Please review"}}], "next_cursor": None}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        # The entrypoint's shebang chooses this venv. A dummy system python3
        # rejects imports, demonstrating that the shell hook never depends on it.
        binaries = tmp_path / "bin"
        binaries.mkdir()
        python_stub = binaries / "python3"
        python_stub.write_text("#!/bin/sh\nexit 91\n")
        python_stub.chmod(0o755)
        entrypoint = binaries / "agent-bus"
        entrypoint.write_text(f"#!{sys.executable}\nfrom agent_bus.cli.main import app\napp()\n")
        entrypoint.chmod(0o755)
        env["PATH"] = f"{binaries}:/usr/bin:/bin"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        env["AGENT_BUS_AGENT_ID"] = "alice"
        env["AGENT_BUS_URL"] = f"http://127.0.0.1:{server.server_port}"
        hook = Path(__file__).resolve().parents[1] / "hooks" / "stop-check-inbox.sh"
        result = subprocess.run(["/bin/bash", str(hook)], env=env, capture_output=True, text=True, timeout=8)
        assert result.returncode == 0
        assert json.loads(result.stdout)["decision"] == "block", result.stderr
        assert requests == [("/inbox/alice/messages?reply_needed=true&limit=3", f"Bearer {credentials['alice'][0]['token']}")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
