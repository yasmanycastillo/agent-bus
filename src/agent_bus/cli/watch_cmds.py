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
import hashlib
import time
import shutil
import subprocess
from pathlib import Path

import click
import httpx

from agent_bus.security import async_bus_client

from agent_bus.config import DEFAULT_CONFIG_DIR, get_config_dir
from agent_bus.worker.client import BusEventClient, worker_environment

logger = logging.getLogger("agent_bus.cli.watch")

SESSIONS_FILE = DEFAULT_CONFIG_DIR / "watch_sessions.json"


def load_session_map(path: Path | None = None) -> dict[str, str]:
    path = path or get_config_dir() / "watch_sessions.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_session_map(mapping: dict[str, str], path: Path | None = None) -> None:
    path = path or get_config_dir() / "watch_sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=1))


def extract_session_id(output: str) -> str | None:
    """Extrae el session_id de la salida JSON de `claude -p --output-format json`."""
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            sid = data.get("session_id") or data.get("sessionId")
            return sid if isinstance(sid, str) else None
    except (json.JSONDecodeError, TypeError):
        return None
    return None


def extract_response_text(output: str) -> str:
    """Extrae el texto de respuesta devuelto por el CLI."""
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            return str(data.get("result") or data.get("response") or data.get("text") or "").strip()
    except Exception:
        pass
    return output.strip()


def build_prompt(message: dict) -> str:
    """Traduce un mensaje del bus a prompt para el agente interactivo."""
    sender = message.get("from_agent", "?")
    body = message.get("body") or {}
    text = body.get("text", json.dumps(body, ensure_ascii=False)) if isinstance(body, dict) else str(body)
    related = message.get("related_task")
    task_note = f" (relacionado con tarea {related})" if related else ""
    return (
        f"El agente '{sender}' te escribió por agent-bus{task_note}: \"{text}\"\n"
        "Devuelve tu respuesta como texto final. El watcher la enviará y confirmará el mensaje. "
        "No envíes ni confirmes el mensaje por CLI o MCP.\n"
    )


async def _run_cli(cmd: list[str], agent_id: str) -> subprocess.CompletedProcess:
    """Keep the event loop responsive and reap the CLI on cancellation or timeout."""
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=worker_environment(agent_id),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    return subprocess.CompletedProcess(
        cmd, process.returncode, stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _record_failure(client, agent_id: str, message_id: str, error: str) -> None:
    try:
        response = await client.post(f"/inbox/{agent_id}/{message_id}/fail", json={"error": error.strip()[:500] or "CLI failed without error details"})
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Could not record watcher failure: %s", exc)


async def run_turn(
    agent_id: str,
    message: dict,
    session_map: dict[str, str],
    cli: str = "claude",
    dry_run: bool = False,
    sessions_file: Path | None = None,
    bus_url: str = "http://localhost:8420",
) -> str | None:
    """Send one durable reply and acknowledge only after the CLI succeeds."""
    message_id = message["message_id"]
    if dry_run:
        click.echo(f"[dry-run] {cli} -p ... (message {message_id[:8]})")
        return None
    async with async_bus_client(agent_id, base_url=bus_url.rstrip("/"), timeout=10.0) as client:
        try:
            response = await client.get(f"/inbox/{agent_id}/{message_id}")
            response.raise_for_status()
            message = response.json()
            if message.get("acknowledged"):
                return None
            thread_id = (message.get("conversation_id") or (message.get("metadata") or {}).get("thread_id")
                         or message_id)
            session_id = session_map.get(thread_id)
            binary = shutil.which(cli)
            if not binary:
                raise RuntimeError(f"CLI '{cli}' not found in PATH")
            cmd = [binary, "-p", build_prompt(message), "--output-format", "json"]
            if session_id:
                cmd.extend(["--resume", session_id])
            result = await _run_cli(cmd, agent_id)
            if result.returncode != 0:
                raise RuntimeError(f"CLI turn failed ({result.returncode}): {result.stderr[:200]}")
            try:
                output_data = json.loads(result.stdout)
            except json.JSONDecodeError:
                output_data = None
            if cli == "claude" and (
                not isinstance(output_data, dict)
                or not isinstance(output_data.get("result"), str)
                or not output_data["result"].strip()
            ):
                raise RuntimeError("Claude CLI returned no successful text result")
            if isinstance(output_data, dict) and output_data.get("is_error"):
                raise RuntimeError("CLI returned an error result")
            new_session = extract_session_id(result.stdout)
            if new_session:
                session_map[thread_id] = new_session
                save_session_map(session_map, sessions_file)
            reply_text = extract_response_text(result.stdout)
            if not reply_text:
                raise ValueError("CLI returned no reply text")
            key = "watch-reply:" + message_id
            if len(key) > 128:
                key = "watch-reply:" + hashlib.sha256(message_id.encode()).hexdigest()
            response = await client.post(
                f"/inbox/{agent_id}/{message_id}/reply",
                json={"body": {"text": reply_text}, "idempotency_key": key,
                      "reply_needed": False, "acknowledge": True},
            )
            response.raise_for_status()
            logger.info("Reply delivered and message acknowledged: %s", message_id)
            return new_session or session_id
        except asyncio.CancelledError:
            await asyncio.shield(_record_failure(client, agent_id, message_id, "CLI turn cancelled"))
            raise
        except Exception as exc:
            await _record_failure(client, agent_id, message_id, str(exc))
            logger.warning("Watcher left message pending: %s", exc)
            return None


class PendingMessageWatcher:
    """SSE wakes bounded polling; persisted delivery state decides what to execute."""
    def __init__(self, agent_id: str, *, cli: str = "claude", bus_url: str = "http://localhost:8420",
                 dry_run: bool = False, sessions_file: Path | None = None):
        self.agent_id = agent_id
        self.cli = cli
        self.bus_url = bus_url
        self.dry_run = dry_run
        self.sessions_file = sessions_file or get_config_dir() / "watch" / agent_id / "sessions.json"
        self.session_map = load_session_map(self.sessions_file)
        self.cursor: str | None = None
        self.retry_after: dict[str, float] = {}
        self.wake = asyncio.Event()

    async def on_event(self, event: dict) -> None:
        # Events are hints only; never execute unverified event payloads.
        self.wake.set()

    async def poll_once(self, *, limit: int = 10) -> None:
        params = {"limit": limit, "reply_needed": "true"}
        if self.cursor:
            params["cursor"] = self.cursor
        async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10) as client:
            response = await client.get(f"/inbox/{self.agent_id}/messages", params=params)
            response.raise_for_status()
            page = response.json()
        self.cursor = page.get("next_cursor")
        for message in page["messages"]:
            message_id = message["message_id"]
            if time.monotonic() < self.retry_after.get(message_id, 0):
                continue
            try:
                await run_turn(self.agent_id, message, self.session_map, cli=self.cli,
                               dry_run=self.dry_run, sessions_file=self.sessions_file, bus_url=self.bus_url)
            finally:
                self.retry_after[message_id] = time.monotonic() + 3
        # Retain only unexpired backoff entries; no durable retry ledger is claimed.
        self.retry_after = {key: value for key, value in self.retry_after.items() if value > time.monotonic()}

    async def run(self, *, once: bool = False) -> None:
        events = BusEventClient(self.agent_id, bus_url=self.bus_url, on_event=self.on_event)
        event_task = asyncio.create_task(events.start())
        try:
            while True:
                self.wake.clear()
                try:
                    await self.poll_once(limit=1 if once else 10)
                except Exception as exc:
                    logger.warning("Pending inbox unavailable: %s", exc)
                if once:
                    return
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        finally:
            events.stop()
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)


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

    # Fail immediately for missing/mismatched credentials, including dry-run.
    worker_environment(agent_id)
    watcher = PendingMessageWatcher(agent_id, cli=cli, bus_url=bus_url, dry_run=dry_run)
    click.echo(f"👀 Watcheando el bus como '{agent_id}' (CLI: {cli})")
    click.echo("   Consulta persistida y SSE activos. Ctrl+C para salir.")
    try:
        asyncio.run(watcher.run(once=once))
    except KeyboardInterrupt:
        click.echo("\nWatcher detenido")
