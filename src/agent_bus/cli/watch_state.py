"""Private durable watcher state; no credentials or provider reasoning are stored."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from agent_bus.worker.execution import ExecutionGuard


def write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".watch-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reply_file(sessions_file: Path, message_id: str) -> Path:
    key = hashlib.sha256(message_id.encode()).hexdigest()
    return sessions_file.parent / "outbox" / f"{key}.json"


def record_status(path: Path, cli: str, state: str) -> None:
    write_private_json(path, {"pid": os.getpid(), "cli": cli, "state": state,
                              "updated_at": time.time()})


def watcher_status(agent: str, path: Path) -> dict:
    owner = ExecutionGuard(agent, kind="watcher").inspect()
    result = {"agent_id": agent, **owner, "state": "stopped", "can_dispatch": False}
    if not owner["active"]:
        return result
    if owner.get("kind") != "watcher":
        return {**result, "state": "other_executor"}
    try:
        saved = json.loads(path.read_text())
        age = time.time() - saved["updated_at"]
        fresh = 0 <= age <= (620 if saved["state"] == "running" else 15)
        if saved["pid"] != owner.get("pid") or not fresh:
            raise ValueError("Stale status")
    except (OSError, ValueError, KeyError, TypeError):
        return {**result, "state": "unknown"}
    return {**result, "state": saved["state"], "cli": saved["cli"],
            "updated_at": saved["updated_at"], "can_dispatch": saved["state"] == "waiting"}
