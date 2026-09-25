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
        worker_caps = [cap for cap in CAPS if cap != "code-review"]
        await client.post("/agents/worker-01/route-profile", json={
            "declared": worker_caps, "approved": worker_caps, "priority": 1, "can_edit": True,
        })
        await client.post("/agents/worker-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('ok')"],
        })
        compiled = await client.post("/workflows/compile", json={"name": "feature-development", "instance_id": "run-1"})
        assert compiled.status_code == 200
        seen = []
        for _ in range(3):
            advanced = await client.post("/workflows/advance", json={
                "workflow": "feature-development", "instance_id": "run-1",
                "workspace_ref": str(repo), "timeout": 10,
            })
            assert advanced.status_code == 200, advanced.text
            body = advanced.json()
            assert body["status"] == "dispatched"
            assert body["task_status"] == "done"
            seen.append(body["task_id"].rsplit("-", 1)[-1])
        await bus.registry.register(AgentInfo(agent_id="reviewer-01", display_name="Reviewer"))
        await client.post("/agents/reviewer-01/route-profile", json={
            "declared": ["code-review"], "approved": ["code-review"], "priority": 1, "can_edit": False,
        })
        await client.post("/agents/reviewer-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('review')"],
        })
        reviewed = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-1",
            "workspace_ref": str(repo), "timeout": 10,
        })
        assert reviewed.json()["status"] == "dispatched"
        assert reviewed.json()["selected_agent"] == "reviewer-01"
        seen.append("review")
        assert seen == ["discovery", "design", "implementation", "review"]
        held = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-1", "workspace_ref": str(repo),
        })
        assert held.json()["status"] == "waiting_for_integration"
        assert held.json()["task_id"].endswith("-integration")
        integration = (await client.get("/tasks/feature-development-run-1-integration")).json()
        assert integration["status"] == "pending"
    await db.close()


@pytest.mark.asyncio
async def test_integration_uses_the_implementation_sha(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    work = tmp_path / "worker"
    git(repo, "worktree", "add", "-b", "agent/impl", str(work))
    script = tmp_path / "commit.py"
    script.write_text(
        "import subprocess\n"
        "from pathlib import Path\n"
        "Path('feature.txt').write_text('feature\\n')\n"
        "subprocess.check_call(['git', 'add', 'feature.txt'])\n"
        "subprocess.check_call(['git', 'commit', '-m', 'feature'])\n"
    )
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    await bus.registry.register(AgentInfo(agent_id="analyst-01", display_name="Analyst"))
    await bus.registry.register(AgentInfo(agent_id="impl-01", display_name="Impl"))
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        await client.post("/agents/analyst-01/route-profile", json={
            "declared": ["repository-analysis", "long-context", "architecture", "code-review"],
            "approved": ["repository-analysis", "long-context", "architecture", "code-review"],
            "priority": 1, "can_edit": True,
        })
        await client.post("/agents/impl-01/route-profile", json={
            "declared": ["implementation", "tests"],
            "approved": ["implementation", "tests"],
            "priority": 1, "can_edit": True,
        })
        await client.post("/agents/analyst-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('ok')"],
        })
        await client.post("/agents/impl-01/runtime", json={
            "runtime": "external", "command": [sys.executable, str(script)],
        })
        assert (await client.post("/workflows/compile", json={"name": "feature-development", "instance_id": "run-2"})).status_code == 200
        for expected in ("discovery", "design", "implementation", "review"):
            advanced = await client.post("/workflows/advance", json={
                "workflow": "feature-development", "instance_id": "run-2",
                "workspace_ref": str(work), "timeout": 10,
            })
            assert advanced.status_code == 200, advanced.text
            assert advanced.json()["task_id"].endswith(expected)
            assert advanced.json()["task_status"] == "done"
        baseline = git(repo, "rev-parse", "HEAD")
        mismatched = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-2",
            "workspace_ref": str(repo), "repo_dir": str(repo), "candidate_branch": "main",
        })
        assert mismatched.json()["status"] == "blocked"
        assert "candidate SHA" in mismatched.json()["error"]
        assert git(repo, "rev-parse", "HEAD") == baseline
        await db.conn.execute(
            "UPDATE tasks SET status = 'pending' WHERE task_id = ?",
            ("feature-development-run-2-integration",),
        )
        await db.conn.commit()
        integrated = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-2",
            "workspace_ref": str(work), "repo_dir": str(repo), "candidate_branch": "agent/impl",
        })
        assert integrated.status_code == 200, integrated.text
        assert integrated.json()["status"] == "integrated"
        assert git(repo, "rev-parse", "HEAD^2") == git(work, "rev-parse", "HEAD")
    await db.close()


@pytest.mark.asyncio
async def test_review_cannot_be_done_by_the_implementer(tmp_path):
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
    await bus.registry.register(AgentInfo(agent_id="impl-01", display_name="Impl"))
    caps = ["repository-analysis", "long-context", "architecture", "implementation", "tests", "code-review"]
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        await client.post("/agents/impl-01/route-profile", json={
            "declared": caps, "approved": caps, "priority": 1, "can_edit": True,
        })
        await client.post("/agents/impl-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('ok')"],
        })
        assert (await client.post("/workflows/compile", json={"name": "feature-development", "instance_id": "run-3"})).status_code == 200
        for expected in ("discovery", "design", "implementation"):
            advanced = await client.post("/workflows/advance", json={
                "workflow": "feature-development", "instance_id": "run-3",
                "workspace_ref": str(repo), "timeout": 10,
            })
            assert advanced.json()["task_id"].endswith(expected)
        blocked = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-3",
            "workspace_ref": str(repo), "timeout": 10,
        })
        assert blocked.json()["status"] == "unroutable"
        review = (await client.get("/tasks/feature-development-run-3-review")).json()
        assert review["status"] == "pending"
        assert "feature-development-run-3-implementation" in review["independent_from"]
        claim = await client.post("/tasks/feature-development-run-3-review/claim", json={"agent_id": "impl-01"})
        assert claim.status_code == 409
        await bus.registry.register(AgentInfo(agent_id="reviewer-01", display_name="Reviewer"))
        await client.post("/agents/reviewer-01/route-profile", json={
            "declared": ["code-review"], "approved": ["code-review"], "priority": 1, "can_edit": False,
        })
        await client.post("/agents/reviewer-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('review')"],
        })
        reviewed = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-3",
            "workspace_ref": str(repo), "timeout": 10,
        })
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["selected_agent"] == "reviewer-01"
        assert reviewed.json()["task_status"] == "done"
    await db.close()
