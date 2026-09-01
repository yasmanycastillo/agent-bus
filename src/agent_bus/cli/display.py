from __future__ import annotations


from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent_bus.config import DEFAULT_CONFIG_DIR

console = Console()

import os

CURRENT_AGENT_FILE = DEFAULT_CONFIG_DIR / "current_agent"


def get_current_agent() -> str | None:
    # 1. Check environment variable first (allows multi-terminal isolation)
    env_agent = os.environ.get("AGENT_BUS_AGENT_ID") or os.environ.get("AGENT_ID")
    if env_agent and env_agent.strip():
        return env_agent.strip()

    # 2. Fall back to global current_agent file
    if CURRENT_AGENT_FILE.exists():
        return CURRENT_AGENT_FILE.read_text().strip() or None
    return None


def set_current_agent(agent_id: str) -> None:
    CURRENT_AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_AGENT_FILE.write_text(agent_id)


def generate_dashboard_renderable(
    status: dict,
    tasks: list[dict],
    inbox: list[dict],
    locks: list[dict],
    decisions: list[dict],
    agents: list[dict],
    current_agent: str | None,
):
    from rich.console import Group

    sections = []

    # Header
    agents_online = sum(1 for a in agents if a.get("status") == "online")
    ts = status.get("timestamp", "")[:19].replace("T", " ")
    header = f"Server: [green]online[/green]  Agents: {agents_online}/{len(agents)} online  Time: {ts}"
    if current_agent:
        header += f"\nCurrent agent: [cyan bold]{current_agent}[/cyan bold]"
    sections.append(Panel(header, title="[bold blue]agent-bus top[/bold blue]", border_style="blue"))

    # Tasks
    if tasks:
        table = Table(show_header=True, header_style="bold", expand=True)
        table.add_column("ID", style="cyan", width=6)
        table.add_column("Tarea", style="green")
        table.add_column("Owner", style="yellow", width=14)
        table.add_column("Status", style="magenta", width=14)
        for t in tasks[:10]:
            table.add_row(t["task_id"], t["title"], t["owner"], t["status"])
        sections.append(Panel(table, title="Tareas", border_style="green"))

    # Inbox
    if inbox:
        lines = []
        for m in inbox[:5]:
            body_text = str(m.get("body", {}))[:60]
            lines.append(f"[yellow]{m['from_agent']}[/]: {body_text}")
        sections.append(Panel("\n".join(lines), title=f"Inbox ({len(inbox)})", border_style="yellow"))

    # Locks
    if locks:
        lines = []
        for lk in locks:
            reason = f" ({lk['reason']})" if lk.get("reason") else ""
            lines.append(f"[red]{lk['file_path']}[/] -> {lk['locked_by']}{reason}")
        sections.append(Panel("\n".join(lines), title="Locks Activos", border_style="red"))

    # Decisions
    if decisions:
        lines = []
        for d in decisions[-5:]:
            lines.append(f"[cyan]{d['decision_id']}[/] {d['title']}  ({d['decided_by']}, {d['created_at'][:10]})")
        sections.append(Panel("\n".join(lines), title="Decisiones", border_style="magenta"))

    return Group(*sections)


def print_dashboard(
    status: dict,
    tasks: list[dict],
    inbox: list[dict],
    locks: list[dict],
    decisions: list[dict],
    agents: list[dict],
    current_agent: str | None,
) -> None:
    renderable = generate_dashboard_renderable(
        status, tasks, inbox, locks, decisions, agents, current_agent
    )
    console.print(renderable)


def print_tasks_table(tasks: list[dict]) -> None:
    table = Table(title="Tareas", show_header=True, header_style="bold")
    table.add_column("ID", style="cyan")
    table.add_column("Tarea", style="green")
    table.add_column("Owner", style="yellow")
    table.add_column("Status", style="magenta")
    table.add_column("Locked files", style="dim")
    for t in tasks:
        files = ", ".join(t.get("locked_files", []))
        table.add_row(t["task_id"], t["title"], t["owner"], t["status"], files or "-")
    console.print(table)


def print_inbox_list(messages: list[dict], agent_id: str) -> None:
    table = Table(title=f"Inbox ({agent_id})", show_header=True, header_style="bold")
    table.add_column("ID", style="dim", width=8)
    table.add_column("De", style="cyan", width=10)
    table.add_column("Tipo", style="green", width=10)
    table.add_column("Reply?", style="yellow", width=6)
    table.add_column("Body", style="white")
    for m in messages:
        body_text = str(m.get("body", {}))[:60]
        table.add_row(
            m["message_id"][:8],
            m["from_agent"],
            m["message_type"],
            "Si" if m.get("reply_needed") else "No",
            body_text,
        )
    console.print(table)


def print_locks_list(locks: list[dict]) -> None:
    if not locks:
        console.print("[dim]No hay locks activos[/dim]")
        return
    table = Table(title="Locks", show_header=True, header_style="bold")
    table.add_column("Archivo", style="cyan")
    table.add_column("Agente", style="yellow")
    table.add_column("Razon", style="dim")
    for lk in locks:
        table.add_row(lk["file_path"], lk["locked_by"], lk.get("reason") or "-")
    console.print(table)


def print_decisions_list(decisions: list[dict]) -> None:
    if not decisions:
        console.print("[dim]No hay decisiones registradas[/dim]")
        return
    table = Table(title="Decisiones", show_header=True, header_style="bold")
    table.add_column("ID", style="cyan")
    table.add_column("Titulo", style="green")
    table.add_column("Por", style="yellow")
    table.add_column("Fecha", style="dim")
    for d in decisions:
        table.add_row(d["decision_id"], d["title"], d["decided_by"], d["created_at"][:10])
    console.print(table)


def print_kickoff_progress(steps: list[dict]) -> None:
    if not steps:
        console.print("[dim]No hay kickoff activo. Ejecuta [cyan]agent-bus setup[/cyan] primero.[/dim]")
        return
    table = Table(title="Kickoff", show_header=True, header_style="bold")
    table.add_column("Paso", style="cyan", width=5)
    table.add_column("Nombre", style="green")
    table.add_column("Estado", style="yellow")
    table.add_column("Por", style="dim")
    for s in steps:
        icon = "[green]done[/green]" if s["status"] == "done" else "[dim]pending[/dim]"
        by = s.get("completed_by") or "-"
        table.add_row(str(s["step"]), s["name"], icon, by)
    console.print(table)


def print_agents_table(agents: list[dict]) -> None:
    if not agents:
        console.print("[dim]No hay agentes registrados[/dim]")
        return
    table = Table(title="Agentes", show_header=True, header_style="bold")
    table.add_column("ID", style="cyan")
    table.add_column("Nombre", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Capacidades", style="magenta")
    status_colors = {"online": "green", "away": "yellow", "offline": "red", "busy": "blue"}
    for a in agents:
        st = a.get("status", "offline")
        colored = f"[{status_colors.get(st, 'white')}]{st}[/{status_colors.get(st, 'white')}]"
        caps = ", ".join(a.get("capabilities", []))
        table.add_row(a["agent_id"], a["display_name"], colored, caps or "-")
    console.print(table)


def print_pending_summary(data: dict, agent_id: str) -> None:
    count = data.get("count", 0)
    reply = data.get("reply_needed", 0)
    if count == 0:
        console.print(f"[dim]Inbox vacio ({agent_id})[/dim]")
        return
    senders = ", ".join(data.get("latest_senders", []))
    console.print(
        f"[bold yellow]{count} mensajes pendientes[/bold yellow]"
        f" ({reply} requieren respuesta)"
        f"  — de: {senders}"
    )
    for m in data.get("latest_summary", []):
        console.print(f"  [cyan]{m['from']}[/]: {m['text']}")
