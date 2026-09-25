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
        import json
        await db.conn.execute(
            "UPDATE tasks SET test_cmd = ? WHERE task_id = ?",
            (json.dumps([sys.executable, "-c", "print('suite ok')"]), "feature-development-run-2-implementation"),
        )
        await db.conn.commit()
        baseline = git(repo, "rev-parse", "HEAD")
        candidate = git(work, "rev-parse", "HEAD")
        held = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-2",
            "workspace_ref": str(work), "repo_dir": str(repo), "candidate_branch": "agent/impl",
        })
        assert held.json()["status"] == "waiting_for_review"
        assert held.json()["error"] == "independent review is not from the assigned reviewer"
        assert held.json()["task_status"] == "pending"
        assert git(repo, "rev-parse", "HEAD") == baseline
        review_task = "feature-development-run-2-review"
        for reviewer_id, verdict, sha, expected in (
            ("impl-01", "approve", candidate, "independent review was recorded by the implementer"),
            ("stranger-01", "approve", candidate, "independent review is not from the assigned reviewer"),
            ("analyst-01", "changes_requested", candidate, "independent review is not approved"),
            ("analyst-01", "approve", baseline, "independent review is not approved"),
        ):
            posted = await client.post("/reviews", json={
                "task_id": review_task, "sha": sha, "verdict": verdict,
                "reason": "independent review", "reviewer_agent_id": reviewer_id,
            })
            assert posted.status_code == 200, posted.text
            refused = await client.post("/workflows/advance", json={
                "workflow": "feature-development", "instance_id": "run-2",
                "workspace_ref": str(work), "repo_dir": str(repo), "candidate_branch": "agent/impl",
            })
            assert refused.json()["status"] == "waiting_for_review"
            assert refused.json()["error"] == expected
            assert (await client.get("/tasks/feature-development-run-2-integration")).json()["status"] == "pending"
            assert git(repo, "rev-parse", "HEAD") == baseline
        approved = await client.post("/reviews", json={
            "task_id": review_task, "sha": candidate, "verdict": "approve",
            "reason": "independent review", "reviewer_agent_id": "analyst-01",
        })
        assert approved.status_code == 200, approved.text
        mismatched = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-2",
            "workspace_ref": str(repo), "repo_dir": str(repo), "candidate_branch": "main",
        })
        assert mismatched.json()["status"] == "blocked"
        assert "candidate SHA" in mismatched.json()["error"]
        blocked_task = (await client.get("/tasks/feature-development-run-2-integration")).json()
        assert blocked_task["status"] == "blocked"
        assert "candidate SHA" in blocked_task["blocked_reason"]
        for recipient in ("impl-01", "analyst-01"):
            inbox = (await client.get(f"/inbox/{recipient}/messages")).json()
            assert any(
                "candidate SHA" in ((message.get("body") or {}).get("text") or "")
                for message in inbox["messages"]
            )
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
        assert git(repo, "rev-parse", "HEAD^2") == candidate
        done = (await client.get("/tasks/feature-development-run-2-integration")).json()
        assert done["status"] == "done"
        reviews = (await client.get("/tasks/feature-development-run-2-integration/reviews")).json()
        assert any(review["sha"] == candidate for review in reviews)
        report = (await client.get("/tasks/feature-development-run-2-integration/artifacts")).json()
        content = await client.get(f"/artifacts/{report[0]['artifact_id']}/content")
        assert b"suite ok" in content.content
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


@pytest.mark.asyncio
async def test_failing_suite_blocks_integration(tmp_path):
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
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        assert (await client.post("/workflows/compile", json={"name": "feature-development", "instance_id": "run-4"})).status_code == 200
        await db.conn.execute(
            """UPDATE tasks SET status = 'done', test_cmd = ?
               WHERE task_id = 'feature-development-run-4-implementation'""",
            (json.dumps([sys.executable, "-c", "raise SystemExit('suite failed')"]),),
        )
        await db.conn.execute(
            """UPDATE tasks SET status = 'done'
               WHERE task_id LIKE 'feature-development-run-4-%'
                 AND task_id != 'feature-development-run-4-integration'""",
        )
        await db.conn.execute(
            "UPDATE tasks SET status = 'pending' WHERE task_id = ?",
            ("feature-development-run-4-integration",),
        )
        await db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at, outcome, candidate_sha, log_refs)
               VALUES ('att-impl', 'feature-development-run-4-implementation', 'dispatch:impl', 'completed', 'external:1', ?, ?, 'completed', ?, '[]')""",
            (str(repo), "2026-01-01T00:00:00+00:00", sha),
        )
        await db.conn.execute(
            """UPDATE tasks SET owner = 'impl-01'
               WHERE task_id = 'feature-development-run-4-implementation'""",
        )
        await db.conn.execute(
            """UPDATE tasks SET owner = 'reviewer-01'
               WHERE task_id = 'feature-development-run-4-review'""",
        )
        await db.conn.commit()
        waiting = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-4",
            "workspace_ref": str(repo), "repo_dir": str(repo), "candidate_branch": "main",
        })
        assert waiting.json()["status"] == "waiting_for_review"
        assert waiting.json()["error"] == "independent review is missing"
        assert (await client.get("/tasks/feature-development-run-4-integration")).json()["status"] == "pending"
        assert git(repo, "rev-parse", "HEAD") == sha
        approved = await client.post("/reviews", json={
            "task_id": "feature-development-run-4-review", "sha": sha, "verdict": "approve",
            "reason": "independent review", "reviewer_agent_id": "reviewer-01",
        })
        assert approved.status_code == 200, approved.text
        blocked = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-4",
            "workspace_ref": str(repo), "repo_dir": str(repo), "candidate_branch": "main",
        })
        assert blocked.json()["status"] == "blocked"
        assert "tests failed" in blocked.json()["error"]
        assert "suite failed" in blocked.json()["error"]
        blocked_task = (await client.get("/tasks/feature-development-run-4-integration")).json()
        assert blocked_task["status"] == "blocked"
        assert "tests failed" in blocked_task["blocked_reason"]
        assert "suite failed" in blocked_task["blocked_reason"]
        await db.conn.execute(
            "UPDATE tasks SET status = 'pending' WHERE task_id = ?",
            ("feature-development-run-4-integration",),
        )
        await db.conn.commit()
        repeated = await client.post("/workflows/advance", json={
            "workflow": "feature-development", "instance_id": "run-4",
            "workspace_ref": str(repo), "repo_dir": str(repo), "candidate_branch": "main",
        })
        assert repeated.json()["status"] == "blocked"
        for recipient in ("impl-01", "reviewer-01"):
            inbox = (await client.get(f"/inbox/{recipient}/messages")).json()
            texts = [((message.get("body") or {}).get("text") or "") for message in inbox["messages"]]
            assert sum("tests failed" in text and "suite failed" in text for text in texts) == 1
        assert git(repo, "rev-parse", "HEAD") == sha
    await db.close()
