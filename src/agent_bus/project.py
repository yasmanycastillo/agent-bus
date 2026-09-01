from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_DIR = ".agent-bus"


def find_project_dir(cwd: Path | None = None) -> Path | None:
    base = cwd or Path.cwd()
    candidate = base / PROJECT_DIR
    if candidate.is_dir():
        return candidate
    return None


def init_project(cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    project = base / PROJECT_DIR
    project.mkdir(exist_ok=True)
    (project / "agents").mkdir(exist_ok=True)

    config = project / "config.yaml"
    if not config.exists():
        config.write_text("bus_url: http://localhost:8420\n")

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
- **Always** lock files before editing them (`agent-bus work lock <file>`).
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
    base = cwd or Path.cwd()
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
