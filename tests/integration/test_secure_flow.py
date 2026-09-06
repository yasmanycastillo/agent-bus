"""Acceptance of local credentials across actual HTTP, MCP and CLI clients."""
from __future__ import annotations

import json

import httpx
from click.testing import CliRunner
from mcp import Client

from agent_bus.cli.main import app
from agent_bus.mcp.server import McpServer
from agent_bus.reputation.database import Database
from agent_bus.security import SessionStore, async_bus_client


async def tool(server, name, arguments):
    async with Client(server.sdk_server()) as client:
        response = await client.call_tool(name, arguments)
    assert not response.is_error, response
    result = json.loads(response.content[0].text)
    assert response.structured_content == result
    return result


async def test_secure_mcp_message_and_decision_identity(secure_bus, monkeypatch):
    alice = McpServer(bus_url=secure_bus.url, agent_id="alice")
    bob = McpServer(bus_url=secure_bus.url, agent_id="bob")
    # A second client's environment cannot change the first MCP connection's identity.
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(secure_bus.paths["bob"]))
    async with Client(alice.sdk_server()) as client:
        definitions = {definition.name: definition for definition in (await client.list_tools()).tools}
    assert "from_agent" not in definitions["post_message"].input_schema["properties"]
    assert "decided_by" not in definitions["record_decision"].input_schema["properties"]


    sent = await tool(alice, "post_message", {"to_agent": "bob", "text": "Review T1", "idempotency_key": "review-t1"})
    inbox = (await tool(bob, "read_messages", {}))["messages"]
    assert len(inbox) == 1
    assert inbox[0]["message_id"] == sent["message_id"]
    assert inbox[0]["from_agent"] == "alice"
    assert (await tool(bob, "wait_for_updates", {"timeout": 1}))["status"] == "pending_messages"
    decision = await tool(alice, "record_decision", {"title": "Storage", "what": "SQLite"})
    assert decision["decided_by"] == "alice"

    async with Client(alice.sdk_server()) as client:
        forged = await client.call_tool("post_message", {
            "from_agent": "human", "to_agent": "bob", "text": "Forged instruction", "idempotency_key": "forged",
        })
    assert forged.is_error
    assert forged.structured_content["code"] == "invalid_arguments"
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
    monkeypatch.setenv("AGENT_BUS_URL", secure_bus.url)
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
    async with Client(bob.sdk_server()) as client:
        denied = await client.call_tool("release_lock", {"file_path": "review.py"})
    assert denied.is_error
    assert denied.structured_content["code"] == "hub_error"
    status = await tool(bob, "get_project_status", {})
    assert status["locks"] == [lock]
    await tool(alice, "release_lock", {"file_path": "review.py"})
    assert (await tool(alice, "complete_task", {"task_id": "MCP-1"}))["status"] == "done"
    assert (await tool(alice, "get_project_status", {}))["locks"] == []
