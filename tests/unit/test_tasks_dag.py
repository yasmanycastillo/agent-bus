from __future__ import annotations

import pytest

from agent_bus.core.tasks import (
    CycleDetectedError, MissingDependencyError, TaskManager,
)
from agent_bus.reputation.database import Database
from agent_bus.types import TaskStatus


async def test_task_creation_with_contract_and_metadata(tmp_db: Database):
    tm = TaskManager(tmp_db)
    task = await tm.create(
        task_id="T1",
        title="Add authentication",
        description="Support bearer tokens",
        acceptance_criteria=["Tokens expire after 1h", "Invalid tokens return 401"],
        test_cmd=["pytest", "-k", "test_auth"],
        operation_key="op-101",
    )
    assert task.task_id == "T1"
    assert task.acceptance_criteria == ["Tokens expire after 1h", "Invalid tokens return 401"]
    assert task.test_cmd == ["pytest", "-k", "test_auth"]
    assert task.operation_key == "op-101"
    assert task.depends_on == []
    assert task.status == TaskStatus.PENDING

    fetched = await tm.get("T1")
    assert fetched is not None
    assert fetched.acceptance_criteria == ["Tokens expire after 1h", "Invalid tokens return 401"]
    assert fetched.test_cmd == ["pytest", "-k", "test_auth"]
    assert fetched.operation_key == "op-101"


async def test_detect_self_dependency_cycle(tmp_db: Database):
    tm = TaskManager(tmp_db)
    with pytest.raises(CycleDetectedError) as exc_info:
        await tm.create("T1", "Self loop", depends_on=["T1"])
    assert "cannot depend on itself" in str(exc_info.value)


async def test_detect_direct_cycle(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("T1", "Task 1")
    await tm.create("T2", "Task 2", depends_on=["T1"])

    with pytest.raises(CycleDetectedError) as exc_info:
        await tm.create_batch([
            {"task_id": "T1", "title": "Task 1 updated", "depends_on": ["T2"]},
        ])
    assert "Cyclic dependency detected" in str(exc_info.value)


async def test_detect_transitive_cycle_in_batch(tmp_db: Database):
    tm = TaskManager(tmp_db)
    batch = [
        {"task_id": "A", "title": "A", "depends_on": ["C"]},
        {"task_id": "B", "title": "B", "depends_on": ["A"]},
        {"task_id": "C", "title": "C", "depends_on": ["B"]},
    ]
    with pytest.raises(CycleDetectedError) as exc_info:
        await tm.create_batch(batch)
    assert "Cyclic dependency detected" in str(exc_info.value)

    # Ensure none were created
    assert await tm.get("A") is None
    assert await tm.get("B") is None
    assert await tm.get("C") is None


async def test_missing_dependency_rejection(tmp_db: Database):
    tm = TaskManager(tmp_db)
    with pytest.raises(MissingDependencyError) as exc_info:
        await tm.create("T2", "Task 2", depends_on=["NON_EXISTENT"])
    assert "non-existent task 'NON_EXISTENT'" in str(exc_info.value)


async def test_batch_creation_idempotency_with_operation_key(tmp_db: Database):
    tm = TaskManager(tmp_db)
    batch = [
        {"task_id": "step-1", "title": "Step 1", "depends_on": []},
        {"task_id": "step-2", "title": "Step 2", "depends_on": ["step-1"]},
    ]
    created = await tm.create_batch(batch, operation_key="epic-breakdown-1")
    assert len(created) == 2
    assert created[0].task_id == "step-1"
    assert created[0].status == TaskStatus.PENDING
    assert created[1].task_id == "step-2"
    assert created[1].status == TaskStatus.BLOCKED

    # Idempotent retry with the same operation_key returns identical existing tasks
    retried = await tm.create_batch(batch, operation_key="epic-breakdown-1")
    assert len(retried) == 2
    assert retried[0].task_id == "step-1"
    assert retried[1].task_id == "step-2"


async def test_worker_claim_gated_by_dependencies(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("T1", "Prerequisite")
    await tm.create("T2", "Dependent", depends_on=["T1"])

    t2 = await tm.get("T2")
    assert t2.status == TaskStatus.BLOCKED

    # Worker cannot claim blocked task
    claim_result = await tm.claim("T2", "agent-alpha")
    assert claim_result is None

    # Worker claims and completes T1
    c1 = await tm.claim("T1", "agent-alpha")
    assert c1 is not None
    assert c1.status == TaskStatus.IN_PROGRESS

    await tm.complete("T1", actor="agent-alpha")
    assert (await tm.get("T1")).status == TaskStatus.DONE

    # T2 should now be automatically unblocked to PENDING
    t2_after = await tm.get("T2")
    assert t2_after.status == TaskStatus.PENDING

    # Now worker can claim T2
    claim_result = await tm.claim("T2", "agent-alpha")
    assert claim_result is not None
    assert claim_result.status == TaskStatus.IN_PROGRESS
    assert claim_result.owner == "agent-alpha"


async def test_multiple_dependencies_unblocking(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("A", "Dep A")
    await tm.create("B", "Dep B")
    await tm.create("C", "Final Task", depends_on=["A", "B"])

    assert (await tm.get("C")).status == TaskStatus.BLOCKED

    # Complete A only
    await tm.claim("A", "worker")
    await tm.complete("A", actor="worker")

    # C should still be blocked because B is pending
    assert (await tm.get("C")).status == TaskStatus.BLOCKED
    assert await tm.claim("C", "worker") is None

    # Complete B
    await tm.claim("B", "worker")
    await tm.complete("B", actor="worker")

    # C should now be unlocked
    assert (await tm.get("C")).status == TaskStatus.PENDING
    c_claim = await tm.claim("C", "worker")
    assert c_claim is not None


async def test_list_all_ready_only_filter(tmp_db: Database):
    tm = TaskManager(tmp_db)
    await tm.create("T1", "Ready task")
    await tm.create("T2", "Blocked task", depends_on=["T1"])

    ready_tasks = await tm.list_all(ready_only=True)
    ready_ids = [t.task_id for t in ready_tasks]
    assert "T1" in ready_ids
    assert "T2" not in ready_ids

    # After T1 is done, T2 is in ready tasks
    await tm.claim("T1", "worker")
    await tm.complete("T1", actor="worker")

    ready_after = await tm.list_all(ready_only=True)
    ready_ids_after = [t.task_id for t in ready_after]
    assert "T2" in ready_ids_after
