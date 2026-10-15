"""A claim whose owner died returns to pending; any sign of life keeps it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.types import AgentInfo, TaskStatus

LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=2)


@pytest.fixture
async def bus(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    monkeypatch.setenv("AGENT_BUS_CLAIM_TTL", "600")
    bus = MessageBus(db=tmp_db, registry=AgentRegistry(), inbox=InboxManager(tmp_db))
    bus._started_at = LONG_AGO
    await bus.tasks.create("T1", "Abandoned work")
    assert await bus.tasks.claim("T1", "alice") is not None
    await tmp_db.conn.execute("UPDATE tasks SET updated_at = ? WHERE task_id = 'T1'", (LONG_AGO.isoformat(),))
    await tmp_db.conn.commit()
    return bus


async def heartbeat_at(bus, when):
    await bus.registry.register(AgentInfo(agent_id="alice", display_name="alice"))
    (await bus.registry.get("alice")).last_heartbeat = when


async def test_dead_owner_claim_returns_to_pending_with_audit(bus):
    await heartbeat_at(bus, LONG_AGO)
    await bus._reap_abandoned_claims()
    task = await bus.tasks.get("T1")
    assert (task.status, task.owner) == (TaskStatus.PENDING, "free")
    rows = await bus.db.conn.execute_fetchall(
        "SELECT previous_owner, new_owner FROM audit_log WHERE action = 'claim_expired' AND task_id = 'T1'"
    )
    assert [tuple(row) for row in rows] == [("alice", "free")]
    assert await bus.tasks.claim("T1", "bob") is not None


async def test_recent_heartbeat_keeps_claim(bus):
    await heartbeat_at(bus, datetime.now(timezone.utc))
    await bus._reap_abandoned_claims()
    assert (await bus.tasks.get("T1")).owner == "alice"


async def test_live_lock_keeps_claim(bus):
    await bus.locks.acquire("src/a.py", "alice", session_id="s1")
    await bus._reap_abandoned_claims()
    assert (await bus.tasks.get("T1")).owner == "alice"


async def test_running_attempt_keeps_claim(bus):
    await bus.db.conn.execute(
        "INSERT INTO runtime_attempts (attempt_id, task_id, idempotency_key, state, updated_at) "
        "VALUES ('A1', 'T1', 'k1', 'started', ?)", (LONG_AGO.isoformat(),),
    )
    await bus.db.conn.commit()
    await bus._reap_abandoned_claims()
    assert (await bus.tasks.get("T1")).owner == "alice"


async def test_fresh_hub_waits_for_heartbeats_before_reaping(bus):
    bus._started_at = datetime.now(timezone.utc)
    await bus._reap_abandoned_claims()
    assert (await bus.tasks.get("T1")).owner == "alice"


async def test_listing_tasks_reaps_abandoned_claims(bus):
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://bus") as client:
        listed = (await client.get("/tasks", params={"status": "pending"})).json()
    assert [task["task_id"] for task in listed] == ["T1"]
