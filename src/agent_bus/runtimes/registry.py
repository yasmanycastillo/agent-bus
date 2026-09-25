"""Bind a routed agent to the runtime that may execute its task."""

from __future__ import annotations

import json
import shlex
import time
import uuid
from typing import Any, Callable

from agent_bus.core.usage import UsageLedger
from agent_bus.reputation.database import Database
from agent_bus.runtimes.external import ExternalCommandRuntime
from agent_bus.runtimes.native import NativeRuntime
from agent_bus.runtimes.protocol import RuntimeStartRequest

RUNTIMES = frozenset({"native", "external", "sandbox"})


def expand_provider_command(command: list[str], prompt: str) -> list[str]:
    """Give a bare Claude or Codex command the task, so it exits instead of waiting for a keyboard."""
    if not command or not prompt:
        return list(command)
    program = command[0]
    if program == "claude" and "-p" not in command:
        return ["claude", "-p", prompt, "--output-format", "json"]
    if program == "codex" and "exec" not in command:
        return ["codex", "exec", "--json", prompt]
    return list(command)


class RegistryError(Exception):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


class RuntimeRegistry:
    def __init__(self, db: Database, project_id: str, sandbox_opener: Callable | None = None) -> None:
        self._db = db
        self._project_id = project_id
        self._sandbox_opener = sandbox_opener

    async def _task_prompt(self, task_id: str) -> str:
        rows = await self._db.conn.execute_fetchall(
            "SELECT title, description FROM tasks WHERE task_id = ?", (task_id,),
        )
        if not rows:
            return ""
        title = rows[0]["title"] or ""
        description = rows[0]["description"] or ""
        return f"{title}\n\n{description}".strip()

    async def register(self, agent_id: str, runtime: str, command: list[str] | None = None) -> dict:
        if runtime not in RUNTIMES:
            raise RegistryError("runtime must be native, external, or sandbox")
        if runtime == "external" and not command:
            raise RegistryError("an external runtime needs a command")
        if runtime == "sandbox" and not command:
            raise RegistryError("a sandbox runtime needs a command")
        await self._db.conn.execute(
            """INSERT INTO agent_runtimes (agent_id, project_id, runtime, command)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 project_id=excluded.project_id, runtime=excluded.runtime, command=excluded.command""",
            (agent_id, self._project_id, runtime, json.dumps(command or [])),
        )
        await self._db.conn.commit()
        return await self.get(agent_id)

    async def get(self, agent_id: str) -> dict | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM agent_runtimes WHERE agent_id = ? AND project_id = ?",
            (agent_id, self._project_id),
        )
        if not rows:
            return None
        row = rows[0]
        return {"agent_id": agent_id, "runtime": row["runtime"], "command": json.loads(row["command"] or "[]")}

    async def launch(self, task_id: str, agent_id: str, runtime: str, command: list[str], workspace_ref: str | None, timeout: float) -> dict[str, Any]:
        request = RuntimeStartRequest(
            f"att-{uuid.uuid4().hex[:12]}", task_id, f"dispatch:{task_id}", agent_id, workspace_ref,
        )
        started = time.monotonic()
        if runtime == "native":
            session = await NativeRuntime(self._db).start(request)
            report_state, sha = session.state, None
        elif runtime == "external":
            if not workspace_ref:
                raise RegistryError("an external runtime needs workspace_ref")
            command = expand_provider_command(command, await self._task_prompt(task_id))
            adapter = ExternalCommandRuntime(self._db, command)
            session = await adapter.start(request)
            if session.state == "started":
                report = await adapter.wait(session, timeout)
                report_state, sha = report.state, report.candidate_sha
            else:
                report_state, sha = session.state, None
        elif runtime == "sandbox":
            if self._sandbox_opener is None:
                raise RegistryError("a sandbox runtime needs an injected opener")
            if not workspace_ref:
                raise RegistryError("a sandbox runtime needs workspace_ref")
            from agent_bus.runtimes.openhands import OpenHandsRuntime
            adapter = OpenHandsRuntime(self._db, self._sandbox_opener)
            session = await adapter.start(request)
            if session.state == "started" and command:
                await adapter.send(session, shlex.join(command))
            report = await adapter.finish(session) if session.state == "started" or command else None
            if report is None:
                report_state, sha = session.state, None
            else:
                report_state, sha = report.state, report.candidate_sha
        else:
            raise RegistryError(f"unsupported runtime {runtime}")
        wall = round(time.monotonic() - started, 3)
        await UsageLedger(self._db, self._project_id).record({
            "agent_id": agent_id,
            "runtime": runtime,
            "task_id": task_id,
            "wall_seconds": wall,
            "source": "runtime",
            "confidence": "low",
        })
        return {
            "attempt_id": session.attempt_id,
            "agent_id": agent_id,
            "runtime": runtime,
            "state": report_state,
            "candidate_sha": sha,
            "external_ref": session.external_ref,
        }
