from __future__ import annotations

from datetime import datetime, timezone

from agent_bus.types import AgentInfo, AgentStatus


class AgentRegistry:
    """Live presence in memory; with a database, registrations also survive a hub restart.

    Agents restored from the database are offline, without active work, until their
    next heartbeat.
    """

    def __init__(self, heartbeat_miss_threshold: int = 3, db=None) -> None:
        self._agents: dict[str, AgentInfo] = {}
        self._heartbeat_miss_threshold = heartbeat_miss_threshold
        self.db = db
        self._loaded = False

    async def _load(self) -> None:
        if self._loaded or self.db is None:
            return
        self._loaded = True
        for row in await self.db.conn.execute_fetchall("SELECT info FROM registered_agents"):
            info = AgentInfo.model_validate_json(row["info"])
            info.status = AgentStatus.OFFLINE
            self._agents.setdefault(info.agent_id, info)

    async def register(self, info: AgentInfo) -> AgentInfo:
        await self._load()
        self._agents[info.agent_id] = info
        if self.db is not None:
            await self.db.conn.execute(
                "INSERT OR REPLACE INTO registered_agents (agent_id, info) VALUES (?, ?)",
                (info.agent_id, info.model_dump_json(exclude={"status", "active_work"})),
            )
            await self.db.conn.commit()
        return info

    async def unregister(self, agent_id: str) -> None:
        await self._load()
        self._agents.pop(agent_id, None)
        if self.db is not None:
            await self.db.conn.execute("DELETE FROM registered_agents WHERE agent_id = ?", (agent_id,))
            await self.db.conn.commit()

    async def heartbeat(self, agent_id: str) -> None:
        await self._load()
        agent = self._agents.get(agent_id)
        if agent:
            agent.last_heartbeat = datetime.now(timezone.utc)
            if agent.status in (AgentStatus.AWAY, AgentStatus.OFFLINE):
                agent.status = AgentStatus.BUSY if agent.active_work else AgentStatus.ONLINE

    async def get(self, agent_id: str) -> AgentInfo | None:
        await self._load()
        await self.check_heartbeats()
        return self._agents.get(agent_id)

    async def list_all(self) -> list[AgentInfo]:
        await self._load()
        await self.check_heartbeats()
        return list(self._agents.values())

    async def update_status(self, agent_id: str, status: AgentStatus) -> None:
        await self._load()
        agent = self._agents.get(agent_id)
        if agent:
            agent.status = status

    async def update_active_work(self, agent_id: str, work: dict | None) -> None:
        await self._load()
        agent = self._agents.get(agent_id)
        if agent:
            agent.active_work = work
            agent.status = AgentStatus.BUSY if work else AgentStatus.ONLINE

    async def check_heartbeats(self) -> list[str]:
        """Check for stale agents. Returns list of agent IDs that went OFFLINE.

        Runs on every read, so a worker stopped or crashed without a clean shutdown
        stops showing busy once its heartbeats lapse; its active work is dropped.
        """
        now = datetime.now(timezone.utc)
        went_offline: list[str] = []

        for agent_id, agent in self._agents.items():
            diff = (now - agent.last_heartbeat).total_seconds()
            if agent.status == AgentStatus.OFFLINE:
                continue
            if diff > self._heartbeat_miss_threshold * 30:
                agent.status = AgentStatus.OFFLINE
                agent.active_work = None
                went_offline.append(agent_id)
            elif diff > 30:
                agent.status = AgentStatus.AWAY

        return went_offline
