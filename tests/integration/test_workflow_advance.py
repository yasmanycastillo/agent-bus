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

CAPS = ["repository-analysis", "long-context", "architecture", "implementation", "tests", "code-review"]


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_workflow_advances_until_integration(tmp_path):
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
    await bus.registry.register(AgentInfo(agent_id="worker-01", display_name="Worker"))
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        await client.post("/agents/worker-01/route-profile", json={
            "declared": CAPS, "approved": CAPS, "priority": 1, "can_edit": True,
        })
        await client.post("/agents/worker-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('ok')"],
        })
        compiled = await client.post("/workflows/compile", json={"name": "feature-development", "instance_id": "run-1"})
        assert compiled.status_code == 200
        seen = []
        for _ in range(4):
            advanced = await client.post("/workflows/advance", json={
                "workflow": "feature-development", "instance_id": "run-1",
                "workspace_ref": str(repo), "timeout": 10,
            })
            assert advanced.status_code == 200, advanced.text
            body = advanced.json()
            assert body["status"] == "dispatched"
            assert body["task_status"] == "done"
            seen.append(body["task_id"].rsplit("-", 1)[-1])
        assert seen == ["discovery", "design", "implementation", "review"]
        held = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-1", "workspace_ref": str(repo),
        })
        assert held.json()["status"] == "waiting_for_integration"
        assert held.json()["task_id"].endswith("-integration")
        integration = (await client.get("/tasks/feature-development-run-1-integration")).json()
        assert integration["status"] == "pending"
    await db.close()
