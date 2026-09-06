from __future__ import annotations

from pathlib import Path
import pytest

from agent_bus.worker.runner import AgentRunner, RunnerResult


@pytest.mark.asyncio
async def test_agent_runner_prompt_assembly():
    runner = AgentRunner(agent_id="test_agent", provider="mock")
    prompt = runner.assemble_prompt(
        task={"task_id": "T100", "title": "Implement feature", "locked_files": ["a.py"]},
        message={"from_agent": "claude", "message_type": "inbox", "body": {"text": "Need help?"}},
        decisions=[{"decision_id": "D1", "title": "Use SQLite", "decision": "Local DB"}],
        git_diff_summary="+ added new function",
    )

    assert "test_agent" in prompt
    assert "T100" in prompt
    assert "Need help?" in prompt
    assert "Use SQLite" in prompt
    assert "+ added new function" in prompt


@pytest.mark.asyncio
async def test_agent_runner_mock_execution():
    runner = AgentRunner(agent_id="claude", provider="mock")
    result = await runner.execute_turn("Test prompt", thread_id="thread-1")

    assert result.success is True
    assert "[mock:claude]" in result.output
    assert result.session_id == "mock-sess-thread-1"


@pytest.mark.asyncio
async def test_agent_runner_custom_executor():
    async def custom_fn(prompt: str, session_id: str | None) -> RunnerResult:
        return RunnerResult(success=True, output=f"Handled: {prompt[:10]}", session_id="custom-123")

    runner = AgentRunner(agent_id="custom_agent", custom_executor=custom_fn)
    result = await runner.execute_turn("Hello world", thread_id="t1")

    assert result.success is True
    assert result.output == "Handled: Hello worl"
    assert runner.session_map.get("t1") == "custom-123"


@pytest.mark.asyncio
async def test_agent_runner_unsupported_provider():
    runner = AgentRunner(agent_id="bot", provider="unknown_provider_xyz")
    result = await runner.execute_turn("Hello")

    assert result.success is False
    assert "Unsupported provider" in (result.error or "")


@pytest.mark.asyncio
async def test_agent_runner_missing_binaries(monkeypatch):
    # Monkeypatch shutil.which to simulate missing binaries
    monkeypatch.setattr("shutil.which", lambda name: None)

    runner_claude = AgentRunner(agent_id="c", provider="claude")
    runner_agy = AgentRunner(agent_id="a", provider="agy")
    runner_aider = AgentRunner(agent_id="ai", provider="aider")
    runner_grok = AgentRunner(agent_id="g", provider="grok")

    res_c = await runner_claude.execute_turn("test")
    assert res_c.success is False
    assert res_c.exit_code == 127
    assert "not found in PATH" in (res_c.error or "")

    res_a = await runner_agy.execute_turn("test")
    assert res_a.success is False
    assert res_a.exit_code == 127
    assert "not found in PATH" in (res_a.error or "")

    res_ai = await runner_aider.execute_turn("test")
    assert res_ai.success is False
    assert res_ai.exit_code == 127
    assert "not found in PATH" in (res_ai.error or "")

    res_g = await runner_grok.execute_turn("test")
    assert res_g.success is False
    assert res_g.exit_code == 127
    assert "not found in PATH" in (res_g.error or "")


@pytest.mark.asyncio
async def test_codex_uses_native_exec_and_resume(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)

    async def fake_subprocess(cmd, timeout, thread_id=None):
        calls.append(cmd)
        return RunnerResult(True, '{"session_id":"sid-1"}', session_id="sid-1")

    runner = AgentRunner("codex", provider="codex", model="gpt-test", session_file=tmp_path / "sessions.json")
    monkeypatch.setattr(runner, "_run_subprocess", fake_subprocess)
    await runner.execute_turn("first", thread_id="thread")
    await runner.execute_turn("second", thread_id="thread")
    assert calls[0][:4] == ["/usr/bin/codex", "exec", "--json", "--model"]
    assert calls[1][:4] == ["/usr/bin/codex", "exec", "resume", "--json"]
    assert "sid-1" in calls[1]
    restored = AgentRunner("codex", provider="codex", session_file=tmp_path / "sessions.json")
    assert restored.session_map == {"thread": "sid-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, expected", [
    ("agy", ["--prompt", "hello", "--output-format", "json"]),
    ("grok", ["-p", "hello", "--output-format", "json", "--no-alt-screen", "--no-plan"]),
])
async def test_real_provider_adapters_use_headless_commands(monkeypatch, provider, expected):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    calls = []
    async def fake_subprocess(cmd, timeout, thread_id=None):
        calls.append(cmd)
        return RunnerResult(True, '{"sessionId":"sid"}', session_id="sid")
    runner = AgentRunner(provider, provider=provider)
    monkeypatch.setattr(runner, "_run_subprocess", fake_subprocess)
    await runner.execute_turn("hello", thread_id="t")
    assert calls[0][1:1 + len(expected)] == expected
