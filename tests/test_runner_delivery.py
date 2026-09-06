"""Runner outcomes consumed by delivery workers must represent actual success."""
from __future__ import annotations

import asyncio
import json

import pytest

from agent_bus.worker.runner import AgentRunner, RunnerResult


@pytest.mark.parametrize("envelope,success", [
    ({"result": "  Reviewed successfully  ", "is_error": False, "session_id": "session"}, True),
    ({"result": "Model failed", "is_error": True, "session_id": "session"}, False),
    ({"result": "", "is_error": False}, False),
    ({"result": {"error": "unexpected object"}, "is_error": False}, False),
    ([], False),
])
async def test_claude_envelope_controls_reply_and_success(monkeypatch, envelope, success):
    runner = AgentRunner("alice", provider="claude")
    monkeypatch.setattr("agent_bus.worker.runner.shutil.which", lambda _: "/fake/claude")
    async def execute(cmd, timeout_seconds, thread_id=None):
        assert cmd[-2:] == ["--output-format", "json"]
        return RunnerResult(success=True, output=json.dumps(envelope), exit_code=0, session_id="session")
    monkeypatch.setattr(runner, "_run_subprocess", execute)
    result = await runner.execute_turn("Reply to bob")
    assert result.success is success
    assert result.exit_code == 0
    if success:
        assert result.output == "Reviewed successfully"
        assert result.session_id == "session"
    else:
        assert result.output == ""
        assert result.error


async def test_claude_non_json_output_is_not_a_successful_reply(monkeypatch):
    runner = AgentRunner("alice")
    monkeypatch.setattr("agent_bus.worker.runner.shutil.which", lambda _: "/fake/claude")
    async def execute(*args, **kwargs):
        return RunnerResult(success=True, output="Unexpected CLI diagnostic", exit_code=0)
    monkeypatch.setattr(runner, "_run_subprocess", execute)
    result = await runner.execute_turn("Reply")
    assert not result.success
    assert result.output == ""


@pytest.mark.parametrize("cancel", [True, False])
async def test_subprocess_cancel_and_timeout_reap_child(monkeypatch, cancel):
    started = asyncio.Event()
    class Process:
        returncode = None
        terminated = False
        reaped = False
        async def communicate(self):
            started.set()
            await asyncio.Event().wait()
        def terminate(self):
            self.terminated = True
        async def wait(self):
            self.reaped = True
            self.returncode = -15
            return self.returncode
    process = Process()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr("agent_bus.worker.runner.asyncio.create_subprocess_exec", spawn)
    task = asyncio.create_task(AgentRunner("alice")._run_subprocess(["fake"], 60 if cancel else 0.01))
    await started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert not result.success
        assert "timed out" in result.error
    assert process.terminated and process.reaped
