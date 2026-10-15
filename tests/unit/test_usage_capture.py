"""Tokens and cost reported by the CLIs reach the /usage ledger; reporting never changes a turn."""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.core.usage import UsageLedger
from agent_bus.worker import runner as runner_module
from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner, RunnerResult, claude_usage, codex_usage

CLAUDE_ENVELOPE = {
    "type": "result", "is_error": False, "result": "done", "total_cost_usd": 0.0421,
    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 3000,
              "output_tokens": 450},
    "modelUsage": {"claude-opus-5-5": {}},
}


def test_claude_usage_counts_cache_reads_and_writes_as_input():
    assert claude_usage(CLAUDE_ENVELOPE) == {
        "input_tokens": 3210, "cached_input_tokens": 3000, "output_tokens": 450,
        "estimated_cost_usd": 0.0421, "model": "claude-opus-5-5",
    }


def test_missing_numbers_stay_unknown():
    assert claude_usage({"result": "done"}) == {
        "input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
        "estimated_cost_usd": None, "model": None,
    }
    assert codex_usage({"type": "turn.completed"}) == {
        "input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
    }


def test_codex_usage():
    event = {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 400, "output_tokens": 70}}
    assert codex_usage(event) == {"input_tokens": 900, "cached_input_tokens": 400, "output_tokens": 70}


@pytest.mark.parametrize("is_error", [False, True])
async def test_claude_turn_carries_usage_even_when_it_fails(monkeypatch, tmp_path, is_error):
    monkeypatch.setattr(runner_module.shutil, "which", lambda name: "/bin/claude")
    runner = AgentRunner(agent_id="bob", provider="claude", worktree_dir=tmp_path)

    async def subprocess(cmd, timeout, thread_id=None):
        return RunnerResult(True, json.dumps({**CLAUDE_ENVELOPE, "is_error": is_error}))

    monkeypatch.setattr(runner, "_run_subprocess", subprocess)
    result = await runner.execute_turn("hi")
    assert result.success is not is_error
    assert result.metadata["usage"]["output_tokens"] == 450


@pytest.fixture
async def bus(tmp_db):
    return MessageBus(db=tmp_db, registry=AgentRegistry(), inbox=InboxManager(tmp_db))


def daemon_with(bus, client, metadata):
    async def executor(prompt, session_id):
        return RunnerResult(True, "ok", metadata=metadata)

    daemon = WorkerDaemon(agent_id="bob", runner=AgentRunner(agent_id="bob", custom_executor=executor),
                          bus_url="http://test")
    daemon._client = client
    return daemon


async def test_turn_usage_reaches_the_ledger(bus):
    usage = {"input_tokens": 3210, "cached_input_tokens": 3000, "output_tokens": 450,
             "estimated_cost_usd": 0.0421, "model": "claude-opus-5-5"}
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        result = await daemon_with(bus, client, {"usage": usage})._run_turn("hi", task_id="T1")
    assert result.success
    [record] = await UsageLedger(bus.db, bus.project_id).list(task_id="T1")
    assert {key: record[key] for key in usage} == usage
    assert (record["agent_id"], record["confidence"]) == ("bob", "high")
    assert record["wall_seconds"] is not None


async def test_usage_failure_does_not_change_the_turn(bus):
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        daemon = daemon_with(bus, client, {"usage": {"output_tokens": 1}})

        async def broken_post(*args, **kwargs):
            raise OSError("hub unreachable")

        daemon._client.post = broken_post
        result = await daemon._run_turn("hi", task_id="T1")
    assert result.success and result.output == "ok"
