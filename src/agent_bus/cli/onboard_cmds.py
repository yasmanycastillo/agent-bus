"""Guided project onboarding with explicit credential consent."""
from __future__ import annotations

import subprocess
import sys

import click
from agent_bus.config import get_config_dir


def _run(*args: str) -> None:
    result = subprocess.run([sys.executable, "-m", "agent_bus.cli.main", *args], check=False)
    if result.returncode:
        raise click.ClickException(f"Falló: agent-bus {' '.join(args)}")


@click.command("onboard")
@click.option("--agents", default=None, help="Lista agente:proveedor separada por comas.")
@click.option("--admin", default="integrator", show_default=True, help="Identidad administradora.")
@click.option("--mock", is_flag=True, help="Usar workers mock durante la prueba inicial.")
def onboard(agents: str | None, admin: str, mock: bool) -> None:
    """Preparar un proyecto completo con preguntas guiadas y confirmaciones."""
    click.echo("\nagent-bus: asistente de puesta en marcha\n")
    _run("init")
    if not agents:
        agents = click.prompt("Agentes (ej. claude:claude,agy:agy,grok:grok)",
                              default="claude:claude,agy:agy,grok:grok")
    pairs: list[tuple[str, str]] = []
    for item in agents.split(","):
        name, sep, provider = item.strip().partition(":")
        if not sep:
            provider = name
        if name:
            pairs.append((name.strip(), provider.strip()))
    if not pairs:
        raise click.ClickException("Debes indicar al menos un agente")
    click.echo("\nSe crearán estas identidades:")
    for name, provider in pairs:
        click.echo(f"  - {name} ({provider})")
    click.echo(f"  - {admin} (admin, integrator)")
    if not click.confirm("¿Crear estas credenciales locales?", default=True):
        raise click.Abort()
    for name, provider in pairs:
        credential = get_config_dir() / "credentials" / f"{name}.json"
        if credential.exists():
            click.echo(f"  Credencial existente: {name}")
        else:
            _run("auth", "create", "--agent", name, "--provider", provider)
    admin_credential = get_config_dir() / "credentials" / f"{admin}.json"
    if admin_credential.exists():
        click.echo(f"  Credencial existente: {admin}")
    else:
        _run("auth", "create", "--agent", admin, "--provider", "mock", "--role", "admin")
    quickstart_args = ["quickstart", "--agents", ",".join(name for name, _ in pairs)]
    if mock:
        quickstart_args.append("--mock")
    _run(*quickstart_args)
    if click.confirm("¿Iniciar el integrador ahora?", default=True):
        _run("integrator", "start", "--agent", admin)
    click.echo("\nProyecto listo. Consulta el estado con: agent-bus show")
