"""T08 client contracts: bounded reads and explicit, retryable mutations."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest
from click.testing import CliRunner

from agent_bus.cli import main as commands
from agent_bus.mcp.server import McpServer


@pytest.fixture
def mcp_transport(monkeypatch):
    def install(handler):
        @asynccontextmanager
        async def client(self, timeout=30):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as value:
                yield value
        monkeypatch.setattr(McpServer, "_client", client)
        return McpServer(agent_id="alice")
    return install


@pytest.mark.parametrize("tool,args", [
    ("post_message", {"from_agent": "alice", "to_agent": "bob", "text": "missing key"}),
    ("post_message", {"from_agent": "alice", "to_agent": "bob", "text": "bad key", "idempotency_key": ""}),
    ("post_message", {"from_agent": "alice", "to_agent": "bob", "text": "bad key", "idempotency_key": "x" * 129}),
    ("post_message", {"from_agent": "alice", "to_agent": "bob", "text": "bad flag", "idempotency_key": "key", "reply_needed": "false"}),
    ("read_messages", {"agent_id": "alice", "limit": 101}),
    ("read_messages", {"agent_id": "alice", "limit": True}),
    ("ack_messages", {"agent_id": "alice", "message_ids": []}),
    ("ack_messages", {"agent_id": "alice", "message_ids": ["m"] * 101}),
    ("reply_message", {"agent_id": "alice", "message_id": "m1", "text": "missing key"}),
])
async def test_invalid_tool_arguments_never_send(tool, args, mcp_transport):
    server = mcp_transport(lambda request: pytest.fail("Invalid arguments reached HTTP"))
    with pytest.raises(ValueError):
        await server.execute_tool(tool, args)


async def test_mcp_page_cursor_forwarded_without_ack(mcp_transport):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"message_id": "m1"}], "next_cursor": "opaque-next"})
    server = mcp_transport(handle)
    page = await server.execute_tool("read_messages", {"agent_id": "alice", "cursor": "opaque-current", "limit": 2})
    assert page["next_cursor"] == "opaque-next"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/inbox/alice/messages"
    assert dict(requests[0].url.params) == {"cursor": "opaque-current", "limit": "2"}


async def test_mcp_explicit_reply_and_ack_payloads(mcp_transport):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"acknowledged": ["m1"], "message_id": "reply", "conversation_id": "conversation"})
    server = mcp_transport(handle)
    reply = await server.execute_tool("reply_message", {
        "agent_id": "alice", "message_id": "m1", "text": "Done", "idempotency_key": "reply-key", "acknowledge": True,
    })
    assert reply["conversation_id"] == "conversation"
    assert requests[0].url.path == "/inbox/alice/m1/reply"
    assert json.loads(requests[0].content) == {
        "body": {"text": "Done"}, "idempotency_key": "reply-key", "reply_needed": False, "acknowledge": True,
    }
    await server.execute_tool("ack_messages", {"agent_id": "alice", "message_ids": ["m1"]})
    assert requests[1].url.path == "/inbox/alice/ack"
    assert json.loads(requests[1].content) == {"message_ids": ["m1"]}


async def test_mcp_wait_fetches_only_five_messages(mcp_transport):
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/events/cursor"):
            return httpx.Response(200, json={"cursor": "checkpoint"})
        return httpx.Response(200, json={"messages": [{"message_id": f"m{i}"} for i in range(5)], "next_cursor": "more"})
    result = await mcp_transport(handle)._wait_for_updates("alice", 1)
    assert result["count"] == 5
    assert result["next_cursor"] == "more"
    assert result["event_cursor"] == "checkpoint"
    assert "total_in_inbox" not in result
    assert len(requests) == 2
    assert requests[0].url.path == "/inbox/alice/events/cursor"
    assert requests[1].url.path == "/inbox/alice/messages"
    assert requests[1].url.params["limit"] == "5"


@pytest.fixture
def cli_transport(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "alice")
    def install(handler):
        monkeypatch.setattr(commands, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test"))
    return install


def test_cli_generated_key_is_visible_before_network_failure(cli_transport):
    attempted = []
    def fail(request):
        # Click captures stdout independently; the output after failure still
        # contains the exact generated key transmitted to the server.
        attempted.append(json.loads(request.content))
        raise httpx.ReadTimeout("ambiguous timeout", request=request)
    cli_transport(fail)
    result = CliRunner().invoke(commands.app, ["work", "msg", "bob", "Hello"])
    assert result.exit_code != 0
    key = attempted[0]["idempotency_key"]
    assert UUID(key)
    assert f"Clave de reintento: {key}" in result.output


def test_cli_reuses_supplied_key_and_surfaces_conflict(cli_transport):
    keys = []
    def handle(request):
        keys.append(json.loads(request.content)["idempotency_key"])
        return httpx.Response(409, json={"error": "Idempotency key reused with different content"})
    cli_transport(handle)
    result = CliRunner().invoke(commands.app, ["work", "msg", "bob", "Hello", "--idempotency-key", "same-key"])
    assert result.exit_code != 0
    assert keys == ["same-key"]
    assert "different content" in result.output


def test_cli_reply_and_ack_are_explicit(cli_transport):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"message_id": "reply-id", "acknowledged": ["m1", "m2"]})
    cli_transport(handle)
    runner = CliRunner()
    assert runner.invoke(commands.app, ["work", "reply", "m1", "Reviewed", "--idempotency-key", "reply-key", "--ack"]).exit_code == 0
    assert requests[0].url.path == "/inbox/alice/m1/reply"
    assert json.loads(requests[0].content)["acknowledge"] is True
    assert "from_agent" not in json.loads(requests[0].content)
    assert runner.invoke(commands.app, ["work", "ack", "m1", "m2"]).exit_code == 0
    assert json.loads(requests[1].content) == {"message_ids": ["m1", "m2"]}


def test_cli_read_keeps_messages_pending_and_prints_cursor(cli_transport):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"messages": [], "next_cursor": "opaque-next"})
    cli_transport(handle)
    result = CliRunner().invoke(commands.app, ["work", "inbox", "--cursor", "opaque-first", "--limit", "2"])
    assert result.exit_code == 0
    assert "opaque-next" in result.output
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert dict(requests[0].url.params) == {"limit": "2", "cursor": "opaque-first"}


@pytest.mark.parametrize("arguments", [["show", "dashboard"], ["top", "--once"], ["show", "inbox"]])
def test_dashboard_and_show_inbox_reads_are_bounded(cli_transport, arguments):
    inbox_requests = []
    def handle(request):
        path = request.url.path
        if path.startswith("/inbox/"):
            inbox_requests.append(request)
            assert path == "/inbox/alice/messages"
            return httpx.Response(200, json={"messages": [], "next_cursor": None})
        if path == "/status":
            return httpx.Response(200, json={"bus_version": "test", "agents_online": 0, "agents_total": 0})
        return httpx.Response(200, json=[])
    cli_transport(handle)
    result = CliRunner().invoke(commands.app, arguments)
    assert result.exit_code == 0, result.output
    assert len(inbox_requests) == 1
    assert int(inbox_requests[0].url.params["limit"]) <= 50


def test_cli_inbox_displays_complete_ids_for_reply(cli_transport):
    message_id = "12345678-1234-4321-abcd-123456789abc"
    cli_transport(lambda request: httpx.Response(200, json={
        "messages": [{"message_id": message_id, "from_agent": "bob", "message_type": "inbox", "body": {"text": "Please review"}}],
        "next_cursor": None,
    }))
    result = CliRunner().invoke(commands.app, ["work", "inbox"])
    assert result.exit_code == 0
    assert message_id in result.output


async def test_mcp_retry_keeps_caller_key_and_conflict_is_visible(mcp_transport):
    payloads = []
    def handle(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            raise httpx.ReadTimeout("Response lost", request=request)
        return httpx.Response(409, json={"error": "Same key, different content"})
    server = mcp_transport(handle)
    args = {"from_agent": "alice", "to_agent": "bob", "text": "Review", "idempotency_key": "stable-key"}
    with pytest.raises(httpx.ReadTimeout):
        await server.execute_tool("post_message", args)
    with pytest.raises(httpx.HTTPStatusError) as error:
        await server.execute_tool("post_message", args)
    assert error.value.response.status_code == 409
    assert payloads[0] == payloads[1]
    assert payloads[0]["idempotency_key"] == "stable-key"
