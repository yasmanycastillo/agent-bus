"""Acceptance of local credentials across actual HTTP, MCP and CLI clients."""
from __future__ import annotations

import json

import httpx
from click.testing import CliRunner

from agent_bus.cli.main import app
from agent_bus.mcp.server import McpServer
from agent_bus.reputation.database import Database
from agent_bus.security import SessionStore, async_bus_client


async def tool(server, name, arguments):
    response = await server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert "error" not in response, response
    assert not response["result"].get("isError"), response
    return json.loads(response["result"]["content"][0]["text"])


async def test_secure_mcp_message_and_decision_identity(secure_bus, monkeypatch):
    alice = McpServer(bus_url=secure_bus.url, agent_id="alice")
    bob = McpServer(bus_url=secure_bus.url, agent_id="bob")
    # A second client's environment cannot change the first MCP connection's identity.
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(secure_bus.paths["bob"]))
    listed = await alice.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    definitions = {definition["name"]: definition for definition in listed["result"]["tools"]}
    assert "from_agent" not in definitions["post_message"]["inputSchema"]["properties"]
    assert "decided_by" not in definitions["record_decision"]["inputSchema"]["properties"]

    sent = await tool(alice, "post_message", {"to_agent": "bob", "text": "Review T1", "idempotency_key": "review-t1"})
    inbox = (await tool(bob, "read_messages", {}))["messages"]
    assert len(inbox) == 1
    assert inbox[0]["message_id"] == sent["message_id"]
    assert inbox[0]["from_agent"] == "alice"
    assert (await tool(bob, "wait_for_updates", {"timeout": 1}))["status"] == "pending_messages"
    decision = await tool(alice, "record_decision", {"title": "Storage", "what": "SQLite"})
    assert decision["decided_by"] == "alice"

    forged = await alice.handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "post_message", "arguments": {
            "from_agent": "human", "to_agent": "bob", "text": "Forged instruction",
        }},
    })
    assert "error" in forged or forged.get("result", {}).get("isError")
    assert len((await tool(bob, "read_messages", {}))["messages"]) == 1


async def test_secure_task_reassignment_revokes_previous_owner(secure_bus):
    async with (
        async_bus_client(agent_id="human", base_url=secure_bus.url) as admin,
        async_bus_client(agent_id="alice", base_url=secure_bus.url) as alice,
        async_bus_client(agent_id="bob", base_url=secure_bus.url) as bob,
    ):
        response = await admin.post("/tasks", json={"task_id": "T1", "title": "Review"})
        assert response.status_code == 200
        assert (await alice.post("/tasks/T1/claim", json={"agent_id": "alice"})).status_code == 200
        assert (await bob.post("/tasks/T1/done", json={"agent_id": "bob"})).status_code in (403, 409)
        assert (await alice.post("/tasks/T1/reassign", json={"new_owner": "bob"})).status_code == 403
        assert (await admin.post("/tasks/T1/reassign", json={"new_owner": "bob"})).status_code == 200
        assert (await alice.post("/tasks/T1/done", json={"agent_id": "alice"})).status_code in (403, 409)
        assert (await bob.post("/tasks/T1/done", json={"agent_id": "bob"})).status_code == 200
        assert (await alice.get("/inbox/bob")).status_code == 403
        assert (await alice.get("/room/api/overview")).status_code == 403


async def test_secure_credentials_persist_and_revoke(secure_bus):
    # Reopening storage uses the same persisted trust; no in-memory registration required.
    db = Database(secure_bus.db_path)
    await db.initialize()
    try:
        store = SessionStore(db, project_id="acceptance-project")
        session = secure_bus.sessions["alice"]
        assert (await store.authenticate(session["token"])).agent_id == "alice"
        async with async_bus_client(agent_id="alice", base_url=secure_bus.url) as alice:
            assert (await alice.get("/status")).status_code == 200
            assert await store.revoke(session["session_id"])
            assert (await alice.get("/status")).status_code == 401
    finally:
        await db.close()


def test_secure_cli_uses_bound_identity(secure_bus, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "alice")
    monkeypatch.setattr("agent_bus.cli.main.DEFAULT_URL", secure_bus.url)
    result = CliRunner().invoke(app, ["work", "msg", "bob", "CLI message"])
    assert result.exit_code == 0, result.output
    with httpx.Client(base_url=secure_bus.url, headers={
        "Authorization": f"Bearer {secure_bus.sessions['bob']['token']}",
    }) as bob:
        messages = bob.get("/inbox/bob").json()
    assert len(messages) == 1
    assert messages[0]["from_agent"] == "alice"
    assert messages[0]["body"]["text"] == "CLI message"


async def test_secure_mcp_task_and_lock_lifecycle(secure_bus):
    alice = McpServer(bus_url=secure_bus.url, agent_id="alice")
    bob = McpServer(bus_url=secure_bus.url, agent_id="bob")
    async with async_bus_client("human", base_url=secure_bus.url) as admin:
        response = await admin.post("/tasks", json={"task_id": "MCP-1", "title": "Review"})
        response.raise_for_status()
    claimed = await tool(alice, "claim_task", {"task_id": "MCP-1"})
    assert (claimed["owner"], claimed["status"]) == ("alice", "in_progress")
    lock = await tool(alice, "acquire_lock", {"file_path": "review.py", "reason": "MCP review"})
    assert lock["locked_by"] == "alice"
    denied = await bob.handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "release_lock", "arguments": {"file_path": "review.py"}},
    })
    assert "error" in denied or denied.get("result", {}).get("isError")
    status = await tool(bob, "get_project_status", {})
    assert status["locks"] == [lock]
    await tool(alice, "release_lock", {"file_path": "review.py"})
    assert (await tool(alice, "complete_task", {"task_id": "MCP-1"}))["status"] == "done"
    assert (await tool(alice, "get_project_status", {}))["locks"] == []
