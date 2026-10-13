import json
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


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_native_agent_completes_feature_development(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    work = tmp_path / "native-work"
    git(repo, "worktree", "add", "-b", "agent/native", str(work))
    implementation_id = "feature-development-native-1-implementation"

    async def execute(prompt: str, session_id: str | None) -> RunnerResult:
        del session_id
        if implementation_id in prompt:
            (work / "native.txt").write_text("native\n")
            git(work, "add", "native.txt")
            git(work, "commit", "-m", "native")
        return RunnerResult(success=True, output="done")

    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    await bus.registry.register(AgentInfo(agent_id="native-01", display_name="Native"))
    await bus.registry.register(AgentInfo(agent_id="reviewer-01", display_name="Reviewer"))
    runner = AgentRunner(agent_id="native-01", custom_executor=execute, worktree_dir=work)
    daemon = WorkerDaemon(agent_id="native-01", runner=runner, bus_url="http://test")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        daemon._client = client
        daemon._running = True
        await client.post("/agents/native-01/route-profile", json={
            "declared": ["repository-analysis", "long-context", "architecture", "implementation", "tests"],
            "approved": ["repository-analysis", "long-context", "architecture", "implementation", "tests"],
            "priority": 1, "can_edit": True,
        })
        await client.post("/agents/reviewer-01/route-profile", json={
            "declared": ["code-review"], "approved": ["code-review"],
            "priority": 1, "can_edit": False,
        })
        await client.post("/agents/native-01/runtime", json={"runtime": "native"})
        await client.post("/agents/reviewer-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('review')"],
        })
        compiled = await client.post("/workflows/compile", json={
            "name": "feature-development", "instance_id": "native-1",
        })
        assert compiled.status_code == 200, compiled.text
        assert [task["title"] for task in compiled.json()["tasks"]] == [
            "discovery", "design", "implementation", "review", "integration",
        ]
        for expected in ("discovery", "design", "implementation"):
            advanced = await client.post("/workflows/advance", json={
                "workflow": "feature-development", "instance_id": "native-1",
                "workspace_ref": str(work), "timeout": 10,
            })
            assert advanced.status_code == 200, advanced.text
            body = advanced.json()
            assert body["task_id"].endswith(expected)
            assert body["selected_agent"] == "native-01"
            assert body["runtime"] == "native"
            assert body["task_status"] == "in_progress"
            await daemon._check_and_process_pending()
            stored = (await client.get(f"/tasks/{body['task_id']}")).json()
            assert stored["status"] == "done"
        review = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "native-1",
            "workspace_ref": str(work), "timeout": 10,
        })
        assert review.status_code == 200, review.text
        assert review.json()["selected_agent"] == "reviewer-01"
        assert review.json()["task_status"] == "done"
        candidate = git(work, "rev-parse", "HEAD")
        await db.conn.execute(
            "UPDATE tasks SET test_cmd = ? WHERE task_id = ?",
            (json.dumps([sys.executable, "-c", "print('suite ok')"]), implementation_id),
        )
        await db.conn.commit()
        approved = await client.post("/tasks/feature-development-native-1-review/verdict", json={
            "agent_id": "reviewer-01", "verdict": "approve", "sha": candidate, "reason": "approved",
        })
        assert approved.status_code == 200, approved.text
        integrated = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "native-1",
            "workspace_ref": str(work), "repo_dir": str(repo),
            "candidate_branch": "agent/native", "timeout": 10,
        })
        assert integrated.status_code == 200, integrated.text
        assert integrated.json()["status"] == "integrated"
        assert git(repo, "rev-parse", "HEAD^2") == candidate
        final = (await client.get("/tasks/feature-development-native-1-integration")).json()
        assert final["status"] == "done"
        assert (repo / "native.txt").read_text() == "native\n"
    await db.close()
