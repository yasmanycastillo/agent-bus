from __future__ import annotations

import asyncio
import json
import os
from typing import Any
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent_bus.cli.display import get_current_agent
from agent_bus.config import get_bus_url
from agent_bus.orchestrator.config import OrchestratorConfig
from agent_bus.orchestrator.hermes import HermesOrchestrator
from agent_bus.security import AuthenticationError, load_session, sync_bus_client

console = Console()


def _get_authenticated_bus_client(bus_url: str | None) -> Any:
    resolved_bus_url = get_bus_url(bus_url)
    actor = get_current_agent()
    if os.environ.get("AGENT_BUS_ALLOW_UNSIGNED") != "1" or os.environ.get("AGENT_BUS_SESSION_FILE"):
        try:
            actor = load_session(actor)["agent_id"]
        except AuthenticationError as exc:
            raise click.ClickException(str(exc)) from exc
    return sync_bus_client(actor, base_url=resolved_bus_url, timeout=15.0)


@click.command("breakdown")
@click.argument("objective")
@click.option("--publish/--no-publish", "do_publish", default=True, help="Publicar las tareas desglosadas al bus")
@click.option("--dry-run", is_flag=True, help="Solo generar el desglose sin publicarlo al bus")
@click.option("--operation-key", default=None, help="Clave de idempotencia única para el desglose")
@click.option("--provider", default=None, help="Proveedor LLM: openai, vllm, ollama, openrouter")
@click.option("--model", default=None, help="Modelo a utilizar")
@click.option("--base-url", default=None, help="Endpoint base del proveedor")
@click.option("--bus-url", default=None, help="URL del bus agent-bus")
@click.option("--json-output", is_flag=True, help="Imprimir salida en formato JSON")
def breakdown(
    objective: str,
    do_publish: bool,
    dry_run: bool,
    operation_key: str | None,
    provider: str | None,
    model: str | None,
    base_url: str | None,
    bus_url: str | None,
    json_output: bool,
) -> None:
    """Desglosar un objetivo técnico en un grafo de tareas (DAG) estructurado usando Hermes."""
    should_publish = do_publish and not dry_run

    config = OrchestratorConfig.from_env(
        provider=provider,  # type: ignore[arg-type]
        base_url=base_url,
        model=model,
    )
    orchestrator = HermesOrchestrator(config=config)

    if not json_output:
        console.print(f"[bold cyan]🔍 Analizando y desglosando objetivo con Hermes ({config.provider} / {config.model})...[/bold cyan]")

    async def _run() -> None:
        if should_publish:
            bus_client = _get_authenticated_bus_client(bus_url)
            with bus_client:
                res = await orchestrator.orchestrate(
                    objective_text=objective,
                    operation_key=operation_key,
                    bus_client=bus_client,
                )
        else:
            res = await orchestrator.orchestrate(
                objective_text=objective,
                operation_key=operation_key,
                bus_client=None,
            )

        if not res.success:
            failure = res.failure
            if json_output:
                click.echo(json.dumps(failure.to_dict() if failure else {"error": "unknown"}, indent=2))
            else:
                console.print(
                    Panel(
                        f"[bold red]Fallo en el desglose ({failure.error_type if failure else 'error'}):[/bold red]\n"
                        f"{failure.reason if failure else 'Desconocido'}",
                        title="[red]Error de Desglose[/red]",
                    )
                )
                if failure and failure.details:
                    console.print("[yellow]Detalles de validación:[/yellow]")
                    for d in failure.details:
                        console.print(f"  • {d}")
            raise SystemExit(1)

        plan = res.plan
        if plan is None:
            raise click.ClickException("Plan no retornado")

        if json_output:
            out = {
                "plan": plan.model_dump(),
                "published": should_publish,
                "tasks": res.tasks,
            }
            click.echo(json.dumps(out, indent=2))
            return

        # Display rich representation
        table = Table(title=f"Plan de Tareas: {plan.objective}", header_style="bold magenta")
        table.add_column("Task ID", style="cyan", no_wrap=True)
        table.add_column("Título", style="white")
        table.add_column("Dependencias", style="yellow")
        table.add_column("Criterios de Aceptación", style="green")
        table.add_column("Test Cmd", style="dim")

        for t in plan.tasks:
            deps = ", ".join(t.depends_on) if t.depends_on else "[dim]ninguna[/dim]"
            criteria = "\n".join(f"• {c}" for c in t.acceptance_criteria) if t.acceptance_criteria else "[dim]ninguno[/dim]"
            test_cmd = " ".join(t.test_cmd) if t.test_cmd else "[dim]n/a[/dim]"
            table.add_row(t.task_id, t.title, deps, criteria, test_cmd)

        console.print(table)
        if plan.summary:
            console.print(f"\n[bold]Resumen de arquitectura:[/bold] {plan.summary}")

        if should_publish:
            console.print(
                f"\n[bold green]✅ {len(res.tasks)} tareas publicadas al bus con éxito[/bold green] "
                f"(operation_key: {plan.operation_key or 'n/a'})."
            )
        else:
            console.print("\n[dim]ℹ️  Modo dry-run: tareas no publicadas al bus.[/dim]")

    asyncio.run(_run())


@click.command("orchestrate")
@click.argument("objective")
@click.option("--operation-key", default=None, help="Clave de idempotencia única para el desglose")
@click.option("--provider", default=None, help="Proveedor LLM: openai, vllm, ollama, openrouter")
@click.option("--model", default=None, help="Modelo a utilizar")
@click.option("--base-url", default=None, help="Endpoint base del proveedor")
@click.option("--bus-url", default=None, help="URL del bus agent-bus")
@click.option("--json-output", is_flag=True, help="Imprimir salida en formato JSON")
@click.pass_context
def orchestrate(
    ctx: click.Context,
    objective: str,
    operation_key: str | None,
    provider: str | None,
    model: str | None,
    base_url: str | None,
    bus_url: str | None,
    json_output: bool,
) -> None:
    """Orquestar y publicar un objetivo técnico en el equipo multi-agente."""
    ctx.forward(
        breakdown,
        objective=objective,
        do_publish=True,
        dry_run=False,
        operation_key=operation_key,
        provider=provider,
        model=model,
        base_url=base_url,
        bus_url=bus_url,
        json_output=json_output,
    )
