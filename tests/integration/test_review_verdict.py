import json
import subprocess
import sys

import httpx
import pytest
from click.testing import CliRunner
from httpx import ASGITransport

from agent_bus.cli.main import work
from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.mcp.server import McpServer
from agent_bus.reputation.database import Database
from agent_bus.types import AgentInfo


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_assigned_reviewer_records_the_verdict(tmp_path, monkeypatch):
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
    review_id = "feature-development-run-5-review"
    implementation_id = "feature-development-run-5-implementation"
    integration_id = "feature-development-run-5-integration"
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        assert (await client.post("/workflows/compile", json={
            "name": "feature-development", "instance_id": "run-5",
        })).status_code == 200
        await db.conn.execute(
            "UPDATE tasks SET status = 'done', owner = 'impl-01', test_cmd = ? WHERE task_id = ?",
            (json.dumps([sys.executable, "-c", "raise SystemExit('suite failed')"]), implementation_id),
        )
        await db.conn.execute(
            """UPDATE tasks SET status = 'done'
               WHERE task_id LIKE 'feature-development-run-5-%' AND task_id != ?""",
            (integration_id,),
        )
        await db.conn.execute(
            "UPDATE tasks SET status = 'pending', owner = 'reviewer-01' WHERE task_id = ?",
            (review_id,),
        )
        await db.conn.execute(
            "UPDATE tasks SET status = 'pending' WHERE task_id = ?",
            (integration_id,),
        )
        await db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at, outcome, candidate_sha, log_refs)
               VALUES ('att-impl', ?, 'dispatch:impl', 'completed', 'external:1', ?, ?, 'completed', ?, '[]')""",
            (implementation_id, str(repo), "2026-01-01T00:00:00+00:00", sha),
        )
        await db.conn.commit()

        async def verdict(agent, verdict_name, candidate, verdict_reason="reviewed"):
            return await client.post(f"/tasks/{review_id}/verdict", json={
                "agent_id": agent, "verdict": verdict_name, "sha": candidate, "reason": verdict_reason,
            })

        stranger = await verdict("stranger-01", "approve", sha)
        assert stranger.status_code == 409
        assert "owner" in stranger.json()["error"]
        implementer = await verdict("impl-01", "approve", sha)
        assert implementer.status_code == 409
        await db.conn.execute("UPDATE tasks SET owner = 'impl-01' WHERE task_id = ?", (review_id,))
        await db.conn.commit()
        owned_by_implementer = await verdict("impl-01", "approve", sha)
        assert owned_by_implementer.status_code == 409
        assert "implementer" in owned_by_implementer.json()["error"]
        await db.conn.execute("UPDATE tasks SET owner = 'reviewer-01' WHERE task_id = ?", (review_id,))
        await db.conn.commit()
        wrong = await verdict("reviewer-01", "approve", "0" * 40)
        assert wrong.status_code == 409
        assert "sha" in wrong.json()["error"]
        assert git(repo, "rev-parse", "HEAD") == sha

        await db.conn.execute("UPDATE tasks SET status = 'blocked' WHERE task_id = ?", (integration_id,))
        await db.conn.commit()
        requested = await verdict("reviewer-01", "changes_requested", sha, "please fix")
        assert requested.status_code == 200, requested.text
        reopened = (await client.get(f"/tasks/{implementation_id}")).json()
        assert reopened["status"] == "pending"
        assert reopened["owner"] == "free"
        assert (await client.get(f"/tasks/{integration_id}")).json()["status"] == "pending"
        inbox = (await client.get("/inbox/impl-01/messages")).json()
        assert any("please fix" in ((message.get("body") or {}).get("text") or "") for message in inbox["messages"])
        assert git(repo, "rev-parse", "HEAD") == sha

        await bus.registry.register(AgentInfo(agent_id="impl-02", display_name="Impl"))
        await client.post("/agents/impl-02/route-profile", json={
            "declared": ["implementation", "tests"],
            "approved": ["implementation", "tests"],
            "priority": 1, "can_edit": True,
        })
        await client.post("/agents/impl-02/runtime", json={
            "runtime": "external", "command": [sys.executable, "-c", "print('revised')"],
        })
        dispatched = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-5",
            "workspace_ref": str(repo), "timeout": 10,
        })
        assert dispatched.status_code == 200, dispatched.text
        assert dispatched.json()["task_id"] == implementation_id
        assert dispatched.json()["status"] == "dispatched"
        assert dispatched.json()["task_status"] == "done"
        assert git(repo, "rev-parse", "HEAD") == sha

        new_sha = "b" * 40
        await db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at, outcome, candidate_sha, log_refs)
               VALUES ('att-new', ?, 'dispatch:new', 'completed', 'external:2', ?, '2099-01-01T00:00:00+00:00', 'completed', ?, '[]')""",
            (implementation_id, str(repo), new_sha),
        )
        await db.conn.commit()
        stale = await verdict("reviewer-01", "approve", sha)
        assert stale.status_code == 409
        held = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-5",
            "workspace_ref": str(repo), "repo_dir": str(repo), "candidate_branch": "main",
        })
        assert held.json()["status"] == "waiting_for_review"
        assert held.json()["error"] == "independent review is missing"
        assert (await client.get(f"/tasks/{integration_id}")).json()["status"] == "pending"
        assert git(repo, "rev-parse", "HEAD") == sha

        approved = await verdict("reviewer-01", "approve", new_sha)
        assert approved.status_code == 200, approved.text
        blocked = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-5",
            "workspace_ref": str(repo), "repo_dir": str(repo), "candidate_branch": "main",
        })
        assert blocked.json()["status"] == "blocked"
        assert "suite failed" in blocked.json()["error"]
        assert git(repo, "rev-parse", "HEAD") == sha
    await db.close()

    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"verdict": "approve"}

    class Client:
        def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return Response()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("agent_bus.cli.main._require_agent", lambda: "reviewer-01")
    monkeypatch.setattr("agent_bus.cli.main._client", lambda: Client())
    result = CliRunner().invoke(work, [
        "verdict", review_id, "--verdict", "approve", "--sha", sha, "--reason", "reviewed",
    ])
    assert result.exit_code == 0, result.output
    assert captured["url"] == f"/tasks/{review_id}/verdict"
    assert captured["json"] == {
        "agent_id": "reviewer-01", "verdict": "approve", "sha": sha, "reason": "reviewed",
    }

    monkeypatch.delenv("AGENT_BUS_SESSION_FILE", raising=False)
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    server = McpServer()
    tool = next(item for item in server.tools if item["name"] == "record_verdict")
    assert tool["inputSchema"]["required"] == ["task_id", "verdict", "sha", "agent_id"]
    assert "dueño de la tarea de review" in tool["description"]
