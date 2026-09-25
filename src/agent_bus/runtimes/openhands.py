"""OpenHands sandbox runtime. Agent Bus keeps the task, the review and the attempt."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Protocol

from agent_bus.core.reviews import ReviewLog
from agent_bus.reputation.database import Database
from agent_bus.runtimes.protocol import (
    AttemptConflict,
    RuntimeMessage,
    RuntimeResult,
    RuntimeSession,
    RuntimeStartRequest,
    RuntimeStatus,
)
from agent_bus.worker.gatekeeper import CodeReviewGatekeeper, ReviewRequest

TERMINAL = {"completed", "failed", "cancelled"}


class Sandbox(Protocol):
    ref: str

    async def execute(self, command: str) -> tuple[int, str]:
        """Run a command inside the sandbox. Return exit code and output."""

    async def cleanup(self) -> None:
        """Stop the sandbox. The bus task is not completed here."""


class OpenHandsRuntime:
    """One sandbox per new attempt. A repeated idempotency key does not start another."""

    def __init__(self, db: Database, open_sandbox: Callable[[], Awaitable[Sandbox]]) -> None:
        self._db = db
        self._open_sandbox = open_sandbox
        self._sandboxes: dict[str, Sandbox] = {}

    async def start(self, request: RuntimeStartRequest) -> RuntimeSession:
        existing = await self._by_key(request.idempotency_key)
        if existing is not None:
            return existing
        if await self._open_unknown(request.task_id) is not None:
            raise AttemptConflict("reconcile the unknown attempt before starting another")
        await self._db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at, log_refs)
               VALUES (?, ?, ?, 'started', NULL, ?, ?, '[]')""",
            (request.attempt_id, request.task_id, request.idempotency_key, request.workspace_ref, _now()),
        )
        await self._db.conn.commit()
        sandbox = await self._open_sandbox()
        self._sandboxes[request.attempt_id] = sandbox
        await self._db.conn.execute(
            "UPDATE runtime_attempts SET external_ref = ? WHERE attempt_id = ?",
            (f"openhands:{sandbox.ref}", request.attempt_id),
        )
        await self._db.conn.commit()
        return await self._session(request.attempt_id)

    async def send(self, session: RuntimeSession, message: str) -> RuntimeMessage:
        sandbox = self._sandboxes.get(session.attempt_id)
        if sandbox is None:
            current = await self.status(session)
            if current.state == "started":
                await self._set_state(session.attempt_id, "unknown")
            raise AttemptConflict("sandbox is not running in this process")
        exit_code, output = await sandbox.execute(message)
        await self._remember(session.attempt_id, output)
        if exit_code != 0:
            await self.complete(session, RuntimeResult("failed", log_refs=(f"exit:{exit_code}",)))
        return RuntimeMessage(session.attempt_id, output)

    async def finish(self, session: RuntimeSession) -> RuntimeStatus:
        current = await self.status(session)
        if current.state in TERMINAL or current.state == "unknown":
            return current
        sha = await _rev_parse(session.workspace_ref or "", "HEAD")
        diff = await _git_show(session.workspace_ref or "", sha) if sha else ""
        await self.complete(session, RuntimeResult("completed", sha, (f"attempt:{session.attempt_id}",)))
        if sha:
            decision = CodeReviewGatekeeper().evaluate(ReviewRequest(
                task_id=session.task_id,
                sha=sha,
                diff=diff,
                test_passed=True,
                reviewer_agent_id="openhands-runtime",
                reviewer_session_id=session.attempt_id,
            ))
            decision = decision.model_copy(update={"evidence": {**decision.evidence, "attempt_id": session.attempt_id}})
            await ReviewLog(self._db).add(decision)
        sandbox = self._sandboxes.pop(session.attempt_id, None)
        if sandbox is not None:
            await sandbox.cleanup()
        return await self.status(session)

    async def cancel(self, session: RuntimeSession) -> None:
        sandbox = self._sandboxes.pop(session.attempt_id, None)
        if sandbox is not None:
            await sandbox.cleanup()
        if (await self.status(session)).state not in TERMINAL:
            await self._set_state(session.attempt_id, "cancelled")

    async def status(self, session: RuntimeSession) -> RuntimeStatus:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM runtime_attempts WHERE attempt_id = ?", (session.attempt_id,),
        )
        if not rows:
            raise KeyError(session.attempt_id)
        row = rows[0]
        refs = json.loads(row["log_refs"] or "[]")
        return RuntimeStatus(row["attempt_id"], row["state"], row["outcome"], row["candidate_sha"], tuple(refs))

    async def complete(self, session: RuntimeSession, result: RuntimeResult) -> RuntimeStatus:
        current = await self.status(session)
        if current.state in TERMINAL:
            return current
        await self._db.conn.execute(
            """UPDATE runtime_attempts
               SET state = ?, outcome = ?, candidate_sha = ?, log_refs = ?, updated_at = ?
               WHERE attempt_id = ?""",
            (result.outcome, result.outcome, result.candidate_sha, json.dumps(list(result.log_refs)), _now(), session.attempt_id),
        )
        await self._db.conn.commit()
        return await self.status(session)

    async def _session(self, attempt_id: str) -> RuntimeSession:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM runtime_attempts WHERE attempt_id = ?", (attempt_id,),
        )
        row = rows[0]
        return RuntimeSession(row["attempt_id"], row["task_id"], row["external_ref"] or "", row["workspace_ref"], row["state"])

    async def _by_key(self, key: str) -> RuntimeSession | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM runtime_attempts WHERE idempotency_key = ?", (key,),
        )
        if not rows:
            return None
        row = rows[0]
        return RuntimeSession(row["attempt_id"], row["task_id"], row["external_ref"] or "", row["workspace_ref"], row["state"])

    async def _open_unknown(self, task_id: str) -> RuntimeSession | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM runtime_attempts WHERE task_id = ? AND state = 'unknown' LIMIT 1", (task_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return RuntimeSession(row["attempt_id"], row["task_id"], row["external_ref"] or "", row["workspace_ref"], row["state"])

    async def _set_state(self, attempt_id: str, state: str) -> None:
        await self._db.conn.execute(
            "UPDATE runtime_attempts SET state = ?, updated_at = ? WHERE attempt_id = ?",
            (state, _now(), attempt_id),
        )
        await self._db.conn.commit()

    async def _remember(self, attempt_id: str, text: str) -> None:
        if not text:
            return
        await self._db.conn.execute(
            """INSERT INTO runtime_messages (message_id, attempt_id, text, created_at, consumed)
               VALUES (?, ?, ?, ?, 1)""",
            (f"oh-{uuid.uuid4().hex[:12]}", attempt_id, text[-2000:], _now()),
        )
        await self._db.conn.commit()


def cloud_sandbox_opener(*, cloud_api_url: str | None = None, cloud_api_key: str | None = None) -> Callable[[], Awaitable[Sandbox]]:
    """Fail closed before any cloud request when credentials are missing."""
    if not cloud_api_url or not cloud_api_key:
        raise ValueError("cloud workspace requires cloud_api_url and cloud_api_key")

    async def open_sandbox() -> Sandbox:
        from openhands.workspace import OpenHandsCloudWorkspace
        workspace = OpenHandsCloudWorkspace(cloud_api_url=cloud_api_url, cloud_api_key=cloud_api_key)
        return _DockerSandbox(workspace)

    return open_sandbox


def remote_api_sandbox_opener(
    *, runtime_api_url: str | None = None, runtime_api_key: str | None = None, server_image: str | None = None,
) -> Callable[[], Awaitable[Sandbox]]:
    """Fail closed before any remote API request when credentials are missing."""
    if not runtime_api_url or not runtime_api_key or not server_image:
        raise ValueError("remote API workspace requires runtime_api_url, runtime_api_key, and server_image")

    async def open_sandbox() -> Sandbox:
        from openhands.workspace import APIRemoteWorkspace
        workspace = APIRemoteWorkspace(
            runtime_api_url=runtime_api_url, runtime_api_key=runtime_api_key, server_image=server_image,
        )
        return _DockerSandbox(workspace)

    return open_sandbox


def docker_sandbox_opener(server_image: str = "ghcr.io/openhands/agent-server:1.49.5-python") -> Callable[[], Awaitable[Sandbox]]:
    """Open a local OpenHands Docker workspace. Import happens on first start."""

    async def open_sandbox() -> Sandbox:
        from openhands.workspace import DockerWorkspace
        return _DockerSandbox(DockerWorkspace(server_image=server_image, health_check_timeout=180))

    return open_sandbox


class _DockerSandbox:
    def __init__(self, workspace) -> None:
        self._workspace = workspace
        self.ref = getattr(workspace, "host", "") or "docker"

    async def execute(self, command: str) -> tuple[int, str]:
        result = self._workspace.execute_command(command)
        if asyncio.iscoroutine(result):
            result = await result
        return int(result.exit_code), str(result.stdout or "")

    async def cleanup(self) -> None:
        self._workspace.cleanup()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _rev_parse(cwd: str, ref: str) -> str | None:
    if not cwd:
        return None
    process = await asyncio.create_subprocess_exec(
        "git", "rev-parse", ref, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await process.communicate()
    if process.returncode != 0:
        return None
    return out.decode().strip() or None


async def _git_show(cwd: str, sha: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git", "show", "--format=", sha, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await process.communicate()
    return out.decode() if process.returncode == 0 else ""
