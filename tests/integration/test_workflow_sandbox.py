import json
import subprocess
import sys

import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.core.reviews import ReviewLog
from agent_bus.reputation.database import Database
from agent_bus.types import AgentInfo


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class FakeSandbox:
    def __init__(self, cwd) -> None:
        self.cwd = cwd
        self.ref = "sandbox-1"
        self.closed = False

    async def execute(self, command: str) -> tuple[int, str]:
        result = subprocess.run(command, cwd=self.cwd, shell=True, capture_output=True, text=True)
        return result.returncode, result.stdout + result.stderr

    async def cleanup(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_sandbox_implementation_reaches_done_without_its_own_review(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    work = tmp_path / "sandbox-work"
    git(repo, "worktree", "add", "-b", "agent/sandbox", str(work))
    script = tmp_path / "commit_sandbox.py"
    script.write_text(
        "import subprocess\nfrom pathlib import Path\n"
        "Path('sandbox.txt').write_text('sandbox\\n')\n"
        "subprocess.check_call(['git', 'add', 'sandbox.txt'])\n"
        "subprocess.check_call(['git', 'commit', '-m', 'sandbox'])\n"
    )
    opened = 0

    async def open_sandbox():
        nonlocal opened
        opened += 1
        return FakeSandbox(work)

    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    await bus.registry.register(AgentInfo(agent_id="analyst-01", display_name="Analyst"))
    await bus.registry.register(AgentInfo(agent_id="sandbox-01", display_name="Sandbox"))
    await bus.registry.register(AgentInfo(agent_id="reviewer-01", display_name="Reviewer"))
    implementation_id = "feature-development-sandbox-1-implementation"
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        await client.post("/agents/analyst-01/route-profile", json={
            "declared": ["repository-analysis", "long-context", "architecture"],
            "approved": ["repository-analysis", "long-context", "architecture"],
            "priority": 1, "can_edit": True,
        })
        await client.post("/agents/analyst-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('ok')"],
        })
        await client.post("/agents/reviewer-01/route-profile", json={
            "declared": ["code-review"], "approved": ["code-review"],
            "priority": 1, "can_edit": False,
        })
        await client.post("/agents/reviewer-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('review')"],
        })
        await client.post("/agents/sandbox-01/route-profile", json={
            "declared": ["implementation", "tests"],
            "approved": ["implementation", "tests"],
            "priority": 1, "can_edit": True,
        })
        await client.post("/agents/sandbox-01/runtime", json={
            "runtime": "sandbox", "command": [sys.executable, str(script)],
        })
        compiled = await client.post("/workflows/compile", json={
            "name": "feature-development", "instance_id": "sandbox-1",
        })
        assert compiled.status_code == 200, compiled.text
        await db.conn.execute(
            "UPDATE tasks SET test_cmd = ? WHERE task_id = ?",
            (json.dumps([sys.executable, "-c", "print('suite ok')"]), implementation_id),
        )
        await db.conn.commit()
        for expected in ("discovery", "design"):
            advanced = await client.post("/workflows/advance", json={
                "workflow": "feature-development", "instance_id": "sandbox-1",
                "workspace_ref": str(work), "timeout": 10,
            })
            assert advanced.status_code == 200, advanced.text
            assert advanced.json()["task_status"] == "done"
        refused = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "sandbox-1",
            "workspace_ref": str(work), "timeout": 10,
        })
        assert refused.status_code == 409
        assert "injected opener" in refused.json()["error"]
        assert (await client.get(f"/tasks/{implementation_id}")).json()["status"] == "pending"
        assert opened == 0
        bus.sandbox_opener = open_sandbox
        implemented = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "sandbox-1",
            "workspace_ref": str(work), "timeout": 10,
        })
        assert implemented.status_code == 200, implemented.text
        assert implemented.json()["runtime"] == "sandbox"
        assert implemented.json()["task_status"] == "done"
        assert opened == 1
        candidate = git(work, "rev-parse", "HEAD")
        runtime_review = (await ReviewLog(db).list_for_task(implementation_id))[0]
        assert runtime_review.reviewer_agent_id == "openhands-runtime"
        assert runtime_review.sha == candidate
        review = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "sandbox-1",
            "workspace_ref": str(work), "timeout": 10,
        })
        assert review.json()["selected_agent"] == "reviewer-01"
        assert review.json()["task_status"] == "done"
        held = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "sandbox-1",
            "workspace_ref": str(work), "repo_dir": str(repo),
            "candidate_branch": "agent/sandbox", "timeout": 10,
        })
        assert held.json()["status"] == "waiting_for_review"
        assert git(repo, "rev-parse", "HEAD") != candidate
        approved = await client.post("/tasks/feature-development-sandbox-1-review/verdict", json={
            "agent_id": "reviewer-01", "verdict": "approve", "sha": candidate, "reason": "approved",
        })
        assert approved.status_code == 200, approved.text
        integrated = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "sandbox-1",
            "workspace_ref": str(work), "repo_dir": str(repo),
            "candidate_branch": "agent/sandbox", "timeout": 10,
        })
        assert integrated.status_code == 200, integrated.text
        assert integrated.json()["status"] == "integrated"
        merge_head = git(repo, "rev-parse", "HEAD")
        assert git(repo, "rev-parse", "HEAD^2") == candidate
        final = (await client.get("/tasks/feature-development-sandbox-1-integration")).json()
        assert final["status"] == "done"
        assert (repo / "sandbox.txt").read_text() == "sandbox\n"
        again = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "sandbox-1",
            "workspace_ref": str(work), "repo_dir": str(repo),
            "candidate_branch": "agent/sandbox", "timeout": 10,
        })
        assert again.status_code == 200, again.text
        assert git(repo, "rev-parse", "HEAD") == merge_head
        assert opened == 1
    await db.close()
