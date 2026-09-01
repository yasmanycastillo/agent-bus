from __future__ import annotations

import os
import tempfile

import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
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
async def client(bus_app: MessageBus):
    transport = ASGITransport(app=bus_app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_register_agent(client: AsyncClient):
    resp = await client.post(
        "/register",
        json={
            "agent_id": "claude",
            "display_name": "Claude",
            "capabilities": ["code", "review"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_id"] == "claude"
    assert "code" in data["capabilities"]


async def test_register_duplicate(client: AsyncClient):
    await client.post(
        "/register",
        json={"agent_id": "claude", "display_name": "Claude"},
    )
    resp = await client.post(
        "/register",
        json={"agent_id": "claude", "display_name": "Claude"},
    )
    assert resp.status_code == 409


async def test_list_agents(client: AsyncClient):
    await client.post("/register", json={"agent_id": "a", "display_name": "A"})
    await client.post("/register", json={"agent_id": "b", "display_name": "B"})
    resp = await client.get("/agents")
    assert len(resp.json()) == 2


async def test_send_direct_message(client: AsyncClient):
    await client.post("/register", json={"agent_id": "claude", "display_name": "Claude"})
    await client.post("/register", json={"agent_id": "codex", "display_name": "Codex"})

    resp = await client.post(
        "/messages",
        json={
            "from_agent": "claude",
            "to_agent": "codex",
            "message_type": "inbox",
            "body": {"text": "Review this"},
            "reply_needed": True,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "delivered"
    assert "message_id" in data


async def test_send_broadcast(client: AsyncClient):
    await client.post("/register", json={"agent_id": "a", "display_name": "A"})
    await client.post("/register", json={"agent_id": "b", "display_name": "B"})
    await client.post("/register", json={"agent_id": "c", "display_name": "C"})

    resp = await client.post(
        "/messages",
        json={
            "from_agent": "a",
            "message_type": "broadcast",
            "body": {"text": "Heads up everyone"},
        },
    )
    data = resp.json()
    assert data["status"] == "broadcast"
    assert len(data["message_ids"]) == 2  # b and c, not a


async def test_get_inbox(client: AsyncClient):
    await client.post("/register", json={"agent_id": "claude", "display_name": "Claude"})
    await client.post("/register", json={"agent_id": "codex", "display_name": "Codex"})

    await client.post(
        "/messages",
        json={
            "from_agent": "claude",
            "to_agent": "codex",
            "body": {"text": "msg1"},
        },
    )
    await client.post(
        "/messages",
        json={
            "from_agent": "claude",
            "to_agent": "codex",
            "body": {"text": "msg2"},
        },
    )

    resp = await client.get("/inbox/codex")
    messages = resp.json()
    assert len(messages) == 2


async def test_archive_message(client: AsyncClient):
    await client.post("/register", json={"agent_id": "claude", "display_name": "Claude"})
    await client.post("/register", json={"agent_id": "codex", "display_name": "Codex"})

    resp = await client.post(
        "/messages",
        json={"from_agent": "claude", "to_agent": "codex", "body": {"text": "test"}},
    )
    msg_id = resp.json()["message_id"]

    resp = await client.post(f"/inbox/codex/{msg_id}/archive")
    assert resp.json()["status"] == "archived"

    resp = await client.get("/inbox/codex")
    assert len(resp.json()) == 0


async def test_status_endpoint(client: AsyncClient):
    resp = await client.get("/status")
    data = resp.json()
    assert data["bus_version"] == "0.1.0"
    assert data["agents_total"] == 0


async def test_get_message_not_found(client: AsyncClient):
    resp = await client.get("/inbox/nonexistent/does-not-exist")
    assert resp.status_code == 404


async def test_pending_inbox_empty(client: AsyncClient):
    await client.post("/register", json={"agent_id": "claude", "display_name": "Claude"})
    resp = await client.get("/inbox/claude/pending")
    data = resp.json()
    assert data["count"] == 0
    assert data["reply_needed"] == 0


async def test_pending_inbox_with_messages(client: AsyncClient):
    await client.post("/register", json={"agent_id": "claude", "display_name": "Claude"})
    await client.post("/register", json={"agent_id": "codex", "display_name": "Codex"})

    await client.post(
        "/messages",
        json={
            "from_agent": "codex",
            "to_agent": "claude",
            "body": {"text": "Need review"},
            "reply_needed": True,
        },
    )
    await client.post(
        "/messages",
        json={
            "from_agent": "codex",
            "to_agent": "claude",
            "body": {"text": "FYI update"},
        },
    )

    resp = await client.get("/inbox/claude/pending")
    data = resp.json()
    assert data["count"] == 2
    assert data["reply_needed"] == 1
    assert "codex" in data["latest_senders"]
    assert len(data["latest_summary"]) == 2


async def test_signature_middleware_valid_and_invalid(client: AsyncClient, tmp_path, monkeypatch):
    from agent_bus.worker.auth import WorkerAuth

    auth = WorkerAuth(keys_dir=tmp_path / "agents")
    headers = auth.sign_operation("claude", "POST", "/tasks", {"task_id": "T99", "title": "Signed task"})

    # 1. Valid signature
    resp = await client.post("/tasks", json={"task_id": "T99", "title": "Signed task"}, headers=headers)
    assert resp.status_code in (200, 201)

    # 2. Tampered signature -> 401
    bad_headers = dict(headers)
    bad_headers["X-Agent-Signature"] = "00" * 64
    resp_bad = await client.post("/tasks", json={"task_id": "T100", "title": "Bad task"}, headers=bad_headers)
    assert resp_bad.status_code == 401
    assert "Invalid Ed25519 signature" in resp_bad.json()["error"]

    # 3. Enforce signatures (AGENT_BUS_ALLOW_UNSIGNED=0) -> missing headers rejected with 401
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "0")
    resp_no_sig = await client.post("/tasks", json={"task_id": "T101", "title": "No sig"})
    assert resp_no_sig.status_code == 401
