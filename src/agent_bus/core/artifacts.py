"""Project-scoped artifacts. The filesystem path is not the identifier."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_bus.reputation.database import Database

KINDS = frozenset({
    "repository-analysis",
    "patch",
    "diff",
    "test-report",
    "architecture",
    "structured-data",
    "log",
})
MAX_BYTES = 256 * 1024


class ArtifactError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    project_id: str
    task_id: str
    producer: str
    kind: str
    media_type: str
    sha256: str
    size_bytes: int
    retention: str
    summary: str
    attempt_id: str | None
    created_at: str

    @property
    def uri(self) -> str:
        return f"artifact://{self.project_id}/{self.artifact_id}"

    def metadata(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "producer": self.producer,
            "kind": self.kind,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "retention": self.retention,
            "summary": self.summary,
            "attempt_id": self.attempt_id,
            "created_at": self.created_at,
        }


class ArtifactStore:
    def __init__(self, db: Database, project_id: str) -> None:
        self._db = db
        self._project_id = project_id
        self._root = Path(db.db_path).resolve().parent / "artifacts" / project_id

    async def publish(
        self,
        *,
        task_id: str,
        producer: str,
        kind: str,
        media_type: str,
        content: bytes,
        summary: str = "",
        attempt_id: str | None = None,
        retention: str = "until-task-deleted",
    ) -> Artifact:
        if kind not in KINDS:
            raise ArtifactError(f"unsupported artifact kind: {kind}", 422)
        if not media_type or "/" not in media_type:
            raise ArtifactError("media_type is required", 422)
        if not content:
            raise ArtifactError("artifact content is empty", 422)
        if len(content) > MAX_BYTES:
            raise ArtifactError("artifact exceeds the 256 KiB inline limit", 413)
        task = await self._db.conn.execute_fetchall("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,))
        if not task:
            raise ArtifactError("Task not found", 404)
        artifact_id = f"art_{uuid.uuid4().hex[:16]}"
        digest = hashlib.sha256(content).hexdigest()
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / artifact_id
        temporary = target.with_suffix(".partial")
        temporary.write_bytes(content)
        temporary.replace(target)
        created_at = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """INSERT INTO artifacts
               (artifact_id, project_id, task_id, producer, kind, media_type, sha256,
                size_bytes, retention, summary, attempt_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id, self._project_id, task_id, producer, kind, media_type, digest,
                len(content), retention, summary, attempt_id, created_at,
            ),
        )
        await self._db.conn.commit()
        return await self.get(artifact_id)

    async def list_for_task(self, task_id: str) -> list[Artifact]:
        rows = await self._db.conn.execute_fetchall(
            """SELECT * FROM artifacts WHERE project_id = ? AND task_id = ? ORDER BY created_at""",
            (self._project_id, task_id),
        )
        return [_row(row) for row in rows]

    async def get(self, artifact_id: str) -> Artifact:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM artifacts WHERE artifact_id = ? AND project_id = ?",
            (artifact_id, self._project_id),
        )
        if not rows:
            raise ArtifactError("Artifact not found", 404)
        return _row(rows[0])

    async def read(self, artifact_id: str) -> tuple[Artifact, bytes]:
        artifact = await self.get(artifact_id)
        path = self._root / artifact.artifact_id
        if not path.is_file():
            raise ArtifactError("Artifact payload is missing", 404)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ArtifactError("Artifact checksum does not match", 409)
        return artifact, content


def _row(row) -> Artifact:
    return Artifact(
        row["artifact_id"], row["project_id"], row["task_id"], row["producer"], row["kind"],
        row["media_type"], row["sha256"], row["size_bytes"], row["retention"], row["summary"] or "",
        row["attempt_id"], row["created_at"],
    )


def content_from_json(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
