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
