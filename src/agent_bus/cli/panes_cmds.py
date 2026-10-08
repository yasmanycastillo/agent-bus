"""`agent-bus panes`: the coordinator opens, drives and closes agent TUIs in tmux."""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Callable

import click

from agent_bus import panes as backend


def _run(action: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return action(*args, **kwargs)
    except backend.PaneError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group(name="panes")
def panes() -> None:
    """Paneles tmux con TUIs de agentes que maneja el coordinador; el usuario solo mira."""
    if not shutil.which("tmux"):
        raise click.ClickException("tmux no está instalado")


@panes.command("spawn")
@click.argument("name")
@click.argument("preset", type=click.Choice(sorted(backend.PRESETS)))
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
@click.option("--cwd", default=None, help="Directorio de trabajo (por defecto el actual).")
@click.option("--model", default=None, help="Modelo para el CLI.")
@click.option("--prompt", default=None, help="Prompt inicial de la sesión interactiva.")
@click.option("--agent", "agent_id", default=None,
              help="Identidad de agent-bus del panel; inyecta su credencial (AGENT_BUS_*).")
def spawn(name: str, preset: str, extra_args: tuple[str, ...], cwd: str | None, model: str | None,
          prompt: str | None, agent_id: str | None) -> None:
    """Abrir NAME con PRESET. Argumentos tras `--` van al CLI (ej. permisos)."""
    env = None
    if agent_id:
        from agent_bus.worker.client import worker_environment
        env = worker_environment(agent_id, per_agent=True)
    _run(backend.spawn, name, preset, cwd=cwd, model=model, prompt=prompt,
         extra_args=tuple(extra_args), env=env)
    click.echo(f"Panel '{name}' abierto ({preset}). Míralo con: agent-bus panes view")


@panes.command("close")
@click.argument("name")
def close(name: str) -> None:
    """Cerrar el panel NAME y terminar su agente."""
    _run(backend.close, name)
    click.echo(f"Panel '{name}' cerrado")


@panes.command("list")
@click.option("--json", "as_json", is_flag=True, help="Salida JSON.")
def list_cmd(as_json: bool) -> None:
    """Paneles abiertos y su estado: idle, working, blocked o dead."""
    items = backend.list_panes()
    if as_json:
        click.echo(json.dumps(items))
        return
    for item in items:
        click.echo(f"{item['name']}\t{item['preset']}\t{item['state']}")


@panes.command("send")
@click.argument("name")
@click.argument("text", required=False)
@click.option("--force", is_flag=True, help="Escribir aunque el agente esté trabajando o bloqueado.")
def send(name: str, text: str | None, force: bool) -> None:
    """Escribir TEXT (o stdin) en el panel NAME y enviarlo."""
    if text is None:
        text = sys.stdin.read()
    if not text.strip():
        raise click.UsageError("texto vacío")
    _run(backend.send, name, text.rstrip("\n"), force=force)


@panes.command("screen")
@click.argument("name")
@click.option("--lines", default=40, show_default=True, help="Últimas líneas visibles.")
def screen(name: str, lines: int) -> None:
    """Mostrar lo que ve el panel NAME."""
    click.echo(_run(backend.screen, name, lines))


@panes.command("view")
def view() -> None:
    """Ver los paneles en solo lectura (desconectar: prefijo + d)."""
    if not backend.list_panes():
        raise click.ClickException("no hay paneles abiertos")
    command = backend.view_command()
    os.execvp(command[0], command)
