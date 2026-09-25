"""Advance one ready workflow task through the router and its runtime."""

from __future__ import annotations

from typing import Any

from agent_bus.core.bus import MessageBus
from agent_bus.core.evidence import EvidenceLog
from agent_bus.runtimes.registry import RuntimeRegistry


async def advance_workflow(
    bus: MessageBus,
    workflow: str,
    instance_id: str,
    *,
    workspace_ref: str | None = None,
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
