"""Local session observation reports only what the provider recorded."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from agent_bus.observe.sessions import active_seconds, scan_providers


def test_active_time_skips_silence():
    start = datetime(2026, 9, 25, tzinfo=timezone.utc)
    measured = active_seconds([start, start + timedelta(seconds=10), start + timedelta(minutes=10)])
    assert measured == 10
    assert active_seconds([start]) is None


def test_codex_session_in_the_project_keeps_unknown_cost(tmp_path):
    project = tmp_path / "traza"
    project.mkdir()
    sessions = tmp_path / "home" / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "rollout.jsonl"
    start = datetime.now(timezone.utc).replace(microsecond=0)
    events = [
        {"timestamp": start.isoformat(), "type": "session_meta", "payload": {"cwd": str(project), "runtime_workspace_roots": [str(project)]}},
        {"timestamp": (start + timedelta(seconds=5)).isoformat(), "type": "token_usage_record", "payload": {"usage": {"input_tokens": 12, "output_tokens": 4}}},
        {"timestamp": (start + timedelta(seconds=9)).isoformat(), "type": "response_item", "payload": {"path": str(project / "docs" / "catalogo.md")}},
    ]
    log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    codex = found["codex"]
    assert codex["session_in_project"] is True
    assert codex["input_tokens"] == 12
    assert codex["output_tokens"] == 4
    assert codex["estimated_cost_usd"] is None
    assert codex["active_seconds"] == 9
    assert codex["files"] == [str(project / "docs" / "catalogo.md")]
    assert found["agy"]["session_in_project"] is None
    assert found["agy"]["input_tokens"] is None


def test_claude_directory_name_marks_the_project(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    encoded = re.sub(r"[^A-Za-z0-9]", "-", project.resolve().as_posix())
    folder = tmp_path / "home" / ".claude" / "projects" / encoded
    folder.mkdir(parents=True)
    (folder / "session.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-09-25T10:00:00Z", "cwd": str(project)}) + "\n",
        encoding="utf-8",
    )
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    assert found["claude"]["session_in_project"] is True
    assert found["claude"]["input_tokens"] is None


def test_claude_directory_name_replaces_dots_and_underscores(tmp_path):
    project = tmp_path / ".worktrees" / "my_agent"
    project.mkdir(parents=True)
    # Claude Code turns every non-alphanumeric character into "-": /.worktrees -> --worktrees.
    encoded = re.sub(r"[^A-Za-z0-9]", "-", project.resolve().as_posix())
    assert "--worktrees-my-agent" in encoded
    folder = tmp_path / "home" / ".claude" / "projects" / encoded
    folder.mkdir(parents=True)
    (folder / "session.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-09-25T10:00:00Z", "cwd": str(project)}) + "\n",
        encoding="utf-8",
    )
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    assert found["claude"]["session_in_project"] is True


def test_hermes_included_cost_stays_unknown(tmp_path):
    project = tmp_path / "praxia"
    project.mkdir()
    database = tmp_path / "home" / ".hermes" / "state.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE sessions (
            id TEXT, cwd TEXT, git_repo_root TEXT, started_at TEXT, ended_at TEXT,
            last_activity_at TEXT, input_tokens INTEGER, output_tokens INTEGER,
            actual_cost_usd REAL, cost_status TEXT
        )"""
    )
    connection.execute(
        """CREATE TABLE messages (session_id TEXT, timestamp TEXT)"""
    )
    connection.execute(
        """INSERT INTO sessions VALUES ('s1', ?, ?, '2026-09-25T10:00:00Z', NULL,
           '2026-09-25T10:00:20Z', 0, 3, 1.5, 'included')""",
        (str(project.resolve()), str(project.resolve())),
    )
    connection.execute(
        "INSERT INTO messages VALUES ('s1', '2026-09-25T10:00:00Z'), ('s1', '2026-09-25T10:00:20Z')"
    )
    connection.commit()
    connection.close()
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    hermes = found["hermes"]
    assert hermes["session_in_project"] is True
    assert hermes["input_tokens"] == 0
    assert hermes["output_tokens"] == 3
    assert hermes["estimated_cost_usd"] is None
    assert hermes["active_seconds"] == 20


def test_codex_finds_an_older_session_behind_other_projects(tmp_path):
    project = tmp_path / "mine"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    sessions = tmp_path / "home" / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    start = datetime.now(timezone.utc).replace(microsecond=0)

    def write(name, cwd, mtime):
        log = sessions / name
        log.write_text(json.dumps({"timestamp": start.isoformat(), "type": "session_meta", "payload": {"cwd": str(cwd)}}) + "\n", encoding="utf-8")
        os.utime(log, (mtime, mtime))

    now = start.timestamp()
    write("mine.jsonl", project, now - 1000)
    for index in range(40):
        write(f"other-{index}.jsonl", other, now - index)
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    assert found["codex"]["session_in_project"] is True


def test_claude_live_status_comes_from_running_sessions_in_the_project(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    live = tmp_path / "home" / ".claude" / "sessions"
    live.mkdir(parents=True)

    def publish(pid, cwd, status):
        (live / f"{pid}.json").write_text(json.dumps({"pid": pid, "cwd": str(cwd), "status": status}), encoding="utf-8")

    publish(os.getpid(), project / "src", "busy")
    publish(os.getpid() + 10_000_000, project, "waiting")  # no such process: stale file
    publish(os.getppid(), tmp_path / "elsewhere", "waiting")
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    assert found["claude"]["live_status"] == "busy"
    assert found["codex"]["live_status"] is None

    publish(os.getpid(), project, "idle")
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    assert found["claude"]["live_status"] == "idle"


def test_agy_session_comes_from_history_and_its_transcript(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    cli = tmp_path / "home" / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    history = [
        {"display": "otro", "timestamp": int(start.timestamp() * 1000), "workspace": str(tmp_path / "otro"), "conversationId": "c-other"},
        {"display": "aquí", "timestamp": int(start.timestamp() * 1000) - 5000, "workspace": str(project), "conversationId": "c-mine"},
    ]
    (cli / "history.jsonl").write_text("".join(json.dumps(item) + "\n" for item in history), encoding="utf-8")
    logs = cli / "brain" / "c-mine" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    steps = [{"step_index": 0, "created_at": start.isoformat()}, {"step_index": 1, "created_at": (start + timedelta(seconds=7)).isoformat()}]
    (logs / "transcript.jsonl").write_text("".join(json.dumps(step) + "\n" for step in steps), encoding="utf-8")
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    assert found["agy"]["session_in_project"] is True
    assert found["agy"]["active_seconds"] == 7
    assert found["agy"]["input_tokens"] is None


def test_agy_history_of_other_projects_reports_no_session(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    cli = tmp_path / "home" / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True)
    (cli / "history.jsonl").write_text(json.dumps({"workspace": str(tmp_path / "otro"), "conversationId": "c", "timestamp": 1}) + "\n", encoding="utf-8")
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    assert found["agy"]["session_in_project"] is False


def test_unreadable_claude_session_file_is_skipped(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    live = tmp_path / "home" / ".claude" / "sessions"
    live.mkdir(parents=True)
    locked = live / "1.json"
    locked.write_text("{}", encoding="utf-8")
    locked.chmod(0)
    (live / "2.json").write_text(json.dumps({"pid": os.getpid(), "cwd": str(project), "status": "idle"}), encoding="utf-8")
    try:
        found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    finally:
        locked.chmod(0o600)
    assert found["claude"]["live_status"] == "idle"
