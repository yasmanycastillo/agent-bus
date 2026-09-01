"""Tests de integración del War Room (T19): UI + endpoints /room/api."""

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


async def _register(client, agent_id):
    r = await client.post("/register", json={"agent_id": agent_id, "display_name": agent_id})
    assert r.status_code == 201


async def test_room_ui_sirve_html(client):
    r = await client.get("/room")
    assert r.status_code == 200
    assert "War Room" in r.text
    assert "EventSource" in r.text  # feed SSE conectado
    assert "/room/api/overview" in r.text


async def test_overview_snapshot(client):
    await _register(client, "claude")
    await client.post("/tasks", json={"task_id": "W1", "title": "tarea room"})
    r = await client.get("/room/api/overview")
    assert r.status_code == 200
    d = r.json()
    assert any(t["task_id"] == "W1" for t in d["tasks"])
    assert any(a["agent_id"] == "claude" for a in d["agents"])
    assert d["pending_approvals"] == 0


async def test_assign_notifica_al_agente(client):
    await _register(client, "claude")
    await client.post("/tasks", json={"task_id": "W2", "title": "asignable"})
    r = await client.post("/room/api/assign", json={"task_id": "W2", "agent_id": "claude"})
    assert r.status_code == 200
    assert r.json()["status"] == "assigned"
    # la tarea queda in_progress con owner
    task = (await client.get("/tasks/W2")).json()
    assert task["owner"] == "claude"
    assert task["status"] == "in_progress"
    # y al agente le llegó notificación reply_needed
    msgs = (await client.get("/inbox/claude")).json()
    notif = [m for m in msgs if m.get("related_task") == "W2"]
    assert notif and notif[0]["reply_needed"] is True


async def test_assign_tarea_inexistente_404(client):
    r = await client.post("/room/api/assign", json={"task_id": "X9", "agent_id": "claude"})
    assert r.status_code == 404


async def test_message_directo_y_broadcast(client):
    await _register(client, "claude")
    await _register(client, "agy")
    # directo
    r = await client.post("/room/api/message", json={"to_agent": "claude", "text": "hola"})
    assert r.json()["status"] == "delivered"
    # broadcast llega a todos los agentes registrados
    r = await client.post("/room/api/message", json={"to_agent": "*", "text": "equipo"})
    assert r.json()["status"] == "broadcast"
    assert len(r.json()["message_ids"]) == 2


async def test_approve_flujo_aprobacion(client):
    await _register(client, "agy")
    # agy pide aprobación al humano
    await client.post(
        "/messages",
        json={
            "from_agent": "agy",
            "to_agent": "human",
            "message_type": "inbox",
            "body": {"text": "¿Puedo pushear a main?"},
            "reply_needed": True,
        },
    )
    aps = (await client.get("/room/api/pending-approvals")).json()
    assert len(aps) == 1
    # el humano aprueba desde el room
    r = await client.post(
        "/room/api/approve",
        json={"message_id": aps[0]["message_id"], "decision": "approve", "note": "dale"},
    )
    assert r.status_code == 200
    # la decisión le llega a agy con correlation al mensaje original
    msgs = (await client.get("/inbox/agy")).json()
    dec = [m for m in msgs if (m.get("body") or {}).get("type") == "approval_decision"]
    assert dec and dec[0]["correlation_id"] == aps[0]["message_id"]
    assert dec[0]["body"]["decision"] == "approve"
    # y desaparece de pendientes
    aps2 = (await client.get("/room/api/pending-approvals")).json()
    assert aps2 == []
