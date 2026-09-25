"""Camino interactivo: agentes, capacidades y avance hasta el veredicto."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path

import click

CAPABILITIES = (
    "repository-analysis",
    "long-context",
    "architecture",
    "implementation",
    "tests",
    "python",
    "odoo",
    "code-review",
)
IMPLEMENTER_PRESET = (
    "repository-analysis",
    "long-context",
    "architecture",
    "implementation",
    "tests",
    "python",
    "odoo",
)
REVIEWER_PRESET = ("code-review", "odoo")
IMPLEMENTER_REQUIRED = ("repository-analysis", "long-context", "architecture", "implementation", "tests")
CAPABILITY_HELP = {
    "repository-analysis": "leer el repositorio y decir qué hay",
    "long-context": "leer mucho código de una vez",
    "architecture": "proponer cómo se hace el cambio",
    "implementation": "escribir el código",
    "tests": "ocuparse de las pruebas",
    "python": "trabajar en Python",
    "odoo": "trabajar en un módulo de Odoo",
    "code-review": "revisar el cambio de otro, sin programarlo",
}


def collect_answers(prompt, confirm, echo=None) -> dict:
    """Pregunta la corrida. prompt(text, default=, type=) y confirm(text, default=)."""
    say = echo or click.echo
    say("Vamos a preparar una corrida. Tú respondes. Si aceptas lo sugerido, pulsa Enter.")
    say("Hacen falta dos papeles distintos: uno escribe el código y otro solo dice si ese commit se puede unir.")
    instance = prompt("Nombre de esta corrida. Sirve para no mezclarla con otras. Ejemplo: praxia", default="odoo-1")
    implementer = prompt("Nombre de quien escribe el código. Ejemplo: impl", default="impl")
    reviewer = prompt("Nombre de quien revisa. Tiene que ser otro nombre, no el de quien programa", default="reviewer")
    if not instance or not implementer or not reviewer:
        raise click.ClickException("La corrida, el implementador y el revisor tienen nombre")
    if implementer == reviewer:
        raise click.ClickException("El revisor tiene que ser otro agente")
    say("Paquete de quien programa: leer el repo, diseñar, escribir el código y probar, incluido Odoo.")
    say("Si pulsas Enter, se usa ese paquete. Escribe elegir solo si quieres marcar habilidad por habilidad.")
    impl_caps = _caps(prompt, confirm, "quien programa", IMPLEMENTER_PRESET, IMPLEMENTER_REQUIRED)
    say("Paquete de quien revisa: solo revisar el cambio. No escribe código.")
    say("Si pulsas Enter, se usa ese paquete. Escribe elegir solo si quieres marcar habilidad por habilidad.")
    review_caps = _caps(prompt, confirm, "quien revisa", REVIEWER_PRESET, ("code-review",))
    impl_runtime, impl_command = _runtime(prompt, "quien programa")
    review_runtime, review_command = _runtime(prompt, "quien revisa")
    test_text = prompt(
        "Comando que prueba el módulo antes de unirlo a main. Enter lo deja vacío y se usa uv run pytest -q",
        default="",
    )
    return {
        "instance": instance,
        "implementer": implementer,
        "reviewer": reviewer,
        "impl_caps": impl_caps,
        "review_caps": review_caps,
        "impl_runtime": impl_runtime,
        "impl_command": impl_command,
        "review_runtime": review_runtime,
        "review_command": review_command,
        "test_cmd": shlex.split(test_text) if test_text else [],
    }


def _caps(prompt, confirm, role: str, preset: tuple[str, ...], required: tuple[str, ...]) -> list[str]:
    mode = prompt(
        f"Para {role}: escribe recomendado o elegir",
        default="recomendado",
        type=click.Choice(["recomendado", "elegir"]),
    )
    if mode == "recomendado":
        return list(preset)
    chosen = [
        name for name in CAPABILITIES
        if confirm(f"¿{role} puede {CAPABILITY_HELP[name]}?", default=name in preset)
    ]
    missing = [name for name in required if name not in chosen]
    if missing:
        plain = ", ".join(CAPABILITY_HELP[name] for name in missing)
        raise click.ClickException(f"A {role} le falta poder: {plain}")
    return chosen


def _runtime(prompt, role: str) -> tuple[str, list[str]]:
    kind = prompt(
        f"Cómo trabaja {role}: comando (se lanza un programa y se espera) o worker (queda un proceso del bus abierto)",
        default="comando",
        type=click.Choice(["comando", "worker"]),
    )
    if kind == "worker":
        return "native", []
    tool = prompt(
        f"Qué programa usa {role}: claude, codex u otro",
        default="claude",
        type=click.Choice(["claude", "codex", "otro"]),
    )
    default = {"claude": "claude", "codex": "codex"}.get(tool, "")
    text = prompt(
        f"Comando exacto de {role}. Si elegiste claude o codex, Enter deja ese nombre",
        default=default,
    )
    command = shlex.split(text)
    if not command:
        raise click.ClickException(f"{role} necesita un comando")
    return "external", command


def workflow_yaml(test_cmd: list[str]) -> str:
    rendered = ""
    if test_cmd:
        rendered = "\n    test_cmd: [" + ", ".join(json.dumps(part) for part in test_cmd) + "]"
    return f"""workflow: feature-development
version: 1
steps:
  - id: discovery
    requires: [repository-analysis, long-context]
  - id: design
    requires: [architecture]
    depends_on: [discovery]
  - id: implementation
    requires: [implementation, tests]
    depends_on: [design]{rendered}
  - id: review
    requires: [code-review]
    depends_on: [implementation]
    policy:
      independent_from: [implementation]
  - id: integration
    depends_on: [review]
    gate:
      tests: passed
      review: approved
"""


def _ok(response, allowed: tuple[int, ...] = (200,)) -> dict:
    if response.status_code not in allowed:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        raise click.ClickException(detail)
    if not response.content:
        return {}
    return response.json()


def ensure_worktree(repo: Path, agent: str) -> tuple[Path, str]:
    branch = f"agent/{agent}"
    path = repo / ".worktrees" / agent
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    exclude = Path(common)
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude = exclude / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text() if exclude.exists() else ""
    if ".worktrees/" not in current:
        exclude.write_text(current + "\n.worktrees/\n")
    if (path / ".git").exists() or path.exists():
        return path, branch
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = subprocess.run(["git", "show-ref", "--verify", f"refs/heads/{branch}"], cwd=repo, capture_output=True)
    command = ["git", "worktree", "add", str(path), branch] if exists.returncode == 0 else [
        "git", "worktree", "add", "-b", branch, str(path),
    ]
    subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)
    return path, branch


def _register_agent(client, agent_id: str, caps: list[str], runtime: str, command: list[str], *, edit: bool) -> None:
    created = client.post("/register", json={"agent_id": agent_id, "display_name": agent_id, "capabilities": caps})
    if created.status_code not in (200, 201, 409):
        _ok(created)
    _ok(client.post(f"/agents/{agent_id}/route-profile", json={
        "declared": caps, "approved": caps, "priority": 1, "can_edit": edit,
    }))
    _ok(client.post(f"/agents/{agent_id}/runtime", json={"runtime": runtime, "command": command}))


def advance_once(client, instance: str, workspace: Path, agents: list[str], *, repo: Path | None, branch: str | None) -> dict:
    for agent_id in agents:
        _ok(client.post(f"/agents/{agent_id}/heartbeat"))
    body: dict = {
        "workflow": "feature-development",
        "instance_id": instance,
        "workspace_ref": str(workspace),
        "timeout": 30,
    }
    if repo is not None and branch:
        body["repo_dir"] = str(repo)
        body["candidate_branch"] = branch
    return _ok(client.post("/workflows/advance", json=body), allowed=(200, 409))


def drive_run(client, answers: dict, *, repo: Path, ask, verdict_client=None) -> dict:
    """Registra, compila y avanza. ask devuelve (approve|changes_requested, motivo)."""
    workspace, branch = ensure_worktree(repo, answers["implementer"])
    agents = [answers["implementer"], answers["reviewer"]]
    _register_agent(client, answers["implementer"], answers["impl_caps"], answers["impl_runtime"], answers["impl_command"], edit=True)
    _register_agent(client, answers["reviewer"], answers["review_caps"], answers["review_runtime"], answers["review_command"], edit=False)
    compiled = _ok(client.post("/workflows/compile", json={
        "yaml": workflow_yaml(answers["test_cmd"]), "instance_id": answers["instance"],
    }))
    click.echo(f"Tareas: {', '.join(task['task_id'] for task in compiled.get('tasks') or [])}")
    review_id = f"feature-development-{answers['instance']}-review"
    for _ in range(20):
        result = advance_once(client, answers["instance"], workspace, agents, repo=None, branch=None)
        status = result.get("status")
        click.echo(f"Avance: {status} {result.get('task_id', '')} {result.get('task_status', '')}".strip())
        if status == "dispatched" and result.get("task_status") == "in_progress":
            _wait_done(client, result["task_id"])
            continue
        if status == "dispatched" and result.get("task_status") == "done":
            continue
        if status in ("waiting_for_integration", "waiting_for_review"):
            verdict, reason = ask()
            _ok((verdict_client or client).post(f"/tasks/{review_id}/verdict", json={
                "agent_id": answers["reviewer"], "verdict": verdict, "reason": reason,
            }))
            if verdict != "approve":
                click.echo("Cambios pedidos. La implementación se reabre.")
                continue
            merged = advance_once(client, answers["instance"], workspace, agents, repo=repo, branch=branch)
            click.echo(f"Integración: {merged.get('status')} {merged.get('error', '')}".strip())
            if merged.get("status") == "blocked":
                raise click.ClickException(merged.get("error") or "integración bloqueada")
            if merged.get("status") != "integrated":
                raise click.ClickException(merged.get("error") or merged.get("status") or "la integración no terminó")
            return merged
        if status == "blocked":
            raise click.ClickException(result.get("error") or "bloqueado")
        if status in ("unroutable", "no_runtime", "idle", "not_claimed"):
            raise click.ClickException(result.get("error") or status or "no hay paso para avanzar")
        if result.get("error"):
            raise click.ClickException(result["error"])
    raise click.ClickException("La corrida no terminó")


def _wait_done(client, task_id: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        stored = _ok(client.get(f"/tasks/{task_id}"))
        if stored.get("status") == "done":
            return
        time.sleep(1)
    raise click.ClickException(f"{task_id} sigue en curso. Revisa el worker de ese agente.")


def git_root(start: Path | None = None) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start or Path.cwd(), capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise click.ClickException("Este directorio no es un repositorio Git")
    return Path(result.stdout.strip())


def ensure_session(agent_id: str, role: str = "agent") -> None:
    """Crea la credencial local si no existe. No pide que elijas un agente antes."""
    from agent_bus.cli.auth_cmds import create
    from agent_bus.config import get_config_dir
    from agent_bus.security import AuthenticationError, load_session

    path = get_config_dir() / "credentials" / f"{agent_id}.json"
    if not path.exists():
        create.callback(agent=agent_id, provider=None, role=role, ttl=86400, output=None, quiet=True, show_token=False)
    try:
        load_session(agent_id, session_file=path)
    except AuthenticationError as exc:
        raise click.ClickException(
            f"La sesión de {agent_id} no sirve ({exc}). Borra {path} y vuelve a correr."
        ) from exc


def _session_client(agent_id: str, timeout: float):
    from agent_bus.config import get_bus_url
    from agent_bus.security import load_session, sync_bus_client

    return sync_bus_client(
        agent_id, session=load_session(agent_id), base_url=get_bus_url(), timeout=timeout,
    )


def _probe_health(url: str) -> dict | None:
    import httpx

    try:
        response = httpx.get(url.rstrip("/") + "/health", timeout=2)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def ensure_project_hub(url: str, project_id: str, *, probe, start, free_port, echo, wait) -> str:
    """Use this project's hub. If the port belongs to another project, start one on a free port."""
    if (probe(url) or {}).get("project_id") == project_id:
        return url
    host, port = _host_port(url)
    if probe(url) is None:
        start(host, port)
        if wait(url, project_id):
            return url
    new_port = free_port()
    new_url = f"http://{host}:{new_port}"
    echo(f"El puerto {port} ya lo usa otro proyecto. Este hub queda en {new_url}")
    start(host, new_port)
    if not wait(new_url, project_id):
        raise click.ClickException(f"El hub no quedó listo en {new_url}. Revisa el log del bus.")
    return new_url


def _host_port(url: str) -> tuple[str, int]:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _remember_bus_url(repo: Path, url: str) -> None:
    import yaml

    path = repo / ".agent-bus" / "config.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    data["bus_url"] = url
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_interactive() -> None:
    from agent_bus.cli.main import _start_daemon
    from agent_bus.config import get_bus_url, load_config
    from agent_bus.project import init_project

    repo = git_root()
    answers = collect_answers(click.prompt, click.confirm)
    init_project(repo)
    ensure_session("operator", role="admin")
    ensure_session(answers["implementer"])
    ensure_session(answers["reviewer"])

    def wait(url: str, project_id: str) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (_probe_health(url) or {}).get("project_id") == project_id:
                return True
            time.sleep(0.2)
        return False

    url = ensure_project_hub(
        get_bus_url(), load_config().bus.project_id,
        probe=_probe_health, start=_start_daemon, free_port=_free_port, echo=click.echo, wait=wait,
    )
    _remember_bus_url(repo, url)
    with _session_client("operator", 3600) as client, _session_client(answers["reviewer"], 60) as reviewer:
        if answers["impl_runtime"] == "native":
            _start_worker(answers["implementer"], repo / ".worktrees" / answers["implementer"])
        if answers["review_runtime"] == "native":
            _start_worker(answers["reviewer"], repo / ".worktrees" / answers["implementer"])

        def ask():
            choice = click.prompt("Veredicto", type=click.Choice(["aprobar", "cambios"]))
            if choice == "aprobar":
                return "approve", "aprobado"
            return "changes_requested", click.prompt("Qué hay que cambiar")

        drive_run(client, answers, repo=repo, ask=ask, verdict_client=reviewer)
    click.echo("Corrida integrada")


def _start_worker(agent_id: str, worktree: Path) -> None:
    from agent_bus.cli.worker_cmds import worker_start

    worker_start.callback(
        agent_id=agent_id, provider=None, model=None, worktree_dir=str(worktree), bus_url=None, foreground=False,
    )


@click.command("advance")
@click.option("--instance", required=True, help="Identificador de la corrida")
@click.option("--workspace", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--repo", default=None, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--branch", default=None)
@click.option("--agent", "agents", multiple=True, help="Agente al que latir antes de avanzar")
def workflow_advance(instance: str, workspace: Path, repo: Path | None, branch: str | None, agents: tuple[str, ...]) -> None:
    """Avanzar un paso del workflow. Late a los agentes indicados."""
    from agent_bus.cli.main import _client

    with _client() as client:
        body = advance_once(client, instance, workspace, list(agents), repo=repo, branch=branch)
    click.echo(json.dumps(body, ensure_ascii=False))


@click.command("run")
def run_command() -> None:
    """Preguntar agentes, capacidades y test, y correr feature-development."""
    run_interactive()
