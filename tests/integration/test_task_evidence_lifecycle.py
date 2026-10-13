"""Integration tests for task lifecycle with review, evidence persistence, audit log, and endpoints."""
from __future__ import annotations

import sqlite3
from click.testing import CliRunner
import pytest

from agent_bus.cli.main import app
from agent_bus.security import async_bus_client


@pytest.mark.asyncio
async def test_task_lifecycle_review_evidence_and_audit(secure_bus, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "alice")
    monkeypatch.setenv("AGENT_BUS_URL", secure_bus.url)

    async with (
        async_bus_client(agent_id="human", base_url=secure_bus.url) as admin,
        async_bus_client(agent_id="alice", base_url=secure_bus.url) as alice,
        async_bus_client(agent_id="bob", base_url=secure_bus.url) as bob,
    ):
        # 1. Admin creates task T-EV1
        resp = await admin.post("/tasks", json={
            "task_id": "T-EV1",
            "title": "Implement feature X",
            "description": "Must have tests and evidence",
        })
        assert resp.status_code == 200
        task_data = resp.json()
        assert task_data["status"] == "pending"
        assert task_data["owner"] == "free"

        # 2. Negative checks: Non-owner cannot review or complete
        resp_rev_unowned = await alice.post("/tasks/T-EV1/review")
        assert resp_rev_unowned.status_code in (400, 403)

        resp_done_unowned = await alice.post("/tasks/T-EV1/done", json={"evidence": "early done"})
        assert resp_done_unowned.status_code in (403, 409)

        # 3. Alice claims the task
        resp_claim = await alice.post("/tasks/T-EV1/claim", json={"agent_id": "alice"})
        assert resp_claim.status_code == 200
        assert resp_claim.json()["owner"] == "alice"
        assert resp_claim.json()["status"] == "in_progress"

        # Bob cannot claim, review, or complete Alice's task
        assert (await bob.post("/tasks/T-EV1/claim", json={"agent_id": "bob"})).status_code == 409
        assert (await bob.post("/tasks/T-EV1/review")).status_code == 403
        assert (await bob.post("/tasks/T-EV1/done", json={"evidence": "stolen"})).status_code in (403, 409)

        # 4. Alice submits task for review
        resp_rev = await alice.post("/tasks/T-EV1/review")
        assert resp_rev.status_code == 200
        assert resp_rev.json()["status"] == "in_review"

        # Verify task is in_review via GET
        resp_get = await alice.get("/tasks/T-EV1")
        assert resp_get.status_code == 200
        assert resp_get.json()["status"] == "in_review"

        # 5. Alice completes task with structured evidence
        evidence_payload = {
            "summary": "Implemented and tested",
            "test_count": 42,
            "git_commit": "abcdef123456",
        }
        resp_done = await alice.post("/tasks/T-EV1/done", json={"evidence": evidence_payload})
        assert resp_done.status_code == 200
        assert resp_done.json()["status"] == "done"

        # 6. Verify evidence endpoint GET /tasks/T-EV1/evidence
        resp_ev = await alice.get("/tasks/T-EV1/evidence")
        assert resp_ev.status_code == 200
        ev_data = resp_ev.json()
        assert ev_data["task_id"] == "T-EV1"
        assert len(ev_data["evidence"]) >= 1
        latest_ev = ev_data["evidence"][-1]
        assert latest_ev["action"] in ("complete_with_evidence", "task_complete")
        assert latest_ev["actor_agent_id"] == "alice"
        assert latest_ev["previous_owner"] == "alice"
        assert latest_ev["new_owner"] == "done"
        assert latest_ev["evidence"] == evidence_payload
        assert latest_ev["created_at"] is not None

        # Verify audit_log table directly in SQLite database
        conn = sqlite3.connect(secure_bus.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT task_id, action, actor_agent_id, actor_session_id, previous_owner, new_owner, evidence "
                "FROM audit_log WHERE task_id = 'T-EV1' AND action IN ('complete_with_evidence', 'task_complete')"
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "T-EV1"
            assert row[2] == "alice"
            # actor_session_id should NOT be the evidence string
            assert row[3] != str(evidence_payload)
            assert row[4] == "alice"
            assert row[5] == "done"
            # evidence column holds the json string
            assert "abcdef123456" in row[6]
        finally:
            conn.close()

    # 7. CLI command: agent-bus show task T-EV1 displays task and evidence
    runner = CliRunner()
    result = runner.invoke(app, ["show", "task", "T-EV1"])
    assert result.exit_code == 0, result.output
    assert "T-EV1" in result.output
    assert "done" in result.output
    assert "Evidencia Registrada" in result.output
    assert "abcdef123456" in result.output
