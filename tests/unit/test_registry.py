from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from agent_bus.core.registry import AgentRegistry
from agent_bus.types import AgentInfo, AgentStatus


@pytest.fixture
def registry():
    return AgentRegistry(heartbeat_miss_threshold=3)


async def test_register_and_get(registry: AgentRegistry):
    agent = AgentInfo(agent_id="claude", display_name="Claude")
    await registry.register(agent)
    result = await registry.get("claude")
    assert result is not None
    assert result.agent_id == "claude"


async def test_unregister(registry: AgentRegistry):
    agent = AgentInfo(agent_id="claude", display_name="Claude")
    await registry.register(agent)
    await registry.unregister("claude")
    assert await registry.get("claude") is None


async def test_heartbeat(registry: AgentRegistry):
    agent = AgentInfo(
        agent_id="claude",
        display_name="Claude",
        last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=5),
        status=AgentStatus.AWAY,
    )
    await registry.register(agent)
    await registry.heartbeat("claude")
    result = await registry.get("claude")
    assert result is not None
    assert result.status == AgentStatus.ONLINE


async def test_list_all(registry: AgentRegistry):
    await registry.register(AgentInfo(agent_id="a", display_name="A"))
    await registry.register(AgentInfo(agent_id="b", display_name="B"))
    agents = await registry.list_all()
    assert len(agents) == 2


async def test_update_status(registry: AgentRegistry):
    agent = AgentInfo(agent_id="claude", display_name="Claude")
    await registry.register(agent)
    await registry.update_status("claude", AgentStatus.BUSY)
    result = await registry.get("claude")
    assert result is not None
    assert result.status == AgentStatus.BUSY


async def test_update_active_work(registry: AgentRegistry):
    agent = AgentInfo(agent_id="claude", display_name="Claude")
    await registry.register(agent)
    await registry.update_active_work("claude", {"task": "T1", "files": ["a.py"]})
    result = await registry.get("claude")
    assert result is not None
    assert result.status == AgentStatus.BUSY
    assert result.active_work["task"] == "T1"


async def test_heartbeat_detection_offline(registry: AgentRegistry):
    agent = AgentInfo(
        agent_id="claude",
        display_name="Claude",
        last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    await registry.register(agent)
    went_offline = await registry.check_heartbeats()
    assert "claude" in went_offline
    result = await registry.get("claude")
    assert result is not None
    assert result.status == AgentStatus.OFFLINE


async def test_heartbeat_detection_away(registry: AgentRegistry):
    agent = AgentInfo(
        agent_id="claude",
        display_name="Claude",
        last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=45),
    )
    await registry.register(agent)
    await registry.check_heartbeats()
    result = await registry.get("claude")
    assert result is not None
    assert result.status == AgentStatus.AWAY


async def test_stale_agent_is_listed_offline_without_active_work(registry: AgentRegistry):
    # A stopped or crashed worker sends no more heartbeats; its work must not stay busy.
    await registry.register(AgentInfo(agent_id="codex", display_name="Codex"))
    await registry.update_active_work("codex", {"message_id": "m1"})
    (await registry.get("codex")).last_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=5)
    [agent] = await registry.list_all()
    assert agent.status == AgentStatus.OFFLINE
    assert agent.active_work is None
    await registry.heartbeat("codex")
    assert (await registry.get("codex")).status == AgentStatus.ONLINE


async def test_registered_agents_survive_a_hub_restart_offline(tmp_path):
    from agent_bus.reputation.database import Database

    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    first = AgentRegistry(db=db)
    await first.register(AgentInfo(agent_id="grok", display_name="Grok", capabilities=["code-review"]))
    await first.update_active_work("grok", {"message_id": "m1"})
    await db.close()

    reopened = Database(str(tmp_path / "bus.db"))
    await reopened.initialize()
    restarted = AgentRegistry(db=reopened)
    [agent] = await restarted.list_all()
    assert (agent.agent_id, agent.capabilities) == ("grok", ["code-review"])
    assert agent.status == AgentStatus.OFFLINE
    assert agent.active_work is None
    await restarted.heartbeat("grok")
    assert (await restarted.get("grok")).status == AgentStatus.ONLINE
    await restarted.unregister("grok")
    assert await AgentRegistry(db=reopened).list_all() == []
    await reopened.close()


async def test_restarted_hub_lists_known_agents_offline(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from agent_bus.core.bus import MessageBus
    from agent_bus.core.inbox import InboxManager
    from agent_bus.reputation.database import Database

    for restart in (False, True):
        db = Database(str(tmp_path / "bus.db"))
        await db.initialize()
        bus = MessageBus(db, AgentRegistry(), InboxManager(db))
        async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
            if not restart:
                assert (await client.post("/register", json={"agent_id": "codex", "display_name": "Codex"})).status_code == 201
            else:
                [agent] = (await client.get("/agents")).json()
                assert (agent["agent_id"], agent["status"]) == ("codex", "offline")
                await client.post("/agents/codex/heartbeat")
                assert (await client.get("/agents")).json()[0]["status"] == "online"
        await db.close()
