"""Delivery acknowledgements follow successful processing, never merely an event."""
from __future__ import annotations

import asyncio
import json
import subprocess

import httpx
import pytest

from agent_bus.cli import watch_cmds as watch
from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner, RunnerResult


class DeliveryHub:
    def __init__(self):
        self.message = dict(message_id="source", from_agent="alice", to_agent="bob",
                            body={"text": "Review please"}, reply_needed=True,
                            conversation_id="conversation-1", acknowledged=False)
        self.failures = []
        self.replies = []
        self.pages = []
        self.reply_status = 200
        self.lose_reply_response = False

    def handle(self, request):
        path = request.url.path
        if path == "/inbox/bob/messages":
            self.pages.append(dict(request.url.params))
            return httpx.Response(200, json={"messages": [] if self.message["acknowledged"] else [self.message],
                                            "next_cursor": None})
        if path == "/inbox/bob/source":
            return httpx.Response(200, json=self.message)
        if path == "/inbox/bob/source/reply":
            if self.reply_status != 200:
                return httpx.Response(self.reply_status, json={"error": "Commit failed"})
            payload = json.loads(request.content)
            self.replies.append(payload)
            self.message["acknowledged"] = payload["acknowledge"]
            if self.lose_reply_response:
                raise httpx.ReadError("Response lost after commit", request=request)
            return httpx.Response(200, json={"message_id": "reply", "conversation_id": "conversation-1"})
        if path == "/inbox/bob/source/fail":
            self.failures.append(json.loads(request.content)["error"])
            return httpx.Response(200, json={"acknowledged": self.message["acknowledged"]})
        if path in ("/decisions", "/tasks"):
            return httpx.Response(200, json=[])
        if path == "/agents/bob/active-work":
            return httpx.Response(200, json={})
        pytest.fail(f"Unexpected request {request.method} {path}")

    def client(self, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), **kwargs)


def make_worker(execute):
    return WorkerDaemon("bob", AgentRunner("bob", custom_executor=execute))


@pytest.mark.parametrize("failure", ["result", "exception", "empty", "cancelled"])
async def test_worker_failures_remain_pending(failure):
    hub = DeliveryHub()
    async def execute(*args):
        if failure == "exception":
            raise RuntimeError("Runner crashed")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        return RunnerResult(success=failure != "result", output="", error="Runner failed")
    worker = make_worker(execute)
    async with hub.client() as client:
        worker._client = client
        if failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await worker._handle_urgent_message(hub.message)
        else:
            result = await worker._handle_urgent_message(hub.message)
            assert not result.success
    assert not hub.message["acknowledged"]
    assert hub.failures
    assert not hub.replies


async def test_worker_reply_commit_failure_then_restart_recovers():
    hub = DeliveryHub()
    calls = []
    async def execute(prompt, session_id):
        calls.append(prompt)
        return RunnerResult(success=True, output="Reviewed")
    hub.reply_status = 503
    async with hub.client() as client:
        first = make_worker(execute)
        first._client = client
        await first._check_and_process_pending()
        # The same process backs off even if an SSE hint arrives immediately.
        await first._check_and_process_pending()
        assert len(calls) == 1
        assert not hub.message["acknowledged"]
        assert hub.failures
        hub.reply_status = 200
        restarted = make_worker(execute)
        restarted._client = client
        await restarted._check_and_process_pending()
        await restarted._check_and_process_pending()
    assert len(calls) == 2
    assert hub.message["acknowledged"]
    assert len(hub.replies) == 1
    assert hub.replies[0]["idempotency_key"] == "worker-reply:source"
    assert hub.pages[0] == {"limit": "10", "reply_needed": "true"}
    assert "Do not send or acknowledge" in calls[0]


async def test_worker_committed_reply_lost_response_does_not_repeat_runner():
    hub = DeliveryHub()
    hub.lose_reply_response = True
    calls = []
    async def execute(*args):
        calls.append(1)
        return RunnerResult(success=True, output="Reviewed")
    async with hub.client() as client:
        worker = make_worker(execute)
        worker._client = client
        await worker._handle_urgent_message(hub.message)
        # A stale event is checked against persisted ack before invoking a runner.
        await worker._handle_urgent_message(hub.message)
    assert len(calls) == 1
    assert len(hub.replies) == 1
    assert hub.message["acknowledged"]


@pytest.fixture
def watcher_hub(monkeypatch):
    hub = DeliveryHub()
    monkeypatch.setattr(watch, "async_bus_client", lambda *args, **kwargs: hub.client(**kwargs))
    monkeypatch.setattr(watch.shutil, "which", lambda _: "/fake-cli")
    return hub


@pytest.mark.parametrize("failure", ["exit", "exception", "empty", "cancelled", "reply", "error_result"])
async def test_watcher_failure_never_acknowledges(watcher_hub, monkeypatch, tmp_path, failure):
    hub = watcher_hub
    async def run(*args):
        if failure == "exception":
            raise RuntimeError("CLI crashed")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        return subprocess.CompletedProcess([], 1 if failure == "exit" else 0,
                                           json.dumps({"session_id": "s1", "result": "" if failure == "empty" else "Reply", "is_error": failure == "error_result"}), "error")
    monkeypatch.setattr(watch, "_run_cli", run)
    if failure == "reply":
        hub.reply_status = 503
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await watch.run_turn("bob", hub.message, {}, sessions_file=tmp_path / "sessions.json")
    else:
        assert await watch.run_turn("bob", hub.message, {}, sessions_file=tmp_path / "sessions.json") is None
    assert hub.failures
    assert not hub.message["acknowledged"]
    assert not hub.replies


async def test_watcher_restart_recovers_without_sse_and_stale_event_is_ignored(watcher_hub, monkeypatch, tmp_path):
    hub = watcher_hub
    calls = []
    async def run(cmd, agent):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"session_id":"s1","result":"Reviewed"}', '')
    monkeypatch.setattr(watch, "_run_cli", run)
    hub.reply_status = 503
    first = watch.PendingMessageWatcher("bob", sessions_file=tmp_path / "sessions.json")
    await first.poll_once()
    await first.on_event(hub.message)
    await first.poll_once()
    assert len(calls) == 1  # backoff keeps repeated hints from hammering the CLI
    hub.reply_status = 200
    restarted = watch.PendingMessageWatcher("bob", sessions_file=tmp_path / "sessions.json")
    await restarted.poll_once()
    await restarted.on_event(hub.message)
    await restarted.poll_once()
    await watch.run_turn("bob", hub.message, restarted.session_map, sessions_file=tmp_path / "sessions.json")
    assert len(calls) == 2
    assert "--resume" in calls[1]
    assert restarted.session_map["conversation-1"] == "s1"
    assert hub.replies[0]["idempotency_key"] == "watch-reply:source"
    assert hub.message["acknowledged"]


async def test_watcher_cli_cancel_terminates_and_reaps_child(monkeypatch):
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
    process = Process()
    async def spawn(*args, **kwargs):
        assert kwargs["env"]["AGENT_BUS_AGENT_ID"] == "bob"
        return process
    monkeypatch.setattr(watch.asyncio, "create_subprocess_exec", spawn)
    running = asyncio.create_task(watch._run_cli(["fake-cli"], "bob"))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert process.terminated and process.reaped
