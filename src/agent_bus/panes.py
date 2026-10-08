"""Agent TUIs in a private tmux server that the coordinator opens, types into and closes.

The user watches with `agent-bus panes view` (`tmux attach -r`): read-only, so only the
coordinator writes into the agents. State comes from markers each TUI draws on screen.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any, NamedTuple

# One tmux server per project (socket agent-bus-<project_id>), apart from the user's own,
# so one project's MCP or hub never sees another project's panes. Tests pin TMUX.
TMUX: list[str] | None = None
_project: str | None = None
SESSION = "agents"
# Each pane with an identity gets its watcher here, out of the session the user views.
WATCHERS = "watchers"
# Same code as the caller, so a watcher runs the version that opened its pane.
WATCH_COMMAND = [sys.executable, "-m", "agent_bus.cli.main", "watch", "--cli", "tmux"]
SCREEN_LINES = 12
# '.' and ':' separate window and pane in tmux targets; keep names to plain words.
NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")
# A pane gets only these from the caller: the identity of one agent-bus credential.
IDENTITY_ENV = frozenset({"AGENT_BUS_AGENT_ID", "AGENT_BUS_SESSION_FILE", "AGENT_BUS_URL", "AGENT_BUS_PROJECT_ID",
                      "AGENT_BUS_PROJECT_ROOT", "AGENT_BUS_CONFIG_DIR", "AGENT_BUS_DATABASE_PATH"})
# Everything else a pane would inherit is removed except these (no API keys or tokens):
# the agent CLIs read their own login from files under HOME.
BASE_ENV = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
                      "LC_MESSAGES", "TERM", "COLORTERM", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
                      "XDG_DATA_HOME", "XDG_CACHE_HOME", "SSH_AUTH_SOCK", "DISPLAY", "WAYLAND_DISPLAY",
                      "DBUS_SESSION_BUS_ADDRESS"})
# C0/C1 controls except tab and newline: an ESC[201~ would end the bracketed paste early and
# turn the rest of a message into real keystrokes in the agent's TUI.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class Preset(NamedTuple):
    program: str
    prompt_flag: str | None  # None: the initial prompt is a positional argument after "--"
    working: str  # regex (per line) drawn only while a turn runs
    blocked: str  # regex (per line) drawn while a trust or permission question waits


# Markers measured in tmux, 2026-10: Claude Code 2.1 spinner line "✶ Meandering…" (older
# builds: "esc to interrupt"); agy footer "esc to cancel"; trust and permission prompts ask
# to confirm or navigate a menu.
PRESETS = {
    # Only the spinner glyphs: a reply line "● Revisando…" is not a running turn.
    "claude": Preset("claude", None, r"^[·✢✳✶✻✽*] \S+…|esc to interrupt",
                     r"Enter to confirm|Do you want to proceed\?"),
    # agy permission menus also show "esc to cancel", so blocked is checked first.
    "agy": Preset("agy", "-i", r"esc to cancel", r"enter Confirm|↑/↓ Navigate|^Run this command\?"),
}


class PaneError(RuntimeError):
    pass


class PaneBusy(PaneError):
    """The agent is working or waiting on a question; try again later."""


def use_project(project_id: str) -> None:
    """Select the project whose tmux server the following calls use (one project per process)."""
    global _project
    _project = project_id


def _base() -> list[str]:
    if TMUX:
        return TMUX
    project = _project or os.environ.get("AGENT_BUS_PROJECT_ID")
    if not project:
        from agent_bus.config import load_config
        project = load_config().bus.project_id
    return ["tmux", "-L", "agent-bus-" + re.sub(r"[^A-Za-z0-9_-]", "_", project)]


def _tmux(*args: str, input_text: str | None = None) -> str:
    try:
        result = subprocess.run([*_base(), *args], input=input_text, capture_output=True, text=True)
    except FileNotFoundError:
        raise PaneError("tmux is not installed") from None
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
    """Names a new pane would inherit beyond BASE_ENV, to be removed.

    Panes get the tmux server's global environment (copied from whoever started the
    server), not the caller's, so both are scanned. That covers secrets (API keys,
    tokens), a coordinating Claude Code's own session (CLAUDE*; claude re-applies its
    settings.json env itself) and the coordinator's identity (AGENT_BUS_*).
    """
    names = set(os.environ)
    try:
        names |= {line.split("=", 1)[0] for line in _tmux("show-environment", "-g").splitlines()
                  if not line.startswith("-")}
    except PaneError:
        pass  # no server yet: it starts with this process's environment
    return names - BASE_ENV


def _target(name: str, session: str = SESSION) -> str:
    return f"{session}:={name}"  # '=' matches the window name exactly


def _windows(session: str, fields: str) -> list[list[str]]:
    try:
        out = _tmux("list-windows", "-t", f"={session}", "-F", fields)
    except PaneError:
        return []  # no server or no session yet
    return [line.split("\t") for line in out.splitlines()]


def list_panes() -> list[dict[str, str]]:
    """Panes with their state and watcher: 'running', 'stopped' (exited; see its window) or 'none'."""
    watchers = {name: "stopped" if dead == "1" else "running"
                for name, dead in _windows(WATCHERS, "#{window_name}\t#{pane_dead}")}
    return [{"name": name, "preset": preset, "state": "dead" if dead == "1" else state(name, preset),
             "watcher": watchers.get(name, "none")}
            for name, preset, dead in _windows(SESSION, "#{window_name}\t#{@agent_bus_preset}\t#{pane_dead}")]


def _open_window(session: str, name: str, cwd: str, command: list[str], *options: str) -> None:
    where = ["-n", name, "-c", cwd]
    if _tmux_ok("has-session", "-t", f"={session}"):
        create = ["new-window", "-d", "-t", f"={session}:", *where, "--", *command]
    else:
        create = ["new-session", "-d", "-s", session, "-x", "200", "-y", "50", *where, "--", *command]
    # One tmux invocation, so a program that exits at once still stays visible as dead.
    sets = [arg for option, value in zip(options[::2], options[1::2])
            for arg in (";", "set-option", "-w", "-t", _target(name, session), option, value)]
    _tmux(*create, ";", "set-option", "-w", "-t", _target(name, session), "remain-on-exit", "on", *sets)


def spawn(name: str, preset: str, *, cwd: str | None = None, model: str | None = None,
          prompt: str | None = None, extra_args: tuple[str, ...] = (),
          env: dict[str, str] | None = None, watch: bool = False) -> None:
    """Open NAME running PRESET. `env` is filtered to IDENTITY_ENV; prompt and model stay values.

    watch starts `agent-bus watch --cli tmux` for the identity in `env`, so the bus can
    wake the pane without anyone starting a watcher by hand.
    """
    spec = PRESETS.get(preset)
    if spec is None:
        raise PaneError(f"unknown preset '{preset}'; use one of {', '.join(PRESETS)}")
    if not NAME.fullmatch(name):
        raise PaneError(f"invalid pane name '{name}': use 1-40 letters, digits, '_' or '-'")
    if any(p["name"] == name for p in list_panes()):
        raise PaneError(f"pane '{name}' already exists")
    agent_id = (env or {}).get("AGENT_BUS_AGENT_ID")
    if watch and not agent_id:
        raise PaneError("a watcher needs the pane's agent-bus identity")
    unset = [arg for var in sorted(_inherited_names()) for arg in ("-u", var)]
    identity = [f"{k}={v}" for k, v in (env or {}).items() if k in IDENTITY_ENV]
    command = ["env", *unset, *identity, spec.program]
    # "--x=value" and "--" keep a model or prompt starting with '-' from becoming a flag.
    if model:
        command.append(f"--model={model}")
    command += extra_args
    if prompt:
        command += [f"{spec.prompt_flag}={prompt}"] if spec.prompt_flag else ["--", prompt]
    cwd = cwd or os.getcwd()
    _open_window(SESSION, name, cwd, command, "@agent_bus_preset", preset)
    if watch:
        _tmux_ok("kill-window", "-t", _target(name, WATCHERS))  # a stopped one left by an earlier pane
        watcher = ["env", *unset, *identity, *WATCH_COMMAND, "--agent", str(agent_id), "--tmux-agent", name]
        _open_window(WATCHERS, name, cwd, watcher)


def close(name: str) -> None:
    """Close the pane and stop its watcher, if any."""
    _tmux_ok("kill-window", "-t", _target(name, WATCHERS))
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
    """Paste the text as one bracketed paste and submit it; refuses a busy pane unless forced.

    Control characters are removed first, so the text can only ever be typed, not pressed.
    """
    text = CONTROL_CHARS.sub("", text)
    if not text.strip():
        raise PaneError("nothing to send")
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


# A menu option line: "> 1. Yes, run command" (agy) or "❯ 1. Yes" (Claude Code).
OPTION = re.compile(r"^\s*([>❯])?\s*(\d+)\.\s+(\S.*)$")


def question(name: str) -> dict[str, Any] | None:
    """The menu a blocked pane shows: its options, the selected one and, for agy, the command.

    A numbered list in the transcript is not a menu: one option must carry the cursor.
    """
    lines = screen(name, 40).splitlines()
    found = [m for m in map(OPTION.match, lines) if m]
    if not any(m.group(1) for m in found):
        return None
    command = None
    if "Requesting permission for:" in lines and "Run this command?" in lines:
        start, end = lines.index("Requesting permission for:"), lines.index("Run this command?")
        command = "\n".join(line.rstrip() for line in lines[start + 1:end] if line.strip())
    return {"command": command,
            "options": [{"number": int(m.group(2)), "label": m.group(3).strip()} for m in found],
            "selected": next((int(m.group(2)) for m in found if m.group(1)), None)}


def answer(name: str, number: int) -> None:
    """Move the menu cursor to option NUMBER and confirm it."""
    pane = next((p for p in list_panes() if p["name"] == name), None)
    if pane is None or pane["state"] != "blocked":
        raise PaneError(f"pane '{name}' is not waiting on a question")
    asked = question(name)
    numbers = [o["number"] for o in asked["options"]] if asked else []
    if asked is None or asked["selected"] is None or number not in numbers:
        raise PaneError(f"pane '{name}' has no option {number}: {numbers}")
    moves = number - asked["selected"]
    if moves:
        _tmux("send-keys", "-t", _target(name), "-N", str(abs(moves)), "Down" if moves > 0 else "Up")
    _tmux("send-keys", "-t", _target(name), "Enter")


def view_command(session: str = SESSION, writable: bool = False) -> list[str]:
    return [*_base(), "attach", *([] if writable else ["-r"]), "-t", f"={session}"]
