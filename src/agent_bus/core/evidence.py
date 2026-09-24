"""Acceptance evidence. An artifact is output; evidence says that output satisfies a criterion."""

from __future__ import annotations

import uuid

from agent_bus.core.artifacts import ArtifactError, ArtifactStore
from agent_bus.reputation.database import Database


class EvidenceError(Exception):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class EvidenceLog:
    def __init__(self, db: Database, project_id: str) -> None:
        self._db = db
        self._store = ArtifactStore(db, project_id)

    async def set_policy(self, task_id: str, *, strict: bool, require_review: bool, require_sha_match: bool) -> dict:
        await self._require_task(task_id)
        await self._db.conn.execute(
            """INSERT INTO evidence_policies (task_id, strict, require_review, require_sha_match)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 strict=excluded.strict, require_review=excluded.require_review,
                 require_sha_match=excluded.require_sha_match""",
            (task_id, int(strict), int(require_review), int(require_sha_match)),
        )
        await self._db.conn.commit()
        return await self.policy(task_id)

    async def policy(self, task_id: str) -> dict | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM evidence_policies WHERE task_id = ?", (task_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "task_id": task_id,
            "strict": bool(row["strict"]),
            "require_review": bool(row["require_review"]),
            "require_sha_match": bool(row["require_sha_match"]),
        }

    async def add(
        self, task_id: str, *, criterion: str, artifact_id: str, status: str, attempt_id: str, target_sha: str,
    ) -> dict:
        await self._require_task(task_id)
        if status not in {"passed", "failed"}:
            raise EvidenceError("status must be passed or failed", 422)
        evidence_id = f"ev_{uuid.uuid4().hex[:12]}"
        await self._db.conn.execute(
            """INSERT INTO task_evidence
               (evidence_id, task_id, criterion, artifact_id, status, attempt_id, target_sha)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (evidence_id, task_id, criterion, artifact_id, status, attempt_id, target_sha),
        )
        await self._db.conn.commit()
        return {"evidence_id": evidence_id, "criterion": criterion, "artifact_id": artifact_id, "status": status, "attempt_id": attempt_id}

    async def gap(self, task_id: str, *, candidate_sha: str, target_sha: str, verdict: str) -> str | None:
        policy = await self.policy(task_id)
        if policy is None or not policy["strict"]:
            return None
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM task_evidence WHERE task_id = ?", (task_id,),
        )
        if not rows:
            return "required evidence is missing"
        for row in rows:
            if row["status"] != "passed":
                return f"evidence for {row['criterion']} did not pass"
            try:
                artifact, _ = await self._store.read(row["artifact_id"])
            except ArtifactError as exc:
                return str(exc)
            if artifact.kind != "test-report":
                return f"evidence artifact {row['artifact_id']} is not a test report"
            if artifact.task_id != task_id:
                return "evidence artifact belongs to another task"
            if row["target_sha"] != target_sha:
                return "evidence target baseline does not match the validated baseline"
            attempt = await self._db.conn.execute_fetchall(
                "SELECT * FROM runtime_attempts WHERE attempt_id = ?", (row["attempt_id"],),
            )
            if not attempt:
                return f"runtime attempt {row['attempt_id']} is missing"
            if attempt[0]["task_id"] != task_id:
                return "runtime attempt belongs to another task"
            if attempt[0]["candidate_sha"] != candidate_sha:
                return "evidence is not bound to the candidate SHA"
            if policy["require_sha_match"] and candidate_sha != attempt[0]["candidate_sha"]:
                return "candidate SHA does not match the runtime attempt"
        if policy["require_review"] and verdict != "approve":
            return f"strict policy requires an approved verdict, got {verdict}"
        return None

    async def _require_task(self, task_id: str) -> None:
        rows = await self._db.conn.execute_fetchall("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,))
        if not rows:
            raise EvidenceError("Task not found", 404)
