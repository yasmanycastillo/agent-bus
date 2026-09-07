"""Contrato congelado de presupuesto/consumo (T-21 → T-20): GET /room/api/usage."""

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
async def secured_bus():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, "test.db"))
        await db.initialize()
        registry = AgentRegistry()
        inbox = InboxManager(db)
        bus = MessageBus(db=db, registry=registry, inbox=inbox, project_id="usage-test")
        admin = await bus.sessions.create("root", role="admin")
        agent = await bus.sessions.create("worker", role="agent")
        yield bus, admin["token"], agent["token"]
        await db.close()


async def _client(bus, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = ASGITransport(app=bus.app)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


async def test_usage_stub_shape_congelado(secured_bus):
    bus, admin_token, _ = secured_bus
    async with await _client(bus, admin_token) as ac:
        r = await ac.get("/room/api/usage")
    assert r.status_code == 200
    assert r.json() == {
        "available": False,
        "reason": "metrics_unavailable",
        "message": "Presupuesto/consumo (T-20) aún no implementado",
        "schema_version": 1,
        "data": None,
    }


async def test_usage_requiere_sesion(secured_bus, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "0")
    bus, _admin, _agent = secured_bus
    async with await _client(bus) as ac:
        r = await ac.get("/room/api/usage")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


async def test_usage_requiere_admin(secured_bus):
    bus, _admin, agent_token = secured_bus
    async with await _client(bus, agent_token) as ac:
        r = await ac.get("/room/api/usage")
    assert r.status_code == 403
