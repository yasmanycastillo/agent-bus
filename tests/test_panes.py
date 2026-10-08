"""Agent panes on a real tmux server with a private socket; `sh` stands in for an agent TUI."""
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from agent_bus import panes

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@pytest.fixture
def tmux(monkeypatch):
    socket = f"agent-bus-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(panes, "TMUX", ["tmux", "-L", socket])
    monkeypatch.setattr(panes, "_project", None)
    monkeypatch.setattr(panes, "PRESETS", {"sh": panes.Preset("sh", None, r"^WORKING$", r"^CONFIRM\?$")})
    yield
    kill_server(socket)


def kill_server(socket):
    """Stop a test server and remove its socket file, which kill-server leaves behind."""
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    (Path(tmpdir) / f"tmux-{os.getuid()}" / socket).unlink(missing_ok=True)


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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-key")  # secrets must not reach a pane
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
    panes.send("b", 'echo "id=[$AGENT_BUS_AGENT_ID] key=[$ANTHROPIC_API_KEY] home=[$HOME]"')
    wait_for(lambda: f"id=[] key=[] home=[{os.environ['HOME']}]" in panes.screen("b"))

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
    asked, role = [], ["agent"]

    class Hub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, path):
            asked.append(path)
            return httpx.Response(200, json={"agent_id": "alice", "role": role[0]},
                                  request=httpx.Request("GET", "http://hub" + path))

    monkeypatch.setattr(server, "_client", lambda: Hub())
    monkeypatch.setattr(panes, "WATCH_COMMAND", ["sh", "-c", "exec sleep 60", "watcher"])
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
        assert (await call(action="list"))[1]["panes"] == [
            {"name": "a", "preset": "claude", "state": "idle", "watcher": "none"}]
        error, body = await call(action="spawn", name="a", preset="claude")
        assert error and body["code"] == "pane_error" and "already exists" in body["error"]
        error, body = await call(action="send", name="a")
        assert error and body["code"] == "invalid_arguments"
        assert (await call(action="close", name="a"))[0] is False

        # Lending another identity's credential takes a session the hub knows as administrator.
        error, body = await call(action="spawn", name="b", preset="claude", as_agent="claude-01")
        assert error and body["code"] == "invalid_arguments" and "administrator" in body["error"]
        assert asked == ["/auth/me"] and panes.list_panes() == []
        role[0] = "admin"
        assert await call(action="spawn", name="b", preset="claude", as_agent="claude-01") == (
            False, {"status": "ok", "name": "b"})
        await call(action="send", name="b", text='echo "id=$AGENT_BUS_AGENT_ID"')
        wait_for(lambda: "id=claude-01" in panes.screen("b"))
        assert panes.list_panes()[0]["watcher"] == "running"  # as_agent brings its watcher
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


AGY_PERMISSION = """\
● Ran (git status --short; touch /tmp/x) (ctrl+o to expand)
Command
Requesting permission for:
   git status --short; touch /tmp/x
Run this command?
> 1. Yes, run command
  2. Yes, and always allow in this conversation for commands that start with
'touch'
  3. Yes, and always allow for commands that start with 'touch' (Persist to
settings.json)
  4. No, cancel
  ↑/↓ Navigate · tab Amend · ctrl+g edit/expand command
esc to cancel"""


def test_question_reads_the_menu_and_ignores_numbered_transcript(monkeypatch):
    monkeypatch.setattr(panes, "screen", lambda name, lines=0: AGY_PERMISSION)
    asked = panes.question("x")
    assert asked["command"] == "   git status --short; touch /tmp/x"
    assert [o["number"] for o in asked["options"]] == [1, 2, 3, 4] and asked["selected"] == 1
    assert asked["options"][3]["label"] == "No, cancel"
    monkeypatch.setattr(panes, "screen", lambda name, lines=0: "Pasos:\n  1. Instala\n  2. Arranca\n>")
    assert panes.question("x") is None
    monkeypatch.setattr(panes, "screen", lambda name, lines=0: "Do you want to proceed?\n❯ 1. Yes\n  2. No")
    assert panes.question("x") == {"command": None, "selected": 1,
                                   "options": [{"number": 1, "label": "Yes"}, {"number": 2, "label": "No"}]}


def test_answer_moves_the_cursor_then_confirms(monkeypatch):
    sent = []
    monkeypatch.setattr(panes, "list_panes", lambda: [{"name": "x", "preset": "agy", "state": "blocked"}])
    monkeypatch.setattr(panes, "screen", lambda name, lines=0: AGY_PERMISSION)
    monkeypatch.setattr(panes, "_tmux", lambda *args, **kw: sent.append(args) or "")
    panes.answer("x", 4)
    assert sent == [("send-keys", "-t", "agents:=x", "-N", "3", "Down"), ("send-keys", "-t", "agents:=x", "Enter")]
    with pytest.raises(panes.PaneError, match="no option 9"):
        panes.answer("x", 9)
    monkeypatch.setattr(panes, "list_panes", lambda: [{"name": "x", "preset": "agy", "state": "working"}])
    with pytest.raises(panes.PaneError, match="not waiting"):
        panes.answer("x", 1)


def test_cli_answer_is_for_people_only(monkeypatch):
    from click.testing import CliRunner

    from agent_bus.cli.panes_cmds import panes as panes_cli

    monkeypatch.setattr(panes, "question", lambda name: {
        "command": "touch /tmp/x", "selected": 1,
        "options": [{"number": 1, "label": "Yes, run command"}, {"number": 2, "label": "No, cancel"}]})
    answered = []
    monkeypatch.setattr(panes, "answer", lambda name, n: answered.append(n))
    shown = CliRunner().invoke(panes_cli, ["answer", "x"])
    assert shown.exit_code == 0 and "touch /tmp/x" in shown.output and "> 1. Yes, run command" in shown.output
    # An agent's Bash has no terminal: it can read the question but never approve it.
    refused = CliRunner().invoke(panes_cli, ["answer", "x", "1"], input="y\n")
    assert refused.exit_code != 0 and "terminal interactiva" in refused.output and not answered


def test_identified_pane_runs_its_watcher_until_closed(tmux, tmp_path, monkeypatch):
    # Records how it was started instead of watching a hub.
    monkeypatch.setattr(panes, "WATCH_COMMAND", [
        "sh", "-c", 'echo "$AGENT_BUS_AGENT_ID:$*" > watcher.txt; exec sleep 60', "watcher"])
    with pytest.raises(panes.PaneError, match="identity"):
        panes.spawn("a", "sh", cwd=str(tmp_path), watch=True)
    panes.spawn("a", "sh", cwd=str(tmp_path), env={"AGENT_BUS_AGENT_ID": "claude-01"}, watch=True)
    wait_for(lambda: (tmp_path / "watcher.txt").exists() and (tmp_path / "watcher.txt").read_text())
    assert (tmp_path / "watcher.txt").read_text() == "claude-01:--agent claude-01 --tmux-agent a\n"
    assert panes.list_panes() == [{"name": "a", "preset": "sh", "state": "idle", "watcher": "running"}]
    # The user's view attaches to the agents only, never to the watchers.
    assert panes.view_command()[-1] == "=agents"

    subprocess.run([*panes.TMUX, "send-keys", "-t", "watchers:=a", "C-c"])  # watcher dies
    wait_for(lambda: panes.list_panes()[0]["watcher"] == "stopped")
    panes.close("a")
    assert panes.list_panes() == []
    assert subprocess.run([*panes.TMUX, "has-session", "-t", "=watchers"], capture_output=True).returncode != 0


def test_send_strips_control_characters(monkeypatch):
    """An ESC[201~ would end the bracketed paste and press the rest as real keys."""
    loaded = []
    monkeypatch.setattr(panes, "list_panes", lambda: [{"name": "x", "preset": "sh", "state": "idle"}])
    monkeypatch.setattr(panes.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(panes, "_tmux", lambda *a, input_text=None: loaded.append(input_text) or "")
    panes.send("x", "hola\x1b[201~\x1b[B\rfin\x00\x9b1;2\nlínea 2\tok\x7f")
    assert loaded[0] == "hola[201~[Bfin1;2\nlínea 2\tok"
    with pytest.raises(panes.PaneError, match="nothing"):
        panes.send("x", "\x1b\x03")


def test_each_project_has_its_own_tmux_server(monkeypatch):
    monkeypatch.setattr(panes, "TMUX", None)
    monkeypatch.setattr(panes, "_project", None)
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "env-project")
    assert panes._base() == ["tmux", "-L", "agent-bus-env-project"]
    panes.use_project("proj/../x y")
    assert panes._base() == ["tmux", "-L", "agent-bus-proj____x_y"]


def test_missing_tmux_means_no_panes_not_an_error(monkeypatch):
    monkeypatch.setattr(panes, "TMUX", ["agent-bus-no-such-tmux"])
    assert panes.list_panes() == []
    with pytest.raises(panes.PaneError, match="not installed"):
        panes.close("x")


@pytest.mark.parametrize("footer, expected", [
    # Captured from Claude Code 2.1 in tmux, 2026-10.
    ("✶ Meandering…\n  ⎿  Tip: Connect Claude to your IDE · /ide\n❯ \n  ⏵⏵ accept edits on", "working"),
    ("· Meandering…\n❯ ", "working"),
    ("● 1 2 3 4 5\n✻ Cooked for 9s · done 11:47 AM\n❯ \n  ⏵⏵ accept edits on", "idle"),
    ("● Revisando…\n❯ ", "idle"),  # a reply line, not the spinner
    ("* Revisando…\n❯ ", "idle"),  # a markdown bullet in the reply, not the spinner
    (" Claude Code'll be able to read, edit, and execute files here.\n ❯ No, exit\n"
     " Enter to confirm · Esc to cancel", "blocked"),
])
def test_claude_states_from_its_footer(monkeypatch, footer, expected):
    monkeypatch.setattr(panes, "screen", lambda name, lines=panes.SCREEN_LINES: footer)
    assert panes.state("x", "claude") == expected


def test_tmux_command_separators_and_formats_are_refused(monkeypatch):
    monkeypatch.setattr(panes, "_tmux", lambda *args, **kwargs: "")
    with pytest.raises(panes.PaneError, match="';'"):
        panes.spawn("x", "claude", model="sonnet;")
    with pytest.raises(panes.PaneError, match="';'"):
        panes.spawn("y", "claude", prompt="haz esto;")
    with pytest.raises(panes.PaneError, match="'#'"):
        panes.spawn("z", "claude", cwd="/tmp/a#(id)b")


def test_panes_do_not_inherit_the_ssh_agent():
    assert "SSH_AUTH_SOCK" not in panes.BASE_ENV


def test_console_listing_captures_each_screen_once(monkeypatch):
    captures = []

    def fake_tmux(*args, **kwargs):
        if args[0] == "list-windows":
            return "a\tclaude\t0\n" if "agents" in args[2] else ""
        captures.append(args)
        return "respuesta\n✶ Meandering…\n❯ \n"

    monkeypatch.setattr(panes, "_tmux", fake_tmux)
    [pane] = panes.list_panes(50)
    assert pane["state"] == "working" and "Meandering" in pane["screen"]
    assert len(captures) == 1
