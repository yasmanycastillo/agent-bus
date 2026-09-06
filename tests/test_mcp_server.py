from __future__ import annotations

import json

import httpx
import pytest
from agent_bus.mcp.server import McpServer, TOOLS_DEFINITIONS


@pytest.mark.asyncio
async def test_mcp_initialize():
    server = McpServer()
    resp = await server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["serverInfo"]["name"] == "agent-bus"
    assert "tools" in resp["result"]["capabilities"]


@pytest.mark.asyncio
async def test_mcp_tools_list():
    server = McpServer()
    resp = await server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "wait_for_updates" in tool_names
    assert "post_message" in tool_names
    assert "claim_task" in tool_names
    assert "acquire_lock" in tool_names


@pytest.mark.asyncio
async def test_mcp_wait_for_updates_timeout(live_bus_url):
    server = McpServer(bus_url=live_bus_url)
    res = await server._wait_for_updates("claude", timeout=1)
    assert res["status"] == "timeout"


async def call_tool(server, name, arguments):
    response = await server.handle_request({
        "jsonrpc": "2.0", "id": 10, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


async def test_mcp_wait_for_updates_bus_unavailable(unavailable_bus_url):
    server = McpServer(bus_url=unavailable_bus_url)
    res = await call_tool(server, "wait_for_updates", {"agent_id": "claude", "timeout": 1})
    assert res["status"] == "error"
    assert res["error"]


async def test_mcp_message_tools_against_real_hub(live_bus_url):
    server = McpServer(bus_url=live_bus_url)
    sent = await call_tool(server, "post_message", {
        "from_agent": "claude", "to_agent": "reviewer", "text": "Please review",
        "reply_needed": True, "related_task": "T1",
    })
    inbox = await call_tool(server, "read_messages", {"agent_id": "reviewer"})
    assert len(inbox) == 1
    assert inbox[0]["message_id"] == sent["message_id"]
    assert inbox[0]["body"]["text"] == "Please review"
    assert inbox[0]["related_task"] == "T1"
    pending = await call_tool(server, "wait_for_updates", {"agent_id": "reviewer"})
    assert pending["status"] == "pending_messages"
    assert pending["messages"] == inbox


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
    assert status["locks"] == [lock]
    assert status["server"]["bus_version"]
    assert status["agents"] == []
    await call_tool(server, "release_lock", {"file_path": "a.py", "agent_id": "claude"})
    done = await call_tool(server, "complete_task", {"task_id": "T1", "agent_id": "claude"})
    assert done["status"] == "done"
    assert (await call_tool(server, "get_project_status", {}))["locks"] == []
