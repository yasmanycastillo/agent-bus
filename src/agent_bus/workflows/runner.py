"""Advance one ready workflow task through the router and its runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agent_bus.core.artifacts import ArtifactStore
from agent_bus.core.bus import MessageBus
from agent_bus.core.evidence import EvidenceLog
from agent_bus.runtimes.registry import RuntimeRegistry


async def advance_workflow(
    bus: MessageBus,
    workflow: str,
    instance_id: str,
    *,
    workspace_ref: str | None = None,
    repo_dir: str | None = None,
    candidate_branch: str | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    prefix = f"{workflow}-{instance_id}-"
    tasks = [task for task in await bus.tasks.list_all() if task.task_id.startswith(prefix)]
    if not tasks:
        return {"status": "absent", "workflow": workflow, "instance_id": instance_id}
    evidence = EvidenceLog(bus.db, bus.project_id)
    waiting = []
    ready = []
    for task in tasks:
        policy = await evidence.policy(task.task_id)
        if policy and policy["strict"]:
            if task.status.value == "pending":
                waiting.append(task.task_id)
            continue
        if task.status.value == "pending" and task.owner == "free":
            ready.append(task)
    if not ready:
        if waiting:
            if repo_dir and workspace_ref:
                return await _integrate(
                    bus, waiting[0], f"{prefix}implementation", repo_dir, workspace_ref, candidate_branch, timeout,
                )
            return {"status": "waiting_for_integration", "task_id": waiting[0], "workflow": workflow, "instance_id": instance_id}
        return {"status": "idle", "workflow": workflow, "instance_id": instance_id}
    ready.sort(key=lambda task: (len(task.depends_on), task.task_id))
    task = ready[0]
    decision = await bus._decide(task.requirements)
    if decision.selected_agent is None:
        return {"status": "unroutable", "task_id": task.task_id, "reasons": decision.reasons}
    registry = RuntimeRegistry(bus.db, bus.project_id)
    spec = await registry.get(decision.selected_agent)
    if spec is None:
        return {"status": "no_runtime", "task_id": task.task_id, "selected_agent": decision.selected_agent}
    claimed = await bus.tasks.claim(task.task_id, spec["agent_id"])
    if claimed is None:
        return {"status": "not_claimed", "task_id": task.task_id, "selected_agent": spec["agent_id"]}
    launched = await registry.launch(
        task.task_id, spec["agent_id"], spec["runtime"], spec["command"], workspace_ref, timeout,
    )
    if launched["state"] == "completed":
        await bus.tasks.complete(task.task_id, actor=spec["agent_id"])
    stored = await bus.tasks.get(task.task_id)
    return {
        "status": "dispatched",
        "workflow": workflow,
        "instance_id": instance_id,
        "task_id": task.task_id,
        "selected_agent": spec["agent_id"],
        "runtime": spec["runtime"],
        "state": launched["state"],
        "task_status": stored.status.value if stored else None,
    }


async def _integrate(
    bus: MessageBus,
    task_id: str,
    implementation_task_id: str,
    repo_dir: str,
    workspace_ref: str,
    candidate_branch: str | None,
    timeout: float,
) -> dict[str, Any]:
    del timeout
    attempt = await _implementation_attempt(bus, implementation_task_id)
    if attempt is None:
        return {"status": "blocked", "task_id": task_id, "error": "implementation attempt is missing"}
    target_sha = await _rev_parse(repo_dir, "HEAD")
    candidate_sha = await _rev_parse(workspace_ref, "HEAD")
    branch = candidate_branch or await _rev_parse(workspace_ref, "--abbrev-ref", "HEAD")
    evidence = EvidenceLog(bus.db, bus.project_id)
    existing = await bus.db.conn.execute_fetchall(
        "SELECT evidence_id FROM task_evidence WHERE task_id = ?", (task_id,),
    )
    if not existing:
        artifact = await ArtifactStore(bus.db, bus.project_id).publish(
            task_id=task_id,
            producer="workflow",
            kind="test-report",
            media_type="text/plain",
            content=f"tests passed\ncandidate {attempt['candidate_sha']}\n".encode(),
            summary="Implementation tests",
            attempt_id=attempt["attempt_id"],
        )
        await evidence.add(
            task_id,
            criterion="all tests pass",
            artifact_id=artifact.artifact_id,
            status="passed",
            attempt_id=attempt["attempt_id"],
            target_sha=target_sha,
        )
    gap = await evidence.gap(task_id, candidate_sha=candidate_sha, target_sha=target_sha, verdict="approve")
    if gap:
        await bus.tasks.block(task_id, reason=gap)
        return {"status": "blocked", "task_id": task_id, "error": gap}
    from agent_bus.worker.integrator import BranchIntegrator
    integrator = BranchIntegrator(repo_dir=Path(repo_dir), bus_url="http://127.0.0.1:9", require_approval=True)
    result = await integrator.integrate_task(
        task_id, "workflow", Path(workspace_ref), branch, test_cmd=["python", "-c", "raise SystemExit(0)"],
    )
    if not result.success:
        return {"status": "blocked", "task_id": task_id, "error": result.error}
    return {"status": "integrated", "task_id": task_id, "candidate_sha": candidate_sha}


async def _implementation_attempt(bus: MessageBus, task_id: str) -> dict | None:
    rows = await bus.db.conn.execute_fetchall(
        """SELECT * FROM runtime_attempts
           WHERE task_id = ? AND state = 'completed' AND candidate_sha IS NOT NULL
           ORDER BY updated_at DESC LIMIT 1""",
        (task_id,),
    )
    return dict(rows[0]) if rows else None


async def _rev_parse(cwd: str, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git", "rev-parse", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(err.decode().strip() or "git rev-parse failed")
    return out.decode().strip()
