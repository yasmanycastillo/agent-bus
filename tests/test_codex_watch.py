"""Native Codex protocol, durable resume and no ACK on failed/incomplete turns."""
import json

import httpx
import pytest

from agent_bus.cli import watch_cmds as watch
from agent_bus.worker.runner import AgentRunner, RunnerResult


def events(*items):
    return "\n".join(json.dumps(item) for item in items)


SUCCESS = events(
    {"type": "thread.started", "thread_id": "codex-session"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Project answer"}},
    {"type": "turn.completed"},
)


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    "not JSON", '{"session_id":"wrong","result":"wrong protocol"}',
    events({"type": "thread.started", "thread_id": "s"}, {"type": "turn.completed"}),
    SUCCESS.replace('"turn.completed"', '"turn.failed"'),
    SUCCESS.rsplit("\n", 1)[0],
])
async def test_failed_codex_never_replies_or_saves_session(monkeypatch, tmp_path, output):
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"message_id": "m", "conversation_id": "c"})
        posted.append(request.url.path)
        return httpx.Response(200, json={})

    monkeypatch.setattr(watch, "async_bus_client", lambda *a, **kw:
                        httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr("shutil.which", lambda name: "/bin/codex")

    async def run(self, *args, **kwargs):
        return RunnerResult(True, output)

    monkeypatch.setattr(AgentRunner, "_run_subprocess", run)
    sessions = {}
    path = tmp_path / "sessions.json"
    assert await watch.run_turn("codex", {"message_id": "m"}, sessions,
                                cli="codex", sessions_file=path) is None
    assert posted == ["/inbox/codex/m/fail"]
    assert sessions == {} and not path.exists()


@pytest.mark.asyncio
async def test_watcher_codex_replies_and_resumes_after_restart(monkeypatch, tmp_path):
    calls, replies = [], []
    acknowledged = False

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"message_id": "m", "conversation_id": "conversation",
                                            "acknowledged": acknowledged})
        replies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    monkeypatch.setattr(watch, "async_bus_client", lambda *a, **kw:
                        httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr("shutil.which", lambda name: "/bin/codex")

    async def run(self, cmd, *args, **kwargs):
        calls.append(cmd)
        return RunnerResult(True, SUCCESS)

    monkeypatch.setattr(AgentRunner, "_run_subprocess", run)
    path = tmp_path / "sessions.json"
    await watch.run_turn("codex", {"message_id": "m"}, {}, cli="codex", sessions_file=path)
    restored = watch.load_session_map(path)
    assert restored == {"conversation": "codex-session"}
    await watch.run_turn("codex", {"message_id": "m2"}, restored, cli="codex", sessions_file=path)
    assert calls[0][:3] == ["/bin/codex", "exec", "--json"]
    assert calls[1][:4] == ["/bin/codex", "exec", "resume", "--json"]
    assert 'sandbox_mode="read-only"' in calls[0] and 'sandbox_mode="read-only"' in calls[1]
    assert "codex-session" in calls[1]
    assert all(r["body"]["text"] == "Project answer" and r["acknowledge"] for r in replies)
    acknowledged = True
    await watch.run_turn("codex", {"message_id": "m2"}, restored, cli="codex", sessions_file=path)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_durable_attempt_limit_skips_model(monkeypatch, tmp_path):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"attempts": 5})

    monkeypatch.setattr(watch, "async_bus_client", lambda *a, **kw:
                        httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))

    async def forbidden(*a, **kw):
        pytest.fail("Exhausted message invoked the model")

    monkeypatch.setattr(AgentRunner, "execute_turn", forbidden)
    assert await watch.run_turn("codex", {"message_id": "m"}, {}, cli="codex",
                                sessions_file=tmp_path / "sessions.json") is None
