"""Agent panes on a real tmux server with a private socket; `sh` stands in for an agent TUI."""
import shutil
import subprocess
import time
import uuid

import pytest

from agent_bus import panes

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@pytest.fixture
def tmux(monkeypatch):
    socket = f"agent-bus-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(panes, "TMUX", ["tmux", "-L", socket])
    monkeypatch.setattr(panes, "PRESETS", {"sh": panes.Preset("sh", None, r"^WORKING$", r"^CONFIRM\?$")})
    yield
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def wait_for(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.05)


def states():
    return {p["name"]: p["state"] for p in panes.list_panes()}


def test_coordinator_drives_pane_lifecycle(tmux, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    assert panes.list_panes() == []
    panes.spawn("a", "sh", cwd=str(tmp_path), env={"AGENT_BUS_AGENT_ID": "a"})
    panes.spawn("b", "sh", cwd=str(tmp_path))
    assert states() == {"a": "idle", "b": "idle"}
    with pytest.raises(panes.PaneError, match="already exists"):
        panes.spawn("a", "sh")
    with pytest.raises(panes.PaneError, match="unknown preset"):
        panes.spawn("c", "nope")

    panes.send("a", 'echo "$AGENT_BUS_AGENT_ID in $PWD [$CLAUDE_CODE_SESSION_ID]"')
    wait_for(lambda: f"a in {tmp_path} []" in panes.screen("a"))

    panes.send("a", "echo WORK''ING")
    wait_for(lambda: states()["a"] == "working")
    with pytest.raises(panes.PaneError, match="is working"):
        panes.send("a", "echo again")
    panes.send("b", "echo CONFIRM''?")
    wait_for(lambda: states()["b"] == "blocked")

    panes.send("b", "exit", force=True)
    wait_for(lambda: states()["b"] == "dead")
    panes.close("b")
    panes.close("a")
    assert panes.list_panes() == []


async def test_coordinator_drives_panes_through_mcp(tmux, tmp_path, monkeypatch):
    import json

    from mcp import Client

    # The tool's preset enum is fixed at import (agy, claude); run `sh` under "claude".
    monkeypatch.setattr(panes, "PRESETS", {"agy": panes.PRESETS["sh"], "claude": panes.PRESETS["sh"]})
    from agent_bus.mcp.server import McpServer
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "0")
    monkeypatch.setattr("agent_bus.mcp.server.load_session", lambda agent_id: {
        "agent_id": "alice", "session_id": "s", "project_id": "p", "role": "agent",
        "token": "t", "expires_at": time.time() + 3600,
    })
    assert "agent_panes" not in {t["name"] for t in McpServer(agent_id="alice").tools}
    monkeypatch.setenv("AGENT_BUS_PANES", "1")
    server = McpServer(agent_id="alice")
    server._lock_project_root = tmp_path

    async with Client(server.sdk_server()) as client:
        async def call(**args):
            result = await client.call_tool("agent_panes", args)
            return result.is_error, json.loads(result.content[0].text)

        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        # No free CLI arguments or directory: an agent cannot start another without permissions.
        assert set(tools["agent_panes"].input_schema["properties"]) == {
            "action", "name", "preset", "as_agent", "model", "prompt", "text", "lines"}
        spawned = await call(action="spawn", name="a", preset="claude")
        assert spawned == (False, {"status": "ok", "name": "a"}), spawned
        assert await call(action="send", name="a", text="pwd") == (False, {"status": "ok", "name": "a"})
        wait_for(lambda: str(tmp_path) in panes.screen("a"))
        assert (await call(action="list"))[1]["panes"] == [{"name": "a", "preset": "claude", "state": "idle"}]
        error, body = await call(action="spawn", name="a", preset="claude")
        assert error and body["code"] == "pane_error" and "already exists" in body["error"]
        error, body = await call(action="send", name="a")
        assert error and body["code"] == "invalid_arguments"
        assert (await call(action="close", name="a"))[0] is False
        assert (await call(action="list"))[1]["panes"] == []
