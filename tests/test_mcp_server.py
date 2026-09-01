from __future__ import annotations

import json
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
async def test_mcp_wait_for_updates_timeout(monkeypatch):
    server = McpServer(bus_url="http://localhost:8420")
    # Execute with 0s timeout or mocked fast timeout
    res = await server._wait_for_updates("claude", timeout=1)
    assert res["status"] in ("timeout", "pending_messages", "event_received")
