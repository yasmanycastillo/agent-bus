from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from agent_bus.reputation.database import Database
from agent_bus.worker.gatekeeper import ReviewDecision, Verdict


class ReviewLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, review: ReviewDecision) -> ReviewDecision:
        evidence_json = json.dumps(review.evidence)
        test_results_json = json.dumps(review.test_results)
        created_at_iso = (
            review.created_at.isoformat()
            if isinstance(review.created_at, datetime)
            else str(review.created_at)
        )
        verdict_str = (
            review.verdict.value
            if isinstance(review.verdict, Verdict)
            else str(review.verdict)
        )

        def transaction(conn):
            conn.execute("SAVEPOINT review_add")
            try:
                conn.execute(
                    """INSERT INTO reviews
                    (review_id, task_id, reviewer_agent_id, reviewer_session_id,
                     sha, verdict, reason, evidence, test_results, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        review.review_id,
                        review.task_id,
                        review.reviewer_agent_id,
                        review.reviewer_session_id,
                        review.sha,
                        verdict_str,
                        review.reason,
                        evidence_json,
                        test_results_json,
                        created_at_iso,
                    ),
                )
                conn.execute(
                    """INSERT INTO audit_log
                    (action, task_id, actor_agent_id, actor_session_id, previous_owner, new_owner, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "gatekeeper_review",
                        review.task_id,
                        review.reviewer_agent_id,
                        review.reviewer_session_id,
                        None,
                        None,
                        created_at_iso,
                    ),
                )
                conn.execute("RELEASE review_add")
            except BaseException:
                conn.execute("ROLLBACK TO review_add")
                raise

        await self._db.conn._execute(transaction, self._db.conn._conn)
        await self._db.conn.commit()
        return review

    async def get(self, review_id: str) -> ReviewDecision | None:
        rows = await self._db.conn.execute_fetchall(
            "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
        )
        if not rows:
            return None
        return self._row_to_review(rows[0])

    async def list_all(self, task_id: str | None = None) -> list[ReviewDecision]:
        if task_id:
            rows = await self._db.conn.execute_fetchall(
                "SELECT * FROM reviews WHERE task_id = ? ORDER BY created_at DESC",
                (task_id,),
            )
        else:
            rows = await self._db.conn.execute_fetchall(
                "SELECT * FROM reviews ORDER BY created_at DESC"
            )
        return [self._row_to_review(row) for row in rows]

    async def list_for_task(self, task_id: str) -> list[ReviewDecision]:
        return await self.list_all(task_id=task_id)

    @staticmethod
    def _row_to_review(row: Any) -> ReviewDecision:
        created_at_raw = row["created_at"]
        if isinstance(created_at_raw, str):
            try:
                created_at = datetime.fromisoformat(created_at_raw)
            except ValueError:
                created_at = datetime.now(timezone.utc)
        elif isinstance(created_at_raw, datetime):
            created_at = created_at_raw
        else:
            created_at = datetime.now(timezone.utc)

        evidence = json.loads(row["evidence"]) if row["evidence"] else {}
        test_results = json.loads(row["test_results"]) if row["test_results"] else {}
        return ReviewDecision(
            review_id=row["review_id"],
            task_id=row["task_id"],
            reviewer_agent_id=row["reviewer_agent_id"],
            reviewer_session_id=row["reviewer_session_id"],
            sha=row["sha"],
            verdict=Verdict(row["verdict"]),
            reason=row["reason"],
            evidence=evidence,
            test_results=test_results,
            created_at=created_at,
        )
