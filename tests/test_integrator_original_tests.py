"""Real Git: a candidate that rewrites existing tests must also pass their original versions."""
import sys
from unittest.mock import AsyncMock

import pytest

from agent_bus.worker.gatekeeper import is_test_path
from agent_bus.worker.integrator import BranchIntegrator
from tests.test_integrator_exact_sha import git, policy_free_client

# The "suite": the first word of tests/expected must equal the code's value.
SUITE = [sys.executable, "-c", "assert open('tests/expected').read().split()[0] == open('value').read()"]


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "value").write_text("1")
    (repo / "tests" / "expected").write_text("1")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo


def candidate(tmp_path, repo, files):
    wt = tmp_path / "worker"
    git(repo, "worktree", "add", "-b", "agent/alice", str(wt))
    for name, content in files.items():
        (wt / name).write_text(content)
    git(wt, "add", ".")
    git(wt, "commit", "-m", "candidate")
    integrator = BranchIntegrator(repo_dir=repo, client_factory=policy_free_client)
    integrator._record_review = AsyncMock(side_effect=lambda decision: decision)
    integrator._finalize_merge = AsyncMock(return_value=(True, None))
    integrator._notify_author_review_feedback = AsyncMock(return_value=True)
    integrator._durable_retry_count = AsyncMock(return_value=1)
    return integrator, wt


async def integrate(integrator, wt, criteria=()):
    return await integrator.integrate_task(
        "T1", "alice", wt, "agent/alice", test_cmd=SUITE, acceptance_criteria=list(criteria),
    )


async def test_rewritten_test_that_hides_a_regression_requests_changes(tmp_path, repo):
    integrator, wt = candidate(tmp_path, repo, {"value": "2", "tests/expected": "2"})
    initial = git(repo, "rev-parse", "HEAD")
    result = await integrate(integrator, wt)
    assert result.status == "retry_requested" and not result.merged
    review = result.metadata["review"]
    assert "fails their original versions" in review["reason"]
    assert review["evidence"]["changed_tests"] == ["tests/expected"]
    assert git(repo, "rev-parse", "HEAD") == initial


async def test_task_creator_can_allow_test_changes(tmp_path, repo):
    integrator, wt = candidate(tmp_path, repo, {"value": "2", "tests/expected": "2"})
    result = await integrate(integrator, wt, ["allow_test_changes"])
    assert result.success and result.merged
    assert (repo / "value").read_text() == "2"


async def test_changed_test_whose_original_still_passes_merges(tmp_path, repo):
    integrator, wt = candidate(tmp_path, repo, {"tests/expected": "1 extra case"})
    result = await integrate(integrator, wt)
    assert result.success and result.merged
    assert result.metadata["review"]["evidence"]["changed_tests"] == ["tests/expected"]


async def test_added_tests_do_not_trigger_a_rerun(tmp_path, repo):
    integrator, wt = candidate(tmp_path, repo, {"tests/new": "new"})
    integrator.run_tests = AsyncMock(return_value=(True, "passed"))
    result = await integrate(integrator, wt)
    assert result.success and result.merged
    assert integrator.run_tests.await_count == 1


@pytest.mark.parametrize("path, expected", [
    ("tests/unit/test_a.py", True), ("pkg/test_a.py", True), ("pkg/a_test.go", True),
    ("conftest.py", True), ("web/app.spec.ts", True), ("src/__tests__/x.js", True),
    ("src/agent_bus/testing.py", False), ("src/contest.py", False), ("docs/tests.md", False),
])
def test_is_test_path(path, expected):
    assert is_test_path(path) is expected


async def test_only_an_admin_can_allow_test_changes(tmp_db):
    from httpx import ASGITransport, AsyncClient

    from agent_bus.core.bus import MessageBus
    from agent_bus.core.inbox import InboxManager
    from agent_bus.core.registry import AgentRegistry

    bus = MessageBus(db=tmp_db, registry=AgentRegistry(), inbox=InboxManager(tmp_db), project_id="allow-test")
    agent = await bus.sessions.create("worker", role="agent")
    admin = await bus.sessions.create("root", role="admin")
    task = {"task_id": "T1", "title": "Rewrite tests", "acceptance_criteria": ["allow_test_changes"]}

    async def post(token, path, body):
        async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://bus",
                               headers={"Authorization": f"Bearer {token}"}) as client:
            return await client.post(path, json=body)

    assert (await post(agent["token"], "/tasks", task)).status_code == 403
    assert (await post(agent["token"], "/tasks/batch", {"tasks": [task]})).status_code == 403
    assert (await post(admin["token"], "/tasks", task)).status_code == 200
