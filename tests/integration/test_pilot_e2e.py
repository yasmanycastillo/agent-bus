"""E2E Operational Pilot Test (T-16).

Validates complete real-world operational cycle:
1. Hub initialization & secure isolated project configuration.
2. Worker daemon & Branch integrator coordination across git worktrees.
3. Goal submission, worker task claiming, file modification, git commit & in_review submission.
4. Branch integrator executing candidate tests, merging green commits into target branch (main), and completing task.
5. Process restart & state persistence: verify surviving deliveries, task status, decision audit trails and SHA traces without leaking secrets.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from agent_bus.cli.main import app
from agent_bus.security import async_bus_client, sync_bus_client
from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.integrator import BranchIntegrator
from agent_bus.worker.runner import AgentRunner, RunnerResult
from agent_bus.worker.worktrees import WorktreeManager


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return res.stdout.strip()


@pytest.fixture
def pilot_git_repo(tmp_path: Path) -> Path:
    """Creates an isolated git repository with an initial commit on main and a basic test suite."""
    repo = tmp_path / "pilot_repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "pilot@agent-bus.test")
    _git(repo, "config", "user.name", "Pilot Agent")

    # Initial file and test script
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text(
        "import sys\nfrom calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n\nif __name__ == '__main__':\n    test_add()\n    print('ALL_TESTS_PASSED')\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: initial commit on main")
    return repo


@pytest.mark.asyncio
async def test_pilot_end_to_end_operational_cycle(
    tmp_path: Path,
    pilot_git_repo: Path,
    live_bus_url: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """Executes the full end-to-end pilot cycle with isolated hub, worker, worktree and integrator."""
    # Ensure project_id matches live_bus default project ("default")
    monkeypatch.setenv("AGENT_BUS_URL", live_bus_url)
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "default")
    monkeypatch.setenv("AGENT_BUS_PROJECT_ROOT", str(pilot_git_repo))
    monkeypatch.chdir(pilot_git_repo)

    # 1. Onboarding / Initialization via CLI
    runner = CliRunner()
    init_res = runner.invoke(app, ["init", "--bus-url", live_bus_url], catch_exceptions=False)
    assert init_res.exit_code == 0
    assert "Proyecto inicializado" in init_res.output

    # Register pilot agents (using agent_id matching principal identity)
    worker_agent = "worker"
    integrator_agent = "integrator"

    async with async_bus_client(worker_agent, base_url=live_bus_url) as client:
        r_worker = await client.post("/register", json={"agent_id": worker_agent, "display_name": "Codex Worker"})
        assert r_worker.status_code in (200, 201, 409)

    async with async_bus_client(integrator_agent, base_url=live_bus_url) as client:
        r_lead = await client.post("/register", json={"agent_id": integrator_agent, "display_name": "Lead Integrator"})
        assert r_lead.status_code in (200, 201, 409)

    # Record initial architecture decision
    async with async_bus_client(worker_agent, base_url=live_bus_url) as client:
        dec_resp = await client.post(
            "/decisions",
            json={
                "decision_id": "ADR-001",
                "title": "Use Multi-Operation Arithmetic",
                "context": "Need multiply function in calc.py",
                "decision": "Implement multiply(a, b) and extend test_calc.py",
                "decided_by": worker_agent,
            },
        )
        assert dec_resp.status_code == 200

    # 2. Setup Worktrees for worker
    wm = WorktreeManager(repo_root=pilot_git_repo)
    wt_info = wm.create(worker_agent, base_ref="main")
    assert wt_info.path.exists()
    assert (wt_info.path / "calc.py").exists()

    # 3. Create a Custom Executor simulating a real coding turn in the worker worktree
    async def simulated_real_code_turn(prompt: str, session_id: str | None) -> RunnerResult:
        # Worker edits calc.py and test_calc.py inside its worktree
        calc_file = wt_info.path / "calc.py"
        content = calc_file.read_text()
        if "def multiply" not in content:
            calc_file.write_text(content + "\ndef multiply(a, b):\n    return a * b\n")

        test_file = wt_info.path / "test_calc.py"
        test_content = test_file.read_text()
        if "test_multiply" not in test_content:
            test_file.write_text(
                "import sys\nfrom calc import add, multiply\n\n"
                "def test_add():\n    assert add(2, 3) == 5\n\n"
                "def test_multiply():\n    assert multiply(3, 4) == 12\n\n"
                "if __name__ == '__main__':\n    test_add()\n    test_multiply()\n    print('ALL_TESTS_PASSED')\n"
            )

        return RunnerResult(
            success=True,
            output=f"Implemented multiply function and updated tests in {wt_info.path}",
            session_id=session_id or "session-turn-1",
        )

    agent_runner = AgentRunner(
        agent_id=worker_agent,
        provider="mock",
        worktree_dir=wt_info.path,
        custom_executor=simulated_real_code_turn,
        bus_url=live_bus_url,
    )

    worker_daemon = WorkerDaemon(
        agent_id=worker_agent,
        runner=agent_runner,
        bus_url=live_bus_url,
        poll_interval_seconds=0.1,
    )

    # 4. Create Task on the bus
    task_id = "TASK-PILOT-01"
    async with async_bus_client(integrator_agent, base_url=live_bus_url) as client:
        t_create = await client.post(
            "/tasks",
            json={
                "task_id": task_id,
                "title": "Implement multiplication feature",
                "description": "Add multiply(a, b) in calc.py and verify test_calc.py",
                "owner": "free",
            },
        )
        assert t_create.status_code == 200

    # 5. Run one step of worker daemon polling / task claiming & execution
    worker_daemon._client = async_bus_client(worker_agent, base_url=live_bus_url, timeout=10.0)
    try:
        # Check and process pending tasks
        await worker_daemon._check_and_process_pending()
    finally:
        await worker_daemon._client.aclose()
        worker_daemon._client = None

    # Verify task state transitioned to 'in_review'
    async with async_bus_client(worker_agent, base_url=live_bus_url) as client:
        t_status = await client.get(f"/tasks/{task_id}")
        assert t_status.status_code == 200
        task_data = t_status.json()
        assert task_data["status"] == "in_review"
        assert task_data["owner"] == worker_agent

    # Verify commit in candidate worktree branch
    candidate_commit_msg = _git(wt_info.path, "log", "-1", "--pretty=%B")
    assert f"feat(agent): complete {task_id}" in candidate_commit_msg
    candidate_sha = _git(wt_info.path, "rev-parse", "HEAD")
    assert candidate_sha

    # 6. Branch Integrator Execution
    integrator = BranchIntegrator(
        repo_dir=pilot_git_repo,
        bus_url=live_bus_url,
        agent_id=integrator_agent,
    )

    # Run integration using custom test command (python test_calc.py)
    results = await integrator.run_once(
        test_cmd=["python3", "test_calc.py"],
        target_branch="main",
    )

    assert len(results) == 1
    int_res = results[0]
    assert int_res.success is True
    assert int_res.merged is True
    assert int_res.status == "integrated"

    # Verify task is now 'done' on the bus
    async with async_bus_client(integrator_agent, base_url=live_bus_url) as client:
        t_done = await client.get(f"/tasks/{task_id}")
        assert t_done.status_code == 200
        assert t_done.json()["status"] == "done"

    # Verify main branch now contains the merge and new feature
    main_calc = (pilot_git_repo / "calc.py").read_text()
    assert "def multiply" in main_calc
    main_test = (pilot_git_repo / "test_calc.py").read_text()
    assert "def test_multiply" in main_test

    # Run tests on main branch to verify integration
    test_run = subprocess.run(["python3", "test_calc.py"], cwd=str(pilot_git_repo), capture_output=True, text=True)
    assert test_run.returncode == 0
    assert "ALL_TESTS_PASSED" in test_run.stdout

    # 7. Restart Simulation & State Persistence Recovery
    # Simulate worker and coordinator restart: create new instances and verify persistence
    restarted_runner = AgentRunner(
        agent_id=worker_agent,
        provider="mock",
        worktree_dir=wt_info.path,
        bus_url=live_bus_url,
    )
    restarted_daemon = WorkerDaemon(
        agent_id=worker_agent,
        runner=restarted_runner,
        bus_url=live_bus_url,
    )
    restarted_daemon._client = async_bus_client(worker_agent, base_url=live_bus_url, timeout=10.0)
    try:
        # Check decisions persistence
        decisions = await restarted_daemon._fetch_recent_decisions()
        assert any(d.get("decision_id") == "ADR-001" for d in decisions)

        # Check task remains 'done' and no duplicate executions occur
        async with async_bus_client(worker_agent, base_url=live_bus_url) as client:
            t_after = await client.get(f"/tasks/{task_id}")
            assert t_after.json()["status"] == "done"

            # Check audit / task list
            all_tasks = (await client.get("/tasks")).json()
            assert len(all_tasks) == 1
            assert all_tasks[0]["task_id"] == task_id
            assert all_tasks[0]["status"] == "done"
    finally:
        await restarted_daemon._client.aclose()
        restarted_daemon._client = None


@pytest.mark.asyncio
async def test_pilot_integration_rejection_and_feedback_cycle(
    tmp_path: Path,
    pilot_git_repo: Path,
    live_bus_url: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """Validates that a failing candidate branch is rejected by the integrator and sends feedback to the author."""
    monkeypatch.setenv("AGENT_BUS_URL", live_bus_url)
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "default")
    monkeypatch.setenv("AGENT_BUS_PROJECT_ROOT", str(pilot_git_repo))
    monkeypatch.chdir(pilot_git_repo)

    worker_agent = "worker"
    integrator_agent = "integrator"

    async with async_bus_client(worker_agent, base_url=live_bus_url) as client:
        await client.post("/register", json={"agent_id": worker_agent, "display_name": "Codex Worker"})

    async with async_bus_client(integrator_agent, base_url=live_bus_url) as client:
        await client.post("/register", json={"agent_id": integrator_agent, "display_name": "Lead Integrator"})

    wm = WorktreeManager(repo_root=pilot_git_repo)
    wt_info = wm.create(worker_agent, base_ref="main")

    # Worker introduces broken code
    calc_file = wt_info.path / "calc.py"
    calc_file.write_text("def add(a, b):\n    return a - b  # Bug intentional\n")

    _git(wt_info.path, "add", "-A")
    _git(wt_info.path, "commit", "-m", "feat(agent): complete TASK-FAIL-01")

    task_id = "TASK-FAIL-01"
    async with async_bus_client(integrator_agent, base_url=live_bus_url) as client:
        await client.post(
            "/tasks",
            json={"task_id": task_id, "title": "Failing Task", "owner": worker_agent},
        )
        # Move to review
        await client.post(f"/tasks/{task_id}/review")

    integrator = BranchIntegrator(
        repo_dir=pilot_git_repo,
        bus_url=live_bus_url,
        agent_id=integrator_agent,
    )

    results = await integrator.run_once(
        test_cmd=["python3", "test_calc.py"],
        target_branch="main",
    )

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].merged is False
    assert results[0].status == "retry_requested"

    # Verify task ownership remained/reassigned to worker and inbox received feedback
    async with async_bus_client(worker_agent, base_url=live_bus_url) as client:
        t_data = (await client.get(f"/tasks/{task_id}")).json()
        assert t_data["owner"] == worker_agent
        assert t_data["status"] == "in_progress"

        inbox_resp = await client.get(f"/inbox/{worker_agent}/messages", params={"reply_needed": "true"})
        messages = inbox_resp.json()["messages"]
        feedback = [m for m in messages if m.get("related_task") == task_id]
        assert len(feedback) >= 1
        assert "Integration test/merge failed" in feedback[0]["body"]["text"]
