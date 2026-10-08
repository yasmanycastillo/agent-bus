"""Agent panes on a real tmux server with a private socket; `sh` stands in for an agent TUI."""
import shutil
import subprocess
import time
import uuid

import httpx
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
    # The coordinator's own session and identity must not reach a pane.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "coordinator")
    monkeypatch.setenv("AGENT_BUS_PANES", "1")
    assert panes.list_panes() == []
    panes.spawn("a", "sh", cwd=str(tmp_path), env={"AGENT_BUS_AGENT_ID": "a", "AGENT_BUS_PANES": "1"})
    panes.spawn("b", "sh", cwd=str(tmp_path))
    assert states() == {"a": "idle", "b": "idle"}
    with pytest.raises(panes.PaneError, match="already exists"):
        panes.spawn("a", "sh")
    with pytest.raises(panes.PaneError, match="unknown preset"):
        panes.spawn("c", "nope")
    with pytest.raises(panes.PaneError, match="invalid pane name"):
        panes.spawn("foo.bar", "sh")

    panes.send("a", 'echo "$AGENT_BUS_AGENT_ID in $PWD [$CLAUDE_CODE_SESSION_ID$AGENT_BUS_PANES]"')
    wait_for(lambda: f"a in {tmp_path} []" in panes.screen("a"))
    panes.send("b", 'echo "id=[$AGENT_BUS_AGENT_ID]"')
    wait_for(lambda: "id=[]" in panes.screen("b"))

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


def test_prompt_and_model_stay_values_not_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(panes, "_tmux", lambda *args, **kwargs: calls.append(args) or "")

    def argv():
        create = next(c for c in calls if c[0] in ("new-session", "new-window"))
        calls.clear()
        return create[create.index("--") + 1:create.index(";")]

    panes.spawn("x", "claude", prompt="--dangerously-skip-permissions", model="--settings=evil")
    assert argv()[-3:] == ("--model=--settings=evil", "--", "--dangerously-skip-permissions")
    panes.spawn("y", "agy", prompt="--dangerously-skip-permissions")
    assert argv()[-2:] == ("agy", "-i=--dangerously-skip-permissions")


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
    asked = []

    class Hub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, path):
            asked.append(path)
            return httpx.Response(200, json={"coordinator": "alice", "agents": ["claude-01"]},
                                  request=httpx.Request("GET", "http://hub" + path))

    monkeypatch.setattr(server, "_client", lambda: Hub())
    monkeypatch.setattr("agent_bus.worker.client.worker_environment",
                        lambda agent_id, per_agent: {"AGENT_BUS_AGENT_ID": agent_id})

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

        # A pane may only borrow the identity of an agent this coordinator assigned work to.
        error, body = await call(action="spawn", name="b", preset="claude", as_agent="admin")
        assert error and body["code"] == "invalid_arguments" and "claude-01" in body["error"]
        assert asked == ["/instructions/assignees"]
        assert await call(action="spawn", name="b", preset="claude", as_agent="claude-01") == (
            False, {"status": "ok", "name": "b"})
        await call(action="send", name="b", text='echo "id=$AGENT_BUS_AGENT_ID"')
        wait_for(lambda: "id=claude-01" in panes.screen("b"))
        await call(action="close", name="b")
        assert (await call(action="list"))[1]["panes"] == []


async def test_console_shows_each_pane_screen_read_only(tmux, tmp_path):
    from httpx import ASGITransport, AsyncClient

    from agent_bus.core.bus import MessageBus
    from agent_bus.core.inbox import InboxManager
    from agent_bus.core.registry import AgentRegistry
    from agent_bus.reputation.database import Database

    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db=db, registry=AgentRegistry(), inbox=InboxManager(db))
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        assert (await client.get("/room/api/panes")).json() == {"panes": []}
        panes.spawn("a", "sh", cwd=str(tmp_path))
        panes.send("a", "echo VISIBLE-IN-CONSOLE")
        wait_for(lambda: "VISIBLE-IN-CONSOLE\n" in panes.screen("a", 50) + "\n")
        [pane] = (await client.get("/room/api/panes")).json()["panes"]
        assert pane["name"] == "a" and pane["state"] == "idle" and "VISIBLE-IN-CONSOLE" in pane["screen"]
    await db.close()


@pytest.mark.parametrize("footer, expected", [
    ("⡿  Generating...\n>\nesc to cancel    Claude Opus 5.5 · medium", "working"),
    # Captured from agy 2026-10: its permission menu keeps the "esc to cancel" footer.
    ("Run this command?\n> 1. Yes, run command\n  4. No, cancel\n"
     "  ↑/↓ Navigate · tab Amend · ctrl+g edit/expand command\nesc to cancel", "blocked"),
    ("Do you trust the contents of this project?\n> Yes, I trust this folder\n"
     "  ↑/↓ Navigate · enter Confirm", "blocked"),
    ("  ok\n>\n? for shortcuts", "idle"),
])
def test_agy_states_from_its_footer(monkeypatch, footer, expected):
    monkeypatch.setattr(panes, "screen", lambda name, lines=panes.SCREEN_LINES: footer)
    assert panes.state("x", "agy") == expected
