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


@pytest.mark.asyncio
async def test_same_requirement_can_run_on_two_runtimes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "agent/external")
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
    await bus.registry.register(AgentInfo(agent_id="external-01", display_name="External"))
    await bus.registry.register(AgentInfo(agent_id="native-01", display_name="Native"))
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        for agent_id, priority in (("external-01", 5), ("native-01", 1)):
            await client.post(f"/agents/{agent_id}/route-profile", json={
                "declared": ["implementation"], "approved": ["implementation"],
                "priority": priority, "can_edit": True,
            })
        await client.post("/agents/external-01/runtime", json={"runtime": "external", "command": [sys.executable, str(script)]})
        await client.post("/agents/native-01/runtime", json={"runtime": "native"})
        await client.post("/tasks", json={"task_id": "T-ext", "title": "External"})
        await client.post("/tasks/T-ext/requirements", json={"requires": ["implementation"]})
        external = await client.post("/tasks/T-ext/dispatch", json={"workspace_ref": str(repo), "timeout": 10})
        assert external.status_code == 200, external.text
        body = external.json()
        assert body["selected_agent"] == "external-01"
        assert body["runtime"] == "external"
        assert body["state"] == "completed"
        assert body["candidate_sha"]
        assert body["task_status"] == "pending"
        assert git(repo, "rev-parse", "main") != body["candidate_sha"]

        await client.post("/agents/external-01/route-profile", json={
            "declared": ["implementation"], "approved": ["implementation"], "priority": 0, "can_edit": True,
        })
        await client.post("/agents/native-01/route-profile", json={
            "declared": ["implementation"], "approved": ["implementation"], "priority": 9, "can_edit": True,
        })
        await client.post("/tasks", json={"task_id": "T-native", "title": "Native"})
        await client.post("/tasks/T-native/requirements", json={"requires": ["implementation"]})
        native = await client.post("/tasks/T-native/dispatch", json={})
        assert native.status_code == 200, native.text
        native_body = native.json()
        assert native_body["selected_agent"] == "native-01"
        assert native_body["runtime"] == "native"
        assert native_body["external_ref"].startswith("native:")
        assert native_body["task_status"] == "pending"
        summary = (await client.get("/usage/summary")).json()
        assert summary["records"] == 2
        assert {native_body["runtime"], body["runtime"]} == {"native", "external"}
    await db.close()
