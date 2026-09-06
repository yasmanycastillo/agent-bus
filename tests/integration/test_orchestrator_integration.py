from __future__ import annotations

import json
import os
import tempfile
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.orchestrator.client import InferenceClient
from agent_bus.orchestrator.config import OrchestratorConfig
from agent_bus.orchestrator.hermes import HermesOrchestrator
from agent_bus.reputation.database import Database


@pytest.fixture
async def bus_app():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, "test.db"))
        await db.initialize()
        registry = AgentRegistry()
        inbox = InboxManager(db)
        bus = MessageBus(db=db, registry=registry, inbox=inbox)
        yield bus
        await db.close()


@pytest.fixture
async def bus_client(bus_app: MessageBus):
    transport = ASGITransport(app=bus_app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_full_orchestration_flow_success(bus_client: AsyncClient):
    """Test full breakdown -> validation -> bus publication flow."""
    # Register an agent so broadcasts are received in inbox
    await bus_client.post("/register", json={"agent_id": "worker-1", "display_name": "Worker 1"})

    valid_plan_json = json.dumps({
        "objective": "Build payment processing system",
        "summary": "Database schema, stripe webhook, payment endpoints",
        "tasks": [
            {
                "task_id": "pay-db",
                "title": "Payment DB schema",
                "description": "Create invoices and payments tables",
                "acceptance_criteria": ["Invoices table migrated", "Payments table migrated"],
                "test_cmd": ["pytest", "tests/unit/test_pay_db.py"],
                "depends_on": [],
            },
            {
                "task_id": "pay-api",
                "title": "Payment checkout endpoints",
                "description": "POST /checkout creates session",
                "acceptance_criteria": ["Returns 201 with session_id"],
                "test_cmd": ["pytest", "tests/unit/test_pay_api.py"],
                "depends_on": ["pay-db"],
            },
            {
                "task_id": "pay-webhook",
                "title": "Stripe webhook listener",
                "description": "Handle invoice.paid event",
                "acceptance_criteria": ["Updates invoice status to paid"],
                "test_cmd": ["pytest", "tests/integration/test_webhook.py"],
                "depends_on": ["pay-api"],
            },
        ],
    })

    async def mock_llm_handler(request: httpx.Request) -> httpx.Response:
        req_data = json.loads(request.content)
        assert req_data["model"] == "hermes-3"
        assert req_data["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "id": "mock-hermes-1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": valid_plan_json,
                        }
                    }
                ],
            },
        )

    mock_llm_transport = httpx.MockTransport(mock_llm_handler)
    async with httpx.AsyncClient(transport=mock_llm_transport) as mock_http:
        inference_client = InferenceClient(
            config=OrchestratorConfig(provider="vllm", model="hermes-3"),
            http_client=mock_http,
        )
        orchestrator = HermesOrchestrator(client=inference_client)

        result = await orchestrator.orchestrate(
            objective_text="Build payment processing system",
            operation_key="op-pay-1",
            bus_client=bus_client,
        )

        assert result.success is True
        assert result.plan is not None
        assert len(result.tasks) == 3

        # Verify tasks in the actual bus
        resp_tasks = await bus_client.get("/tasks")
        assert resp_tasks.status_code == 200
        tasks = resp_tasks.json()
        assert len(tasks) == 3

        tasks_by_id = {t["task_id"]: t for t in tasks}
        assert tasks_by_id["pay-db"]["status"] == "pending"
        assert tasks_by_id["pay-api"]["status"] == "blocked"
        assert tasks_by_id["pay-webhook"]["status"] == "blocked"
        assert tasks_by_id["pay-api"]["depends_on"] == ["pay-db"]
        assert tasks_by_id["pay-webhook"]["depends_on"] == ["pay-api"]

        # Verify broadcast message was received in worker's inbox
        resp_inbox = await bus_client.get("/inbox/worker-1/messages")
        assert resp_inbox.status_code == 200
        inbox_data = resp_inbox.json()
        messages = inbox_data.get("messages", [])
        published_event = any(
            m.get("body", {}).get("event") == "breakdown_published"
            for m in messages
        )
        assert published_event is True


@pytest.mark.asyncio
async def test_full_orchestration_flow_idempotency(bus_client: AsyncClient):
    """Test that re-running with the same operation_key is idempotent."""
    await bus_client.post("/register", json={"agent_id": "worker-1", "display_name": "Worker 1"})

    plan_json = json.dumps({
        "objective": "Idempotent plan",
        "tasks": [
            {
                "task_id": "idem-1",
                "title": "Task 1",
                "acceptance_criteria": ["Done"],
                "depends_on": [],
            }
        ],
    })

    async def mock_llm_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": plan_json}}],
            },
        )

    mock_llm_transport = httpx.MockTransport(mock_llm_handler)
    async with httpx.AsyncClient(transport=mock_llm_transport) as mock_http:
        inference_client = InferenceClient(http_client=mock_http)
        orchestrator = HermesOrchestrator(client=inference_client)

        res1 = await orchestrator.orchestrate("Idempotent plan", operation_key="op-idem-1", bus_client=bus_client)
        assert res1.success is True

        res2 = await orchestrator.orchestrate("Idempotent plan", operation_key="op-idem-1", bus_client=bus_client)
        assert res2.success is True

        resp_tasks = await bus_client.get("/tasks")
        assert len(resp_tasks.json()) == 1


@pytest.mark.asyncio
async def test_full_orchestration_flow_failure_notifies_bus(bus_client: AsyncClient):
    """Test that inference failure leaves goal blocked with observable report and notifies bus."""
    await bus_client.post("/register", json={"agent_id": "worker-1", "display_name": "Worker 1"})

    async def mock_llm_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "LLM worker nodes unavailable"})

    mock_llm_transport = httpx.MockTransport(mock_llm_handler)
    async with httpx.AsyncClient(transport=mock_llm_transport) as mock_http:
        inference_client = InferenceClient(http_client=mock_http)
        orchestrator = HermesOrchestrator(client=inference_client)

        result = await orchestrator.orchestrate(
            "Failing objective",
            operation_key="op-fail-1",
            bus_client=bus_client,
        )

        assert result.success is False
        assert result.failure is not None
        assert result.failure.status == "blocked"
        assert result.failure.error_type == "status_error"
        assert "503" in result.failure.reason

        # Verify no tasks were created in bus
        resp_tasks = await bus_client.get("/tasks")
        assert len(resp_tasks.json()) == 0

        # Verify failure event was notified to the bus inbox
        resp_inbox = await bus_client.get("/inbox/worker-1/messages")
        assert resp_inbox.status_code == 200
        messages = resp_inbox.json().get("messages", [])
        failed_event = next(
            (m for m in messages if m.get("body", {}).get("event") == "breakdown_failed"),
            None,
        )
        assert failed_event is not None
        assert failed_event["body"]["status"] == "blocked"
        assert "503" in failed_event["body"]["reason"]
