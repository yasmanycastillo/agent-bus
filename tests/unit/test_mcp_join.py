"""MCP bootstrap opens a credential in the named project."""
from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from agent_bus.mcp.join import join_workspace
from agent_bus.mcp.server import McpServer
from agent_bus.security import AuthenticationError


class _Server:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.session = None
        self.project_id = None
        self.bus_url = None
        self._lock_cwd = None
        self._lock_project_root = None


def test_missing_credential_does_not_stop_the_mcp_server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_BUS_SESSION_FILE", raising=False)
    monkeypatch.delenv("AGENT_BUS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "0")
    server = McpServer(agent_id="agy")
    assert server.session is None
    assert server.agent_id == "agy"


async def test_bootstrap_refuses_to_create_a_credential_in_the_source_checkout():
    server = _Server("codex-install")
    with pytest.raises(AuthenticationError, match="directorio de agent-bus"):
        await join_workspace(server, str(Path(__file__).resolve().parents[2]))
    credential = Path(__file__).resolve().parents[2] / ".agent-bus" / "runtime" / "credentials" / "codex-install.json"
    assert not credential.exists()


async def test_bootstrap_creates_the_credential_in_the_named_project(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_BUS_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("AGENT_BUS_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AGENT_BUS_DATABASE_PATH", raising=False)
    monkeypatch.delenv("AGENT_BUS_URL", raising=False)
    monkeypatch.delenv("AGENT_BUS_SESSION_FILE", raising=False)
    monkeypatch.delenv("AGENT_BUS_PROJECT_ID", raising=False)
    project = tmp_path / "praxia"
    project.mkdir()
    server = _Server("agy")

    async def no_hub(_root):
        return None

    monkeypatch.setattr("agent_bus.mcp.join._ensure_hub", no_hub)
    await join_workspace(server, str(project))
    credential = project / ".agent-bus" / "runtime" / "credentials" / "agy.json"
    assert credential.is_file()
    assert server.session["agent_id"] == "agy"
    assert server.project_id != "agent-bus-72e880aadd248244"
    pid_file = project / ".agent-bus" / "runtime" / "bus.pid"
    if pid_file.exists():
        os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
