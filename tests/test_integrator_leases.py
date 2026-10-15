"""Real Git and hub: the integrator does not publish over another agent's live lease."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.worker.integrator import BranchIntegrator
from tests.test_integrator_exact_sha import git

# Captured at import, before the autouse fixture in conftest stubs it out.
REAL_FOREIGN_LEASES = BranchIntegrator._foreign_leases


@pytest.fixture
async def setup(tmp_path, tmp_db, monkeypatch):
    monkeypatch.setattr(BranchIntegrator, "_foreign_leases", REAL_FOREIGN_LEASES)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "feature").write_text("base")
    (repo / "other").write_text("base")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    wt = tmp_path / "worker"
    git(repo, "worktree", "add", "-b", "agent/alice", str(wt))
    (wt / "feature").write_text("alice")
    git(wt, "commit", "-am", "feature")
    bus = MessageBus(db=tmp_db, registry=AgentRegistry(), inbox=InboxManager(tmp_db))

    def factory(timeout):
        @asynccontextmanager
        async def opener():
            async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://bus",
                                         timeout=timeout) as client:
                yield client
        return opener()

    integrator = BranchIntegrator(repo_dir=repo, client_factory=factory)
    integrator.run_tests = AsyncMock(return_value=(True, "tests passed"))
    integrator._record_review = AsyncMock(side_effect=lambda decision: decision)
    integrator._finalize_merge = AsyncMock(return_value=(True, None))
    return integrator, bus, repo, wt


async def integrate(integrator, wt):
    return await integrator.integrate_task("T1", "alice", wt, "agent/alice", acceptance_criteria=[])


async def test_foreign_lease_on_a_changed_file_defers_before_tests(setup):
    integrator, bus, repo, wt = setup
    await bus.locks.acquire(str(repo / "feature"), "bob", session_id="s-bob")
    initial = git(repo, "rev-parse", "HEAD")
    result = await integrate(integrator, wt)
    assert result.status == "deferred" and not result.merged
    assert "feature (bob until" in result.error
    integrator.run_tests.assert_not_awaited()
    integrator._record_review.assert_not_awaited()
    assert git(repo, "rev-parse", "HEAD") == initial


@pytest.mark.parametrize("holder, path", [
    ("alice", "feature"),                 # the author's own lease
    ("bob", ".worktrees/bob/feature"),    # bob's private copy, not the target
    ("bob", "other"),                     # a file the candidate does not change
])
async def test_leases_that_do_not_conflict_merge(setup, holder, path):
    integrator, bus, repo, wt = setup
    await bus.locks.acquire(str(repo / path), holder, session_id=f"s-{holder}")
    result = await integrate(integrator, wt)
    assert result.success and result.merged, result


async def test_unreadable_lock_list_fails_closed(setup):
    integrator, _bus, repo, wt = setup

    def broken(timeout):
        @asynccontextmanager
        async def opener():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)),
                                         base_url="http://bus") as client:
                yield client
        return opener()

    integrator._client_factory = broken
    result = await integrate(integrator, wt)
    assert result.status == "deferred" and "lock list unavailable" in result.error
