"""Read-only view of local agent sessions for one project.

Missing measurements stay null. A zero is recorded only when the source reported one.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SILENCE_SECONDS = 120
RECENT_SECONDS = 180
PROVIDERS = ("codex", "claude", "hermes", "agy", "grok")


def active_seconds(timestamps: list[datetime]) -> float | None:
    """Time covered by events that follow each other within the silence gap."""
    if len(timestamps) < 2:
        return None
    ordered = sorted(timestamps)
    total = 0.0
    for earlier, later in zip(ordered, ordered[1:]):
        gap = (later - earlier).total_seconds()
        if 0 <= gap <= SILENCE_SECONDS:
            total += gap
    return total


def scan_providers(project_root: Path, *, home: Path | None = None) -> list[dict[str, Any]]:
    root = project_root.expanduser().resolve()
    base = home.expanduser().resolve() if home is not None else Path.home()
    return [
        _codex(root, base),
        _claude(root, base),
        _hermes(root, base),
        _unobserved("agy"),
        _unobserved("grok"),
    ]


def _blank(provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "executable": shutil.which(provider) is not None,
        "session_in_project": None,
        "last_event_at": None,
        "active_seconds": None,
        "input_tokens": None,
        "output_tokens": None,
        "estimated_cost_usd": None,
        "files": [],
        "finished": None,
    }


def _unobserved(provider: str) -> dict[str, Any]:
    """The executable can be checked. This provider has no local history adapter."""
    return _blank(provider)


def _same_project(project_root: Path, candidate: str | None) -> bool:
    if not candidate or not isinstance(candidate, str):
        return False
    try:
        path = Path(candidate).expanduser().resolve()
    except OSError:
        return False
    return path == project_root or project_root in path.parents


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _finished(last_event: datetime | None, ended: bool) -> bool | None:
    if ended:
        return True
    if last_event is None:
        return None
    age = datetime.now(timezone.utc) - last_event
    if age <= timedelta(seconds=RECENT_SECONDS):
        return False
    return None


def _files_under(project_root: Path, payload: Any, found: list[str]) -> None:
    if len(found) >= 20:
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"path", "file_path", "filePath"} and isinstance(value, str) and _same_project(project_root, value):
                text = str(Path(value).expanduser().resolve())
                if text not in found:
                    found.append(text)
            else:
                _files_under(project_root, value, found)
    elif isinstance(payload, list):
        for item in payload[:40]:
            _files_under(project_root, item, found)


def _codex(project_root: Path, home: Path) -> dict[str, Any]:
    result = _blank("codex")
    root = home / ".codex" / "sessions"
    if not root.is_dir():
        result["session_in_project"] = False
        return result
    files = [path for path in root.rglob("*.jsonl") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    newest: dict[str, Any] | None = None
    for path in files[:30]:
        observed = _read_codex_file(project_root, path)
        if observed is None:
            continue
        if newest is None or (observed["last"] or datetime.min.replace(tzinfo=timezone.utc)) > (newest["last"] or datetime.min.replace(tzinfo=timezone.utc)):
            newest = observed
    result["session_in_project"] = newest is not None
    if newest is None:
        return result
    _fill(result, newest)
    return result


def _read_codex_file(project_root: Path, path: Path) -> dict[str, Any] | None:
    matched = False
    stamps: list[datetime] = []
    files: list[str] = []
    input_tokens = None
    output_tokens = None
    for index, line in enumerate(path.open(encoding="utf-8", errors="replace")):
        if index >= 4000:
            break
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = _parse_time(event.get("timestamp"))
        if stamp is not None:
            stamps.append(stamp)
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event.get("type") == "session_meta":
            cwd = payload.get("cwd")
            roots = payload.get("runtime_workspace_roots") or []
            matched = _same_project(project_root, cwd) or any(_same_project(project_root, item) for item in roots if isinstance(item, str))
        elif event.get("type") == "turn_context":
            matched = matched or _same_project(project_root, payload.get("cwd"))
        elif event.get("type") == "token_usage_record":
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            if "input_tokens" in usage:
                input_tokens = _int_or_none(usage.get("input_tokens"))
            if "output_tokens" in usage:
                output_tokens = _int_or_none(usage.get("output_tokens"))
        if matched:
            _files_under(project_root, payload, files)
    if not matched:
        return None
    last = max(stamps) if stamps else None
    return {"last": last, "stamps": stamps, "files": files, "input_tokens": input_tokens, "output_tokens": output_tokens, "ended": False}


def _claude(project_root: Path, home: Path) -> dict[str, Any]:
    result = _blank("claude")
    encoded = "-" + project_root.as_posix().lstrip("/").replace("/", "-")
    folder = home / ".claude" / "projects" / encoded
    if not folder.is_dir():
        result["session_in_project"] = False
        return result
    logs = sorted(folder.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not logs:
        result["session_in_project"] = False
        return result
    stamps: list[datetime] = []
    files: list[str] = []
    ended = False
    for index, line in enumerate(logs[0].open(encoding="utf-8", errors="replace")):
        if index >= 4000:
            break
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _same_project(project_root, event.get("cwd")) and event.get("cwd"):
            continue
        stamp = _parse_time(event.get("timestamp"))
        if stamp is not None:
            stamps.append(stamp)
        _files_under(project_root, event.get("message"), files)
        if event.get("type") == "result":
            ended = True
    result["session_in_project"] = True
    last = max(stamps) if stamps else None
    _fill(result, {"last": last, "stamps": stamps, "files": files, "input_tokens": None, "output_tokens": None, "ended": ended})
    return result


def _hermes(project_root: Path, home: Path) -> dict[str, Any]:
    result = _blank("hermes")
    database = home / ".hermes" / "state.db"
    if not database.is_file():
        result["session_in_project"] = False
        return result
    uri = f"file:{database}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        result["session_in_project"] = None
        return result
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT id, ended_at, last_activity_at, input_tokens, output_tokens,
                      actual_cost_usd, cost_status
               FROM sessions
               WHERE cwd = ? OR git_repo_root = ?
               ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1""",
            (str(project_root), str(project_root)),
        ).fetchone()
        if row is None:
            result["session_in_project"] = False
            return result
        stamps = [
            parsed
            for (value,) in connection.execute(
                "SELECT timestamp FROM messages WHERE session_id = ? AND timestamp IS NOT NULL",
                (row["id"],),
            )
            if (parsed := _parse_time(value)) is not None
        ]
    except sqlite3.Error:
        result["session_in_project"] = None
        return result
    finally:
        connection.close()
    result["session_in_project"] = True
    last = _parse_time(row["last_activity_at"]) or (max(stamps) if stamps else None)
    cost = row["actual_cost_usd"] if row["cost_status"] == "actual" else None
    _fill(result, {
        "last": last,
        "stamps": stamps,
        "files": [],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "ended": row["ended_at"] is not None,
        "cost": cost,
    })
    return result


def _fill(result: dict[str, Any], observed: dict[str, Any]) -> None:
    last: datetime | None = observed["last"]
    result["last_event_at"] = last.isoformat() if last else None
    result["active_seconds"] = active_seconds(observed["stamps"])
    result["input_tokens"] = observed["input_tokens"]
    result["output_tokens"] = observed["output_tokens"]
    result["estimated_cost_usd"] = observed.get("cost")
    result["files"] = observed["files"]
    result["finished"] = _finished(last, bool(observed["ended"]))


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


async def remember_on_latest_attempt(connection, observations: list[dict[str, Any]]) -> None:
    """Attach the observed session to the newest attempt owned by that provider."""
    for item in observations:
        if item.get("session_in_project") is not True:
            continue
        rows = await connection.execute_fetchall(
            """SELECT runtime_attempts.attempt_id
               FROM runtime_attempts
               JOIN tasks ON tasks.task_id = runtime_attempts.task_id
               WHERE tasks.owner = ?
               ORDER BY runtime_attempts.updated_at DESC LIMIT 1""",
            (item["provider"],),
        )
        if not rows:
            continue
        payload = json.dumps({
            "provider": item["provider"],
            "last_event_at": item["last_event_at"],
            "active_seconds": item["active_seconds"],
            "input_tokens": item["input_tokens"],
            "output_tokens": item["output_tokens"],
            "estimated_cost_usd": item["estimated_cost_usd"],
            "files": item["files"],
            "finished": item["finished"],
        })
        await connection.execute(
            "UPDATE runtime_attempts SET observation_json = ? WHERE attempt_id = ?",
            (payload, rows[0]["attempt_id"]),
        )
    await connection.commit()
