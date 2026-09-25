"""Local session observation reports only what the provider recorded."""
from __future__ import annotations

import json
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
    encoded = "-" + project.resolve().as_posix().lstrip("/").replace("/", "-")
    folder = tmp_path / "home" / ".claude" / "projects" / encoded
    folder.mkdir(parents=True)
    (folder / "session.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-09-25T10:00:00Z", "cwd": str(project)}) + "\n",
        encoding="utf-8",
    )
    found = {item["provider"]: item for item in scan_providers(project, home=tmp_path / "home")}
    assert found["claude"]["session_in_project"] is True
    assert found["claude"]["input_tokens"] is None


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
