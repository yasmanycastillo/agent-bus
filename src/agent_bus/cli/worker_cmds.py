"""Comandos CLI para los workers autónomos y orquestación de equipo."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from agent_bus.config import DEFAULT_CONFIG_DIR

WORKERS_DIR = DEFAULT_CONFIG_DIR / "workers"


def _pid_file(agent_id: str) -> Path:
    return WORKERS_DIR / f"{agent_id}.pid"


def _log_file(agent_id: str) -> Path:
    return WORKERS_DIR / f"{agent_id}.log"


def _runner_code(provider: str, model: str | None, worktree_dir: str | None = None) -> str:
    parts = [f"provider={provider!r}"]
    if model:
        parts.append(f"model={model!r}")
    if worktree_dir:
        parts.append(f"worktree_dir=Path({worktree_dir!r})")
    return f"AgentRunner(agent_id, {', '.join(parts)})"


@click.group(name="worker", help="Gestionar workers autonomos (daemon por agente)")
def worker():
    pass


@worker.command("start")
@click.option("--agent", "agent_id", default=None, help="Agent id (default: agente actual)")
@click.option("--provider", default="claude", help="Proveedor CLI: claude, agy, aider, mock")
@click.option("--model", default=None, help="Modelo para el proveedor")
@click.option("--worktree", "worktree_dir", default=None, help="Directorio worktree para el agente")
@click.option("--bus-url", default="http://localhost:8420", help="URL del bus")
def worker_start(agent_id: str | None, provider: str, model: str | None, worktree_dir: str | None, bus_url: str):
    """Iniciar el daemon worker de un agente en background."""
    if agent_id is None:
        from agent_bus.cli.display import get_current_agent

        agent_id = get_current_agent()
        if not agent_id:
            click.echo("No hay agente por defecto. Usa --agent o agent-bus work as <id>")
            raise SystemExit(1)

    WORKERS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = _pid_file(agent_id)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            click.echo(f"Worker '{agent_id}' ya corriendo (PID {pid})")
            return
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)

    code = f"""
import asyncio
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner

agent_id = {agent_id!r}
daemon = WorkerDaemon(
    agent_id=agent_id,
    runner={_runner_code(provider, model, worktree_dir)},
    bus_url={bus_url!r},
)
try:
    asyncio.run(daemon.start())
except KeyboardInterrupt:
    pass
"""
    cmd = [sys.executable, "-c", code]
    with open(_log_file(agent_id), "a") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
    pid_file.write_text(str(proc.pid))
    click.echo(f"Worker '{agent_id}' iniciado (PID {proc.pid})")
    click.echo(f"  Log: {_log_file(agent_id)}")
    click.echo(f"  Detener con: agent-bus worker stop --agent {agent_id}")


@worker.command("stop")
@click.option("--agent", "agent_id", default=None, help="Agent id (default: agente actual)")
def worker_stop(agent_id: str | None):
    """Detener el daemon worker de un agente."""
    import signal

    if agent_id is None:
        from agent_bus.cli.display import get_current_agent

        agent_id = get_current_agent()
        if not agent_id:
            click.echo("No hay agente por defecto. Usa --agent")
            raise SystemExit(1)

    pid_file = _pid_file(agent_id)
    if not pid_file.exists():
        click.echo(f"No hay worker para '{agent_id}'")
        return
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"SIGTERM enviado al worker '{agent_id}' (PID {pid})")
    except ProcessLookupError:
        click.echo(f"Proceso {pid} no encontrado (stale PID file)")
    finally:
        pid_file.unlink(missing_ok=True)


@worker.command("status")
@click.option("--agent", "agent_id", default=None, help="Agent id (default: agente actual)")
def worker_status(agent_id: str | None):
    """Ver estado del daemon worker."""
    from agent_bus.cli.display import get_current_agent

    if agent_id is None:
        agent_id = get_current_agent()
    if not agent_id:
        click.echo("No hay agente por defecto. Usa --agent")
        raise SystemExit(1)

    pid_file = _pid_file(agent_id)
    if not pid_file.exists():
        click.echo(f"Worker '{agent_id}': no iniciado")
        return
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, 0)
        click.echo(f"Worker '{agent_id}': corriendo (PID {pid})")
    except ProcessLookupError:
        click.echo(f"Worker '{agent_id}': PID {pid} muerto (stale PID file)")


# ═══════════════════════════════════════════
# Orquestación: run-team y submit
# ═══════════════════════════════════════════


@click.command("run-team")
@click.option("--agents", default="claude,antigravity", help="Lista de agentes separados por coma")
@click.option("--mock", is_flag=True, default=False, help="Ejecutar workers en modo mock")
@click.option("--base-ref", default="main", help="Rama base para los worktrees")
@click.option("--bus-url", default="http://localhost:8420", help="URL del bus")
def run_team(agents: str, mock: bool, base_ref: str, bus_url: str):
    """Lanza el equipo multi-agente: inicializa Git Worktrees y arranca daemons autónomos."""
    from agent_bus.worker.worktrees import WorktreeManager

    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    if not agent_list:
        click.echo("Especifica al menos un agente en --agents")
        return

    wm = WorktreeManager()
    click.echo(f"🚀 Preparando equipo autónomo: {', '.join(agent_list)}")

    for agent_id in agent_list:
        # 1. Crear worktree si estamos dentro de git repo
        worktree_path = None
        if wm.is_repo():
            try:
                wt_info = wm.create(agent_id, base_ref=base_ref)
                worktree_path = str(wt_info.path)
                click.echo(f"  📁 Worktree listo para '{agent_id}' en {wt_info.path} (rama: {wt_info.branch})")
            except Exception as exc:
                click.echo(f"  ⚠️  No se pudo crear worktree para '{agent_id}': {exc}")

        # 2. Iniciar daemon en background
        provider = "mock" if mock else agent_id
        # Start worker via subprocess
        WORKERS_DIR.mkdir(parents=True, exist_ok=True)
        pid_file = _pid_file(agent_id)
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                click.echo(f"  ⚙️  Worker '{agent_id}' ya corriendo (PID {pid})")
                continue
            except ProcessLookupError:
                pid_file.unlink(missing_ok=True)

        code = f"""
import asyncio
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner

agent_id = {agent_id!r}
daemon = WorkerDaemon(
    agent_id=agent_id,
    runner={_runner_code(provider, None, worktree_path)},
    bus_url={bus_url!r},
)
try:
    asyncio.run(daemon.start())
except KeyboardInterrupt:
    pass
"""
        cmd = [sys.executable, "-c", code]
        with open(_log_file(agent_id), "a") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
        pid_file.write_text(str(proc.pid))
        click.echo(f"  🤖 Worker '{agent_id}' iniciado en background (PID {proc.pid})")

    click.echo("\n✅ Equipo autónomo en ejecución.")
    click.echo("   - Ver estado: agent-bus worker status --agent <id>")
    click.echo("   - Enviar objetivo: agent-bus submit '<objetivo>'")
    click.echo("   - Monitorear: agent-bus show")


@click.command("submit")
@click.argument("goal")
@click.option("--bus-url", default="http://localhost:8420", help="URL del bus")
def submit_goal(goal: str, bus_url: str):
    """Envía un objetivo global al equipo para descomposición y ejecución autónoma."""
    import httpx

    click.echo(f"🎯 Enviando objetivo al equipo: {goal}")
    with httpx.Client(base_url=bus_url, timeout=10.0) as client:
        # Enviar mensaje broadcast a todos los agentes
        resp = client.post(
            "/messages",
            json={
                "from_agent": "human",
                "to_agent": "*",
                "message_type": "inbox",
                "body": {"text": f"Nuevo objetivo del equipo: {goal}"},
                "reply_needed": False,
            },
        )
        if resp.status_code == 200:
            click.echo("✅ Objetivo transmitido a todos los workers activos.")
        else:
            click.echo(f"⚠️  Error comunicando con el bus: {resp.text}")
