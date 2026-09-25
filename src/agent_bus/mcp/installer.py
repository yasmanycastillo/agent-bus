"""Installer for agent-bus MCP server configurations in AI coding agents and IDEs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
from typing import Any

from agent_bus.project import resolve_project_root

SUPPORTED_CLIENTS: tuple[str, ...] = ("codex", "gemini", "claude", "cursor", "grok", "hermes", "agy")

DEFAULT_CLIENT_AGENTS: dict[str, str] = {
    "codex": "codex",
    "gemini": "gemini",
    "claude": "claude",
    "cursor": "cursor",
    "grok": "grok",
    "hermes": "hermes",
    "agy": "agy",
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
            return root / ".codex" / "config.toml"
        elif client == "grok":
            return root / ".grok" / "mcp.json"
        elif client == "hermes":
            return root / ".hermes" / "config.yaml"
        elif client == "agy":
            return root / ".gemini" / "mcp_config.json"

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
            return home / ".codex" / "config.toml"
        elif client == "grok":
            return home / ".grok" / "mcp.json"
        elif client == "hermes":
            return home / ".hermes" / "config.yaml"
        elif client == "agy":
            return home / ".gemini" / "config" / "mcp_config.json"

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
    client = client.lower().strip()
    config_path = get_mcp_config_path(client, scope=scope, project_root=project_root)
    resolved_agent = agent_id or DEFAULT_CLIENT_AGENTS.get(client, "agent")
    server_cfg = build_mcp_server_config(resolved_agent, command=command, bus_url=bus_url, env=env)
    if client == "codex":
        existed = _install_codex_server(config_path, server_name, server_cfg, dry_run=dry_run)
        return config_path, server_cfg, existed
    if client == "hermes":
        existed = _install_hermes_server(config_path, server_name, server_cfg, dry_run=dry_run)
        return config_path, server_cfg, existed

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
    client = client.lower().strip()
    config_path = get_mcp_config_path(client, scope=scope, project_root=project_root)
    if client == "codex":
        return config_path, _uninstall_codex_server(config_path, server_name, dry_run=dry_run)
    if client == "hermes":
        return config_path, _uninstall_hermes_server(config_path, server_name, dry_run=dry_run)
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


def _hermes_block(name: str, server_cfg: dict[str, Any]) -> str:
    lines = [f"  {name}:", f"    command: {json.dumps(server_cfg['command'])}", "    args:"]
    lines.extend(f"      - {json.dumps(arg)}" for arg in server_cfg["args"])
    env = server_cfg.get("env") or {}
    if env:
        lines.append("    env:")
        lines.extend(f"      {key}: {json.dumps(value)}" for key, value in env.items())
    lines.append("    enabled: true")
    return "\n".join(lines) + "\n"


def _install_hermes_server(
    config_path: Path,
    server_name: str,
    server_cfg: dict[str, Any],
    *,
    dry_run: bool,
) -> bool:
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    block = _hermes_block(server_name, server_cfg)
    updated, existed = _splice_hermes_server(text, server_name, block)
    if not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(updated, encoding="utf-8")
    return existed


def _uninstall_hermes_server(config_path: Path, server_name: str, *, dry_run: bool) -> bool:
    if not config_path.exists():
        return False
    text = config_path.read_text(encoding="utf-8")
    updated, removed = _splice_hermes_server(text, server_name, "")
    if removed and not dry_run:
        config_path.write_text(updated, encoding="utf-8")
    return removed


def _codex_block(name: str, server_cfg: dict[str, Any]) -> str:
    args = ", ".join(json.dumps(arg) for arg in server_cfg["args"])
    lines = [
        f"[mcp_servers.{name}]",
        f"command = {json.dumps(server_cfg['command'])}",
        f"args = [{args}]",
    ]
    return "\n".join(lines) + "\n"


def _install_codex_server(
    config_path: Path,
    server_name: str,
    server_cfg: dict[str, Any],
    *,
    dry_run: bool,
) -> bool:
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    updated, existed = _splice_codex_server(text, server_name, _codex_block(server_name, server_cfg))
    if not dry_run:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(updated, encoding="utf-8")
    return existed


def _uninstall_codex_server(config_path: Path, server_name: str, *, dry_run: bool) -> bool:
    if not config_path.exists():
        return False
    text = config_path.read_text(encoding="utf-8")
    updated, removed = _splice_codex_server(text, server_name, "")
    if removed and not dry_run:
        config_path.write_text(updated, encoding="utf-8")
    return removed


def _codex_table_span(lines: list[str], start: int) -> int:
    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if not stripped or stripped.startswith("#") or lines[end].startswith("["):
            break
        end += 1
    return end


def _splice_codex_server(text: str, server_name: str, block: str) -> tuple[str, bool]:
    """Replace the Codex server table and drop a pinned env table. Leave every other line."""
    lines = text.splitlines(keepends=True)
    header = f"[mcp_servers.{server_name}]"
    env_header = f"[mcp_servers.{server_name}.env]"
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(header) or line.startswith(env_header):
            end = _codex_table_span(lines, index)
            spans.append((index, end))
            index = end
            continue
        index += 1
    if not spans:
        if not block:
            return text, False
        suffix = "" if not text or text.endswith("\n") else "\n"
        return text + suffix + block, False
    skip = {line_no for start, end in spans[1:] for line_no in range(start, end)}
    first = spans[0]
    replacement = [block] if block else []
    rebuilt: list[str] = []
    index = 0
    while index < len(lines):
        if index in skip:
            index += 1
            continue
        if index == first[0]:
            rebuilt.extend(replacement)
            index = first[1]
            continue
        rebuilt.append(lines[index])
        index += 1
    return "".join(rebuilt), True


def _splice_hermes_server(text: str, server_name: str, block: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    key = f"  {server_name}:"
    start = next((index for index, line in enumerate(lines) if line.startswith(key)), None)
    if start is not None:
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() and not line.startswith("    "):
                break
            end += 1
        replacement = [block] if block else []
        return "".join(lines[:start] + replacement + lines[end:]), True
    if not block:
        return text, False
    header = next((index for index, line in enumerate(lines) if line.startswith("mcp_servers:")), None)
    if header is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        return text + suffix + "mcp_servers:\n" + block, False
    return "".join(lines[: header + 1] + [block] + lines[header + 1 :]), False
