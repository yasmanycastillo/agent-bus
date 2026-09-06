"""Public MCP contracts exercised through the official SDK's memory transport."""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from agent_bus.mcp.server import McpServer


TOOL_ARGUMENTS = {
    "wait_for_updates": {"timeout": 1},
    "post_message": {"to_agent": "bob", "text": "Review", "idempotency_key": "contract-message"},
    "read_messages": {},
    "ack_messages": {"message_ids": ["message"]},
    "reply_message": {"message_id": "message", "text": "Reviewed", "idempotency_key": "contract-reply"},
    "claim_task": {"task_id": "T1"},
    "complete_task": {"task_id": "T1"},
    "acquire_lock": {"file_path": "module.py"},
    "release_lock": {"file_path": "module.py", "acquisition_id": "token"},
    "renew_lock": {"file_path": "module.py", "acquisition_id": "token"},
    "get_project_status": {},
    "record_decision": {"title": "Storage", "what": "Use SQLite"},
}


@pytest.fixture
def bound_server(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "0")
    monkeypatch.setattr("agent_bus.mcp.server.load_session", lambda agent_id: {
        "agent_id": "alice", "session_id": "session-alice", "project_id": "contracts",
        "role": "agent", "token": "private-session-token", "expires_at": time.time() + 3600,
    })
    return McpServer(agent_id="alice")


def payload(result):
    value = json.loads(result.content[0].text)
    assert result.structured_content == value
    return value


async def test_secure_tool_catalog_has_no_actor_input_and_forbids_extra_fields(bound_server):
    async with Client(bound_server.sdk_server()) as client:
        result = await client.list_tools()
    assert {tool.name for tool in result.tools} == TOOL_ARGUMENTS.keys()
    for tool in result.tools:
        schema = tool.input_schema
        assert schema["additionalProperties"] is False
        assert not {"agent_id", "from_agent", "decided_by"} & schema["properties"].keys()
        assert not {"agent_id", "from_agent", "decided_by"} & set(schema.get("required", []))


async def test_unknown_tool_is_protocol_error_and_connection_remains_usable(bound_server):
    async with Client(bound_server.sdk_server()) as client:
        with pytest.raises(MCPError) as error:
            await client.call_tool("nonexistent_tool", {})
        assert error.value.code == -32602
        assert "Unknown tool" in error.value.message
        assert (await client.list_tools()).tools


@pytest.mark.parametrize("tool_name", TOOL_ARGUMENTS)
async def test_all_tools_reject_undeclared_fields_before_dispatch(bound_server, monkeypatch, tool_name):
    called = []
    async def execute(*args):
        called.append(args)
        return {"unexpected_dispatch": True}
    monkeypatch.setattr(bound_server, "execute_tool", execute)
    async with Client(bound_server.sdk_server()) as client:
        result = await client.call_tool(tool_name, {**TOOL_ARGUMENTS[tool_name], "undeclared": True})
    assert result.is_error
    assert payload(result)["code"] == "invalid_arguments"
    assert called == []


@pytest.mark.parametrize("tool_name", TOOL_ARGUMENTS)
async def test_even_matching_actor_is_not_public_tool_input(bound_server, monkeypatch, tool_name):
    calls = []
    async def execute(*args):
        calls.append(args)
        return {}
    monkeypatch.setattr(bound_server, "execute_tool", execute)
    async with Client(bound_server.sdk_server()) as client:
        result = await client.call_tool(tool_name, {**TOOL_ARGUMENTS[tool_name], "agent_id": "alice"})
    assert result.is_error
    assert payload(result)["code"] == "invalid_arguments"
    assert calls == []


@pytest.mark.parametrize("tool_name,args", [
    ("wait_for_updates", {"timeout": True}),
    ("wait_for_updates", {"timeout": 121}),
    ("post_message", {"to_agent": "bob", "text": "Missing key"}),
    ("read_messages", {"limit": 101}),
    ("ack_messages", {"message_ids": []}),
    ("claim_task", {"task_id": 23}),
    ("acquire_lock", {"file_path": None}),
    ("record_decision", {"title": "Title", "what": False}),
])
async def test_invalid_tool_arguments_are_execution_errors_not_protocol_errors(bound_server, tool_name, args):
    async with Client(bound_server.sdk_server()) as client:
        result = await client.call_tool(tool_name, args)
        assert result.is_error
        assert payload(result)["code"] == "invalid_arguments"
        assert (await client.list_tools()).tools


@pytest.mark.parametrize("status", [401, 403, 409, 500])
async def test_backend_http_errors_are_sanitized_tool_results(bound_server, monkeypatch, status):
    sensitive_body = {"error": "private-session-token backend stack trace"}
    @asynccontextmanager
    async def backend():
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=sensitive_body),
        )) as client:
            yield client
    monkeypatch.setattr(bound_server, "_client", backend)
    async with Client(bound_server.sdk_server()) as client:
        result = await client.call_tool("read_messages", {})
    assert result.is_error
    value = payload(result)
    assert value["code"] == "hub_error"
    assert value["http_status"] == status
    assert "private-session-token" not in result.content[0].text
    assert "stack trace" not in result.content[0].text


async def test_internal_exception_is_not_leaked_and_next_tool_succeeds(bound_server, monkeypatch):
    first = True
    async def execute(name, arguments):
        nonlocal first
        if first:
            first = False
            raise RuntimeError("private-session-token private database path")
        return {"messages": [], "next_cursor": None}
    monkeypatch.setattr(bound_server, "execute_tool", execute)
    async with Client(bound_server.sdk_server()) as client:
        failed = await client.call_tool("read_messages", {})
        succeeded = await client.call_tool("read_messages", {})
    assert failed.is_error
    assert payload(failed)["code"] == "internal_error"
    assert "private" not in failed.content[0].text
    assert not succeeded.is_error
    assert payload(succeeded) == {"messages": [], "next_cursor": None}


async def test_wait_business_error_sets_tool_error_flag(bound_server, monkeypatch):
    async def execute(name, args):
        return {"status": "error", "code": "cursor_expired", "recovery": "read_messages", "event_cursor": "fresh"}
    monkeypatch.setattr(bound_server, "execute_tool", execute)
    async with Client(bound_server.sdk_server()) as client:
        result = await client.call_tool("wait_for_updates", {"timeout": 1})
    assert result.is_error
    assert payload(result)["code"] == "cursor_expired"
    assert payload(result)["event_cursor"] == "fresh"
