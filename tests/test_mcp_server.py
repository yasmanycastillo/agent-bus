from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
from mcp import Client
from agent_bus.mcp.server import McpServer


@pytest.mark.asyncio
async def test_mcp_initialize():
    server = McpServer()
    async with Client(server.sdk_server()) as client:
        assert client.server_info.name == "agent-bus"
        assert client.server_capabilities.tools is not None



@pytest.mark.asyncio
async def test_mcp_tools_list():
    server = McpServer()
    async with Client(server.sdk_server()) as client:
        tools = (await client.list_tools()).tools
    tool_names = [tool.name for tool in tools]
    assert "wait_for_updates" in tool_names
    assert "post_message" in tool_names
    assert "ack_messages" in tool_names
    assert "reply_message" in tool_names
    assert "claim_task" in tool_names
    assert "acquire_lock" in tool_names


@pytest.mark.asyncio
async def test_mcp_wait_for_updates_timeout(live_bus_url):
    server = McpServer(bus_url=live_bus_url)
    res = await server._wait_for_updates("claude", timeout=1)
    assert res["status"] == "timeout"


async def call_tool(server, name, arguments, *, expected_error=False):
    async with Client(server.sdk_server()) as client:
        response = await client.call_tool(name, arguments)
    assert bool(response.is_error) is expected_error, response
    data = json.loads(response.content[0].text)
    assert response.structured_content == data
    return data


async def test_mcp_wait_for_updates_bus_unavailable(unavailable_bus_url):
    server = McpServer(bus_url=unavailable_bus_url)
    res = await call_tool(server, "wait_for_updates", {"agent_id": "claude", "timeout": 1}, expected_error=True)
    assert res["status"] == "error"
    assert res["error"]


async def test_mcp_message_tools_against_real_hub(live_bus_url):
    server = McpServer(bus_url=live_bus_url)
    sent = await call_tool(server, "post_message", {
        "from_agent": "claude", "to_agent": "reviewer", "text": "Please review",
        "reply_needed": True, "related_task": "T1", "idempotency_key": "review-request",
    })
    page = await call_tool(server, "read_messages", {"agent_id": "reviewer"})
    inbox = page["messages"]
    assert page["next_cursor"] is None
    assert len(inbox) == 1
    assert inbox[0]["message_id"] == sent["message_id"]
    assert inbox[0]["body"]["text"] == "Please review"
    assert inbox[0]["related_task"] == "T1"
    pending = await call_tool(server, "wait_for_updates", {"agent_id": "reviewer"})
    assert pending["status"] == "pending_messages"
    assert pending["messages"] == inbox
    replied = await call_tool(server, "reply_message", {
        "agent_id": "reviewer", "message_id": sent["message_id"], "text": "Reviewed",
        "idempotency_key": "review-reply", "acknowledge": True,
    })
    assert replied["conversation_id"] == sent["conversation_id"]
    assert (await call_tool(server, "read_messages", {"agent_id": "reviewer"}))["messages"] == []
    assert (await call_tool(server, "ack_messages", {
        "agent_id": "reviewer", "message_ids": [sent["message_id"]],
    }))["acknowledged"] == [sent["message_id"]]


async def test_mcp_coordination_tools_against_real_hub(live_bus_url):
    server = McpServer(bus_url=live_bus_url)
    async with httpx.AsyncClient(base_url=live_bus_url) as client:
        response = await client.post("/tasks", json={"task_id": "T1", "title": "Review"})
        response.raise_for_status()
    claimed = await call_tool(server, "claim_task", {"task_id": "T1", "agent_id": "claude"})
    assert (claimed["owner"], claimed["status"]) == ("claude", "in_progress")
    lock = await call_tool(server, "acquire_lock", {"file_path": "a.py", "agent_id": "claude"})
    assert lock["locked_by"] == "claude"
    status = await call_tool(server, "get_project_status", {})
    assert status["tasks"] == [claimed]
    assert len(status["locks"]) == 1
    assert status["locks"][0]["file_path"] == lock["file_path"]
    assert "acquisition_id" not in status["locks"][0]
    assert status["server"]["bus_version"]
    assert status["agents"] == []
    await call_tool(server, "release_lock", {"file_path": "a.py", "agent_id": "claude", "acquisition_id": lock["acquisition_id"]})
    done = await call_tool(server, "complete_task", {"task_id": "T1", "agent_id": "claude"})
    assert done["status"] == "done"
    assert (await call_tool(server, "get_project_status", {}))["locks"] == []


@pytest.mark.parametrize("context", [None, "A shared project needs durable delivery"])
async def test_mcp_record_decision_minimal_and_context(live_bus_url, context):
    server = McpServer(bus_url=live_bus_url)
    args = {"title": "Use SQLite", "what": "Persist each delivery", "decided_by": "claude"}
    if context is not None:
        args["context"] = context
    decision = await call_tool(server, "record_decision", args)
    assert UUID(decision["decision_id"])
    assert decision["decision"] == args["what"]
    assert decision["context"] == (context or "")
    assert decision["decided_by"] == "claude"
    async with httpx.AsyncClient(base_url=live_bus_url) as client:
        response = await client.get(f"/decisions/{decision['decision_id']}")
        response.raise_for_status()
        assert response.json() == decision


@pytest.mark.parametrize("invalid", [
    {"title": "Missing fields"},
    {"title": "Example", "what": "", "decided_by": "claude"},
    {"title": "Example", "what": 123, "decided_by": "claude"},
    {"title": "Example", "what": "Decision", "decided_by": "claude", "context": None},
])
async def test_mcp_record_decision_invalid_arguments(live_bus_url, invalid):
    server = McpServer(bus_url=live_bus_url)
    response = await call_tool(server, "record_decision", invalid, expected_error=True)
    assert response["code"] == "invalid_arguments"
    async with httpx.AsyncClient(base_url=live_bus_url) as client:
        assert (await client.get("/decisions")).json() == []
