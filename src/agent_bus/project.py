from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess

import yaml


PROJECT_DIR = ".agent-bus"


def _base(cwd: Path | None = None) -> Path:
    base = Path(cwd or Path.cwd()).expanduser().resolve()
    if not base.is_dir():
        raise ValueError("Project location must be an existing directory")
    return base


def _git_roots(base: Path) -> tuple[Path, Path] | None:
    """Return (actual checkout, primary checkout) without inheriting Git overrides."""
    env = {key: value for key, value in os.environ.items()
           if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"}}
    try:
        checkout = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True, timeout=3, env=env,
        ).stdout.strip()
        worktrees = subprocess.run(
            ["git", "-C", str(base), "worktree", "list", "--porcelain", "-z"],
            check=True, capture_output=True, text=True, timeout=3, env=env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # Git's first entry is the primary checkout (or bare common repository).
    primary = next((record.removeprefix("worktree ") for record in worktrees.split("\0")
                    if record.startswith("worktree ")), checkout)
    return Path(checkout).resolve(), Path(primary).resolve()


def _explicit_root() -> Path | None:
    value = os.environ.get("AGENT_BUS_PROJECT_ROOT")
    if value is None:
        return None
    path = Path(value).expanduser()
    if not value or not path.is_absolute() or not path.is_dir():
        raise ValueError("AGENT_BUS_PROJECT_ROOT must name an existing absolute directory")
    return path.resolve()


def _has_project_marker(base: Path) -> bool:
    marker = base / PROJECT_DIR
    if not marker.is_dir():
        return False
    # The legacy user configuration directory is not automatically a project.
    if base == Path.home().resolve():
        path = marker / "config.yaml"
        if not path.exists():
            return False
        data = yaml.safe_load(path.read_text()) or {}
        return isinstance(data, dict) and bool({"project_id", "bus_url"} & data.keys())
    return True


def _has_git_boundary(base: Path) -> bool:
    """Recognize repository boundaries even when Git cannot read the metadata."""
    return (base / ".git").exists()


def resolve_project_root(cwd: Path | None = None) -> Path | None:
    """Find the nearest project, sharing canonical state across Git worktrees.

    Nested repositories are hard boundaries, including ones without an agent-bus
    marker. A linked checkout resolves marker paths against the primary checkout.
    """
    explicit = _explicit_root()
    base = explicit if explicit is not None else _base(cwd)
    roots = _git_roots(base)
    if roots:
        checkout, common = roots
        for candidate in (base, *base.parents):
            if not candidate.is_relative_to(checkout):
                break
            canonical = common / candidate.relative_to(checkout)
            if _has_project_marker(canonical):
                return canonical.resolve()
            if candidate == checkout:
                break
        return common
    # Explicit non-Git locations intentionally establish their own boundary.
    if explicit is not None:
        return explicit
    for candidate in (base, *base.parents):
        if _has_project_marker(candidate):
            return candidate
        # Fail closed if Git is unavailable or metadata is broken: do not
        # inherit an outer project's configuration through a nested repository.
        if _has_git_boundary(candidate):
            return candidate
    return None


def get_checkout_root(cwd: Path | None = None) -> Path | None:
    """Actual source checkout for generated files; shared config stays canonical."""
    base = _base(cwd)
    roots = _git_roots(base)
    if roots:
        return roots[0]
    return resolve_project_root(base)


def project_identity(root: Path) -> str:
    canonical = Path(root).resolve()
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", canonical.name).strip("-_.")[:40] or "project"
    digest = hashlib.sha256(str(canonical).encode()).hexdigest()[:16]
    return f"{name}-{digest}"


def find_project_dir(cwd: Path | None = None) -> Path | None:
    root = resolve_project_root(cwd)
    candidate = root / PROJECT_DIR if root else None
    return candidate if candidate and candidate.is_dir() else None


def init_project(cwd: Path | None = None) -> Path:
    base = resolve_project_root(cwd) or _base(cwd)
    project = base / PROJECT_DIR
    project.mkdir(parents=True, exist_ok=True)
    (project / "agents").mkdir(exist_ok=True)
    config = project / "config.yaml"
    existing = yaml.safe_load(config.read_text()) or {} if config.exists() else {}
    if not isinstance(existing, dict):
        raise ValueError("Project config must contain a YAML mapping")
    updated = dict(existing)
    updated.setdefault("bus_url", "http://127.0.0.1:8420")
    updated.setdefault("project_id", project_identity(base))
    if updated != existing or not config.exists():
        config.write_text(yaml.safe_dump(updated, sort_keys=False))
    return project


def load_project_config(cwd: Path | None = None) -> dict:
    project = find_project_dir(cwd)
    if not project:
        return {}
    config_path = project / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def get_agents_dir(cwd: Path | None = None) -> Path | None:
    project = find_project_dir(cwd)
    if not project:
        return None
    agents = project / "agents"
    return agents if agents.is_dir() else None


def get_plan_path(cwd: Path | None = None) -> Path | None:
    project = find_project_dir(cwd)
    if not project:
        return None
    plan = project / "plan.md"
    return plan if plan.exists() else None


def create_agent_config(agent_id: str, config: dict, cwd: Path | None = None) -> Path:
    agents_dir = get_agents_dir(cwd)
    if not agents_dir:
        raise FileNotFoundError("Run 'agent-bus init' first")
    path = agents_dir / f"{agent_id}.yaml"
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    return path


def create_plan(content: str, cwd: Path | None = None) -> Path:
    project = find_project_dir(cwd)
    if not project:
        raise FileNotFoundError("Run 'agent-bus init' first")
    path = project / "plan.md"
    path.write_text(content)
    return path


_AGENT_PROTOCOL_TEMPLATE = """\
# agent-bus protocol for {agent_id}

You are {agent_id}, a participating agent in this project coordinated via **agent-bus**.
Read these instructions at the start of every session and follow them strictly.

## Session startup

1. Run `agent-bus work as {agent_id}` to set yourself as the active agent.
2. **Ensure background listener is active**: Keep a background listener running (`agent-bus watch --agent {agent_id}` or daemon `agent-bus worker start --agent {agent_id}`) so you receive incoming SSE notifications and wake up automatically without waiting for a human prompt. Do not stop it unless the human explicitly asks to end the session.
3. Run `agent-bus work check` to see if you have pending messages. **If you have pending messages, read them FIRST before doing anything else.**
4. Run `agent-bus show dashboard` to see current project state: tasks, inbox, locks, active agents.
5. Review the plan: read `.agent-bus/plan.md` if it exists.
6. Claim a task or continue one assigned to you before starting work.

## When to check inbox

**You MUST check for messages at these moments:**
- At the start of every session (`agent-bus work check`)
- After completing a task (`agent-bus work check` before `work done`)
- Before claiming a new task (`agent-bus work check`)
- After sending a message to another agent (`agent-bus work check`)
- When the user or another agent mentions they sent you a message
- Any time you're about to make a decision that affects other agents

Use `agent-bus work check` for a quick check. Use `agent-bus work inbox` for full details.

## Autonomous Operation Rules

- **Autonomous Background Listener**: You MUST have a background watcher/worker running during the entire session to reactively receive requests from other agents.
- **Never** edit a file locked by another agent. Check with `agent-bus show locks`.
- **Always** lock files before editing them (`agent-bus work lock <file>`). Preserve acquisition_id and expires_at; renew with `agent-bus work renew-lock <file> --acquisition-id <token>` before expiry, and release with `agent-bus work unlock <file> --acquisition-id <token>`. Stop editing if renewal fails. Use `--scope project` consistently for resources shared across worktrees.
- **Always** communicate via `agent-bus work msg` — never assume the other agent knows what you are doing.
- **Always** register decisions that affect architecture or scope (`agent-bus work decide`).
- **Always** hand off tasks properly with context if you cannot finish them.
- **Always** check inbox (`agent-bus work check`) before starting new work and after finishing tasks.
- If another agent sends you a message requiring reply (`reply_needed`), respond promptly.

## Useful commands

| Command | Description |
|---|---|
| `agent-bus work check` | Quick check for pending messages |
| `agent-bus work inbox` | Full inbox listing |
| `agent-bus show dashboard` | Full project overview |
| `agent-bus show tasks` | List all tasks |
| `agent-bus show tasks --status in_progress` | Filter tasks by status |
| `agent-bus show inbox <agent>` | View agent inbox |
| `agent-bus show locks` | Active file locks |
| `agent-bus show decisions` | Registered decisions |
| `agent-bus show agents` | Registered agents |
| `agent-bus work context` | View shared project context |
| `agent-bus work context --update tech_stack "fastapi,postgres"` | Update project context |
"""


def generate_agent_protocol(agent_id: str, cwd: Path | None = None) -> Path | None:
    project = find_project_dir(cwd)
    if not project:
        return None
    base = get_checkout_root(cwd) or project.parent
    path = base / f"{agent_id.upper()}.md"
    path.write_text(_AGENT_PROTOCOL_TEMPLATE.format(agent_id=agent_id))
    return path


def generate_all_agent_protocols(cwd: Path | None = None) -> list[Path]:
    agents_dir = get_agents_dir(cwd)
    if not agents_dir:
        return []
    paths = []
    for config_file in sorted(agents_dir.glob("*.yaml")):
        agent_id = config_file.stem
        path = generate_agent_protocol(agent_id, cwd)
        if path:
            paths.append(path)
    return paths
