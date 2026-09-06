from __future__ import annotations

import os

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent_bus.config import DEFAULT_CONFIG_DIR, get_config_dir

console = Console()

CURRENT_AGENT_FILE = DEFAULT_CONFIG_DIR / "current_agent"


def get_current_agent() -> str | None:
    # 1. Check environment variable first (allows multi-terminal isolation)
    env_agent = os.environ.get("AGENT_BUS_AGENT_ID") or os.environ.get("AGENT_ID")
    if env_agent and env_agent.strip():
        return env_agent.strip()

    if os.environ.get("AGENT_BUS_SESSION_FILE"):
        from agent_bus.security import load_session
        return load_session()["agent_id"]

    # 2. Resolve project/session configuration at use time.
    current_agent_file = get_config_dir() / "current_agent"
    if current_agent_file.exists():
        return current_agent_file.read_text().strip() or None
    return None


def set_current_agent(agent_id: str) -> None:
    current_agent_file = get_config_dir() / "current_agent"
    current_agent_file.parent.mkdir(parents=True, exist_ok=True)
    current_agent_file.write_text(agent_id)


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
        table.add_column("Depends On", style="blue", width=16)
        for t in tasks[:10]:
            deps = ", ".join(t.get("depends_on") or [])
            st = t.get("status", "")
            if st == "blocked":
                status_str = "[bold red]blocked[/bold red]"
            else:
                status_str = st
            table.add_row(t["task_id"], t["title"], t["owner"], status_str, deps or "-")
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
    table.add_column("Depends On", style="blue")
    table.add_column("Locked files", style="dim")
    for t in tasks:
        files = ", ".join(t.get("locked_files", []))
        deps = ", ".join(t.get("depends_on") or [])
        st = t.get("status", "")
        if st == "blocked":
            status_str = "[bold red]blocked[/bold red]"
        else:
            status_str = st
        table.add_row(t["task_id"], t["title"], t["owner"], status_str, deps or "-", files or "-")
    console.print(table)


def print_inbox_list(messages: list[dict], agent_id: str) -> None:
    table = Table(title=f"Inbox ({agent_id})", show_header=True, header_style="bold")
    table.add_column("ID", style="dim", no_wrap=True, overflow="ignore", min_width=36)
    table.add_column("De", style="cyan", width=10)
    table.add_column("Tipo", style="green", width=10)
    table.add_column("Reply?", style="yellow", width=6)
    table.add_column("Body", style="white")
    for m in messages:
        body_text = str(m.get("body", {}))[:60]
        table.add_row(
            m["message_id"],
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


def print_reviews_table(reviews: list[dict]) -> None:
    if not reviews:
        console.print("[dim]No hay revisiones registradas[/dim]")
        return
    table = Table(title="Revisiones (Gatekeeper)", show_header=True, header_style="bold")
    table.add_column("Review ID", style="cyan")
    table.add_column("Task", style="green")
    table.add_column("Verdict", style="yellow")
    table.add_column("Reviewer", style="magenta")
    table.add_column("SHA", style="dim")
    table.add_column("Reason", style="white")

    verdict_styles = {
        "approve": "[bold green]approve[/bold green]",
        "changes_requested": "[bold yellow]changes_requested[/bold yellow]",
        "blocked": "[bold red]blocked[/bold red]",
    }

    for r in reviews:
        v = r.get("verdict", "")
        styled_v = verdict_styles.get(v, v)
        sha = r.get("sha", "")
        short_sha = sha[:8] if len(sha) >= 8 else sha
        table.add_row(
            r.get("review_id", "-"),
            r.get("task_id", "-"),
            styled_v,
            r.get("reviewer_agent_id", "-"),
            short_sha or "-",
            r.get("reason", "-")[:80],
        )
    console.print(table)

