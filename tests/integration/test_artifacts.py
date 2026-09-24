import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.mcp.coordination import TOOL_GUIDANCE
from agent_bus.mcp.server import TOOLS_DEFINITIONS
from agent_bus.reputation.database import Database


@pytest.mark.asyncio
async def test_discovery_artifact_is_consumed_by_id(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    transport = ASGITransport(app=bus.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/tasks", json={"task_id": "discover", "title": "Discover"})
        published = await client.post("/tasks/discover/artifacts", json={
            "producer": "kimi-analysis-01",
            "kind": "repository-analysis",
            "media_type": "application/json",
            "summary": "Mapped account.move",
            "attempt_id": "att-discover",
            "content": {"modules": ["account.move"]},
        })
        assert published.status_code == 200
        meta = published.json()
        assert meta["uri"] == f"artifact://alpha/{meta['artifact_id']}"
        assert "file://" not in json.dumps(meta)
        listed = (await client.get("/tasks/discover/artifacts")).json()
        assert listed[0]["artifact_id"] == meta["artifact_id"]
        fetched = (await client.get(f"/artifacts/{meta['artifact_id']}")).json()
        assert fetched == meta
        body = await client.get(f"/artifacts/{meta['artifact_id']}/content")
        assert body.status_code == 200
        assert body.headers["x-artifact-sha256"] == meta["sha256"]
        assert json.loads(body.content) == {"modules": ["account.move"]}
        stored = Path(db.db_path).parent / "artifacts" / "alpha" / meta["artifact_id"]
        stored.write_bytes(b"tampered")
        assert (await client.get(f"/artifacts/{meta['artifact_id']}/content")).status_code == 409
    other = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="beta")
    async with httpx.AsyncClient(transport=ASGITransport(app=other.app), base_url="http://test") as client:
        assert (await client.get(f"/artifacts/{meta['artifact_id']}")).status_code == 409
    names = {tool["name"] for tool in TOOLS_DEFINITIONS}
    assert {"publish_artifact", "list_task_artifacts", "get_artifact_metadata"} <= names
    assert not ({"publish_artifact", "list_task_artifacts", "get_artifact_metadata"} - TOOL_GUIDANCE.keys())
    await db.close()
