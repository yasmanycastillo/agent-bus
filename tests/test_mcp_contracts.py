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
    "bootstrap_agent": {},
    "my_pending_items": {},
    "get_agent_instructions": {},
    "prepare_edit": {"paths": ["module.py"], "operation_key": "edit-1"},
    "complete_handoff": {"task_id": "T1", "to_agent": "bob", "summary": "Ready", "operation_key": "handoff-1"},
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
    "register_capabilities": {"declared": ["code-review"]},
    "route_task": {"task_id": "T1"},
    "publish_artifact": {"task_id": "T1", "producer": "alice", "kind": "test-report", "content": "ok"},
    "list_task_artifacts": {"task_id": "T1"},
    "get_artifact_metadata": {"artifact_id": "artifact-1"},
    "record_verdict": {"task_id": "T1", "verdict": "approve", "sha": "abc"},
    "submit_instruction": {
        "instruction": "Corregir el cálculo del impuesto",
        "confirmed": True,
        "agents": [{"agent_id": "claude-01", "provider": "claude"}],
    },
    "assign_work": {
        "instruction_id": "ins-1", "assignee": "claude-01", "role": "implement", "title": "Corregir el cálculo",
    },
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


async def test_wait_for_updates_returns_before_common_60s_client_timeouts(bound_server):
    async with Client(bound_server.sdk_server()) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    timeout = tools["wait_for_updates"].input_schema["properties"]["timeout"]
    assert timeout["maximum"] == 50 and timeout["default"] == 50
    assert "50" in tools["wait_for_updates"].description


async def test_secure_tool_catalog_has_no_actor_input_and_forbids_extra_fields(bound_server):
    async with Client(bound_server.sdk_server()) as client:
        result = await client.list_tools()
    assert {tool.name for tool in result.tools} == TOOL_ARGUMENTS.keys()
    for tool in result.tools:
        schema = tool.input_schema
        assert schema["additionalProperties"] is False
        assert not {"agent_id", "from_agent", "decided_by"} & schema["properties"].keys()
        assert not {"agent_id", "from_agent", "decided_by"} & set(schema.get("required", []))


async def test_connection_teaches_onboarding_before_any_tool_call(bound_server, monkeypatch):
    calls = []
    async def execute(*args):
        calls.append(args)
        return {}
    monkeypatch.setattr(bound_server, "execute_tool", execute)
    async with Client(bound_server.sdk_server()) as client:
        assert "primera llamada debe ser bootstrap_agent({})" in client.instructions
        assert "my_pending_items" in client.instructions
        assert "prepare_edit" in client.instructions
        assert "complete_handoff" in client.instructions
        assert "MCP por sí solo no inicia" in client.instructions
        assert "worker start" in client.instructions
        assert "private-session-token" not in client.instructions
        assert calls == []


async def test_legacy_connection_explains_provisioning(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    monkeypatch.delenv("AGENT_BUS_SESSION_FILE", raising=False)
    async with Client(McpServer().sdk_server()) as client:
        assert "modo legacy" in client.instructions
        assert "credencial propia" in client.instructions
        assert "Reconecta MCP" in client.instructions


async def test_discovered_tools_explain_preconditions_and_next_steps(bound_server):
    async with Client(bound_server.sdk_server()) as client:
        tools = {tool.name: tool.description for tool in (await client.list_tools()).tools}
    assert "Primera llamada" in tools["bootstrap_agent"]
    assert "Requiere tarea libre" in tools["claim_task"]
    assert "prepare_edit" in tools["claim_task"]
    assert "authorized=true" in tools["prepare_edit"]
    assert "complete_handoff" in tools["prepare_edit"]
    assert "tokens vigentes" in tools["complete_handoff"]
    assert "detén la edición" in tools["renew_lock"]


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
    ("wait_for_updates", {"timeout": 51}),
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


@pytest.mark.parametrize("body", [
    {"error": "codex implements part of this instruction and cannot review it"},
    {"detail": "codex implements part of this instruction and cannot review it"},
])
async def test_hub_conflict_detail_reaches_the_tool_result_bounded(bound_server, monkeypatch, body):
    key = next(iter(body))
    body = {key: body[key] + " " + "x" * 2000}
    @asynccontextmanager
    async def backend():
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
            lambda request: httpx.Response(409, json=body),
        )) as client:
            yield client
    monkeypatch.setattr(bound_server, "_client", backend)
    async with Client(bound_server.sdk_server()) as client:
        result = await client.call_tool("read_messages", {})
    value = payload(result)
    assert (value["code"], value["http_status"]) == ("hub_error", 409)
    assert value["detail"].startswith("codex implements part of this instruction")
    assert len(value["detail"]) <= 300
    assert "HTTP 409" in value["error"]


@pytest.mark.parametrize("status", [401, 403, 500])
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
    assert value["guidance"]
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
    assert "claves" in payload(failed)["guidance"]
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
    assert "read_messages" in payload(result)["guidance"]


@pytest.mark.parametrize("name,arguments,status,expected", [
    ("prepare_edit", {"paths": ["a.py"], "operation_key": "edit"}, 409, "Detén la edición"),
    ("complete_handoff", {"task_id": "T1", "to_agent": "bob", "summary": "Ready", "operation_key": "handoff"}, 409, "resultado anterior"),
    ("claim_task", {"task_id": "T1"}, 409, "dependencias"),
    ("bootstrap_agent", {}, 401, "project_path"),
    ("bootstrap_agent", {}, 404, "hub incluya estas herramientas"),
    ("prepare_edit", {"paths": ["a.py"], "operation_key": "edit"}, 503, "misma clave"),
])
async def test_recovery_guidance_is_actionable_without_leaking_backend(bound_server, monkeypatch, name, arguments, status, expected):
    @asynccontextmanager
    async def backend():
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"error": "private-session-token private traceback"}),
        )) as client:
            yield client
    monkeypatch.setattr(bound_server, "_client", backend)
    async with Client(bound_server.sdk_server()) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    value = payload(result)
    assert expected in value["guidance"]
    if status in (404, 409):
        # Rule rejections carry the hub's explanation only in `detail`.
        assert value["detail"] == "private-session-token private traceback"
        assert "private" not in json.dumps({k: v for k, v in value.items() if k != "detail"})
    else:
        assert "private" not in json.dumps(value)


IDENTITY_FIELDS = {"agent_id", "from_agent", "decided_by"}
SESSION = {"agent_id": "alice", "session_id": "s", "project_id": "contracts",
           "role": "agent", "token": "t", "expires_at": time.time() + 3600}


@pytest.fixture
def legacy_server(monkeypatch):
    """MCP started before bootstrap_agent: no session yet, as in a global install."""
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    monkeypatch.delenv("AGENT_BUS_SESSION_FILE", raising=False)
    return McpServer(agent_id="alice")


@pytest.mark.parametrize("mode", ["bound", "legacy"])
async def test_every_tool_schema_matches_what_the_server_requires(mode, bound_server, legacy_server):
    server = bound_server if mode == "bound" else legacy_server
    async with Client(server.sdk_server()) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == TOOL_ARGUMENTS.keys()
    for tool in tools:
        schema = tool.input_schema
        required = set(schema.get("required", []))
        assert required <= schema["properties"].keys(), tool.name
        # Identity comes from the session (or --agent); it is never mandatory input.
        assert not IDENTITY_FIELDS & required, tool.name
        # The server validates exactly the published schema...
        assert server._validators[tool.name].schema == schema, tool.name
        # ...so the documented arguments, without identity, pass it.
        server._validators[tool.name].validate(TOOL_ARGUMENTS[tool.name])


def capture_backend(sent, response):
    @asynccontextmanager
    async def backend():
        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=response)
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
            yield client
    return backend


@pytest.mark.parametrize("tool_name,field", [
    ("submit_instruction", "agent_id"), ("complete_task", "agent_id"), ("claim_task", "agent_id"),
    ("record_verdict", "agent_id"), ("post_message", "from_agent"), ("record_decision", "decided_by"),
])
async def test_identity_omitted_after_bootstrap_comes_from_the_session(legacy_server, monkeypatch, tool_name, field):
    sent = []
    monkeypatch.setattr(legacy_server, "_client", capture_backend(sent, {"ok": True}))
    # bootstrap_agent adopts the project credential after the catalog was published.
    legacy_server.session = dict(SESSION)
    async with Client(legacy_server.sdk_server()) as client:
        result = await client.call_tool(tool_name, TOOL_ARGUMENTS[tool_name])
    assert not result.is_error, payload(result)
    assert sent[0][field] == "alice"


async def test_legacy_without_any_identity_reports_missing_agent_id(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    monkeypatch.delenv("AGENT_BUS_SESSION_FILE", raising=False)
    async with Client(McpServer().sdk_server()) as client:
        result = await client.call_tool("complete_task", {"task_id": "T1"})
    assert result.is_error
    assert payload(result)["code"] == "invalid_arguments"
    assert "agent_id" in payload(result)["error"]
