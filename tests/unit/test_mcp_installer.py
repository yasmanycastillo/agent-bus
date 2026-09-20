"""Unit tests for agent-bus MCP installer across codex, gemini, claude, cursor, grok."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from agent_bus.cli.main import app
from agent_bus.mcp.installer import (
    DEFAULT_CLIENT_AGENTS,
    SUPPORTED_CLIENTS,
    build_mcp_server_config,
    get_mcp_config_path,
    install_mcp_config,
    uninstall_mcp_config,
)


def test_supported_clients_list():
    assert set(SUPPORTED_CLIENTS) == {"codex", "gemini", "claude", "cursor", "grok"}
    for client in SUPPORTED_CLIENTS:
        assert client in DEFAULT_CLIENT_AGENTS


def test_get_mcp_config_path_project(tmp_path):
    root = tmp_path / "my_project"
    root.mkdir()

    assert get_mcp_config_path("cursor", scope="project", project_root=root) == root / ".cursor" / "mcp.json"
    assert get_mcp_config_path("claude", scope="project", project_root=root) == root / ".claude" / "mcp.json"
    assert get_mcp_config_path("gemini", scope="project", project_root=root) == root / ".gemini" / "mcp_config.json"
    assert get_mcp_config_path("codex", scope="project", project_root=root) == root / ".codex" / "mcp.json"
    assert get_mcp_config_path("grok", scope="project", project_root=root) == root / ".grok" / "mcp.json"


def test_get_mcp_config_path_global(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    cursor_path = get_mcp_config_path("cursor", scope="global")
    assert cursor_path == fake_home / ".cursor" / "mcp.json"

    gemini_path = get_mcp_config_path("gemini", scope="global")
    assert gemini_path == fake_home / ".gemini" / "antigravity-cli" / "mcp_config.json"

    codex_path = get_mcp_config_path("codex", scope="global")
    assert codex_path == fake_home / ".codex" / "mcp.json"

    grok_path = get_mcp_config_path("grok", scope="global")
    assert grok_path == fake_home / ".grok" / "mcp.json"


def test_get_mcp_config_path_errors():
    with pytest.raises(ValueError, match="Unsupported client 'unknown'"):
        get_mcp_config_path("unknown")

    with pytest.raises(ValueError, match="Invalid scope 'invalid'"):
        get_mcp_config_path("cursor", scope="invalid")


def test_build_mcp_server_config():
    cfg = build_mcp_server_config("hermes", command="agent-bus", bus_url="http://127.0.0.1:9999", env={"FOO": "BAR"})
    assert cfg["command"] == "agent-bus"
    assert cfg["args"] == ["mcp-server", "--agent", "hermes", "--bus-url", "http://127.0.0.1:9999"]
    assert cfg["env"] == {"FOO": "BAR"}


def test_install_mcp_config_clean_and_merge(tmp_path):
    root = tmp_path / "test_repo"
    root.mkdir()

    # 1. Clean installation
    path, cfg, existed = install_mcp_config("cursor", agent_id="alice", project_root=root)
    assert path.exists()
    assert not existed
    assert cfg["args"] == ["mcp-server", "--agent", "alice"]

    data = json.loads(path.read_text())
    assert "agent-bus" in data["mcpServers"]
    assert data["mcpServers"]["agent-bus"]["args"] == ["mcp-server", "--agent", "alice"]

    # 2. Pre-existing other server is preserved
    data["mcpServers"]["other-tool"] = {"command": "other", "args": []}
    path.write_text(json.dumps(data, indent=2))

    # Re-install / update agent-bus
    path2, cfg2, existed2 = install_mcp_config(
        "cursor", agent_id="bob", bus_url="http://127.0.0.1:8420", project_root=root
    )
    assert path2 == path
    assert existed2 is True
    assert cfg2["args"] == ["mcp-server", "--agent", "bob", "--bus-url", "http://127.0.0.1:8420"]

    updated_data = json.loads(path.read_text())
    # other-tool must NOT be wiped out!
    assert "other-tool" in updated_data["mcpServers"]
    assert updated_data["mcpServers"]["other-tool"]["command"] == "other"
    assert updated_data["mcpServers"]["agent-bus"]["args"] == [
        "mcp-server", "--agent", "bob", "--bus-url", "http://127.0.0.1:8420"
    ]


def test_install_mcp_config_dry_run(tmp_path):
    root = tmp_path / "dry_repo"
    root.mkdir()

    path, _, _ = install_mcp_config("claude", project_root=root, dry_run=True)
    assert not path.exists()


def test_uninstall_mcp_config(tmp_path):
    root = tmp_path / "uninst_repo"
    root.mkdir()

    # Uninstall on non-existent config returns False
    path, removed = uninstall_mcp_config("grok", project_root=root)
    assert not path.exists()
    assert removed is False

    # Install first
    path, _, _ = install_mcp_config("grok", project_root=root)
    assert path.exists()

    # Also add another server to verify it remains
    data = json.loads(path.read_text())
    data["mcpServers"]["keep-me"] = {"command": "keep"}
    path.write_text(json.dumps(data))

    # Uninstall
    path, removed = uninstall_mcp_config("grok", project_root=root)
    assert removed is True
    data_after = json.loads(path.read_text())
    assert "agent-bus" not in data_after["mcpServers"]
    assert "keep-me" in data_after["mcpServers"]


def test_cli_mcp_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    # 1. Show command output
    res_show = runner.invoke(app, ["mcp", "show", "--client", "cursor", "--agent", "agentX"])
    assert res_show.exit_code == 0
    show_json = json.loads(res_show.output)
    assert show_json["mcpServers"]["agent-bus"]["args"] == ["mcp-server", "--agent", "agentX"]

    # 2. Install single client
    res_inst_one = runner.invoke(app, ["mcp", "install", "--client", "gemini", "--agent", "my-gemini"])
    assert res_inst_one.exit_code == 0
    assert (tmp_path / ".gemini" / "mcp_config.json").exists()
    gemini_data = json.loads((tmp_path / ".gemini" / "mcp_config.json").read_text())
    assert gemini_data["mcpServers"]["agent-bus"]["args"] == ["mcp-server", "--agent", "my-gemini"]

    # 3. Install all clients
    res_inst_all = runner.invoke(app, ["mcp", "install", "--client", "all"])
    assert res_inst_all.exit_code == 0
    assert (tmp_path / ".cursor" / "mcp.json").exists()
    assert (tmp_path / ".claude" / "mcp.json").exists()
    assert (tmp_path / ".codex" / "mcp.json").exists()
    assert (tmp_path / ".grok" / "mcp.json").exists()

    # 4. Uninstall client
    res_uninst = runner.invoke(app, ["mcp", "uninstall", "--client", "cursor"])
    assert res_uninst.exit_code == 0
    cursor_data = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert "agent-bus" not in cursor_data["mcpServers"]

    # 5. Invalid client validation
    res_err = runner.invoke(app, ["mcp", "install", "--client", "badclient"])
    assert res_err.exit_code != 0
    assert "Cliente(s) no soportado(s): badclient" in res_err.output
