import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database


@pytest.mark.asyncio
async def test_usage_answers_cost_agent_workflow_and_cache(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        await client.post("/usage", json={
            "agent_id": "codex", "runtime": "native", "provider": "openai", "model": "gpt",
            "task_id": "T123", "workflow_id": "feature-42",
            "input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 100,
            "wall_seconds": 12, "estimated_cost_usd": 1.5, "retries": 1,
            "source": "reported", "confidence": "high",
        })
        await client.post("/usage", json={
            "agent_id": "kimi", "task_id": "T123", "workflow_id": "feature-42",
            "estimated_cost_usd": 0.25, "cached_input_tokens": 50, "retries": 0,
            "source": "reported", "confidence": "medium",
        })
        await client.post("/usage", json={
            "agent_id": "claude", "workflow_id": "bugfix-7", "retries": 4,
            "source": "unknown", "confidence": "unknown",
        })
        task = (await client.get("/usage/summary", params={"task": "T123"})).json()
        assert task["estimated_cost_usd"] == 1.75
        assert task["cached_input_tokens"] == 850
        whole = (await client.get("/usage/summary")).json()
        assert whole["top_agent"]["agent_id"] == "codex"
        assert whole["top_workflow_retries"] == {"workflow_id": "bugfix-7", "retries": 4}
        assert whole["unknown_cost_records"] == 1
        assert whole["cached_input_tokens"] == 850
    await db.close()


def test_usage_command_prints_unknown_cost(monkeypatch):
    from unittest.mock import MagicMock
    from click.testing import CliRunner
    from agent_bus.cli import main

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, params=None):
            assert url == "/usage/summary"
            assert params == {"task": "T123"}
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "records": 1, "estimated_cost_usd": None, "cached_input_tokens": None,
                "top_agent": None, "top_workflow_retries": None,
            }
            return resp

    monkeypatch.setattr(main, "_client", lambda: FakeClient())
    result = CliRunner().invoke(main.app, ["usage", "--task", "T123"])
    assert result.exit_code == 0
    assert "desconocido" in result.output
