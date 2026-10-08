"""MCP tool `agent_panes`: the coordinator opens, drives and closes agent TUIs in tmux.

Opt-in with AGENT_BUS_PANES=1 on the coordinator's MCP. Agents cannot pass CLI
arguments (no permission bypass) nor choose the directory: panes start at the
project root the MCP was started in. as_agent lends another identity's credential to
the pane and its watcher, so it needs a session the hub verifies as administrator.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agent_bus import panes

NAME = "agent_panes"
ENABLED_ENV = "AGENT_BUS_PANES"

TOOL = {
    "name": NAME,
    "description": (
        "Paneles TUI de agentes (claude, agy) que el usuario ve en solo lectura. "
        "action=list|spawn|send|screen|close. spawn abre name con preset en la raíz del proyecto; "
        "as_agent (solo sesiones de administrador) le da esa identidad y arranca su watcher, que le avisa del trabajo nuevo; close lo detiene. send escribe text y lo envía sólo si el panel está idle."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "spawn", "send", "screen", "close"]},
            "name": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$"},
            "preset": {"type": "string", "enum": sorted(panes.PRESETS)},
            "as_agent": {"type": "string", "description": "spawn: identidad del panel; requiere sesión de administrador"},
            "model": {"type": "string"},
            "prompt": {"type": "string", "description": "spawn: prompt inicial"},
            "text": {"type": "string", "description": "send: texto a escribir"},
            "lines": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["action"],
    },
}
GUIDANCE = ("Tras spawn, espera a que list muestre idle antes de send. blocked significa una pregunta "
            "de confianza o permiso: avisa al humano. Cierra con close los paneles que ya no uses.")


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV) == "1"


def _call(args: dict[str, Any], cwd: Path, admin: bool) -> dict[str, Any]:
    action = args["action"]
    if action == "list":
        return {"status": "ok", "panes": panes.list_panes()}
    name = args.get("name")
    if not name:
        raise ValueError(f"name is required for action={action}")
    if action == "spawn":
        if "preset" not in args:
            raise ValueError("preset is required for action=spawn")
        env = None
        if args.get("as_agent"):
            if not admin:
                raise ValueError("as_agent requires an administrator session")
            from agent_bus.worker.client import worker_environment
            env = worker_environment(args["as_agent"], per_agent=True)
        panes.spawn(name, args["preset"], cwd=str(cwd), model=args.get("model"),
                    prompt=args.get("prompt"), env=env, watch=env is not None)
        return {"status": "ok", "name": name}
    if action == "send":
        if not args.get("text", "").strip():
            raise ValueError("text is required for action=send")
        panes.send(name, args["text"])
        return {"status": "ok", "name": name}
    if action == "screen":
        return {"status": "ok", "name": name, "text": panes.screen(name, args.get("lines", 40))}
    panes.close(name)
    return {"status": "ok", "name": name}


async def call(args: dict[str, Any], cwd: Path, client: Any, project_id: str) -> dict[str, Any]:
    """client: the caller's authenticated hub client; the hub, not the local file, says who is admin."""
    admin = False
    if args.get("action") == "spawn" and args.get("as_agent"):
        response = await client.get("/auth/me")
        response.raise_for_status()
        admin = response.json().get("role") == "admin"
    panes.use_project(project_id)
    try:
        return await asyncio.to_thread(_call, args, cwd, admin)
    except panes.PaneError as exc:  # tmux's own message: pane names and states only
        return {"status": "error", "code": "pane_error", "error": str(exc)}
