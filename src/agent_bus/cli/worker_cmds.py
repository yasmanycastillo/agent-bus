"""Comandos CLI para los workers autónomos y orquestación de equipo."""

from __future__ import annotations

import os
import subprocess
import select
import sys
from pathlib import Path

import click

from agent_bus.config import DEFAULT_CONFIG_DIR, get_config_dir, get_bus_url
from agent_bus.security import AuthenticationError, load_session, sync_bus_client
from agent_bus.worker.client import worker_environment

WORKERS_DIR = DEFAULT_CONFIG_DIR / "workers"


def _workers_dir() -> Path:
    return get_config_dir() / "workers"


def _pid_file(agent_id: str) -> Path:
    return _workers_dir() / f"{agent_id}.pid"


def _log_file(agent_id: str) -> Path:
    return _workers_dir() / f"{agent_id}.log"


_SUPPORTED_PROVIDERS = {"claude", "agy", "antigravity", "aider", "codex", "grok", "xai", "openai", "mock"}


def _worker_provider(agent_id: str, explicit: str | None, env: dict[str, str]) -> str:
    provider = explicit
    if provider is None and env.get("AGENT_BUS_SESSION_FILE"):
        provider = load_session(agent_id, session_file=Path(env["AGENT_BUS_SESSION_FILE"])).get("provider")
    provider = (provider or (agent_id if agent_id in _SUPPORTED_PROVIDERS else "claude")).lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise click.ClickException(f"Proveedor no soportado: {provider}")
    return provider


def _runner_code(provider: str, model: str | None, worktree_dir: str | None = None) -> str:
    parts = [f"provider={provider!r}"]
    if model:
        parts.append(f"model={model!r}")
    if worktree_dir:
        parts.append(f"worktree_dir=Path({worktree_dir!r})")
    return f"AgentRunner(agent_id, {', '.join(parts)})"



def _spawn_worker(agent_id: str, provider: str, model: str | None, worktree_dir: str | None,
                  bus_url: str, child_env: dict[str, str]) -> None:
    """Announce readiness only after the child owns its guard and registers."""
    from agent_bus.project import get_checkout_root
    checkout = Path(worktree_dir).resolve() if worktree_dir else (get_checkout_root() or Path.cwd()).resolve()
    read_fd, write_fd = os.pipe()
    code = f"""
import asyncio
import logging
import os
from pathlib import Path
from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner
logging.basicConfig(level=logging.INFO)
agent_id = {agent_id!r}
daemon = WorkerDaemon(agent_id=agent_id,
    runner={_runner_code(provider, model, str(checkout))}, bus_url={bus_url!r})
async def launch():
    task = asyncio.create_task(daemon.start())
    try:
        while not daemon._running and not task.done():
            await asyncio.sleep(0.01)
        os.write({write_fd}, b"ready" if daemon._running else b"failed")
    finally:
        os.close({write_fd})
    await task
try:
    asyncio.run(launch())
except KeyboardInterrupt:
    pass
"""
    proc = None
    try:
        with open(_log_file(agent_id), "a") as log:
            proc = subprocess.Popen([sys.executable, "-c", code], stdout=log, stderr=log,
                                    start_new_session=True, env=child_env, cwd=str(checkout),
                                    pass_fds=(write_fd,))
        os.close(write_fd)
        write_fd = -1
        readable, _, _ = select.select([read_fd], [], [], 10)
        ready = os.read(read_fd, 32) if readable else b""
        if ready != b"ready" or proc.poll() is not None:
            raise click.ClickException(
                f"Worker '{agent_id}' no inició (sesión, hub o ejecutor ocupado). Revisa {_log_file(agent_id)}"
            )
        _pid_file(agent_id).write_text(str(proc.pid))
        click.echo(f"Worker '{agent_id}' iniciado (PID {proc.pid})")
        click.echo(f"  Log: {_log_file(agent_id)}")
    except BaseException:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        raise
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

@click.group(name="worker", help="Gestionar workers autonomos (daemon por agente)")
def worker():
    pass


@worker.command("start")
@click.option("--agent", "agent_id", default=None, help="Agent id (default: agente actual)")
@click.option("--provider", default=None, help="Proveedor CLI: claude, agy, aider, mock")
@click.option("--model", default=None, help="Modelo para el proveedor")
@click.option("--worktree", "worktree_dir", default=None, help="Directorio worktree para el agente")
@click.option("--bus-url", default=None, help="URL del bus")
def worker_start(agent_id: str | None, provider: str | None, model: str | None, worktree_dir: str | None, bus_url: str | None):
    """Iniciar el daemon worker de un agente en background."""
    if agent_id is None:
        from agent_bus.cli.display import get_current_agent

        agent_id = get_current_agent()
        if not agent_id:
            click.echo("No hay agente por defecto. Usa --agent o agent-bus work as <id>")
            raise SystemExit(1)

    try:
        bus_url = get_bus_url(bus_url)
        child_env = worker_environment(agent_id, bus_url=bus_url)
    except AuthenticationError as exc:
        raise click.ClickException(str(exc)) from exc
    provider = _worker_provider(agent_id, provider, child_env)
    _workers_dir().mkdir(parents=True, exist_ok=True)
    pid_file = _pid_file(agent_id)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            click.echo(f"Worker '{agent_id}' ya corriendo (PID {pid})")
            return
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)

    _spawn_worker(agent_id, provider, model, worktree_dir, bus_url, child_env)



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
@click.option("--bus-url", default=None, help="URL del bus")
def run_team(agents: str, mock: bool, base_ref: str, bus_url: str | None):
    """Lanza el equipo multi-agente: inicializa Git Worktrees y arranca daemons autónomos."""
    from agent_bus.worker.worktrees import WorktreeManager

    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    if not agent_list:
        click.echo("Especifica al menos un agente en --agents")
        return

    bus_url = get_bus_url(bus_url)
    # Validate every identity before creating worktrees or starting any worker.
    try:
        environments = {agent: worker_environment(agent, per_agent=True, bus_url=bus_url) for agent in agent_list}
    except AuthenticationError as exc:
        raise click.ClickException(str(exc)) from exc
    from agent_bus.project import get_checkout_root
    wm = WorktreeManager(repo_root=get_checkout_root() or Path.cwd())
    click.echo(f"🚀 Preparando equipo autónomo: {', '.join(agent_list)}")

    for agent_id in agent_list:
        child_env = environments[agent_id]
        # 1. Crear worktree si estamos dentro de git repo
        worktree_path = None
        if wm.is_repo():
            try:
                wt_info = wm.create(agent_id, base_ref=base_ref)
                worktree_path = str(wt_info.path)
                click.echo(f"  📁 Worktree listo para '{agent_id}' en {wt_info.path} (rama: {wt_info.branch})")
            except Exception as exc:
                raise click.ClickException(f"No se pudo crear worktree para '{agent_id}': {exc}") from exc

        # 2. Iniciar daemon en background
        provider = _worker_provider(agent_id, "mock" if mock else None, child_env)
        # Start worker via subprocess
        _workers_dir().mkdir(parents=True, exist_ok=True)
        pid_file = _pid_file(agent_id)
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                click.echo(f"  ⚙️  Worker '{agent_id}' ya corriendo (PID {pid})")
                continue
            except ProcessLookupError:
                pid_file.unlink(missing_ok=True)

        _spawn_worker(agent_id, provider, None, worktree_path, bus_url, child_env)

    click.echo("\n✅ Equipo autónomo en ejecución.")
    click.echo("   - Ver estado: agent-bus worker status --agent <id>")
    click.echo("   - Enviar objetivo: agent-bus submit '<objetivo>'")
    click.echo("   - Monitorear: agent-bus show")


@click.command("submit")
@click.argument("goal")
@click.option("--bus-url", default=None, help="URL del bus")
def submit_goal(goal: str, bus_url: str | None):
    """Envía un objetivo global al equipo para descomposición y ejecución autónoma."""
    from agent_bus.cli.display import get_current_agent

    bus_url = get_bus_url(bus_url)
    actor = get_current_agent()
    try:
        if os.environ.get("AGENT_BUS_ALLOW_UNSIGNED") != "1" or os.environ.get("AGENT_BUS_SESSION_FILE"):
            actor = load_session(actor)["agent_id"]
        client = sync_bus_client(actor, base_url=bus_url, timeout=10.0)
    except AuthenticationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"🎯 Enviando objetivo al equipo: {goal}")
    with client:
        # Enviar mensaje broadcast a todos los agentes
        resp = client.post(
            "/messages",
            json={
                "from_agent": actor or "human",
                "to_agent": "*",
                "message_type": "inbox",
                "body": {"text": f"Nuevo objetivo del equipo: {goal}"},
                "reply_needed": False,
            },
        )
        if resp.status_code == 200:
            click.echo("✅ Objetivo transmitido a todos los workers activos.")
        else:
            raise click.ClickException(f"Error comunicando con el bus ({resp.status_code})")
