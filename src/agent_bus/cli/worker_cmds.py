"""Comandos CLI para los workers autónomos: `agent-bus worker start|stop|status`."""

from __future__ import annotations

import os

import click

from agent_bus.config import DEFAULT_CONFIG_DIR

WORKERS_DIR = DEFAULT_CONFIG_DIR / "workers"


def _pid_file(agent_id: str):
    return WORKERS_DIR / f"{agent_id}.pid"


def _log_file(agent_id: str):
    return WORKERS_DIR / f"{agent_id}.log"


def _runner_code(provider: str, model: str | None) -> str:
    model_arg = f", model={model!r}" if model else ""
    return f"AgentRunner(agent_id, provider={provider!r}{model_arg})"


@click.group(name="worker", help="Gestionar workers autonomos (daemon por agente)")
def worker():
    pass


@worker.command("start")
@click.option("--agent", "agent_id", default=None, help="Agent id (default: agente actual)")
@click.option("--provider", default="claude", help="Proveedor CLI: claude, mock")
@click.option("--model", default=None, help="Modelo para el proveedor")
@click.option("--bus-url", default="http://localhost:8420", help="URL del bus")
def worker_start(agent_id: str | None, provider: str, model: str | None, bus_url: str):
    """Iniciar el daemon worker de un agente en background."""
    import subprocess
    import sys

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner

agent_id = {agent_id!r}
daemon = WorkerDaemon(
    agent_id=agent_id,
    runner={_runner_code(provider, model)},
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
