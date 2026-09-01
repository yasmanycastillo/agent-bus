from __future__ import annotations

from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.types import AgentInfo
from agent_bus.worker.integrator import BranchIntegrator, IntegratorResult


@pytest.fixture
async def test_bus(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    await db.initialize()
    registry = AgentRegistry()
    inbox = InboxManager(db)
    bus = MessageBus(db=db, registry=registry, inbox=inbox)
    yield bus
    await db.close()


@pytest.mark.asyncio
async def test_integrator_test_failure_triggers_author_feedback(tmp_path):
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test", max_retries_per_task=2)

    # Mock run_tests to simulate test failure
    async def mock_fail_tests(worktree_dir, test_cmd=None):
        return (False, "Failing test: assertion error in test_foo")

    async def mock_notify(task_id, author_agent, details, retry):
        pass

    integrator.run_tests = mock_fail_tests
    integrator._notify_author_failure = mock_notify  # type: ignore

    res = await integrator.integrate_task(
        task_id="T800",
        author_agent="claude",
        worktree_dir=tmp_path,
        candidate_branch="agent/claude",
    )

    assert res.success is False
    assert res.merged is False
    assert res.status == "retry_requested"
    assert res.retry_count == 1
    assert "Failing test" in res.output


@pytest.mark.asyncio
async def test_integrator_exceeding_max_retries_blocks_task(tmp_path):
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test", max_retries_per_task=2)

    async def mock_fail_tests(worktree_dir, test_cmd=None):
        return (False, "Fatal syntax error")

    async def mock_notify(task_id, author_agent, details, retry):
        pass

    async def mock_block(task_id, author_agent, details):
        pass

    integrator.run_tests = mock_fail_tests
    integrator._notify_author_failure = mock_notify  # type: ignore
    integrator._notify_bus_blocked = mock_block  # type: ignore

    # Attempt 1
    res1 = await integrator.integrate_task("T801", "claude", tmp_path, "agent/claude")
    assert res1.status == "retry_requested"
    assert res1.retry_count == 1

    # Attempt 2
    res2 = await integrator.integrate_task("T801", "claude", tmp_path, "agent/claude")
    assert res2.status == "retry_requested"
    assert res2.retry_count == 2

    # Attempt 3 -> exceeds max_retries (2) -> blocked
    res3 = await integrator.integrate_task("T801", "claude", tmp_path, "agent/claude")
    assert res3.status == "blocked"
    assert res3.retry_count == 3
    assert "blocked" in (res3.error or "")


@pytest.mark.asyncio
async def test_integrator_success_flow(tmp_path):
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")

    async def mock_pass_tests(worktree_dir, test_cmd=None):
        return (True, "All tests passed (10/10)")

    async def mock_clean_merge(candidate_branch, target_branch):
        return (True, "Merged 1 commit cleanly.")

    async def mock_done(task_id):
        pass

    integrator.run_tests = mock_pass_tests
    integrator._merge_branches = mock_clean_merge
    integrator._mark_task_completed = mock_done  # type: ignore

    res = await integrator.integrate_task("T802", "claude", tmp_path, "agent/claude")
    assert res.success is True
    assert res.merged is True
    assert res.status == "integrated"
