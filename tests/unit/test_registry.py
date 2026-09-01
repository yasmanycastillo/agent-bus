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
