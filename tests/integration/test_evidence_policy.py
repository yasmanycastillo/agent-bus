import subprocess
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.runtimes.native import NativeRuntime
from agent_bus.runtimes.protocol import RuntimeResult, RuntimeStartRequest
from agent_bus.worker import integrator as integrator_module
from agent_bus.worker.integrator import BranchIntegrator


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_strict_policy_blocks_when_the_author_claims_completion(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    baseline = git(repo, "rev-parse", "HEAD")
    work = tmp_path / "worker"
    git(repo, "worktree", "add", "-b", "agent/alice", str(work))
    (work / "feature").write_text("reviewed")
    git(work, "add", ".")
    git(work, "commit", "-m", "feature")
    candidate = git(work, "rev-parse", "HEAD")

    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")

    @asynccontextmanager
    async def bus_client(*args, **kwargs):
        async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
            yield client

    monkeypatch.setattr(integrator_module, "async_bus_client", bus_client)
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        await client.post("/tasks", json={"task_id": "T-ev", "title": "Feature", "owner": "alice"})
        await client.post("/tasks/T-ev/done", json={"agent_id": "alice", "evidence": "author says complete"})
        await client.post("/tasks/T-ev/evidence-policy", json={"strict": True, "require_review": True, "require_sha_match": True})

    integrator = BranchIntegrator(repo_dir=repo, bus_url="http://test", require_approval=True)
    integrator.run_tests = AsyncMock(return_value=(True, "tests passed"))
    integrator._mark_task_completed = AsyncMock()
    blocked = await integrator.integrate_task("T-ev", "alice", work, "agent/alice")
    assert blocked.status == "blocked"
    assert "required evidence is missing" in blocked.error
    assert git(repo, "rev-parse", "HEAD") == baseline
    integrator._mark_task_completed.assert_not_awaited()

    runtime = NativeRuntime(db)
    started = await runtime.start(RuntimeStartRequest("att-ev", "T-ev", "native:T-ev", "alice", str(work)))
    await runtime.complete(started, RuntimeResult("completed", candidate, ("worker:alice:T-ev",)))
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        artifact = await client.post("/tasks/T-ev/artifacts", json={
            "producer": "alice",
            "kind": "test-report",
            "media_type": "text/plain",
            "content": "tests passed",
            "attempt_id": "att-ev",
        })
        await client.post("/tasks/T-ev/evidence", json={
            "criterion": "all tests pass",
            "artifact_id": artifact.json()["artifact_id"],
            "status": "passed",
            "attempt_id": "att-ev",
            "target_sha": baseline,
        })
    integrator._mark_task_completed = AsyncMock()
    merged = await integrator.integrate_task("T-ev", "alice", work, "agent/alice")
    assert merged.success, merged.error
    assert git(repo, "rev-parse", "HEAD^2") == candidate
    await db.close()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    work = tmp_path / "worker"
    git(repo, "worktree", "add", "-b", "agent/alice", str(work))
    (work / "feature").write_text("reviewed")
    git(work, "add", ".")
    git(work, "commit", "-m", "feature")
    return repo, work


@pytest.mark.asyncio
async def test_unreachable_evidence_policy_does_not_merge(tmp_path):
    repo, work = _repo(tmp_path)
    baseline = git(repo, "rev-parse", "HEAD")
    integrator = BranchIntegrator(repo_dir=repo, bus_url="http://127.0.0.1:9", require_approval=True)
    integrator.run_tests = AsyncMock(return_value=(True, "tests passed"))
    blocked = await integrator.integrate_task("T-down", "alice", work, "agent/alice")
    assert blocked.success is False
    assert blocked.merged is False
    assert blocked.status == "blocked"
    assert "evidence policy unavailable" in blocked.error
    assert git(repo, "rev-parse", "HEAD") == baseline


@pytest.mark.asyncio
async def test_evidence_policy_http_error_does_not_merge(tmp_path):
    repo, work = _repo(tmp_path)
    baseline = git(repo, "rev-parse", "HEAD")

    def factory(timeout):
        def handler(request):
            if request.url.path.endswith("/evidence-policy"):
                return httpx.Response(503)
            return httpx.Response(200, json={})

        @asynccontextmanager
        async def opener():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="http://bus.local", timeout=timeout,
            ) as client:
                yield client

        return opener()

    integrator = BranchIntegrator(repo_dir=repo, require_approval=True, client_factory=factory)
    integrator.run_tests = AsyncMock(return_value=(True, "tests passed"))
    blocked = await integrator.integrate_task("T-503", "alice", work, "agent/alice")
    assert blocked.success is False
    assert blocked.merged is False
    assert "evidence policy unavailable (503)" in blocked.error
    assert git(repo, "rev-parse", "HEAD") == baseline


@pytest.mark.asyncio
async def test_pending_integration_is_done_after_merge_and_not_merged_twice(tmp_path):
    repo, work = _repo(tmp_path)
    candidate = git(work, "rev-parse", "HEAD")
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")

    def factory(timeout):
        @asynccontextmanager
        async def opener():
            async with httpx.AsyncClient(
                transport=ASGITransport(app=bus.app), base_url="http://bus.local", timeout=timeout,
            ) as client:
                yield client
        return opener()

    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        created = await client.post("/tasks", json={"task_id": "T-int", "title": "Integrate", "owner": "free"})
        assert created.status_code == 200, created.text
    integrator = BranchIntegrator(repo_dir=repo, require_approval=True, client_factory=factory)
    integrator.run_tests = AsyncMock(return_value=(True, "tests passed"))
    merged = await integrator.integrate_task("T-int", "alice", work, "agent/alice", acceptance_criteria=[])
    assert merged.success, merged.error
    assert git(repo, "rev-parse", "HEAD^2") == candidate
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        task = (await client.get("/tasks/T-int")).json()
    assert task["status"] == "done"
    merge_commit = git(repo, "rev-parse", "HEAD")

    failing = BranchIntegrator(repo_dir=repo, require_approval=True, client_factory=factory)
    failing.run_tests = AsyncMock(return_value=(True, "tests passed"))

    async def unavailable(task_id):
        return False

    failing._mark_task_completed = unavailable  # type: ignore[method-assign]
    again = await failing.integrate_task("T-int", "alice", work, "agent/alice", acceptance_criteria=[])
    assert again.success is False
    assert again.merged is True
    assert again.status == "merged_unrecorded"
    assert git(repo, "rev-parse", "HEAD") == merge_commit
    assert git(repo, "rev-parse", "HEAD^2") == candidate
    await db.close()
