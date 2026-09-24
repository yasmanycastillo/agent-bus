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
