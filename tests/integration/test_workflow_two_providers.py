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


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _script(path, filename, message):
    path.write_text(
        "import subprocess\n"
        "from pathlib import Path\n"
        f"Path({filename!r}).write_text({filename!r} + '\\n')\n"
        f"subprocess.check_call(['git', 'add', {filename!r}])\n"
        f"subprocess.check_call(['git', 'commit', '-m', {message!r}])\n"
    )


@pytest.mark.asyncio
async def test_same_workflow_reaches_done_with_two_implementation_providers(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    providers = (
        ("provider-a", "impl-a", "agent/impl-a", tmp_path / "a.py", "provider-a.txt", "provider a"),
        ("provider-b", "impl-b", "agent/impl-b", tmp_path / "b.py", "provider-b.txt", "provider b"),
    )
    for instance_id, _agent_id, branch, _script_path, _filename, _message in providers:
        git(repo, "worktree", "add", "-b", branch, str(tmp_path / instance_id))
    _script(tmp_path / "a.py", "provider-a.txt", "provider a")
    _script(tmp_path / "b.py", "provider-b.txt", "provider b")

    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    await bus.registry.register(AgentInfo(agent_id="analyst-01", display_name="Analyst"))
    await bus.registry.register(AgentInfo(agent_id="impl-a", display_name="Impl A"))
    await bus.registry.register(AgentInfo(agent_id="impl-b", display_name="Impl B"))
    definitions = []
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        await client.post("/agents/analyst-01/route-profile", json={
            "declared": ["repository-analysis", "long-context", "architecture", "code-review"],
            "approved": ["repository-analysis", "long-context", "architecture", "code-review"],
            "priority": 1, "can_edit": True,
        })
        await client.post("/agents/analyst-01/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('ok')"],
        })
        for instance_id, agent_id, branch, script_path, filename, _message in providers:
            await client.post(f"/agents/{agent_id}/route-profile", json={
                "declared": ["implementation", "tests"],
                "approved": ["implementation", "tests"],
                "priority": 1, "can_edit": True,
            })
            await client.post(f"/agents/{agent_id}/runtime", json={
                "runtime": "external", "command": [sys.executable, str(script_path)],
            })
            compiled = await client.post("/workflows/compile", json={
                "name": "feature-development", "instance_id": instance_id,
            })
            assert compiled.status_code == 200, compiled.text
            definitions.append([task["title"] for task in compiled.json()["tasks"]])
            work = tmp_path / instance_id
            for expected in ("discovery", "design", "implementation", "review"):
                advanced = await client.post("/workflows/advance", json={
                    "workflow": "feature-development", "instance_id": instance_id,
                    "workspace_ref": str(work), "timeout": 10,
                })
                assert advanced.status_code == 200, advanced.text
                assert advanced.json()["task_id"].endswith(expected)
                assert advanced.json()["selected_agent"] == (agent_id if expected == "implementation" else "analyst-01")
                assert advanced.json()["task_status"] == "done"
            candidate = git(work, "rev-parse", "HEAD")
            implementation_id = f"feature-development-{instance_id}-implementation"
            review_id = f"feature-development-{instance_id}-review"
            await db.conn.execute(
                "UPDATE tasks SET test_cmd = ? WHERE task_id = ?",
                (json.dumps([sys.executable, "-c", "print('suite ok')"]), implementation_id),
            )
            await db.conn.commit()
            approved = await client.post(f"/tasks/{review_id}/verdict", json={
                "agent_id": "analyst-01", "verdict": "approve", "sha": candidate, "reason": "approved",
            })
            assert approved.status_code == 200, approved.text
            integrated = await client.post("/workflows/advance", json={
                "workflow": "feature-development", "instance_id": instance_id,
                "workspace_ref": str(work), "repo_dir": str(repo), "candidate_branch": branch, "timeout": 10,
            })
            assert integrated.status_code == 200, integrated.text
            assert integrated.json()["status"] == "integrated"
            assert git(repo, "rev-parse", "HEAD^2") == candidate
            stored = (await client.get(f"/tasks/feature-development-{instance_id}-integration")).json()
            assert stored["status"] == "done"
            assert (repo / filename).read_text() == filename + "\n"
            await client.post(f"/agents/{agent_id}/route-profile", json={
                "declared": ["documentation"], "approved": ["documentation"],
                "priority": 1, "can_edit": False,
            })
    assert definitions == [["discovery", "design", "implementation", "review", "integration"]] * 2
    await db.close()
