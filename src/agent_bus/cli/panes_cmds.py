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
@click.option("--watch/--no-watch", default=True, show_default=True,
              help="Con --agent: arrancar su watcher (watch --cli tmux), que close detiene.")
def spawn(name: str, preset: str, extra_args: tuple[str, ...], cwd: str | None, model: str | None,
          prompt: str | None, agent_id: str | None, watch: bool) -> None:
    """Abrir NAME con PRESET. Argumentos tras `--` van al CLI (ej. permisos)."""
    env = None
    if agent_id:
        from agent_bus.worker.client import worker_environment
        env = worker_environment(agent_id, per_agent=True)
    _run(backend.spawn, name, preset, cwd=cwd, model=model, prompt=prompt,
         extra_args=tuple(extra_args), env=env, watch=watch and env is not None)
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
    """Paneles abiertos, su estado (idle, working, blocked o dead) y su watcher."""
    items = backend.list_panes()
    if as_json:
        click.echo(json.dumps(items))
        return
    for item in items:
        click.echo(f"{item['name']}\t{item['preset']}\t{item['state']}\twatcher:{item['watcher']}")


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


@panes.command("answer")
@click.argument("name")
@click.argument("option", type=int, required=False)
def answer(name: str, option: int | None) -> None:
    """Responder la pregunta de permiso del panel NAME (sin OPTION, solo la muestra).

    Pensado para personas: exige una terminal interactiva y confirmación. Es una barrera
    de cortesía, no de seguridad: un agente con shell puede simular una terminal.
    """
    asked = _run(backend.question, name)
    if asked is None:
        raise click.ClickException(f"el panel '{name}' no muestra una pregunta")
    if asked["command"]:
        click.echo(f"Comando:\n{asked['command']}\n")
    for item in asked["options"]:
        mark = ">" if item["number"] == asked["selected"] else " "
        click.echo(f"{mark} {item['number']}. {item['label']}")
    if option is None:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise click.ClickException("answer requiere una terminal interactiva: lo responde una persona")
    label = next((i["label"] for i in asked["options"] if i["number"] == option), None)
    if label is None:
        raise click.UsageError(f"no hay opción {option}")
    click.confirm(f"¿Elegir {option}. {label} en '{name}'?", abort=True)
    _run(backend.answer, name, option)
    click.echo(f"Respondido: {option}. {label}")


@panes.command("view")
@click.option("--writable", is_flag=True, help="Poder escribir (p. ej. responder una pregunta a mano).")
@click.option("--watchers", is_flag=True, help="Ver los watchers de los paneles (y sus errores).")
def view(writable: bool, watchers: bool) -> None:
    """Ver los paneles en solo lectura (desconectar: prefijo + d)."""
    if not backend.list_panes():
        raise click.ClickException("no hay paneles abiertos")
    command = backend.view_command(backend.WATCHERS if watchers else backend.SESSION, writable)
    os.execvp(command[0], command)
