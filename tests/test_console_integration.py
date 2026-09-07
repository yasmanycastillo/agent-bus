"""Tests de integración de la consola (T-21): endpoints /room/api de operaciones."""

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


async def test_create_task_desde_consola(client):
    r = await client.post(
        "/room/api/tasks",
        json={"task_id": "C1", "title": "consola", "description": "creada por admin",
              "acceptance_criteria": ["criterio 1"]},
    )
    assert r.status_code == 201
    assert r.json()["task_id"] == "C1"
    assert r.json()["status"] == "pending"
    overview = (await client.get("/room/api/overview")).json()
    assert any(t["task_id"] == "C1" for t in overview["tasks"])


async def test_create_task_valida_campos(client):
    r = await client.post("/room/api/tasks", json={"task_id": "", "title": "x"})
    assert r.status_code == 400


async def test_task_status_done_desde_consola(client):
    await client.post("/room/api/tasks", json={"task_id": "C2", "title": "a completar"})
    r = await client.post("/room/api/tasks/C2/status", json={"action": "done"})
    assert r.status_code == 200
    assert r.json()["status"] == "done"


async def test_task_status_transicion_invalida_409(client):
    await client.post("/tasks", json={"task_id": "C3", "title": "directa"})
    # done y luego intentar in_review sobre tarea terminal
    await client.post("/room/api/tasks/C3/status", json={"action": "done"})
    r = await client.post("/room/api/tasks/C3/status", json={"action": "in_review"})
    assert r.status_code == 409


async def test_task_status_tarea_inexistente_404(client):
    r = await client.post("/room/api/tasks/X404/status", json={"action": "done"})
    assert r.status_code == 404


async def test_task_status_accion_invalida_400(client):
    await client.post("/room/api/tasks", json={"task_id": "C4", "title": "x"})
    r = await client.post("/room/api/tasks/C4/status", json={"action": "nonsense"})
    assert r.status_code == 400


async def test_task_status_block_y_unblock(client):
    await client.post("/room/api/tasks", json={"task_id": "C5", "title": "x"})
    r = await client.post("/room/api/tasks/C5/status", json={"action": "block", "reason": "espera"})
    assert r.status_code == 200
    assert r.json()["status"] == "blocked"
    r = await client.post("/room/api/tasks/C5/status", json={"action": "unblock"})
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


async def test_pause_worker_impide_assign_y_aparece_en_overview(client):
    await _register(client, "claude")
    await client.post("/room/api/tasks", json={"task_id": "C6", "title": "x"})
    r = await client.post("/room/api/workers/claude/pause")
    assert r.status_code == 200
    assert r.json() == {"agent_id": "claude", "paused": True}
    # la asignación se rechaza mientras está pausado
    r = await client.post("/room/api/assign", json={"task_id": "C6", "agent_id": "claude"})
    assert r.status_code == 409
    assert (await client.get("/workers/claude/paused")).json()["paused"] is True
    overview = (await client.get("/room/api/overview")).json()
    assert overview["paused_workers"] == ["claude"]
    # al reanudar, la asignación funciona de nuevo
    await client.post("/room/api/workers/claude/resume")
    r = await client.post("/room/api/assign", json={"task_id": "C6", "agent_id": "claude"})
    assert r.status_code == 200


async def test_worker_paused_persistido_en_db(bus_app):
    db = bus_app.db
    await db.set_worker_paused("agy", True)
    assert await db.is_worker_paused("agy") is True
    assert await db.list_paused_workers() == ["agy"]
    await db.set_worker_paused("agy", False)
    assert await db.is_worker_paused("agy") is False
    assert await db.list_paused_workers() == []


async def test_console_sirve_html_sin_auth(client):
    r = await client.get("/console")
    assert r.status_code == 200
    assert "agent-bus · Consola" in r.text
    # El HTML debe cargar todas las piezas de la app: vendor, api, components y app.
    for src in ("vendor/react.js", "vendor/react-dom.js", "vendor/htm.js",
                "js/api.js", "js/components.js", "js/app.js", "styles.css"):
        assert f"/console/static/{src}" in r.text, src


async def test_console_estaticos_disponibles(client):
    for rel in ("js/app.js", "js/api.js", "js/components.js", "styles.css",
                "vendor/react.js", "vendor/react-dom.js", "vendor/htm.js"):
        r = await client.get(f"/console/static/{rel}")
        assert r.status_code == 200, rel
        assert len(r.content) > 100


async def test_console_estatico_rechaza_escape(client):
    for rel in ("../../bus.py", "js/../../core/bus.py", "/etc/passwd"):
        r = await client.get(f"/console/static/{rel}")
        assert r.status_code in (404, 400), rel


async def test_room_sin_regresion(client):
    r = await client.get("/room")
    assert r.status_code == 200
    assert "War Room" in r.text
