from __future__ import annotations

from pathlib import Path

import click
import yaml
from rich.console import Console

from agent_bus.cli.display import set_current_agent
from agent_bus.project import create_agent_config, create_plan, find_project_dir, get_agents_dir

console = Console()


def _read_readme(base: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = base / name
        if p.exists():
            content = p.read_text()[:500]
            return content
    return ""


def _create_global_profile(agent_id: str) -> Path:
    from agent_bus.config import DEFAULT_CONFIG_DIR

    profiles_dir = DEFAULT_CONFIG_DIR / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / f"{agent_id}.yaml"
    if path.exists():
        return path

    provider = "claude" if "claude" in agent_id.lower() else "openai" if "codex" in agent_id.lower() else "mock"
    model = ""
    if provider == "claude":
        model = "claude-sonnet-4-20250514"
    elif provider == "openai":
        model = "gpt-4o"

    template = {
        "provider": provider,
        "model": model,
        "autonomy_level": 3,
        "poll_interval_seconds": 5.0,
        "max_concurrent_tasks": 1,
        "notification_preferences": {"desktop": True, "inbox": True},
    }
    with open(path, "w") as f:
        yaml.dump(template, f, default_flow_style=False, sort_keys=False)
    return path


def run_setup(client, cwd: Path | None = None) -> None:
    base = cwd or Path.cwd()
    project = find_project_dir(base)
    if not project:
        console.print("[red]Error: no se encontro .agent-bus/. Ejecuta 'agent-bus init' primero.[/red]")
        return

    console.print(f"\n[bold blue]agent-bus setup[/bold blue] — {base.name}\n")

    # Check server
    try:
        resp = client.get("/status")
        if resp.status_code != 200:
            console.print("[red]Servidor no responde. Ejecuta 'agent-bus serve' primero.[/red]")
            return
    except Exception:
        console.print("[red]No se puede conectar al servidor. Ejecuta 'agent-bus serve' primero.[/red]")
        return

    # Project goal — leer de README o plan.md si existen
    plan_path = project / "plan.md"
    readme_content = _read_readme(base)

    if plan_path.exists():
        console.print("[dim]plan.md ya existe, usando su contenido[/dim]")
        goal = ""
    elif readme_content:
        console.print(f"[dim]README encontrado, usando como base del plan[/dim]")
        create_plan(f"# {base.name}\n\n{readme_content}", cwd=base)
        console.print("  [green]ok[/green] plan.md creado desde README")
        goal = readme_content[:200]
    else:
        console.print("[bold]1. Objetivo del proyecto[/bold]")
        goal = click.prompt("  Describe brevemente el objetivo", default="")
        plan_content = f"# {base.name}\n\n{goal}\n" if goal else f"# {base.name}\n"
        create_plan(plan_content, cwd=base)
        console.print("  [green]ok[/green] plan.md creado")

    # Agents — si ya existen configs, saltar
    agents_dir = get_agents_dir(base)
    existing_agents = []
    if agents_dir:
        existing_agents = sorted(p.stem for p in agents_dir.glob("*.yaml"))

    if existing_agents:
        console.print(f"\n[dim]Agentes ya configurados: {', '.join(existing_agents)}[/dim]")
        agents_input = click.prompt(
            "  Modificar? (comma sep, Enter para mantener)", default=",".join(existing_agents),
        )
    else:
        console.print("\n[bold]2. Agentes participantes[/bold]")
        agents_input = click.prompt("  IDs separados por coma", default="claude,codex")

    agent_ids = [a.strip() for a in agents_input.split(",") if a.strip()]

    agent_info = []
    for agent_id in agent_ids:
        existing_config = agents_dir / f"{agent_id}.yaml" if agents_dir else None
        if existing_config and existing_config.exists():
            with open(existing_config) as f:
                data = yaml.safe_load(f) or {}
            display_name = data.get("display_name", agent_id.title())
            caps = data.get("capabilities", ["python"])
            console.print(f"  [dim]{agent_id}: {display_name} ({', '.join(caps)}) — sin cambios[/dim]")
        else:
            console.print(f"\n  [cyan]{agent_id}[/cyan]")
            display_name = click.prompt("    Display name", default=agent_id.title())
            capabilities = click.prompt("    Capabilities (comma sep)", default="python")
            caps = [c.strip() for c in capabilities.split(",") if c.strip()]

            create_agent_config(agent_id, {
                "display_name": display_name,
                "capabilities": caps,
            }, cwd=base)

        # Ensure global profile
        global_path = _create_global_profile(agent_id)

        # Register on bus
        resp = client.post(
            "/register",
            json={"agent_id": agent_id, "display_name": display_name, "capabilities": caps},
        )
        if resp.status_code == 201:
            console.print(f"    [green]ok[/green] registrado")
        elif resp.status_code == 409:
            console.print(f"    [yellow]ya existe[/yellow]")

        agent_info.append({"id": agent_id, "name": display_name})

    # Default agent
    default_agent = agent_ids[0] if agent_ids else "claude"
    current = click.prompt(
        f"\n  Tu agente por defecto ({'/'.join(agent_ids)})",
        default=default_agent,
    )
    set_current_agent(current)

    # Generate protocol files
    from agent_bus.project import generate_agent_protocol

    for agent_id in agent_ids:
        protocol_path = generate_agent_protocol(agent_id, cwd=base)
        if protocol_path:
            console.print(f"  [green]ok[/green] {protocol_path.name}")

    # Kickoff
    console.print("\n[bold]3. Kickoff[/bold]")
    do_kickoff = click.confirm("  Iniciar kickoff?", default=True)
    if do_kickoff:
        client.post("/kickoff/start")
        client.post(
            "/kickoff/step/0",
            json={"result": {"name": base.name, "description": goal}, "completed_by": current},
        )
        console.print("  [green]ok[/green] kickoff iniciado (paso 0 completado)")

    # Summary
    console.print(
        f"\n[bold green]Setup completo:[/bold green] "
        f"{len(agent_info)} agentes en {base.name}"
    )
    console.print(f"  Proyecto:    [cyan].agent-bus/[/cyan]")
    console.print(f"  Perfiles:    [cyan]~/.agent-bus/profiles/[/cyan]")
    console.print(f"  Protocolos:  [cyan]{', '.join(a.upper() + '.md' for a in agent_ids)}[/cyan]")
    console.print(f"  Comandos:    [cyan]agent-bus work task[/cyan], [cyan]agent-bus show[/cyan]")
    console.print(f"  Handoff:     [cyan]agent-bus work handoff T1 codex[/cyan]\n")
