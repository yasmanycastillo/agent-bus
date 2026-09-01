from __future__ import annotations

from datetime import datetime, timezone

from agent_bus.types import AgentInfo, AgentStatus


class AgentRegistry:
    def __init__(self, heartbeat_miss_threshold: int = 3) -> None:
        self._agents: dict[str, AgentInfo] = {}
        self._heartbeat_miss_threshold = heartbeat_miss_threshold

    async def register(self, info: AgentInfo) -> AgentInfo:
        self._agents[info.agent_id] = info
        return info

    async def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    async def heartbeat(self, agent_id: str) -> None:
        agent = self._agents.get(agent_id)
        if agent:
            agent.last_heartbeat = datetime.now(timezone.utc)
            if agent.status == AgentStatus.AWAY:
                agent.status = AgentStatus.ONLINE

    async def get(self, agent_id: str) -> AgentInfo | None:
        return self._agents.get(agent_id)

    async def list_all(self) -> list[AgentInfo]:
        return list(self._agents.values())

    async def update_status(self, agent_id: str, status: AgentStatus) -> None:
        agent = self._agents.get(agent_id)
        if agent:
            agent.status = status

    async def update_active_work(self, agent_id: str, work: dict | None) -> None:
        agent = self._agents.get(agent_id)
        if agent:
            agent.active_work = work
            agent.status = AgentStatus.BUSY if work else AgentStatus.ONLINE

    async def check_heartbeats(self) -> list[str]:
        """Check for stale agents. Returns list of agent IDs that went OFFLINE."""
        now = datetime.now(timezone.utc)
        went_offline: list[str] = []

        for agent_id, agent in self._agents.items():
            diff = (now - agent.last_heartbeat).total_seconds()
            if agent.status == AgentStatus.OFFLINE:
                continue
            if diff > self._heartbeat_miss_threshold * 30:
                agent.status = AgentStatus.OFFLINE
                went_offline.append(agent_id)
            elif diff > 30:
                agent.status = AgentStatus.AWAY

        return went_offline
