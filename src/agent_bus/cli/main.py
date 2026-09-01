from __future__ import annotations

from pathlib import Path

import click

from agent_bus.cli.display import (
    get_current_agent,
    print_agents_table,
    print_dashboard,
    print_decisions_list,
    print_inbox_list,
    print_kickoff_progress,
    print_locks_list,
    print_tasks_table,
)
from agent_bus.config import DEFAULT_CONFIG_DIR

DEFAULT_URL = "http://localhost:8420"

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


def _client():
    if httpx is None:
        click.echo("httpx not installed. Run: uv sync --extra dev")
        raise SystemExit(1)
    return httpx.Client(base_url=DEFAULT_URL, timeout=10.0)


def _require_agent() -> str:
    agent = get_current_agent()
    if not agent:
        click.echo("No hay agente por defecto. Ejecuta: agent-bus setup")
        raise SystemExit(1)
    return agent


def _explain_error(resp) -> str:
    """Traduce respuestas de error del bus a mensajes accionables."""
    if resp.status_code == 422:
        # validación pydantic: falta un campo o tiene tipo incorrecto
        try:
            details = resp.json().get("detail", [])
            missing = [
                f"'{d['loc'][-1]}'" for d in details if d.get("type") == "missing"
            ]
            if missing:
                return f"Falta(n) parametro(s): {', '.join(missing)}"
            return f"Parametros invalidos: {resp.text[:200]}"
        except (ValueError, KeyError, TypeError):
            return f"Parametros invalidos: {resp.text[:200]}"
    if resp.status_code == 409:
        try:
            return resp.json().get("error", resp.text)
        except ValueError:
            return resp.text
    if resp.status_code == 404:
        return "No encontrado: verifica el id/path usado"
    if resp.status_code == 405:
        return f"Endpoint no soporta ese metodo ({resp.request.method})"
    if resp.status_code >= 500:
        return f"Error del servidor ({resp.status_code}). Revisa el log del bus."
    try:
        return resp.json().get("error", resp.text[:200])
    except ValueError:
        return resp.text[:200]


def _ensure_global_config() -> None:
    config_dir = DEFAULT_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "data").mkdir(exist_ok=True)

    config_file = config_dir / "config.yaml"
    if not config_file.exists():
        import shutil

        default_config = Path(__file__).parent.parent.parent.parent / "config" / "default.yaml"
        if default_config.exists():
            shutil.copy2(default_config, config_file)
        else:
            config_file.write_text("# agent-bus configuration\nbus:\n  port: 8420\n")

    from agent_bus.crypto import generate_keypair

    priv_path = config_dir / "private.key"
    pub_path = config_dir / "public.key"
    if not priv_path.exists():
        priv_hex, pub_hex = generate_keypair()
        priv_path.write_text(priv_hex)
        pub_path.write_text(pub_hex)
        priv_path.chmod(0o600)


# ═══════════════════════════════════════════
# Root group
# ═══════════════════════════════════════════

app = click.Group(help="agent-bus: Protocolo de comunicacion y coordinacion entre agentes IA")


@app.command()
def init():
    """Inicializar .agent-bus/ en el directorio actual (por proyecto)."""
    from agent_bus.project import init_project

    project_path = init_project()
    click.echo(f"Proyecto inicializado: {project_path}")

    # Also ensure global config exists
    _ensure_global_config()

    # Generate protocol files for any already-configured agents
    from agent_bus.project import generate_all_agent_protocols

    protocols = generate_all_agent_protocols()
    if protocols:
        click.echo(f"Protocolos generados: {', '.join(p.name for p in protocols)}")


@app.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8420, type=int, help="Bind port")
@click.option("--daemon", "run_daemon", is_flag=True, help="Correr como daemon en background")
@click.option("--stop", "do_stop", is_flag=True, help="Detener el daemon")
@click.option("--status", "do_status", is_flag=True, help="Verificar estado del daemon")
def serve(host: str, port: int, run_daemon: bool, do_stop: bool, do_status: bool):
    """Iniciar/detener el servidor agent-bus."""
    if do_stop:
        _stop_daemon()
        return
    if do_status:
        _check_daemon()
        return
    if run_daemon:
        _start_daemon(host, port)
        return

    import uvicorn

    _ensure_global_config()
    click.echo(f"Iniciando agent-bus en {host}:{port}")
    uvicorn.run("agent_bus.core.bus:create_app", host=host, port=port, factory=True, reload=False)


def _start_daemon(host: str, port: int) -> None:
    import os
    import subprocess
    import sys

    _ensure_global_config()
    pid_file = DEFAULT_CONFIG_DIR / "bus.pid"
    log_file = DEFAULT_CONFIG_DIR / "bus.log"

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            click.echo(f"Servidor ya corriendo (PID {pid})")
            return
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)

    cmd = [
        sys.executable, "-m", "uvicorn",
        "agent_bus.core.bus:create_app",
        "--host", host, "--port", str(port),
        "--factory",
    ]

    with open(log_file, "a") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log, stderr=log,
            start_new_session=True,
        )

    pid_file.write_text(str(proc.pid))
    click.echo(f"Servidor iniciado como daemon (PID {proc.pid})")
    click.echo(f"  Log: {log_file}")
    click.echo(f"  Detener con: agent-bus serve --stop")


def _stop_daemon() -> None:
    import os
    import signal as sig

    pid_file = DEFAULT_CONFIG_DIR / "bus.pid"
    if not pid_file.exists():
        click.echo("No se encontro servidor daemon")
        return

    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, sig.SIGTERM)
        click.echo(f"SIGTERM enviado (PID {pid})")
    except ProcessLookupError:
        click.echo(f"Proceso {pid} no encontrado")
    finally:
        pid_file.unlink(missing_ok=True)


def _check_daemon() -> None:
    import os

    pid_file = DEFAULT_CONFIG_DIR / "bus.pid"
    if not pid_file.exists():
        click.echo("No hay servidor daemon")
        return

    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, 0)
        click.echo(f"Servidor corriendo (PID {pid})")
    except ProcessLookupError:
        click.echo(f"Proceso {pid} no encontrado (stale PID file)")
        pid_file.unlink(missing_ok=True)


@app.command()
def status():
    """Health check del servidor."""
    with _client() as client:
        resp = client.get("/status")
        data = resp.json()
        click.echo(f"Server: {data.get('bus_version', '?')}  Agents: {data.get('agents_online', 0)} online")


# ═══════════════════════════════════════════
# setup — Wizard interactivo
# ═══════════════════════════════════════════


@app.command()
def setup():
    """Configuracion interactiva del proyecto actual."""
    from agent_bus.cli.setup_wizard import run_setup

    with _client() as client:
        run_setup(client)


# ═══════════════════════════════════════════
# work — Operaciones diarias
# ═══════════════════════════════════════════

work = click.Group(name="work", help="Operaciones diarias de un agente")
app.add_command(work)


@work.command("as")
@click.argument("agent_id")
def work_as(agent_id: str):
    """Cambiar agente por defecto."""
    from agent_bus.cli.display import set_current_agent

    set_current_agent(agent_id)
    click.echo(f"Agente por defecto: {agent_id}")


@work.command("task")
@click.argument("task_id")
@click.argument("title")
@click.option("--owner", default="free", help="Owner inicial")
def work_task(task_id: str, title: str, owner: str):
    """Crear una tarea."""
    with _client() as client:
        resp = client.post("/tasks", json={"task_id": task_id, "title": title, "owner": owner})
        if resp.status_code == 200:
            click.echo(f"Tarea {task_id} creada")
        else:
            click.echo(f"Error: {_explain_error(resp)}")


@work.command("claim")
@click.argument("task_id")
def work_claim(task_id: str):
    """Reclamar una tarea libre."""
    agent = _require_agent()
    with _client() as client:
        resp = client.post(f"/tasks/{task_id}/claim", json={"agent_id": agent})
        if resp.status_code == 200:
            click.echo(f"Tarea {task_id} reclamada por {agent}")
        else:
            click.echo(f"Error: {_explain_error(resp)}")


@work.command("reassign")
@click.argument("task_id")
@click.argument("new_owner")
def work_reassign(task_id: str, new_owner: str):
    """Reasignar el owner de una tarea."""
    with _client() as client:
        resp = client.post(f"/tasks/{task_id}/reassign", json={"new_owner": new_owner})
        if resp.status_code == 200:
            click.echo(f"Tarea {task_id} reasignada a {new_owner}")
        else:
            click.echo(f"Error: {_explain_error(resp)}")


@work.command("done")
@click.argument("task_id")
def work_done(task_id: str):
    """Marcar tarea como completada."""
    with _client() as client:
        resp = client.post(f"/tasks/{task_id}/done")
        if resp.status_code == 200:
            click.echo(f"Tarea {task_id} completada")
        else:
            click.echo(f"Error: {_explain_error(resp)}")


@work.command("lock")
@click.argument("file_path")
@click.option("--reason", default=None, help="Razon del lock")
def work_lock(file_path: str, reason: str | None):
    """Bloquear un archivo."""
    agent = _require_agent()
    with _client() as client:
        resp = client.post("/locks/acquire", json={"file_path": file_path, "agent_id": agent, "reason": reason})
        if resp.status_code == 200:
            click.echo(f"Lock: {file_path}")
        else:
            click.echo(f"Error: {_explain_error(resp)}")


@work.command("unlock")
@click.argument("file_path")
def work_unlock(file_path: str):
    """Liberar un archivo."""
    agent = _require_agent()
    with _client() as client:
        resp = client.post("/locks/release", json={"file_path": file_path, "agent_id": agent})
        if resp.status_code == 200:
            click.echo(f"Unlock: {file_path}")
        else:
            click.echo(f"Error: {_explain_error(resp)}")


@work.command("check")
def work_check():
    """Verificar rapidamente si hay mensajes pendientes."""
    agent = _require_agent()
    with _client() as client:
        resp = client.get(f"/inbox/{agent}/pending")
        data = resp.json()
        count = data.get("count", 0)
        if count == 0:
            click.echo("Inbox vacio")
            return
        reply = data.get("reply_needed", 0)
        click.echo(f"Tienes {count} mensajes pendientes ({reply} requieren respuesta)")
        for m in data.get("latest_summary", []):
            click.echo(f"  De: {m['from']} — {m['text']}")
        raise SystemExit(1)


@work.command("msg")
@click.argument("to_agent")
@click.argument("text")
@click.option("--reply-needed", is_flag=True, help="Requiere respuesta")
@click.option("--task", default=None, help="Tarea relacionada")
def work_msg(to_agent: str, text: str, reply_needed: bool, task: str | None):
    """Enviar mensaje a otro agente."""
    agent = _require_agent()
    with _client() as client:
        resp = client.post(
            "/messages",
            json={
                "from_agent": agent,
                "to_agent": to_agent,
                "message_type": "inbox",
                "body": {"text": text},
                "reply_needed": reply_needed,
                "related_task": task,
            },
        )
        data = resp.json()
        if resp.status_code == 200:
            click.echo(f"Mensaje enviado (id: {data.get('message_id', '?')[:8]})")
        else:
            click.echo(f"Error: {data}")


@work.command("inbox")
@click.option("--read", "msg_id", default=None, help="Leer mensaje especifico")
@click.option("--archive", "archive_id", default=None, help="Archivar mensaje")
def work_inbox(msg_id: str | None, archive_id: str | None):
    """Ver inbox del agente actual."""
    agent = _require_agent()
    with _client() as client:
        if archive_id:
            resp = client.post(f"/inbox/{agent}/{archive_id}/archive")
            click.echo("Archivado" if resp.status_code == 200 else f"Error: {resp.text}")
            return
        if msg_id:
            resp = client.get(f"/inbox/{agent}/{msg_id}")
            if resp.status_code == 200:
                m = resp.json()
                click.echo(f"De:    {m['from_agent']}")
                click.echo(f"Tipo:  {m['message_type']}")
                click.echo(f"Reply: {'Si' if m.get('reply_needed') else 'No'}")
                click.echo(f"Body:  {m.get('body', {})}")
            else:
                click.echo("Mensaje no encontrado")
            return
        resp = client.get(f"/inbox/{agent}")
        messages = resp.json()
        if not messages:
            click.echo("Inbox vacio")
        else:
            print_inbox_list(messages, agent)


@work.command("decide")
@click.argument("title")
@click.argument("what")
@click.option("--context", default="", help="Contexto de la decision")
def work_decide(title: str, what: str, context: str):
    """Registrar una decision."""
    agent = _require_agent()
    import uuid

    did = f"D{uuid.uuid4().hex[:4]}"
    with _client() as client:
        resp = client.post(
            "/decisions",
            json={
                "decision_id": did,
                "title": title,
                "context": context or title,
                "decision": what,
                "decided_by": agent,
            },
        )
        if resp.status_code == 200:
            click.echo(f"Decision {did} registrada")
        else:
            click.echo(f"Error: {_explain_error(resp)}")


@work.command("kickoff")
@click.argument("step", type=int)
@click.option("--result", default=None, help="JSON con resultado")
def work_kickoff(step: int, result: str | None):
    """Completar un paso del kickoff."""
    agent = _require_agent()
    import json as _json

    parsed = _json.loads(result) if result else None
    with _client() as client:
        resp = client.post(f"/kickoff/step/{step}", json={"result": parsed, "completed_by": agent})
        if resp.status_code == 200:
            click.echo(f"Paso {step} ({resp.json()['name']}) completado")
        else:
            click.echo(f"Error: {_explain_error(resp)}")


# ═══════════════════════════════════════════
# show — Ver estado
# ═══════════════════════════════════════════

@click.group(
    name="show",
    help="Ver estado del bus y sus componentes. Sin subcomando muestra el dashboard.",
    invoke_without_command=True,
)
@click.pass_context
def show(ctx: click.Context):
    if ctx.invoked_subcommand is None:
        dashboard = ctx.command.get_command(ctx, "dashboard")
        if dashboard is not None:
            ctx.invoke(dashboard)


app.add_command(show)


from agent_bus.cli.worker_cmds import run_team, submit_goal, worker  # noqa: E402

app.add_command(worker)
app.add_command(run_team)
app.add_command(submit_goal)


from agent_bus.cli.watch_cmds import watch  # noqa: E402

app.add_command(watch)


@show.command("dashboard")
def show_dashboard():
    """Dashboard completo: agentes, tareas, inbox, locks, decisiones."""
    agent = get_current_agent()
    with _client() as client:
        status = client.get("/status").json()
        tasks = client.get("/tasks").json()
        inbox = client.get(f"/inbox/{agent}").json() if agent else []
        locks = client.get("/locks").json()
        decisions = client.get("/decisions").json()
        agents = client.get("/agents").json()
        print_dashboard(
            status=status,
            tasks=[t for t in tasks if t["status"] != "done"],
            inbox=inbox,
            locks=locks,
            decisions=decisions,
            agents=agents,
            current_agent=agent,
        )


@app.command("top")
@click.option("--interval", default=1.0, type=float, help="Intervalo de refresco en segundos")
@click.option("--once", is_flag=True, default=False, help="Renderizar una sola vez y salir")
def top_cmd(interval: float, once: bool):
    """Dashboard interactivo TUI en tiempo real con Rich Live."""
    import time
    from rich.live import Live
    from agent_bus.cli.display import console, generate_dashboard_renderable

    def fetch_renderable():
        agent = get_current_agent()
        with _client() as client:
            status = client.get("/status").json()
            tasks = client.get("/tasks").json()
            inbox = client.get(f"/inbox/{agent}").json() if agent else []
            locks = client.get("/locks").json()
            decisions = client.get("/decisions").json()
            agents = client.get("/agents").json()
            return generate_dashboard_renderable(
                status=status,
                tasks=[t for t in tasks if t["status"] != "done"],
                inbox=inbox,
                locks=locks,
                decisions=decisions,
                agents=agents,
                current_agent=agent,
            )

    if once:
        try:
            console.print(fetch_renderable())
        except Exception as exc:
            click.echo(f"Error conectando con agent-bus: {exc}")
        return

    try:
        with Live(fetch_renderable(), refresh_per_second=max(1, int(1 / interval)), console=console) as live:
            while True:
                time.sleep(interval)
                live.update(fetch_renderable())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        click.echo(f"Dashboard detenido: {exc}")


@app.command("quickstart")
@click.option("--agents", default="claude,antigravity", help="Agentes a registrar e inicializar")
@click.option("--mock", is_flag=True, default=False, help="Usar runners mock")
def quickstart(agents: str, mock: bool):
    """Onboarding en 1 solo paso: inicializa bus, registra agentes y lanza equipo."""
    import subprocess
    import time
    from agent_bus.project import init_project

    click.echo("✨ agent-bus Quickstart: Inicializando entorno multi-agente...\n")

    # 1. Init project
    project_path = init_project()
    _ensure_global_config()
    click.echo(f"  📁 Proyecto configurado en: {project_path}")

    # 2. Start server daemon if not already online
    server_online = False
    try:
        with _client() as client:
            resp = client.get("/status")
            if resp.status_code == 200:
                server_online = True
    except Exception:
        server_online = False

    if not server_online:
        click.echo("  ⚡ Iniciando servidor agent-bus daemon...")
        subprocess.run(["agent-bus", "serve", "--daemon"], check=False)
        time.sleep(1.0)
    else:
        click.echo("  ⚡ Servidor agent-bus ya activo.")

    # 3. Setup agents and protocols
    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    for ag in agent_list:
        set_current_agent(ag)
        try:
            with _client() as client:
                client.post("/register", json={"agent_id": ag, "display_name": ag.capitalize()})
        except Exception:
            pass
    click.echo(f"  🤖 Agentes registrados: {', '.join(agent_list)}")

    # 4. Run team
    click.echo("  🚀 Lanzando equipo autónomo con worktrees y workers...")
    cmd = ["agent-bus", "run-team", "--agents", agents]
    if mock:
        cmd.append("--mock")
    subprocess.run(cmd, check=False)

    click.echo("\n🎉 ¡Listo! El entorno multi-agente está completamente operativo.")
    click.echo("   👉 Ver dashboard vivo:     agent-bus top")
    click.echo("   👉 Enviar un objetivo:     agent-bus submit 'Tu requerimiento'")
    click.echo("   👉 Monitorear logs:        agent-bus worker status")


@show.command("tasks")
@click.option("--status", "task_status", default=None, help="Filtrar por status")
@click.option("--owner", default=None, help="Filtrar por owner")
def show_tasks(task_status: str | None, owner: str | None):
    """Ver tareas."""
    with _client() as client:
        params = {}
        if task_status:
            params["status"] = task_status
        if owner:
            params["owner"] = owner
        tasks = client.get("/tasks", params=params).json()
        if not tasks:
            click.echo("No hay tareas")
        else:
            print_tasks_table(tasks)


@show.command("inbox")
@click.argument("agent_id", required=False)
def show_inbox(agent_id: str | None):
    """Ver inbox de un agente."""
    agent = agent_id or get_current_agent()
    if not agent:
        click.echo("Especifica un agente o ejecuta 'agent-bus setup'")
        return
    with _client() as client:
        messages = client.get(f"/inbox/{agent}").json()
        if not messages:
            click.echo(f"Inbox vacio ({agent})")
        else:
            print_inbox_list(messages, agent)


@show.command("locks")
def show_locks():
    """Ver locks activos."""
    with _client() as client:
        locks = client.get("/locks").json()
        print_locks_list(locks)


@show.command("decisions")
def show_decisions():
    """Ver decisiones registradas."""
    with _client() as client:
        decisions = client.get("/decisions").json()
        print_decisions_list(decisions)


@show.command("kickoff")
def show_kickoff():
    """Ver progreso del kickoff."""
    with _client() as client:
        steps = client.get("/kickoff/progress").json()
        print_kickoff_progress(steps)


@show.command("agents")
def show_agents():
    """Ver agentes registrados."""
    with _client() as client:
        agents = client.get("/agents").json()
        print_agents_table(agents)


# ═══════════════════════════════════════════
# work handoff — Transferir tarea con contexto
# ═══════════════════════════════════════════


@work.command("handoff")
@click.argument("task_id")
@click.argument("to_agent")
@click.option("--summary", default="", help="Resumen de lo hecho")
@click.option("--files", default="", help="Archivos tocados (comma sep)")
@click.option("--questions", default="", help="Preguntas abiertas (comma sep)")
def work_handoff(task_id: str, to_agent: str, summary: str, files: str, questions: str):
    """Transferir una tarea a otro agente con contexto."""
    agent = _require_agent()
    with _client() as client:
        resp = client.post(f"/tasks/{task_id}/handoff", json={
            "from_agent": agent,
            "to_agent": to_agent,
            "summary": summary,
            "files_touched": [f.strip() for f in files.split(",") if f.strip()],
            "open_questions": [q.strip() for q in questions.split(",") if q.strip()],
        })
        if resp.status_code == 200:
            data = resp.json()
            click.echo(f"Tarea {task_id} transferida a {to_agent}")
            if summary:
                click.echo(f"  Resumen: {summary}")
        else:
            click.echo(f"Error: {_explain_error(resp)}")


@work.command("context")
@click.option("--update", "update_field", nargs=2, type=(str, str), help="Campo y valor")
def work_context(update_field: tuple[str, str] | None):
    """Ver o actualizar el contexto del proyecto."""
    with _client() as client:
        if update_field:
            field, value_str = update_field
            if field in ("tech_stack", "conventions"):
                value = [v.strip() for v in value_str.split(",")]
            elif field == "files_map":
                click.echo("Para files_map usa JSON o edita .agent-bus/context.yaml directamente")
                return
            else:
                value = value_str
            resp = client.post("/project/context", json={"field": field, "value": value})
            if resp.status_code == 200:
                click.echo(f"Context actualizado: {field}")
            else:
                click.echo(f"Error: {_explain_error(resp)}")
            return

        resp = client.get("/project/context")
        if resp.status_code == 200:
            import json

            data = resp.json()
            if not data:
                click.echo("Context vacio. Actualiza con: agent-bus work context --update tech_stack 'fastapi,postgres'")
                return
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))


# ═══════════════════════════════════════════
# start — Alias legacy
# ═══════════════════════════════════════════


@app.command("start", hidden=True)
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8420, type=int, help="Bind port")
def start(host: str, port: int):
    """Alias para 'serve' (deprecated, usa 'serve')."""
    import uvicorn

    _ensure_global_config()
    click.echo(f"Iniciando agent-bus en {host}:{port}")
@app.command("mcp-server")
@click.option("--bus-url", default="http://localhost:8420", help="URL del hub agent-bus")
def mcp_server_cmd(bus_url: str):
    """Iniciar el servidor MCP de agent-bus sobre stdio (JSON-RPC 2.0)."""
    import asyncio
    from agent_bus.mcp.server import run_mcp_server

    asyncio.run(run_mcp_server(bus_url=bus_url))


if __name__ == "__main__":
    app()
