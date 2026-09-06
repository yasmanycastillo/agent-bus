from __future__ import annotations

import os
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
from agent_bus.config import get_config_dir, get_bus_url, load_config
from agent_bus.security import AuthenticationError, load_session, sync_bus_client

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


def _raise_auth_error(response):
    if response.status_code == 401:
        raise click.ClickException("Sesión inválida o vencida. Provisiona una sesión con agent-bus auth create --agent <id>")
    if response.status_code == 403:
        raise click.ClickException("La sesión no tiene permiso para esta operación")


def _client():
    if httpx is None:
        click.echo("httpx not installed. Run: uv sync --extra dev")
        raise SystemExit(1)
    try:
        return sync_bus_client(get_current_agent(), base_url=get_bus_url(), timeout=10.0,
                               event_hooks={"response": [_raise_auth_error]})
    except AuthenticationError as exc:
        raise click.ClickException(str(exc)) from exc


def _require_agent() -> str:
    agent = get_current_agent()
    if os.environ.get("AGENT_BUS_ALLOW_UNSIGNED") != "1" or os.environ.get("AGENT_BUS_SESSION_FILE"):
        try:
            return load_session(agent)["agent_id"]
        except AuthenticationError as exc:
            raise click.ClickException(str(exc)) from exc
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
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "data").mkdir(exist_ok=True)

    config_file = config_dir / "config.yaml"
    if not config_file.exists():
        import yaml

        config_file.write_text(yaml.safe_dump({"bus": {"project_id": load_config().bus.project_id}}))

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

@click.group(help="agent-bus: Protocolo de comunicacion y coordinacion entre agentes IA")
@click.option("--project", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Raíz del proyecto")
@click.pass_context
def app(ctx, project):
    if project is not None:
        previous_cwd = Path.cwd()
        relative_paths = {}
        for key in ("AGENT_BUS_CONFIG_DIR", "AGENT_BUS_DATABASE_PATH", "AGENT_BUS_SESSION_FILE"):
            value = os.environ.get(key)
            if value and not Path(value).expanduser().is_absolute():
                relative_paths[key] = value
                os.environ[key] = str(previous_cwd / Path(value).expanduser())
        previous = os.environ.get("AGENT_BUS_PROJECT_ROOT")
        os.environ["AGENT_BUS_PROJECT_ROOT"] = str(project.resolve())
        os.chdir(project.resolve())
        def restore():
            os.chdir(previous_cwd)
            os.environ.update(relative_paths)
            if previous is None:
                os.environ.pop("AGENT_BUS_PROJECT_ROOT", None)
            else:
                os.environ["AGENT_BUS_PROJECT_ROOT"] = previous
        ctx.call_on_close(restore)


@app.command()
@click.option("--bus-url", default=None, help="URL persistente del hub de este proyecto")
def init(bus_url):
    """Inicializar .agent-bus/ en el directorio actual (por proyecto)."""
    from agent_bus.project import init_project

    project_path = init_project()
    if bus_url is not None:
        import yaml
        config_path = project_path / "config.yaml"
        settings = yaml.safe_load(config_path.read_text()) or {}
        settings["bus_url"] = get_bus_url(bus_url)
        config_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    click.echo(f"Proyecto inicializado: {project_path}")

    # Also ensure global config exists
    _ensure_global_config()

    # Generate protocol files for any already-configured agents
    from agent_bus.project import generate_all_agent_protocols

    protocols = generate_all_agent_protocols()
    if protocols:
        click.echo(f"Protocolos generados: {', '.join(p.name for p in protocols)}")


@app.command()
@click.option("--host", default=None, help="Bind host (configured URL by default)")
@click.option("--port", default=None, type=click.IntRange(1, 65535), help="Bind port")
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
    from urllib.parse import urlsplit
    endpoint = urlsplit(get_bus_url())
    host = host or endpoint.hostname or "127.0.0.1"
    port = port or endpoint.port or (443 if endpoint.scheme == "https" else 80)
    if run_daemon:
        _start_daemon(host, port)
        return

    import uvicorn

    _ensure_global_config()
    click.echo(f"Iniciando agent-bus en {host}:{port}")
    uvicorn.run("agent_bus.core.bus:create_app", host=host, port=port, factory=True, reload=False)


def _runtime_environment():
    config = load_config()
    environment = os.environ.copy()
    environment.update(AGENT_BUS_CONFIG_DIR=str(get_config_dir()),
                       AGENT_BUS_DATABASE_PATH=config.database_path,
                       AGENT_BUS_PROJECT_ID=config.bus.project_id,
                       AGENT_BUS_URL=get_bus_url())
    if config.project_root:
        environment["AGENT_BUS_PROJECT_ROOT"] = config.project_root
    return environment


def _start_daemon(host: str, port: int) -> None:
    import os
    import subprocess
    import sys

    _ensure_global_config()
    pid_file = get_config_dir() / "bus.pid"
    log_file = get_config_dir() / "bus.log"

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
            start_new_session=True, env=_runtime_environment(),
        )

    pid_file.write_text(str(proc.pid))
    click.echo(f"Servidor iniciado como daemon (PID {proc.pid})")
    click.echo(f"  Log: {log_file}")
    click.echo("  Detener con: agent-bus serve --stop")


def _stop_daemon() -> None:
    import os
    import signal as sig

    pid_file = get_config_dir() / "bus.pid"
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

    pid_file = get_config_dir() / "bus.pid"
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

    if os.environ.get("AGENT_BUS_ALLOW_UNSIGNED") != "1" or os.environ.get("AGENT_BUS_SESSION_FILE"):
        try:
            load_session(agent_id)
        except AuthenticationError as exc:
            raise click.ClickException(str(exc)) from exc
    set_current_agent(agent_id)
    click.echo(f"Agente por defecto: {agent_id}")


@work.command("task")
@click.argument("task_id")
@click.argument("title")
@click.option("--owner", default="free", help="Owner inicial")
@click.option("--description", default=None, help="Descripcion")
@click.option("--depends-on", "-d", multiple=True, help="IDs de tareas de las que depende")
@click.option("--criteria", "-c", multiple=True, help="Criterios de aceptacion")
@click.option("--test-cmd", default=None, help="Comando de pruebas")
@click.option("--operation-key", default=None, help="Clave de operacion (idempotencia)")
def work_task(
    task_id: str,
    title: str,
    owner: str,
    description: str | None,
    depends_on: tuple[str, ...],
    criteria: tuple[str, ...],
    test_cmd: str | None,
    operation_key: str | None,
):
    """Crear una tarea."""
    with _client() as client:
        payload = {
            "task_id": task_id,
            "title": title,
            "owner": owner,
            "description": description,
            "depends_on": list(depends_on),
            "acceptance_criteria": list(criteria),
            "test_cmd": [test_cmd] if test_cmd else None,
            "operation_key": operation_key,
        }
        resp = client.post("/tasks", json=payload)
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


def _lock_request(action: str, file_path: str, scope: str, **fields):
    from agent_bus.core.lock_paths import client_lock_path
    payload = {"file_path": client_lock_path(file_path, scope), "scope": scope,
               "agent_id": _require_agent(), **fields}
    with _client() as client:
        resp = client.post(f"/locks/{action}", json=payload)
        if resp.status_code != 200:
            raise click.ClickException(_explain_error(resp))
        data = resp.json()
    click.echo(f"{action}: {payload['file_path']} (scope={scope})")
    if action != "release":
        click.echo(f"acquisition_id: {data['acquisition_id']}")
        click.echo(f"expires_at: {data['expires_at']}")


@work.command("lock")
@click.argument("file_path")
@click.option("--reason", default=None, help="Razon del lock")
@click.option("--scope", type=click.Choice(["checkout", "project"]), default="checkout", show_default=True)
@click.option("--ttl", "ttl_seconds", type=click.IntRange(1, 3600), default=300, show_default=True)
def work_lock(file_path: str, reason: str | None, scope: str, ttl_seconds: int):
    """Bloquear un archivo; conserva acquisition_id para renovar o liberar."""
    _lock_request("acquire", file_path, scope, reason=reason, ttl_seconds=ttl_seconds)


@work.command("unlock")
@click.argument("file_path")
@click.option("--acquisition-id", required=True, help="Identificador devuelto por lock")
@click.option("--scope", type=click.Choice(["checkout", "project"]), default="checkout", show_default=True)
def work_unlock(file_path: str, acquisition_id: str, scope: str):
    """Liberar únicamente la adquisición indicada."""
    _lock_request("release", file_path, scope, acquisition_id=acquisition_id)


@work.command("renew-lock")
@click.argument("file_path")
@click.option("--acquisition-id", required=True, help="Identificador devuelto por lock")
@click.option("--scope", type=click.Choice(["checkout", "project"]), default="checkout", show_default=True)
@click.option("--ttl", "ttl_seconds", type=click.IntRange(1, 3600), default=300, show_default=True)
def work_renew_lock(file_path: str, acquisition_id: str, scope: str, ttl_seconds: int):
    """Renovar un bloqueo vigente usando su adquisición."""
    _lock_request("renew", file_path, scope, acquisition_id=acquisition_id, ttl_seconds=ttl_seconds)


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


def _message_key(value: str | None) -> str:
    from uuid import uuid4

    key = value if value is not None else str(uuid4())
    if not 1 <= len(key) <= 128:
        raise click.BadParameter("Debe contener entre 1 y 128 caracteres", param_hint="--idempotency-key")
    # Print before any network request, including failures or ambiguous timeouts.
    click.echo(f"Clave de reintento: {key}")
    return key


def _inbox_page(client, agent: str, *, cursor: str | None = None, limit: int = 50):
    params = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    response = client.get(f"/inbox/{agent}/messages", params=params)
    if response.status_code != 200:
        raise click.ClickException(_explain_error(response))
    return response.json()


def _print_page(page, agent: str):
    if page["messages"]:
        print_inbox_list(page["messages"], agent)
    else:
        click.echo("Inbox vacio")
    if page.get("next_cursor"):
        click.echo(f"Siguiente cursor: {page['next_cursor']}")
        click.echo("Continua con: agent-bus work inbox --cursor <cursor>")


@work.command("msg")
@click.argument("to_agent")
@click.argument("text")
@click.option("--reply-needed", is_flag=True, help="Requiere respuesta")
@click.option("--task", default=None, help="Tarea relacionada")
@click.option("--idempotency-key", default=None, help="Reutiliza la misma clave al reintentar el envío")
def work_msg(to_agent: str, text: str, reply_needed: bool, task: str | None, idempotency_key: str | None):
    """Enviar mensaje a otro agente."""
    agent = _require_agent()
    key = _message_key(idempotency_key)
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
                "idempotency_key": key,
            },
        )
        data = resp.json()
        if resp.status_code == 200:
            click.echo(f"Mensaje enviado (id: {data.get('message_id', '?')[:8]})")
        else:
            raise click.ClickException(_explain_error(resp))


@work.command("reply")
@click.argument("message_id")
@click.argument("text")
@click.option("--idempotency-key", default=None, help="Reutiliza la clave al reintentar la respuesta")
@click.option("--reply-needed", is_flag=True, help="La respuesta requiere otra respuesta")
@click.option("--ack", "acknowledge", is_flag=True, help="Confirmar también el mensaje original")
def work_reply(message_id: str, text: str, idempotency_key: str | None, reply_needed: bool, acknowledge: bool):
    """Responder a un mensaje conservando la conversación."""
    agent = _require_agent()
    key = _message_key(idempotency_key)
    with _client() as client:
        response = client.post(f"/inbox/{agent}/{message_id}/reply", json={
            "body": {"text": text}, "idempotency_key": key,
            "reply_needed": reply_needed, "acknowledge": acknowledge,
        })
        if response.status_code != 200:
            raise click.ClickException(_explain_error(response))
        click.echo(f"Respuesta enviada (id: {response.json()['message_id']})")


@work.command("ack")
@click.argument("message_ids", nargs=-1, required=True)
def work_ack(message_ids: tuple[str, ...]):
    """Confirmar uno o varios mensajes procesados (máximo 100)."""
    if len(message_ids) > 100:
        raise click.BadParameter("Confirma como máximo 100 mensajes por llamada")
    agent = _require_agent()
    with _client() as client:
        response = client.post(f"/inbox/{agent}/ack", json={"message_ids": list(message_ids)})
        if response.status_code != 200:
            raise click.ClickException(_explain_error(response))
        click.echo(f"Confirmados: {', '.join(response.json()['acknowledged'])}")


@work.command("inbox")
@click.option("--read", "msg_id", default=None, help="Leer mensaje especifico")
@click.option("--archive", "archive_id", default=None, help="Archivar mensaje")
@click.option("--cursor", default=None, help="Cursor de la página siguiente")
@click.option("--limit", default=50, type=click.IntRange(1, 100), show_default=True)
def work_inbox(msg_id: str | None, archive_id: str | None, cursor: str | None, limit: int):
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
        _print_page(_inbox_page(client, agent, cursor=cursor, limit=limit), agent)


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
from agent_bus.cli.integrator_cmds import integrator  # noqa: E402
from agent_bus.cli.onboard_cmds import onboard  # noqa: E402

app.add_command(worker)
app.add_command(run_team)
app.add_command(submit_goal)
app.add_command(integrator)
app.add_command(onboard)


from agent_bus.cli.auth_cmds import auth  # noqa: E402

app.add_command(auth)

from agent_bus.cli.watch_cmds import watch  # noqa: E402

app.add_command(watch)

from agent_bus.cli.orchestrator_cmds import breakdown, orchestrate  # noqa: E402

app.add_command(breakdown)
app.add_command(orchestrate)



@show.command("dashboard")
def show_dashboard():
    """Dashboard completo: agentes, tareas, inbox, locks, decisiones."""
    agent = get_current_agent()
    with _client() as client:
        status = client.get("/status").json()
        tasks = client.get("/tasks").json()
        inbox = _inbox_page(client, agent, limit=20)["messages"] if agent else []
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
            inbox = _inbox_page(client, agent, limit=20)["messages"] if agent else []
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
@click.option("--interactive", is_flag=True, help="Preguntar agentes y confirmar cada paso")
def quickstart(agents: str, mock: bool, interactive: bool):
    """Onboarding en 1 solo paso: inicializa bus, registra agentes y lanza equipo."""
    import time
    from agent_bus.project import init_project

    click.echo("✨ agent-bus Quickstart: Inicializando entorno multi-agente...\n")

    # Existing credentials are an explicit prerequisite; HTTP cannot enroll identities.
    from agent_bus.cli.display import set_current_agent
    from agent_bus.worker.client import worker_environment

    if interactive:
        agents = click.prompt("Agentes (separados por coma)", default=agents)
        mock = click.confirm("Usar workers mock (sin llamadas a modelos)?", default=mock)
    agent_list = [a.strip() for a in agents.split(",") if a.strip()]
    if not agent_list:
        raise click.ClickException("Especifica al menos un agente")
    secure = os.environ.get("AGENT_BUS_ALLOW_UNSIGNED") != "1" or bool(os.environ.get("AGENT_BUS_SESSION_FILE"))
    sessions = {}
    if secure:
        try:
            for agent in agent_list:
                environment = worker_environment(agent, per_agent=True)
                sessions[agent] = load_session(agent, session_file=environment["AGENT_BUS_SESSION_FILE"])
        except AuthenticationError as exc:
            raise click.ClickException(
                f"{exc}. Provisiona cada identidad localmente primero: "
                "agent-bus auth create --agent <id>. Para administrar: "
                "agent-bus auth create --agent operator --role admin"
            ) from exc

    project_path = init_project()
    _ensure_global_config()
    click.echo(f"  📁 Proyecto configurado en: {project_path}")
    first = agent_list[0]
    with sync_bus_client(first, session=sessions.get(first), base_url=get_bus_url(), timeout=10) as client:
        try:
            response = client.get("/status")
            response.raise_for_status()
        except httpx.ConnectError:
            from urllib.parse import urlsplit
            endpoint = urlsplit(get_bus_url())
            if (endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1")
                    or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment
                    or endpoint.username or endpoint.password):
                raise click.ClickException("No se puede iniciar automáticamente este hub; inicia el servidor configurado explícitamente")
            _start_daemon(endpoint.hostname, endpoint.port or 80)
            deadline = time.monotonic() + 5
            while True:
                try:
                    response = client.get("/status")
                    response.raise_for_status()
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        raise click.ClickException("Servidor no inició; revisa agent-bus serve --status")
                    time.sleep(0.1)

    if response.json().get("project_id") != load_config().bus.project_id:
        raise click.ClickException("El hub configurado pertenece a otro proyecto")

    for agent in agent_list:
        with sync_bus_client(agent, session=sessions.get(agent), base_url=get_bus_url(), timeout=10) as client:
            response = client.post("/register", json={"agent_id": agent, "display_name": agent.capitalize()})
            if response.status_code != 409:
                response.raise_for_status()
    if not get_current_agent() and not os.environ.get("AGENT_BUS_SESSION_FILE"):
        set_current_agent(first)
    click.echo(f"  🤖 Agentes registrados: {', '.join(agent_list)}")
    click.get_current_context().invoke(run_team, agents=agents, mock=mock, base_ref="main", bus_url=get_bus_url())
    click.echo("\nEquipo iniciado. Verifica agent-bus worker status --agent <id>.")
    if interactive:
        click.echo("Para activar integración automática: agent-bus integrator start --agent integrator")


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
@click.option("--cursor", default=None)
@click.option("--limit", default=50, type=click.IntRange(1, 100), show_default=True)
def show_inbox(agent_id: str | None, cursor: str | None, limit: int):
    """Ver inbox de un agente."""
    agent = agent_id or get_current_agent()
    if not agent:
        click.echo("Especifica un agente o ejecuta 'agent-bus setup'")
        return
    with _client() as client:
        _print_page(_inbox_page(client, agent, cursor=cursor, limit=limit), agent)


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
@click.option("--host", default=None, help="Bind host")
@click.option("--port", default=None, type=click.IntRange(1, 65535), help="Bind port")
def start(host: str, port: int):
    """Alias para 'serve' (deprecated, usa 'serve')."""
    click.get_current_context().invoke(serve, host=host, port=port,
                                       run_daemon=False, do_stop=False, do_status=False)


@app.command("mcp-server")
@click.option("--bus-url", envvar="AGENT_BUS_URL", default=None, help="URL del hub agent-bus")
@click.option("--agent", "agent_id", default=None, help="Identidad de la sesión MCP")
def mcp_server_cmd(bus_url: str, agent_id: str | None):
    """Iniciar el servidor MCP de agent-bus sobre stdio (JSON-RPC 2.0)."""
    import asyncio
    from agent_bus.mcp.server import run_mcp_server

    try:
        asyncio.run(run_mcp_server(bus_url=bus_url, agent_id=agent_id))
    except AuthenticationError as exc:
        raise click.ClickException(str(exc)) from exc


@app.command("hook-inbox", hidden=True)
@click.option("--bus-url", default=None)
@click.option("--agent", "agent_id", default=None)
def hook_inbox(bus_url: str, agent_id: str | None):
    """Emit the Stop-hook result from the installed package's interpreter."""
    import json
    import signal

    def deadline_expired(*_):
        raise TimeoutError("Hook deadline exceeded")

    previous = signal.signal(signal.SIGALRM, deadline_expired)
    signal.alarm(4)
    try:
        actor = agent_id or get_current_agent()
        if os.environ.get("AGENT_BUS_ALLOW_UNSIGNED") != "1" or os.environ.get("AGENT_BUS_SESSION_FILE"):
            actor = load_session(actor)["agent_id"]
        if not actor:
            return
        with sync_bus_client(actor, base_url=bus_url, timeout=3) as client:
            response = client.get(f"/inbox/{actor}/messages", params={"reply_needed": True, "limit": 3})
            response.raise_for_status()
            messages = response.json()["messages"]
        pending = [message for message in messages if message.get("reply_needed")]
        if not pending:
            return
        lines = [
            f"- {message['from_agent']}: {str((message.get('body') or {}).get('text', ''))[:80]}"
            for message in pending[-3:]
        ]
        reason = (
            f"Hay mensajes en agent-bus que requieren respuesta (mostrando {len(pending)}). "
            "Lee: agent-bus work inbox; responde: agent-bus work reply <message_id> \"<respuesta>\" --ack.\n"
            + "\n".join(lines)
        )
        click.echo(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    except Exception:
        # Missing credentials, an unavailable bus or a rejected session must not block Stop.
        return
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    app()
