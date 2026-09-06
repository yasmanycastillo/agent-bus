from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.types import Envelope, MessageType


@pytest.fixture
async def secured(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "0")
    bus = MessageBus(tmp_db, AgentRegistry(), InboxManager(tmp_db), project_id="test-project")
    sessions = {}
    for agent, role in (("alice", "agent"), ("bob", "agent"), ("lead", "admin"), ("all", "agent")):
        sessions[agent] = await bus.sessions.create(agent, role=role)
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        yield bus, client, sessions


def bearer(sessions, agent="alice"):
    return {"Authorization": f"Bearer {sessions[agent]['token']}"}


@pytest.mark.parametrize("path", ["/status", "/agents", "/tasks", "/locks", "/decisions", "/project/context", "/docs", "/openapi.json", "/inbox/alice", "/events/alice", "/events/all"])
async def test_private_get_requires_session(secured, path):
    _, client, _ = secured
    response = await client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_principal_and_public_health(secured):
    _, client, sessions = secured
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/room")).status_code == 200
    me = await client.get("/auth/me", headers=bearer(sessions))
    assert me.status_code == 200
    assert me.json()["agent_id"] == "alice"
    assert me.json()["project_id"] == "test-project"
    assert "token" not in me.json()
    for path in ("/status", "/tasks", "/locks", "/agents", "/decisions", "/skills/alice"):
        assert (await client.get(path, headers=bearer(sessions))).status_code == 200


@pytest.mark.parametrize("path,body", [
    ("/register", {"agent_id": "bob", "display_name": "Spoof"}),
    ("/messages", {"from_agent": "bob", "to_agent": "lead"}),
    ("/locks/acquire", {"file_path": "a.py", "agent_id": "bob"}),
    ("/locks/release", {"file_path": "a.py", "agent_id": "bob"}),
    ("/tasks/T1/claim", {"agent_id": "bob"}),
    ("/tasks/T1/done", {"agent_id": "bob"}),
    ("/tasks/T1/lock-files", {"agent_id": "bob", "files": []}),
    ("/decisions", {"decided_by": "bob"}),
    ("/agents/bob/heartbeat", {}),
    ("/agents/bob/active-work", {"work": {}}),
    ("/agents/all/heartbeat", {}),
])
async def test_actor_spoof_denied(secured, path, body):
    _, client, sessions = secured
    assert (await client.post(path, json=body, headers=bearer(sessions))).status_code == 403


@pytest.mark.parametrize("path,body", [
    ("/skills/alice", {"role": "admin"}),
    ("/project/context", {"field": "owner", "value": "alice"}),
    ("/kickoff/start", {}),
    ("/tasks/T1/reassign", {"new_owner": "alice"}),
    ("/room/api/message", {"text": "Forged human"}),
    ("/room/api/approve", {"decision": "approve"}),
    ("/room/api/assign", {"task_id": "T1", "agent_id": "alice"}),
])
async def test_agent_cannot_administer(secured, path, body):
    _, client, sessions = secured
    assert (await client.post(path, json=body, headers=bearer(sessions))).status_code == 403


async def test_inbox_is_private_including_agent_named_all(secured):
    bus, client, sessions = secured
    sent = await client.post("/messages", json={"to_agent": "bob", "body": {"text": "Private"}}, headers=bearer(sessions))
    assert sent.status_code == 200
    message_id = sent.json()["message_id"]
    for path in ("/inbox/bob", "/inbox/bob/pending", f"/inbox/bob/{message_id}", "/inbox/all", "/events/bob", "/events/all", "/room/api/overview", "/docs"):
        assert (await client.get(path, headers=bearer(sessions))).status_code == 403
    assert (await client.post(f"/inbox/bob/{message_id}/archive", headers=bearer(sessions))).status_code == 403
    inbox = await client.get("/inbox/bob", headers=bearer(sessions, "bob"))
    assert inbox.json()[0]["from_agent"] == "alice"
    assert (await client.post(f"/inbox/bob/{message_id}/archive", headers=bearer(sessions, "bob"))).status_code == 200
    assert await bus.inbox.get_inbox("bob") == []


async def test_owner_changes_and_audit(secured):
    bus, client, sessions = secured
    assert (await client.post("/tasks", json={"task_id": "T1", "title": "Task", "owner": "bob"}, headers=bearer(sessions))).status_code == 403
    await client.post("/tasks", json={"task_id": "T1", "title": "Task"}, headers=bearer(sessions))
    assert (await client.post("/tasks/T1/claim", json={}, headers=bearer(sessions))).status_code == 200
    for endpoint, body in (("done", {}), ("lock-files", {"files": ["x.py"]}), ("handoff", {"to_agent": "bob"})):
        assert (await client.post(f"/tasks/T1/{endpoint}", json=body, headers=bearer(sessions, "bob"))).status_code == 403
    assert (await client.post("/tasks/T1/lock-files", json={"files": ["x.py"]}, headers=bearer(sessions))).status_code == 200
    response = await client.post("/tasks/T1/reassign", json={"new_owner": "bob"}, headers=bearer(sessions, "lead"))
    assert response.status_code == 200
    assert (await client.post("/tasks/T1/done", headers=bearer(sessions))).status_code == 403
    audit = await bus.db.conn.execute_fetchall("SELECT * FROM audit_log WHERE task_id = 'T1'")
    assert len(audit) == 1
    assert audit[0]["previous_owner"] == "alice"
    assert audit[0]["new_owner"] == "bob"
    assert audit[0]["actor_agent_id"] == "lead"
    assert audit[0]["actor_session_id"] == sessions["lead"]["session_id"]
    assert (await client.post("/tasks/T1/done", headers=bearer(sessions, "bob"))).status_code == 200
    assert (await client.post("/tasks/T1/done", headers=bearer(sessions, "bob"))).status_code == 409
    assert (await client.post("/tasks/T1/reassign", json={"new_owner": "alice"}, headers=bearer(sessions, "lead"))).status_code == 409
    assert len(await bus.db.conn.execute_fetchall("SELECT * FROM audit_log")) == 1


async def test_handoff_and_room_actor(secured):
    bus, client, sessions = secured
    await bus.tasks.create("T1", "Task")
    await bus.tasks.claim("T1", "alice")
    handed = await client.post("/tasks/T1/handoff", json={"to_agent": "bob"}, headers=bearer(sessions))
    assert handed.status_code == 200
    assert handed.json()["task"]["owner"] == "bob"
    assert (await bus.inbox.get_inbox("bob"))[0].from_agent == "alice"
    response = await client.post("/room/api/message", json={"to_agent": "bob", "text": "Admin message"}, headers=bearer(sessions, "lead"))
    assert response.status_code == 200
    assert (await bus.inbox.get_message("bob", response.json()["message_id"])).from_agent == "lead"
    assert (await client.post("/messages", json={"from_agent": "human", "to_agent": "bob"}, headers=bearer(sessions, "lead"))).status_code == 403


async def test_valid_session_revocation_and_invalid_legacy_credentials(secured, monkeypatch):
    bus, client, sessions = secured
    await bus.sessions.revoke(sessions["alice"]["session_id"])
    assert (await client.get("/tasks", headers=bearer(sessions))).status_code == 401
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    assert (await client.get("/tasks")).status_code == 200
    assert (await client.get("/tasks", headers={"Authorization": "Bearer bogus"})).status_code == 401


async def test_transfer_audit_failure_rolls_back_owner(secured):
    bus, _, sessions = secured
    await bus.tasks.create("T1", "Task", owner="alice")
    await bus.db.conn.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
    await bus.db.conn.commit()
    with pytest.raises(Exception, match="audit unavailable"):
        await bus.tasks.transfer("T1", "bob", actor="lead", session_id=sessions["lead"]["session_id"])
    assert (await bus.tasks.get("T1")).owner == "alice"
    assert await bus.db.conn.execute_fetchall("SELECT * FROM audit_log") == []


async def test_owner_checked_at_sql_execution_not_preflight(secured, monkeypatch):
    bus, client, sessions = secured
    await bus.tasks.create("T1", "Task")
    await bus.tasks.claim("T1", "alice")
    original = bus.tasks.complete

    async def reassigned_before_update(task_id, actor=None):
        await bus.tasks.transfer(task_id, "bob", actor="lead", session_id=sessions["lead"]["session_id"])
        return await original(task_id, actor)

    monkeypatch.setattr(bus.tasks, "complete", reassigned_before_update)
    assert (await client.post("/tasks/T1/done", headers=bearer(sessions))).status_code == 403
    task = await bus.tasks.get("T1")
    assert task.owner == "bob"
    assert task.status.value == "in_progress"


async def test_websocket_authentication_and_sender(secured):
    bus, _, sessions = secured
    with TestClient(bus.app) as client:
        for path, headers in (("/ws/alice", {}), ("/ws/bob", bearer(sessions)), ("/ws/alice", {"Authorization": "Bearer bogus"})):
            with pytest.raises(WebSocketDisconnect) as error:
                with client.websocket_connect(path, headers=headers):
                    pass
            assert error.value.code == 1008
        with client.websocket_connect("/ws/alice", headers=bearer(sessions)) as ws:
            ws.send_json({"from_agent": "alice", "to_agent": "bob", "message_type": "inbox", "body": {"text": "Hello"}})
            assert ws.receive_json()["type"] == "delivered"
            ws.send_json({"from_agent": "bob", "to_agent": "lead", "message_type": "inbox"})
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1008
    assert (await bus.inbox.get_inbox("bob"))[0].from_agent == "alice"
    assert await bus.inbox.get_inbox("lead") == []


async def test_websocket_revocation_blocks_outbound(secured):
    bus, _, sessions = secured
    with TestClient(bus.app) as client:
        with client.websocket_connect("/ws/alice", headers=bearer(sessions)) as ws:
            client.portal.call(bus.sessions.revoke, sessions["alice"]["session_id"])
            envelope = Envelope(from_agent="bob", to_agent="alice", message_type=MessageType.INBOX, body={"text": "Secret after revoke"})
            client.portal.call(bus._push_to_agent, "alice", envelope)
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1008


@pytest.mark.parametrize("invalidate", ["revoke", "expire"])
async def test_sse_revocation_closes_stream_without_new_delivery(secured, invalidate):
    bus, _, sessions = secured
    sent = []
    started = asyncio.Event()
    delivered = asyncio.Event()
    disconnect = asyncio.Event()
    first_request = True

    async def receive():
        nonlocal first_request
        if first_request:
            first_request = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.start":
            started.set()
        if b"event: message" in message.get("body", b""):
            delivered.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/events/alice", "raw_path": b"/events/alice",
             "query_string": b"", "headers": [(b"authorization", bearer(sessions)["Authorization"].encode())],
             "server": ("test", 80), "client": ("test", 123), "root_path": ""}
    task = asyncio.create_task(bus.app(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert sent[0]["status"] == 200
        event = Envelope(from_agent="bob", to_agent="alice", message_type=MessageType.INBOX, body={"text": "Before revoke"})
        await bus.inbox.deliver(event)
        await bus._push_to_agent("alice", event)
        await asyncio.wait_for(delivered.wait(), 2)
        if invalidate == "revoke":
            await bus.sessions.revoke(sessions["alice"]["session_id"])
        else:
            await bus.db.conn.execute("UPDATE sessions SET expires_at = 0 WHERE session_id = ?", (sessions["alice"]["session_id"],))
            await bus.db.conn.commit()
        event = Envelope(from_agent="bob", to_agent="alice", message_type=MessageType.INBOX, body={"text": "After revoke"})
        await bus.inbox.deliver(event)
        await bus._push_to_agent("alice", event)
        await asyncio.wait_for(task, 2)
    finally:
        disconnect.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    body = b"".join(message.get("body", b"") for message in sent)
    assert b"Before revoke" in body
    assert b"After revoke" not in body
    assert not bus._sse_subscribers["alice"]


async def test_transfer_update_failure_rolls_back_audit(secured):
    bus, _, sessions = secured
    await bus.tasks.create("T1", "Task", owner="alice")
    await bus.db.conn.execute("""CREATE TRIGGER fail_owner BEFORE UPDATE OF owner ON tasks
        BEGIN SELECT RAISE(ABORT, 'owner update unavailable'); END""")
    await bus.db.conn.commit()
    with pytest.raises(Exception, match="owner update unavailable"):
        await bus.tasks.transfer("T1", "bob", actor="lead", session_id=sessions["lead"]["session_id"])
    assert (await bus.tasks.get("T1")).owner == "alice"
    assert await bus.db.conn.execute_fetchall("SELECT * FROM audit_log") == []


async def test_shared_connection_transfer_audit_and_other_writers(secured):
    bus, _, sessions = secured
    for index in range(10):
        await bus.tasks.create(f"T{index}", "Task", owner="alice")
    results = await asyncio.gather(*[
        bus.tasks.transfer(f"T{index}", "bob", actor="lead", session_id=sessions["lead"]["session_id"])
        if index % 2 else bus.tasks.complete(f"T{index}")
        for index in range(10)
    ])
    assert all(result is not None for result in results)
    audit = await bus.db.conn.execute_fetchall("SELECT * FROM audit_log")
    assert len(audit) == 5
    assert all(row["previous_owner"] == "alice" and row["new_owner"] == "bob" for row in audit)


async def test_own_presence_and_decision_actor(secured):
    _, client, sessions = secured
    response = await client.post("/register", json={"agent_id": "alice", "display_name": "Alice"}, headers=bearer(sessions))
    assert response.status_code == 201
    assert (await client.post("/agents/alice/heartbeat", headers=bearer(sessions))).status_code == 200
    assert (await client.post("/agents/alice/active-work", json={"work": {"task": "T1"}}, headers=bearer(sessions))).status_code == 200
    response = await client.post("/decisions", json={"decision_id": "D1", "title": "Title", "context": "Context", "decision": "Decision"}, headers=bearer(sessions))
    assert response.status_code == 200
    assert response.json()["decided_by"] == "alice"


async def test_decision_id_is_immutable_across_actors(secured):
    bus, client, sessions = secured
    original = {"decision_id": "D1", "title": "Original", "context": "Context", "decision": "Keep this"}
    first = await client.post("/decisions", json=original, headers=bearer(sessions, "bob"))
    assert first.status_code == 200
    duplicate = dict(original, title="Overwrite", supersedes="unrelated")
    second = await client.post("/decisions", json=duplicate, headers=bearer(sessions))
    assert second.status_code == 409
    assert (await bus.decisions.get("D1")).model_dump(mode="json") == first.json()


async def test_assignment_states_remain_executable(secured):
    bus, client, sessions = secured
    assigned = await client.post("/tasks", json={"task_id": "T1", "title": "Self assignment", "owner": "alice"}, headers=bearer(sessions))
    assert assigned.status_code == 200
    assert assigned.json()["status"] == "in_progress"
    released = await client.post("/tasks/T1/reassign", json={"new_owner": "free"}, headers=bearer(sessions, "lead"))
    assert released.status_code == 200
    assert released.json()["status"] == "pending"
    claimed = await client.post("/tasks/T1/claim", json={}, headers=bearer(sessions, "bob"))
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "in_progress"
    handed_back = await client.post("/tasks/T1/handoff", json={"to_agent": "free"}, headers=bearer(sessions, "bob"))
    assert handed_back.status_code == 422
    assert (await bus.tasks.get("T1")).owner == "bob"
    assert (await bus.tasks.get("T1")).status.value == "in_progress"
    assert await bus.inbox.get_inbox("free") == []
