"""CLI lifecycle for the repository BranchIntegrator daemon."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import click

from agent_bus.config import get_config_dir, get_bus_url
from agent_bus.security import AuthenticationError
from agent_bus.worker.client import worker_environment


def _dir() -> Path:
    path = get_config_dir() / "integrator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid() -> Path:
    return _dir() / "integrator.pid"


def _log() -> Path:
    return _dir() / "integrator.log"


@click.group(name="integrator")
def integrator():
    """Gestionar el daemon que integra tareas en revisión."""


@integrator.command("start")
@click.option("--agent", default="integrator", help="Identidad del integrador.")
@click.option("--interval", default=5.0, type=float, show_default=True)
@click.option("--bus-url", default=None)
def start(agent: str, interval: float, bus_url: str | None):
    """Iniciar el polling de tareas `in_review`."""
    pid_file = _pid()
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), 0)
            click.echo(f"Integrator ya corriendo (PID {pid_file.read_text().strip()})")
            return
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)
    try:
        env = worker_environment(agent, per_agent=True, bus_url=bus_url)
    except AuthenticationError as exc:
        raise click.ClickException(str(exc)) from exc
    root = Path.cwd().resolve()
    code = (
        "import asyncio; from pathlib import Path; "
        "from agent_bus.worker.integrator import BranchIntegrator; "
        f"asyncio.run(BranchIntegrator(repo_dir=Path({str(root)!r}), agent_id={agent!r}, "
        f"bus_url={get_bus_url(bus_url)!r}).run_forever(poll_interval_seconds={interval!r}))"
    )
    with _log().open("a") as log:
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(root), env=env,
                                stdout=log, stderr=log, start_new_session=True)
    pid_file.write_text(str(proc.pid))
    click.echo(f"Integrator iniciado (PID {proc.pid}); log: {_log()}")


@integrator.command("status")
def status():
    """Mostrar el estado del daemon integrador."""
    if not _pid().exists():
        click.echo("Integrator: no iniciado")
        return
    pid = _pid().read_text().strip()
    try:
        os.kill(int(pid), 0)
        click.echo(f"Integrator: corriendo (PID {pid})")
    except (ProcessLookupError, ValueError):
        click.echo(f"Integrator: PID {pid} muerto (stale PID file)")


@integrator.command("stop")
def stop():
    """Detener el daemon integrador."""
    if not _pid().exists():
        click.echo("Integrator: no iniciado")
        return
    pid = int(_pid().read_text())
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"SIGTERM enviado al integrator (PID {pid})")
    except ProcessLookupError:
        click.echo(f"Integrator: PID {pid} no encontrado")
    finally:
        _pid().unlink(missing_ok=True)
