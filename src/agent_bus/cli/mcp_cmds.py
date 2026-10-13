"""CLI commands for managing MCP server configuration across AI coding agents and IDEs."""
from __future__ import annotations

import json

import click
from rich.console import Console
from rich.table import Table

from agent_bus.mcp.installer import (
    DEFAULT_CLIENT_AGENTS,
    SUPPORTED_CLIENTS,
    build_mcp_server_config,
    get_mcp_config_path,
    install_mcp_config,
    uninstall_mcp_config,
)

console = Console()


@click.group(name="mcp", help="Configurar e instalar el servidor MCP de agent-bus en clientes IA.")
def mcp():
    """Gestión de configuración MCP en Codex, Gemini, Claude, Cursor, Grok, Hermes y AGY."""


def _resolve_clients(client_arg: str) -> list[str]:
    cleaned = client_arg.lower().strip()
    if cleaned == "all":
        return list(SUPPORTED_CLIENTS)
    requested = [c.strip() for c in cleaned.split(",") if c.strip()]
    disallowed = [c for c in requested if c not in SUPPORTED_CLIENTS]
    if disallowed:
        raise click.UsageError(
            f"Cliente(s) no soportado(s): {', '.join(disallowed)}. "
            f"Clientes disponibles: {', '.join(SUPPORTED_CLIENTS)}, o 'all'."
        )
    return requested


@mcp.command(name="install")
@click.option(
    "--client",
    "-c",
    default="all",
    help=f"Cliente(s) a configurar: {', '.join(SUPPORTED_CLIENTS)}, o 'all' (por defecto: all).",
)
@click.option("--agent", "-a", "agent_id", default=None, help="Identidad del agente (ej: claude, grok, hermes).")
@click.option(
    "--scope",
    type=click.Choice(["project", "global"]),
    default="project",
    help="Ámbito de configuración: 'project' (en este workspace) o 'global' (en home del usuario).",
)
@click.option("--global", "is_global", is_flag=True, help="Equivalente a --scope global.")
@click.option("--command", default="agent-bus", help="Comando ejecutable para arrancar el servidor MCP.")
@click.option("--bus-url", default=None, help="URL opcional del bus para el servidor MCP.")
@click.option("--dry-run", is_flag=True, help="Mostrar las rutas y configs sin modificar archivos.")
def install(
    client: str,
    agent_id: str | None,
    scope: str,
    is_global: bool,
    command: str,
    bus_url: str | None,
    dry_run: bool,
):
    """Instalar la configuración MCP de agent-bus en clientes soportados."""
    effective_scope = "global" if is_global else scope
    clients = _resolve_clients(client)

    table = Table(title=f"Instalación MCP ({'Simulación / Dry-run' if dry_run else 'Aplicada'})")
    table.add_column("Cliente", style="cyan bold")
    table.add_column("Agente", style="magenta")
    table.add_column("Ámbito", style="green")
    table.add_column("Archivo de Configuración", style="yellow")
    table.add_column("Estado", style="bold")

    success_count = 0
    seen_paths: set[str] = set()
    for target_client in clients:
        path_key = str(get_mcp_config_path(target_client, scope=effective_scope))
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        target_agent = agent_id or DEFAULT_CLIENT_AGENTS.get(target_client, target_client)
        try:
            path, _, existed = install_mcp_config(
                client=target_client,
                agent_id=target_agent,
                scope=effective_scope,
                command=command,
                bus_url=bus_url,
                dry_run=dry_run,
            )
            status = "Actualizado" if existed else "Creado"
            if dry_run:
                status = f"Simulado ({status})"
            table.add_row(target_client, target_agent, effective_scope, str(path), status)
            success_count += 1
        except Exception as exc:
            table.add_row(target_client, target_agent, effective_scope, "Error", f"[red]{exc}[/red]")

    console.print(table)
    if not dry_run:
        console.print(f"\n[green]✓[/green] Configuración MCP instalada exitosamente para {success_count} cliente(s).")


@mcp.command(name="uninstall")
@click.option(
    "--client",
    "-c",
    default="all",
    help=f"Cliente(s) de los cuales desinstalar: {', '.join(SUPPORTED_CLIENTS)}, o 'all'.",
)
@click.option(
    "--scope",
    type=click.Choice(["project", "global"]),
    default="project",
    help="Ámbito de configuración: 'project' o 'global'.",
)
@click.option("--global", "is_global", is_flag=True, help="Equivalente a --scope global.")
@click.option("--dry-run", is_flag=True, help="Mostrar los archivos que se modificarían.")
def uninstall(client: str, scope: str, is_global: bool, dry_run: bool):
    """Desinstalar la configuración MCP de agent-bus de clientes soportados."""
    effective_scope = "global" if is_global else scope
    clients = _resolve_clients(client)

    seen_paths: set[str] = set()
    for target_client in clients:
        path_key = str(get_mcp_config_path(target_client, scope=effective_scope))
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        path, removed = uninstall_mcp_config(
            client=target_client,
            scope=effective_scope,
            dry_run=dry_run,
        )
        if removed:
            prefix = "[dry-run] Se removería de" if dry_run else "Removido de"
            click.echo(f"✓ {prefix}: {path}")
        else:
            click.echo(f"- No encontrado o sin cambios: {path}")


@mcp.command(name="show")
@click.option(
    "--client",
    "-c",
    type=click.Choice(list(SUPPORTED_CLIENTS)),
    default="claude",
    help="Cliente para el que generar el snippet.",
)
@click.option("--agent", "-a", "agent_id", default=None, help="Identidad del agente.")
@click.option("--command", default="agent-bus", help="Comando ejecutable.")
@click.option("--bus-url", default=None, help="URL opcional del bus.")
def show(client: str, agent_id: str | None, command: str, bus_url: str | None):
    """Mostrar el JSON de configuración MCP para inspección o copia manual."""
    target_agent = agent_id or DEFAULT_CLIENT_AGENTS.get(client, client)
    cfg = build_mcp_server_config(target_agent, command=command, bus_url=bus_url)
    wrapper = {"mcpServers": {"agent-bus": cfg}}
    click.echo(json.dumps(wrapper, indent=2))
