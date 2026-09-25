"""Open a credential in the project being coordinated, not in the agent-bus checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path

from agent_bus.project import init_project, resolve_project_root
from agent_bus.security import AuthenticationError


def is_agent_bus_source(root: Path) -> bool:
    project = root / "pyproject.toml"
    package = root / "src" / "agent_bus" / "__init__.py"
    if not project.is_file() or not package.is_file():
        return False
    return 'name = "agent-bus"' in project.read_text(encoding="utf-8", errors="ignore")


def workspace_root(project_path: str | None) -> Path:
    if project_path:
        candidate = Path(project_path).expanduser()
        if not candidate.is_dir():
            raise AuthenticationError("project_path tiene que ser un directorio que ya existe")
        base = candidate.resolve()
    else:
        base = Path.cwd().resolve()
    return resolve_project_root(base) or base


async def join_workspace(server, project_path: str | None) -> None:
    """Bind the MCP session to project_path. Never create a credential in this source checkout."""
    if server.session is not None:
        return
    if not server.agent_id:
        raise AuthenticationError(
            "El MCP arrancó sin --agent. Vuelve a instalarlo; no crees una credencial a mano."
        )
    root = workspace_root(project_path)
    credential = root / ".agent-bus" / "runtime" / "credentials" / f"{server.agent_id}.json"
    if is_agent_bus_source(root) and not credential.is_file():
        raise AuthenticationError(
            "No creo credenciales en el directorio de agent-bus. "
            "Pasa project_path del proyecto que vas a coordinar."
        )
    if not credential.is_file():
        init_project(root)
    _bind_environment(root)
    if not credential.is_file():
        await _create_credential(server.agent_id)
    await _ensure_hub(root)
    _adopt(server, root, credential)


def _bind_environment(root: Path) -> None:
    os.environ["AGENT_BUS_PROJECT_ROOT"] = str(root)
    for key in (
        "AGENT_BUS_CONFIG_DIR",
        "AGENT_BUS_DATABASE_PATH",
        "AGENT_BUS_PROJECT_ID",
        "AGENT_BUS_URL",
        "AGENT_BUS_SESSION_FILE",
    ):
        os.environ.pop(key, None)


async def _create_credential(agent_id: str) -> None:
    from agent_bus.config import get_config_dir, load_config
    from agent_bus.reputation.database import Database
    from agent_bus.security import SessionStore

    config = load_config()
    target = get_config_dir() / "credentials" / f"{agent_id}.json"
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    database = Database(config.database_path, project_id=config.bus.project_id)
    await database.initialize()
    try:
        session = await SessionStore(database, config.bus.project_id).create(agent_id, "agent", 86400)
        with os.fdopen(fd, "w") as stream:
            fd = None
            json.dump(session, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if fd is not None:
            os.close(fd)
        target.unlink(missing_ok=True)
        raise
    finally:
        await database.close()


async def _ensure_hub(root: Path) -> None:
    from agent_bus.cli.guided_run import _free_port, _probe_health, _remember_bus_url, ensure_project_hub
    from agent_bus.cli.main import _ensure_global_config, _runtime_environment
    from agent_bus.config import get_bus_url, get_config_dir, load_config

    config = load_config()
    url = get_bus_url()

    def start(host: str, port: int) -> None:
        import subprocess
        import sys

        _ensure_global_config()
        pid_file = get_config_dir() / "bus.pid"
        log_file = get_config_dir() / "bus.log"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 0)
                return
            except (OSError, ValueError):
                pid_file.unlink(missing_ok=True)
        command = [sys.executable, "-m", "uvicorn", "agent_bus.core.bus:create_app",
                   "--host", host, "--port", str(port), "--factory"]
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=log, start_new_session=True, env=_runtime_environment(),
            )
        pid_file.write_text(str(process.pid))

    def wait(candidate: str, project_id: str) -> bool:
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (_probe_health(candidate) or {}).get("project_id") == project_id:
                return True
            time.sleep(0.2)
        return False

    chosen = ensure_project_hub(
        url, config.bus.project_id, probe=_probe_health, start=start,
        free_port=_free_port, echo=lambda *_args: None, wait=wait,
    )
    if chosen != url:
        _remember_bus_url(root, chosen)
    os.environ["AGENT_BUS_URL"] = chosen


def _adopt(server, root: Path, credential: Path) -> None:
    from agent_bus.config import get_bus_url, load_config
    from agent_bus.security import load_session

    config = load_config()
    os.environ["AGENT_BUS_SESSION_FILE"] = str(credential)
    os.environ["AGENT_BUS_URL"] = get_bus_url()
    server.session = load_session(server.agent_id, session_file=credential, project_id=config.bus.project_id)
    server.agent_id = server.session["agent_id"]
    server.project_id = config.bus.project_id
    server.bus_url = os.environ["AGENT_BUS_URL"]
    server._lock_cwd = root
    server._lock_project_root = root
