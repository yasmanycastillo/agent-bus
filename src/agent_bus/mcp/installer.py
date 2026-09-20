"""Installer for agent-bus MCP server configurations in AI coding agents and IDEs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
from typing import Any

from agent_bus.project import resolve_project_root

SUPPORTED_CLIENTS: tuple[str, ...] = ("codex", "gemini", "claude", "cursor", "grok")

DEFAULT_CLIENT_AGENTS: dict[str, str] = {
    "codex": "codex",
    "gemini": "gemini",
    "claude": "claude",
    "cursor": "cursor",
    "grok": "grok",
}


def get_mcp_config_path(
    client: str,
    scope: str = "project",
    project_root: Path | None = None,
) -> Path:
    """Return the configuration file path for a given client and scope."""
    client = client.lower().strip()
    if client not in SUPPORTED_CLIENTS:
        raise ValueError(f"Unsupported client '{client}'. Must be one of: {', '.join(SUPPORTED_CLIENTS)}")

    root = (project_root or resolve_project_root() or Path.cwd()).expanduser().resolve()
    home = Path.home().resolve()

    if scope == "project":
        if client == "cursor":
            return root / ".cursor" / "mcp.json"
        elif client == "claude":
            return root / ".claude" / "mcp.json"
        elif client == "gemini":
            return root / ".gemini" / "mcp_config.json"
        elif client == "codex":
            return root / ".codex" / "mcp.json"
        elif client == "grok":
            return root / ".grok" / "mcp.json"

    elif scope == "global":
        if client == "cursor":
            return home / ".cursor" / "mcp.json"
        elif client == "claude":
            system = platform.system()
            if system == "Darwin":
                return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
            elif system == "Windows":
                appdata = os.environ.get("APPDATA")
                base = Path(appdata) if appdata else home / "AppData" / "Roaming"
                return base / "Claude" / "claude_desktop_config.json"
            else:
                return home / ".config" / "Claude" / "claude_desktop_config.json"
        elif client == "gemini":
            return home / ".gemini" / "antigravity-cli" / "mcp_config.json"
        elif client == "codex":
            return home / ".codex" / "mcp.json"
        elif client == "grok":
            return home / ".grok" / "mcp.json"

    raise ValueError(f"Invalid scope '{scope}'. Must be 'project' or 'global'.")


def build_mcp_server_config(
    agent_id: str,
    command: str = "agent-bus",
    bus_url: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the JSON configuration object for the agent-bus MCP server."""
    args = ["mcp-server", "--agent", agent_id]
    if bus_url:
        args.extend(["--bus-url", bus_url])
    cfg: dict[str, Any] = {
        "command": command,
        "args": args,
    }
    if env:
        cfg["env"] = env
    return cfg


def install_mcp_config(
    client: str,
    agent_id: str | None = None,
    scope: str = "project",
    project_root: Path | None = None,
    command: str = "agent-bus",
    bus_url: str | None = None,
    env: dict[str, str] | None = None,
    server_name: str = "agent-bus",
    dry_run: bool = False,
) -> tuple[Path, dict[str, Any], bool]:
    """Install or update the agent-bus MCP server in the client's configuration file.

    Returns:
        (config_path, server_config, already_existed)
    """
    config_path = get_mcp_config_path(client, scope=scope, project_root=project_root)
    resolved_agent = agent_id or DEFAULT_CLIENT_AGENTS.get(client, "agent")
    server_cfg = build_mcp_server_config(resolved_agent, command=command, bus_url=bus_url, env=env)

    data: dict[str, Any] = {}
    already_existed = False
    if config_path.exists():
        try:
            content = config_path.read_text(encoding="utf-8")
            if content.strip():
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    data = parsed
        except Exception:
            data = {}

    mcp_servers = data.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        data["mcpServers"] = mcp_servers

    already_existed = server_name in mcp_servers
    mcp_servers[server_name] = server_cfg

    if not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return config_path, server_cfg, already_existed


def uninstall_mcp_config(
    client: str,
    scope: str = "project",
    project_root: Path | None = None,
    server_name: str = "agent-bus",
    dry_run: bool = False,
) -> tuple[Path, bool]:
    """Remove agent-bus MCP server from the client's configuration file.

    Returns:
        (config_path, was_removed)
    """
    config_path = get_mcp_config_path(client, scope=scope, project_root=project_root)
    if not config_path.exists():
        return config_path, False

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return config_path, False

    if isinstance(data, dict) and "mcpServers" in data and isinstance(data["mcpServers"], dict):
        if server_name in data["mcpServers"]:
            if not dry_run:
                del data["mcpServers"][server_name]
                config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return config_path, True

    return config_path, False
