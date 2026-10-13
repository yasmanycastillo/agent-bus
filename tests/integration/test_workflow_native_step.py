import subprocess
import sys

import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.types import AgentInfo
from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner, RunnerResult

WORKFLOW = """
workflow: two-step
version: 1
steps:
  - id: build
    requires: [implementation]
  - id: package
    requires: [packaging]
    depends_on: [build]
"""


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_native_step_unblocks_the_next_workflow_task(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    sha = git(repo, "rev-parse", "HEAD")
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    await bus.registry.register(AgentInfo(agent_id="native-01", display_name="Native"))
    await bus.registry.register(AgentInfo(agent_id="pack-01", display_name="Pack"))
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        await client.post("/agents/native-01/route-profile", json={
            "declared": ["implementation"], "approved": ["implementation"], "priority": 1, "can_edit": True,
        })
        await client.post("/agents/native-01/runtime", json={"runtime": "native"})
        await client.post("/agents/pack-01/route-profile", json={
            "declared": ["packaging"], "approved": ["packaging"], "priority": 1, "can_edit": False,
        })
        await client.post("/agents/pack-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('packed')"],
        })
        assert (await client.post("/workflows/compile", json={"yaml": WORKFLOW, "instance_id": "run-1"})).status_code == 200
        opened = await client.post("/workflows/advance", json={
            "workflow": "two-step", "instance_id": "run-1", "workspace_ref": str(repo),
        })
        assert opened.json()["runtime"] == "native"
        assert opened.json()["task_status"] == "in_progress"

        async def succeed(prompt: str, session_id: str | None) -> RunnerResult:
            return RunnerResult(success=True, output="built")

        daemon = WorkerDaemon(
            agent_id="native-01",
            runner=AgentRunner(agent_id="native-01", worktree_dir=repo, custom_executor=succeed),
            bus_url="http://test",
        )
        daemon._client = client
        daemon._running = True
        await daemon._check_and_process_pending()
        build = (await client.get("/tasks/two-step-run-1-build")).json()
        assert build["status"] == "done"
        attempt = await db.conn.execute_fetchall(
            "SELECT state, candidate_sha FROM runtime_attempts WHERE task_id = ?",
            ("two-step-run-1-build",),
        )
        assert attempt[0]["state"] == "completed"
        assert attempt[0]["candidate_sha"] == sha
        packaged = await client.post("/workflows/advance", json={
            "workflow": "two-step", "instance_id": "run-1", "workspace_ref": str(repo), "timeout": 10,
        })
        assert packaged.status_code == 200, packaged.text
        assert packaged.json()["task_id"] == "two-step-run-1-package"
        assert packaged.json()["runtime"] == "external"
        assert packaged.json()["task_status"] == "done"
    await db.close()


@pytest.mark.asyncio
async def test_failed_native_step_does_not_unblock_the_next(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    await bus.registry.register(AgentInfo(agent_id="native-01", display_name="Native"))
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        await client.post("/agents/native-01/route-profile", json={
            "declared": ["implementation"], "approved": ["implementation"], "priority": 1, "can_edit": True,
        })
        await client.post("/agents/native-01/runtime", json={"runtime": "native"})
        await client.post("/agents/pack-01/route-profile", json={
            "declared": ["packaging"], "approved": ["packaging"], "priority": 1,
        })
        assert (await client.post("/workflows/compile", json={"yaml": WORKFLOW, "instance_id": "run-2"})).status_code == 200
        await client.post("/workflows/advance", json={"workflow": "two-step", "instance_id": "run-2", "workspace_ref": str(repo)})

        async def fail(prompt: str, session_id: str | None) -> RunnerResult:
            return RunnerResult(success=False, output="", error="build failed")

        daemon = WorkerDaemon(
            agent_id="native-01",
            runner=AgentRunner(agent_id="native-01", worktree_dir=repo, custom_executor=fail),
            bus_url="http://test",
        )
        daemon._client = client
        daemon._running = True
        await daemon._check_and_process_pending()
        build = (await client.get("/tasks/two-step-run-2-build")).json()
        package = (await client.get("/tasks/two-step-run-2-package")).json()
        assert build["status"] == "in_progress"
        assert package["status"] == "blocked"
    await db.close()
