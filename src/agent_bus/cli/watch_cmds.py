"""Comando `agent-bus watch` — despierta una sesión interactiva del agente.

Caso de uso original del proyecto: una sesión de Claude Code (u otra CLI)
viva recibe mensajes de otros agentes por el bus y los responde sin que el
humano retransmita nada terminal por terminal.

Mecanismo (doc, sección 4.3): los mensajes con ``reply_needed`` llevan un
``thread_id``; el watcher mantiene el mapping ``thread_id → session_id``
del CLI y ejecuta ``claude --resume <session_id> -p "<prompt>"`` para
continuar el hilo conversacional. Sesiones nuevas arrancan con ``-p`` fresco
y su session_id se registra para los siguientes turnos del hilo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

import click

from agent_bus.config import DEFAULT_CONFIG_DIR
from agent_bus.worker.client import BusEventClient

logger = logging.getLogger("agent_bus.cli.watch")

SESSIONS_FILE = DEFAULT_CONFIG_DIR / "watch_sessions.json"


def load_session_map(path: Path = SESSIONS_FILE) -> dict[str, str]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_session_map(mapping: dict[str, str], path: Path = SESSIONS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=1))


def extract_session_id(output: str) -> str | None:
    """Extrae el session_id de la salida JSON de `claude -p --output-format json`."""
    try:
        data = json.loads(output)
        sid = data.get("session_id") or data.get("sessionId")
        return sid if isinstance(sid, str) else None
    except (json.JSONDecodeError, TypeError):
        return None


def build_prompt(message: dict) -> str:
    """Traduce un mensaje del bus a prompt para el agente interactivo."""
    sender = message.get("from_agent", "?")
    body = message.get("body") or {}
    text = body.get("text", json.dumps(body, ensure_ascii=False))
    related = message.get("related_task")
    task_note = f" (relacionado con tarea {related})" if related else ""
    return (
        f"El agente '{sender}' te escribió por agent-bus{task_note}: \"{text}\"\n"
        f"Responde ejecutando: agent-bus work msg {sender} \"<tu respuesta>\"\n"
        f"Si no tienes nada que responder, no hagas nada."
    )


async def run_turn(
    agent_id: str,
    message: dict,
    session_map: dict[str, str],
    cli: str = "claude",
    dry_run: bool = False,
    sessions_file: Path = SESSIONS_FILE,
) -> str | None:
    """Ejecuta un turno del CLI por el mensaje dado; devuelve el session_id o None."""
    thread_id = message.get("metadata", {}).get("thread_id") or message.get("message_id")
    prompt = build_prompt(message)
    session_id = session_map.get(thread_id)

    cmd = [cli, "-p", prompt, "--output-format", "json"]
    if session_id:
        cmd.extend(["--resume", session_id])

    if dry_run:
        click.echo(f"[dry-run] {' '.join(cmd[:2])} ... (thread {thread_id[:8]})")
        return session_id

    binary = shutil.which(cli)
    if not binary:
        logger.error("CLI '%s' not found in PATH", cli)
        return None

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        logger.error("CLI turn failed: %s", result.stderr[:200])
        return session_id

    new_session = extract_session_id(result.stdout)
    if new_session:
        session_map[thread_id] = new_session
        save_session_map(session_map, sessions_file)
    return new_session or session_id


@click.command(name="watch")
@click.option("--agent", "agent_id", default=None, help="Agent id (default: agente actual)")
@click.option("--cli", default="claude", help="CLI a despertar: claude, agy, ...")
@click.option("--bus-url", default="http://localhost:8420", help="URL del bus")
@click.option("--dry-run", is_flag=True, help="Solo mostrar qué se ejecutaría")
@click.option("--once", is_flag=True, help="Procesar un solo mensaje pendiente y salir")
def watch(agent_id: str | None, cli: str, bus_url: str, dry_run: bool, once: bool):
    """Escuchar el bus y despertar la sesión interactiva ante mensajes que requieren respuesta."""
    from agent_bus.cli.display import get_current_agent

    if agent_id is None:
        agent_id = get_current_agent()
        if not agent_id:
            click.echo("No hay agente por defecto. Usa --agent o agent-bus work as <id>")
            raise SystemExit(1)

    session_map = load_session_map()
    click.echo(f"👀 Watcheando el bus como '{agent_id}' (CLI: {cli})")
    click.echo("   Mensajes reply_needed despertarán la sesión. Ctrl+C para salir.")

    async def on_event(event: dict) -> None:
        nonlocal once
        if not (event.get("reply_needed") or event.get("to_agent") == agent_id and event.get("reply_needed")):
            return
        if event.get("to_agent") != agent_id:
            return
        click.echo(f"  ⚡ {event.get('from_agent')}: {str(event.get('body'))[:60]}")
        sid = await run_turn(agent_id, event, session_map, cli=cli, dry_run=dry_run)
        if sid:
            click.echo(f"  ✅ turno completado (session {sid[:8]})")
        if once:
            raise SystemExit(0)

    try:
        asyncio.run(BusEventClient(agent_id, bus_url=bus_url, on_event=on_event).start())
    except KeyboardInterrupt:
        click.echo("\nWatcher detenido")
