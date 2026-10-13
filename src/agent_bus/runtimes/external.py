"""External command adapter. The caller owns the workspace; this process only runs in it."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from datetime import datetime, timezone

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


class ExternalCommandRuntime:
    """Run one argv in a workspace the caller created and will delete.

    The child environment drops AGENT_BUS variables. The command cannot mark the
    bus task done. A successful run exports HEAD as the candidate SHA and records
    a review whose evidence names this attempt.
    """

    def __init__(self, db: Database, command: list[str]) -> None:
        self._db = db
        self._command = list(command)
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def start(self, request: RuntimeStartRequest) -> RuntimeSession:
        existing = await self._by_key(request.idempotency_key)
        if existing is not None:
            return existing
        if await self._open_unknown(request.task_id) is not None:
            raise AttemptConflict("reconcile the unknown attempt before starting another")
        now = _now()
        await self._db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at, log_refs)
               VALUES (?, ?, ?, 'started', NULL, ?, ?, '[]')""",
            (request.attempt_id, request.task_id, request.idempotency_key, request.workspace_ref, now),
        )
        await self._db.conn.commit()
        if not request.workspace_ref:
            raise ValueError("workspace_ref is required")
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_BUS")}
        process = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=request.workspace_ref,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._processes[request.attempt_id] = process
        await self._db.conn.execute(
            "UPDATE runtime_attempts SET external_ref = ? WHERE attempt_id = ?",
            (f"external:{process.pid}", request.attempt_id),
        )
        await self._db.conn.commit()
        return await self._session(request.attempt_id)

    async def wait(self, session: RuntimeSession, timeout: float) -> RuntimeStatus:
        process = self._processes.get(session.attempt_id)
        if process is None:
            current = await self.status(session)
            if current.state == "started":
                await self._set_state(session.attempt_id, "unknown")
            return await self.status(session)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            await self._kill(process)
            await self._set_state(session.attempt_id, "unknown")
            return await self.status(session)
        output = (stdout or b"").decode()[-2000:]
        await self._remember_output(session.attempt_id, output)
        if process.returncode != 0:
            await self.complete(session, RuntimeResult("failed", log_refs=(f"stderr:{len(stderr or b'')}",)))
            return await self.status(session)
        sha = await self._head(session.workspace_ref or "")
        diff = await self._diff(session.workspace_ref or "", sha)
        await self.complete(session, RuntimeResult("completed", sha, (f"attempt:{session.attempt_id}",)))
        if sha:
            decision = CodeReviewGatekeeper().evaluate(ReviewRequest(
                task_id=session.task_id,
                sha=sha,
                diff=diff,
                test_passed=True,
                reviewer_agent_id="external-command",
                reviewer_session_id=session.attempt_id,
            ))
            decision = decision.model_copy(update={
                "evidence": {**decision.evidence, "attempt_id": session.attempt_id},
            })
            await ReviewLog(self._db).add(decision)
        return await self.status(session)

    async def send(self, session: RuntimeSession, message: str) -> RuntimeMessage:
        await self._remember_output(session.attempt_id, message)
        return RuntimeMessage(session.attempt_id, message)

    async def cancel(self, session: RuntimeSession) -> None:
        process = self._processes.get(session.attempt_id)
        if process is not None and process.returncode is None:
            await self._kill(process)
        if (await self.status(session)).state not in TERMINAL:
            await self._set_state(session.attempt_id, "cancelled")

    async def status(self, session: RuntimeSession) -> RuntimeStatus:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM runtime_attempts WHERE attempt_id = ?", (session.attempt_id,),
        )
        if not rows:
            raise KeyError(session.attempt_id)
        row = rows[0]
        raw = row["log_refs"] or "[]"
        refs = json.loads(raw) if isinstance(raw, str) else raw
        return RuntimeStatus(row["attempt_id"], row["state"], row["outcome"], row["candidate_sha"], tuple(refs))

    async def complete(self, session: RuntimeSession, result: RuntimeResult) -> RuntimeStatus:
        if result.outcome not in TERMINAL:
            raise ValueError(f"outcome must be one of {sorted(TERMINAL)}")
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

    async def reconcile(self, attempt_id: str, outcome: str) -> RuntimeStatus:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM runtime_attempts WHERE attempt_id = ?", (attempt_id,),
        )
        if not rows:
            raise KeyError(attempt_id)
        session = RuntimeSession(attempt_id, rows[0]["task_id"], rows[0]["external_ref"] or "", rows[0]["workspace_ref"], rows[0]["state"])
        if session.state != "unknown":
            return await self.status(session)
        if outcome not in TERMINAL:
            raise ValueError(f"outcome must be one of {sorted(TERMINAL)}")
        return await self.complete(session, RuntimeResult(outcome))

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
            "SELECT * FROM runtime_attempts WHERE task_id = ? AND state = 'unknown' LIMIT 1",
            (task_id,),
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

    async def _remember_output(self, attempt_id: str, text: str) -> None:
        if not text:
            return
        await self._db.conn.execute(
            """INSERT INTO runtime_messages (message_id, attempt_id, text, created_at, consumed)
               VALUES (?, ?, ?, ?, 1)""",
            (f"out-{uuid.uuid4().hex[:12]}", attempt_id, text[-2000:], _now()),
        )
        await self._db.conn.commit()

    async def _kill(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    async def _head(self, cwd: str) -> str | None:
        process = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await process.communicate()
        if process.returncode != 0:
            return None
        return out.decode().strip() or None

    async def _diff(self, cwd: str, sha: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", "show", "--format=", sha, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await process.communicate()
        return out.decode() if process.returncode == 0 else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
