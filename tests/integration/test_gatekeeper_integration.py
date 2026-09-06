from __future__ import annotations

import pytest
import aiosqlite
from click.testing import CliRunner

from agent_bus.cli.main import app
from agent_bus.security import async_bus_client
from agent_bus.worker.integrator import BranchIntegrator


SAMPLE_CLEAN_DIFF = """diff --git a/src/feature.py b/src/feature.py
--- a/src/feature.py
+++ b/src/feature.py
@@ -1,2 +1,4 @@
+def implemented_logic():
+    return 42
"""

SAMPLE_SECRET_DIFF = """diff --git a/secrets.py b/secrets.py
--- a/secrets.py
+++ b/secrets.py
@@ -1,1 +1,2 @@
+aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
"""


@pytest.mark.asyncio
async def test_reviews_endpoints_and_audit(live_bus_url, tmp_path):
    async with async_bus_client("test-agent", base_url=live_bus_url) as client:
        # Create a task first
        t_resp = await client.post("/tasks", json={"task_id": "T-REV-1", "title": "Review test task"})
        assert t_resp.status_code == 200

        # Post a review
        rev_payload = {
            "task_id": "T-REV-1",
            "sha": "1234567890abcdef",
            "verdict": "approve",
            "reason": "Clean code and tests pass",
            "evidence": {"diff_stats": {"files_changed": 1, "insertions": 2, "total_lines": 2}},
            "test_results": {"passed": True, "output": "3 passed"},
            "reviewer_agent_id": "gatekeeper-tester",
            "reviewer_session_id": "sess-test-123",
        }
        r_resp = await client.post("/reviews", json=rev_payload)
        assert r_resp.status_code == 200
        review_data = r_resp.json()
        assert review_data["verdict"] == "approve"
        assert review_data["sha"] == "1234567890abcdef"
        assert review_data["reviewer_agent_id"] == "gatekeeper-tester"
        assert review_data["reviewer_session_id"] == "sess-test-123"

        # List all reviews
        list_resp = await client.get("/reviews")
        assert list_resp.status_code == 200
        assert len(list_resp.json()) >= 1

        # Filter reviews by task_id
        filtered_resp = await client.get("/reviews", params={"task_id": "T-REV-1"})
        assert filtered_resp.status_code == 200
        assert len(filtered_resp.json()) == 1
        assert filtered_resp.json()[0]["task_id"] == "T-REV-1"

        # Task specific review endpoint
        task_revs_resp = await client.get("/tasks/T-REV-1/reviews")
        assert task_revs_resp.status_code == 200
        assert len(task_revs_resp.json()) == 1

    # Verify directly in SQLite DB that both reviews and audit_log tables are populated
    db_file = tmp_path / "live-bus.db"
    async with aiosqlite.connect(db_file) as db:
        db.row_factory = aiosqlite.Row
        # Check reviews table
        rev_rows = await db.execute_fetchall(
            "SELECT * FROM reviews WHERE task_id = ?", ("T-REV-1",)
        )
        assert len(rev_rows) == 1
        assert rev_rows[0]["verdict"] == "approve"
        assert rev_rows[0]["reviewer_agent_id"] == "gatekeeper-tester"
        assert rev_rows[0]["reviewer_session_id"] == "sess-test-123"

        # Check audit_log table
        audit_rows = await db.execute_fetchall(
            "SELECT * FROM audit_log WHERE action = 'gatekeeper_review' AND task_id = ?",
            ("T-REV-1",),
        )
        assert len(audit_rows) == 1
        assert audit_rows[0]["actor_agent_id"] == "gatekeeper-tester"
        assert audit_rows[0]["actor_session_id"] == "sess-test-123"


@pytest.mark.asyncio
async def test_branch_integrator_require_approval_approve(live_bus_url, tmp_path):
    async with async_bus_client("integrator", base_url=live_bus_url) as client:
        # 1. Create task
        await client.post(
            "/tasks",
            json={"task_id": "T-APP-1", "title": "Implement feature", "owner": "alice"},
        )

    integrator = BranchIntegrator(
        repo_dir=tmp_path,
        bus_url=live_bus_url,
        require_approval=True,
    )

    # Mock candidate worktree execution
    async def mock_pass_tests(worktree_dir, test_cmd=None):
        return (True, "10 passed in 0.5s")

    async def mock_git_output(*args, cwd=None):
        if "rev-parse" in args:
            return "fedcba9876543210"
        if "diff" in args:
            return SAMPLE_CLEAN_DIFF
        return ""

    async def mock_merge(candidate_branch, target_branch):
        return (True, "Fast-forward merge successful")

    integrator.run_tests = mock_pass_tests
    integrator._git_output = mock_git_output  # type: ignore
    integrator._merge_branches = mock_merge  # type: ignore

    res = await integrator.integrate_task(
        task_id="T-APP-1",
        author_agent="alice",
        worktree_dir=tmp_path,
        candidate_branch="agent/alice",
        target_branch="main",
    )

    # Verifications
    assert res.success is True
    assert res.merged is True
    assert res.status == "integrated"
    assert res.metadata["review"]["verdict"] == "approve"

    # Task should be marked done
    async with async_bus_client("integrator", base_url=live_bus_url) as client:
        t_resp = await client.get("/tasks/T-APP-1")
        assert t_resp.json()["status"] == "done"

        # Check durable review
        rev_resp = await client.get("/tasks/T-APP-1/reviews")
        revs = rev_resp.json()
        assert len(revs) == 1
        assert revs[0]["verdict"] == "approve"
        assert revs[0]["sha"] == "fedcba9876543210"


@pytest.mark.asyncio
async def test_branch_integrator_require_approval_changes_requested(live_bus_url, tmp_path):
    async with async_bus_client("integrator", base_url=live_bus_url) as client:
        # Create task
        await client.post(
            "/tasks",
            json={"task_id": "T-REQ-1", "title": "Bugfix", "owner": "bob"},
        )

    integrator = BranchIntegrator(
        repo_dir=tmp_path,
        bus_url=live_bus_url,
        require_approval=True,
    )

    # Failing tests
    async def mock_fail_tests(worktree_dir, test_cmd=None):
        return (False, "FAILED tests/test_calc.py::test_add - AssertionError: 4 != 5")

    async def mock_git_output(*args, cwd=None):
        if "rev-parse" in args:
            return "sha-bob-fail-123"
        if "diff" in args:
            return SAMPLE_CLEAN_DIFF
        return ""

    merge_called = False

    async def mock_merge(candidate_branch, target_branch):
        nonlocal merge_called
        merge_called = True
        return (True, "Merged")

    integrator.run_tests = mock_fail_tests
    integrator._git_output = mock_git_output  # type: ignore
    integrator._merge_branches = mock_merge  # type: ignore

    res = await integrator.integrate_task(
        task_id="T-REQ-1",
        author_agent="bob",
        worktree_dir=tmp_path,
        candidate_branch="agent/bob",
    )

    # Verifications: DO NOT MERGE
    assert res.success is False
    assert res.merged is False
    assert merge_called is False
    assert res.metadata["review"]["verdict"] == "changes_requested"

    # Task should be reassigned to author
    async with async_bus_client("bob", base_url=live_bus_url) as client:
        t_resp = await client.get("/tasks/T-REQ-1")
        assert t_resp.json()["owner"] == "bob"

        # Bob should have received inbox feedback message
        inbox_resp = await client.get("/inbox/bob")
        messages = inbox_resp.json()
        assert any("Gatekeeper review" in str(m.get("body", {})) for m in messages)

        # Review decision durably stored
        rev_resp = await client.get("/tasks/T-REQ-1/reviews")
        assert len(rev_resp.json()) == 1
        assert rev_resp.json()[0]["verdict"] == "changes_requested"


@pytest.mark.asyncio
async def test_branch_integrator_require_approval_blocked(live_bus_url, tmp_path):
    async with async_bus_client("integrator", base_url=live_bus_url) as client:
        await client.post(
            "/tasks",
            json={"task_id": "T-BLK-1", "title": "Security test", "owner": "charlie"},
        )

    integrator = BranchIntegrator(
        repo_dir=tmp_path,
        bus_url=live_bus_url,
        require_approval=True,
    )

    # Passing tests, but secret leaked in diff
    async def mock_pass_tests(worktree_dir, test_cmd=None):
        return (True, "All passed")

    async def mock_git_output(*args, cwd=None):
        if "rev-parse" in args:
            return "sha-charlie-leak"
        if "diff" in args:
            return SAMPLE_SECRET_DIFF
        return ""

    merge_called = False

    async def mock_merge(candidate_branch, target_branch):
        nonlocal merge_called
        merge_called = True
        return (True, "Merged")

    integrator.run_tests = mock_pass_tests
    integrator._git_output = mock_git_output  # type: ignore
    integrator._merge_branches = mock_merge  # type: ignore

    res = await integrator.integrate_task(
        task_id="T-BLK-1",
        author_agent="charlie",
        worktree_dir=tmp_path,
        candidate_branch="agent/charlie",
    )

    # Verifications: DO NOT MERGE and MARK BLOCKED
    assert res.success is False
    assert res.merged is False
    assert merge_called is False
    assert res.status == "blocked"
    assert res.metadata["review"]["verdict"] == "blocked"

    async with async_bus_client("integrator", base_url=live_bus_url) as client:
        # Task status marked blocked
        t_resp = await client.get("/tasks/T-BLK-1")
        assert t_resp.json()["status"] == "blocked"

        # Blocker message sent
        inbox_resp = await client.get("/inbox/charlie")
        messages = inbox_resp.json()
        assert any(m.get("message_type") == "blocker" for m in messages)

        # Review audited
        revs = (await client.get("/tasks/T-BLK-1/reviews")).json()
        assert len(revs) == 1
        assert revs[0]["verdict"] == "blocked"


@pytest.mark.asyncio
async def test_branch_integrator_require_approval_false_policy(live_bus_url, tmp_path):
    # With require_approval=False, clean diff & green tests merges
    integrator = BranchIntegrator(
        repo_dir=tmp_path,
        bus_url=live_bus_url,
        require_approval=False,
    )

    async with async_bus_client("integrator", base_url=live_bus_url) as client:
        await client.post("/tasks", json={"task_id": "T-FALSE-1", "title": "Merge ok", "owner": "dave"})

    async def mock_pass_tests(worktree_dir, test_cmd=None):
        return (True, "Tests ok")

    async def mock_git_clean(*args, cwd=None):
        if "rev-parse" in args:
            return "sha-dave-clean"
        if "diff" in args:
            return SAMPLE_CLEAN_DIFF
        return ""

    async def mock_merge(candidate_branch, target_branch):
        return (True, "Merged")

    integrator.run_tests = mock_pass_tests
    integrator._git_output = mock_git_clean  # type: ignore
    integrator._merge_branches = mock_merge  # type: ignore

    res = await integrator.integrate_task("T-FALSE-1", "dave", tmp_path, "agent/dave")
    assert res.merged is True
    assert res.status == "integrated"

    # But if there's a security violation (BLOCKED verdict), even require_approval=False refuses to merge!
    async with async_bus_client("integrator", base_url=live_bus_url) as client:
        await client.post("/tasks", json={"task_id": "T-FALSE-2", "title": "Leak", "owner": "eve"})

    async def mock_git_secret(*args, cwd=None):
        if "rev-parse" in args:
            return "sha-eve-leak"
        if "diff" in args:
            return SAMPLE_SECRET_DIFF
        return ""

    integrator._git_output = mock_git_secret  # type: ignore
    res_blocked = await integrator.integrate_task("T-FALSE-2", "eve", tmp_path, "agent/eve")
    assert res_blocked.merged is False
    assert res_blocked.status == "blocked"


def test_cli_show_reviews_command(live_bus_url, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_URL", live_bus_url)
    runner = CliRunner()

    # Pre-populate review via API
    import httpx
    httpx.post(
        f"{live_bus_url}/reviews",
        json={
            "task_id": "T-CLI-1",
            "sha": "9876543210abcdef",
            "verdict": "approve",
            "reason": "All checks passed in CLI test",
            "reviewer_agent_id": "integrator",
        },
    )

    result = runner.invoke(app, ["show", "reviews", "--task", "T-CLI-1"])
    assert result.exit_code == 0
    assert "Revisiones (Gatekeeper)" in result.output
    assert "T-CLI-1" in result.output
    assert "approve" in result.output
    assert "integrator" in result.output
    assert "98765432" in result.output
