"""Agent TUIs in a private tmux server that the coordinator opens, types into and closes.

The user watches with `agent-bus panes view` (`tmux attach -r`): read-only, so only the
coordinator writes into the agents. State comes from markers each TUI draws on screen.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from typing import NamedTuple

# A dedicated socket keeps these panes out of the user's own tmux server.
TMUX = ["tmux", "-L", "agent-bus"]
SESSION = "agents"
SCREEN_LINES = 12
# '.' and ':' separate window and pane in tmux targets; keep names to plain words.
NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")
# A pane gets only these from the caller: the identity of one agent-bus credential.
PANE_ENV = frozenset({"AGENT_BUS_AGENT_ID", "AGENT_BUS_SESSION_FILE", "AGENT_BUS_URL", "AGENT_BUS_PROJECT_ID",
                      "AGENT_BUS_PROJECT_ROOT", "AGENT_BUS_CONFIG_DIR", "AGENT_BUS_DATABASE_PATH"})


class Preset(NamedTuple):
    program: str
    prompt_flag: str | None  # None: the initial prompt is a positional argument after "--"
    working: str  # regex (per line) drawn only while a turn runs
    blocked: str  # regex (per line) drawn while a trust or permission question waits


# Markers measured in tmux, 2026-10: Claude Code 2.1 spinner line "✶ Meandering…" (older
# builds: "esc to interrupt"); agy footer "esc to cancel"; trust and permission prompts ask
# to confirm or navigate a menu.
PRESETS = {
    "claude": Preset("claude", None, r"^\S \w+…|esc to interrupt",
                     r"Enter to confirm|Do you want to proceed\?"),
    # agy permission menus also show "esc to cancel", so blocked is checked first.
    "agy": Preset("agy", "-i", r"esc to cancel", r"enter Confirm|↑/↓ Navigate|^Run this command\?"),
}


class PaneError(RuntimeError):
    pass


class PaneBusy(PaneError):
    """The agent is working or waiting on a question; try again later."""


def _tmux(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run([*TMUX, *args], input=input_text, capture_output=True, text=True)
    if result.returncode != 0:
        raise PaneError(result.stderr.strip() or f"tmux {args[0]} failed")
    return result.stdout


def _tmux_ok(*args: str) -> bool:
    try:
        _tmux(*args)
        return True
    except PaneError:
        return False


def _inherited_names() -> set[str]:
    """CLAUDE* and AGENT_BUS_* names a new pane would inherit.

    Panes get the tmux server's global environment (copied from whoever started the
    server), not the caller's, so both are scanned. A coordinator running in Claude
    Code leaks its session through CLAUDE* (a nested claude then misbehaves; it
    re-applies settings.json env itself) and its own identity through AGENT_BUS_*.
    """
    names = set(os.environ)
    try:
        names |= {line.split("=", 1)[0] for line in _tmux("show-environment", "-g").splitlines()
                  if not line.startswith("-")}
    except PaneError:
        pass  # no server yet: it starts with this process's environment
    return {name for name in names if name.startswith(("CLAUDE", "AGENT_BUS_"))}


def _target(name: str) -> str:
    return f"{SESSION}:={name}"  # '=' matches the window name exactly


def list_panes() -> list[dict[str, str]]:
    try:
        out = _tmux("list-windows", "-t", f"={SESSION}", "-F",
                    "#{window_name}\t#{@agent_bus_preset}\t#{pane_dead}")
    except PaneError:
        return []  # no server or no session yet: no panes
    panes = []
    for line in out.splitlines():
        name, preset, dead = line.split("\t")
        panes.append({"name": name, "preset": preset,
                      "state": "dead" if dead == "1" else state(name, preset)})
    return panes


def spawn(name: str, preset: str, *, cwd: str | None = None, model: str | None = None,
          prompt: str | None = None, extra_args: tuple[str, ...] = (),
          env: dict[str, str] | None = None) -> None:
    """Open NAME running PRESET. `env` is filtered to PANE_ENV; prompt and model stay values."""
    spec = PRESETS.get(preset)
    if spec is None:
        raise PaneError(f"unknown preset '{preset}'; use one of {', '.join(PRESETS)}")
    if not NAME.fullmatch(name):
        raise PaneError(f"invalid pane name '{name}': use 1-40 letters, digits, '_' or '-'")
    if any(p["name"] == name for p in list_panes()):
        raise PaneError(f"pane '{name}' already exists")
    unset = [arg for var in sorted(_inherited_names()) for arg in ("-u", var)]
    identity = [f"{k}={v}" for k, v in (env or {}).items() if k in PANE_ENV]
    command = ["env", *unset, *identity, spec.program]
    # "--x=value" and "--" keep a model or prompt starting with '-' from becoming a flag.
    if model:
        command.append(f"--model={model}")
    command += extra_args
    if prompt:
        command += [f"{spec.prompt_flag}={prompt}"] if spec.prompt_flag else ["--", prompt]
    where = ["-n", name, "-c", cwd or os.getcwd()]
    if _tmux_ok("has-session", "-t", f"={SESSION}"):
        create = ["new-window", "-d", "-t", f"={SESSION}:", *where, "--", *command]
    else:
        create = ["new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", *where, "--", *command]
    # One tmux invocation, so an agent that exits at once still stays visible as 'dead'.
    _tmux(*create, ";", "set-option", "-w", "-t", _target(name), "remain-on-exit", "on",
          ";", "set-option", "-w", "-t", _target(name), "@agent_bus_preset", preset)


def close(name: str) -> None:
    _tmux("kill-window", "-t", _target(name))


def screen(name: str, lines: int = SCREEN_LINES) -> str:
    text = _tmux("capture-pane", "-p", "-t", _target(name)).rstrip("\n")
    return "\n".join(text.splitlines()[-lines:])


def state(name: str, preset: str) -> str:
    """'blocked', 'working' or 'idle' from the footer; transcript above it is ignored."""
    spec = PRESETS.get(preset)
    if spec is None:
        return "unknown"
    footer = screen(name)
    if re.search(spec.blocked, footer, re.MULTILINE):
        return "blocked"
    if re.search(spec.working, footer, re.MULTILINE):
        return "working"
    return "idle"


def send(name: str, text: str, *, force: bool = False) -> None:
    """Paste the text as one bracketed paste and submit it; refuses a busy pane unless forced."""
    pane = next((p for p in list_panes() if p["name"] == name), None)
    if pane is None:
        raise PaneError(f"no pane '{name}'")
    if pane["state"] in ("working", "blocked") and not force:
        raise PaneBusy(f"pane '{name}' is {pane['state']}")
    if pane["state"] != "idle" and not force:
        raise PaneError(f"pane '{name}' is {pane['state']}")
    buffer = f"agent-bus-{uuid.uuid4().hex}"  # concurrent sends must not share a buffer
    _tmux("load-buffer", "-b", buffer, "-", input_text=text)
    _tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", _target(name))
    # ponytail: fixed settle delay; TUIs that read the paste slowly take Enter as a newline.
    time.sleep(0.3)
    _tmux("send-keys", "-t", _target(name), "Enter")


def view_command() -> list[str]:
    return [*TMUX, "attach", "-r", "-t", f"={SESSION}"]
