"""Provider-neutral usage ledger. Missing numbers stay unknown."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from agent_bus.reputation.database import Database

CONFIDENCE = frozenset({"high", "medium", "low", "unknown"})


class UsageError(Exception):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


class UsageLedger:
    def __init__(self, db: Database, project_id: str) -> None:
        self._db = db
        self._project_id = project_id

    async def record(self, body: dict[str, Any]) -> dict:
        confidence = body.get("confidence") or "unknown"
        if confidence not in CONFIDENCE:
            raise UsageError("confidence must be high, medium, low or unknown")
        source = body.get("source") or "reported"
        if not isinstance(source, str) or not source:
            raise UsageError("source is required")
        record_id = f"use_{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()
        values = (
            record_id,
            self._project_id,
            _optional_str(body.get("agent_id")),
            _optional_str(body.get("runtime")),
            _optional_str(body.get("provider")),
            _optional_str(body.get("model")),
            _optional_str(body.get("task_id")),
            _optional_str(body.get("workflow_id")),
            _optional_int(body.get("input_tokens")),
            _optional_int(body.get("cached_input_tokens")),
            _optional_int(body.get("output_tokens")),
            _optional_float(body.get("wall_seconds")),
            _optional_float(body.get("estimated_cost_usd")),
            int(body.get("retries") or 0),
            source,
            confidence,
            created_at,
        )
        await self._db.conn.execute(
            """INSERT INTO usage_records
               (record_id, project_id, agent_id, runtime, provider, model, task_id, workflow_id,
                input_tokens, cached_input_tokens, output_tokens, wall_seconds, estimated_cost_usd,
                retries, source, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
        await self._db.conn.commit()
        return await self._one(record_id)

    async def list(self, *, task_id: str | None = None, agent_id: str | None = None, workflow_id: str | None = None) -> list[dict]:
        clauses = ["project_id = ?"]
        params: list[Any] = [self._project_id]
        for column, value in (("task_id", task_id), ("agent_id", agent_id), ("workflow_id", workflow_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        rows = await self._db.conn.execute_fetchall(
            f"SELECT * FROM usage_records WHERE {' AND '.join(clauses)} ORDER BY created_at",
            tuple(params),
        )
        return [_row(row) for row in rows]

    async def summary(self, **filters: str | None) -> dict:
        records = await self.list(**filters)
        known_cost = [row["estimated_cost_usd"] for row in records if row["estimated_cost_usd"] is not None]
        known_cached = [row["cached_input_tokens"] for row in records if row["cached_input_tokens"] is not None]
        by_agent: dict[str, float] = {}
        by_workflow: dict[str, int] = {}
        for row in records:
            if row["agent_id"] and row["estimated_cost_usd"] is not None:
                by_agent[row["agent_id"]] = by_agent.get(row["agent_id"], 0.0) + row["estimated_cost_usd"]
            if row["workflow_id"]:
                by_workflow[row["workflow_id"]] = by_workflow.get(row["workflow_id"], 0) + row["retries"]
        top_agent = max(by_agent, key=by_agent.get) if by_agent else None
        top_workflow = max(by_workflow, key=by_workflow.get) if by_workflow else None
        return {
            "records": len(records),
            "estimated_cost_usd": round(sum(known_cost), 6) if known_cost else None,
            "unknown_cost_records": sum(1 for row in records if row["estimated_cost_usd"] is None),
            "cached_input_tokens": sum(known_cached) if known_cached else None,
            "unknown_cached_records": sum(1 for row in records if row["cached_input_tokens"] is None),
            "top_agent": None if top_agent is None else {"agent_id": top_agent, "estimated_cost_usd": round(by_agent[top_agent], 6)},
            "top_workflow_retries": None if top_workflow is None else {"workflow_id": top_workflow, "retries": by_workflow[top_workflow]},
        }

    async def _one(self, record_id: str) -> dict:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM usage_records WHERE record_id = ? AND project_id = ?",
            (record_id, self._project_id),
        )
        return _row(rows[0])


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _row(row) -> dict:
    return {
        "record_id": row["record_id"],
        "agent_id": row["agent_id"],
        "runtime": row["runtime"],
        "provider": row["provider"],
        "model": row["model"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "input_tokens": row["input_tokens"],
        "cached_input_tokens": row["cached_input_tokens"],
        "output_tokens": row["output_tokens"],
        "wall_seconds": row["wall_seconds"],
        "estimated_cost_usd": row["estimated_cost_usd"],
        "retries": row["retries"],
        "source": row["source"],
        "confidence": row["confidence"],
        "created_at": row["created_at"],
    }
