"""The worker keeps its own task, skips a spent message, and waits out a long test."""
from __future__ import annotations

import httpx
import pytest

from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner, RunnerResult


class CycleHub:
    def __init__(self, messages, tasks, assignments):
        self.messages = messages
        self.tasks = tasks
        self.assignments = assignments
        self.claims = []
        self.timeouts = []

    def handle(self, request):
        path = request.url.path
        if path.endswith("/paused"):
            return httpx.Response(200, json={"paused": False})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": self.messages, "next_cursor": None})
        if path == "/tasks":
            params = dict(request.url.params)
            matched = [
                task for task in self.tasks
                if task.get("owner") == params.get("owner") and task.get("status") == params.get("status")
            ]
            return httpx.Response(200, json=matched)
        if path.endswith("/assignments"):
            return httpx.Response(200, json={"assignments": self.assignments})
        if path.endswith("/claim"):
            self.claims.append(path)
            task_id = path.split("/")[2]
            task = next(item for item in self.tasks if item["task_id"] == task_id)
            return httpx.Response(200, json={**task, "owner": "bob", "status": "in_progress"})
        if path == "/decisions":
            return httpx.Response(200, json=[])
        if path == "/runtime/native/start":
            return httpx.Response(200, json={"state": "started", "attempt_id": "attempt-1", "execute": True})
        if path.endswith("/execution"):
            return httpx.Response(200, json={"execute": True})
        if path.endswith("/active-work"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": path})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), base_url="http://127.0.0.1")


def worker(tmp_path, hub):
    timeouts = hub.timeouts

    async def execute(prompt, thread_id=None, timeout_seconds=300):
        timeouts.append(timeout_seconds)
        return RunnerResult(success=True, output="done")

    runner = AgentRunner("bob", custom_executor=execute, worktree_dir=tmp_path)
    daemon = WorkerDaemon("bob", runner)
    daemon.runner.execute_turn = execute
    return daemon


@pytest.mark.asyncio
async def test_exhausted_message_lets_the_owned_task_run(tmp_path):
    hub = CycleHub(
        messages=[{"message_id": "old-agy", "attempts": 5, "reply_needed": True}],
        tasks=[{"task_id": "adapt-billing", "owner": "bob", "status": "in_progress", "title": "billing", "description": "pay"}],
        assignments=[],
    )
    daemon = worker(tmp_path, hub)
    async with hub.client() as client:
        daemon._client = client
        await daemon._check_and_process_pending()
    assert hub.timeouts == [1800]
    assert hub.claims == []


@pytest.mark.asyncio
async def test_worker_does_not_claim_a_free_task_that_is_not_its_assignment(tmp_path):
    hub = CycleHub(
        messages=[],
        tasks=[{"task_id": "adapt-ar-aging", "owner": "free", "status": "pending", "title": "aging", "description": "report"}],
        assignments=[],
    )
    daemon = worker(tmp_path, hub)
    async with hub.client() as client:
        daemon._client = client
        await daemon._check_and_process_pending()
    assert hub.claims == []
    assert hub.timeouts == []


@pytest.mark.asyncio
async def test_worker_claims_only_the_task_assigned_to_it(tmp_path):
    hub = CycleHub(
        messages=[],
        tasks=[
            {"task_id": "adapt-ar-aging", "owner": "free", "status": "pending", "title": "aging", "description": "report"},
            {"task_id": "adapt-billing", "owner": "free", "status": "pending", "title": "billing", "description": "pay"},
        ],
        assignments=[{"task_id": "adapt-billing", "role": "implement", "agent_id": "bob"}],
    )
    daemon = worker(tmp_path, hub)
    async with hub.client() as client:
        daemon._client = client
        await daemon._check_and_process_pending()
    assert hub.claims == ["/tasks/adapt-billing/claim"]
    assert hub.timeouts == [1800]
