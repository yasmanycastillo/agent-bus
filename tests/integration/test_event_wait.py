"""MCP event waits bound the whole operation and expose resumable failures."""
import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from agent_bus.mcp.server import McpServer


class Stream(httpx.AsyncByteStream):
    def __init__(self, mode):
        self.mode = mode
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        if self.mode == "eof":
            return
        while True:
            await asyncio.sleep(.01)
            if self.mode == "comments":
                yield b": keepalive\n\n"
            elif self.mode == "checkpoint":
                yield b'id: newer\nevent: checkpoint\ndata: {"cursor":"newer"}\n\n'
            elif self.mode == "message":
                yield b'id: delivered\nevent: message\ndata: {"message_id":"m1"}\n\n'
            # silent mode deliberately produces no chunks

    async def aclose(self):
        self.closed = True


def install(monkeypatch, handler):
    @asynccontextmanager
    async def client(self, timeout=30):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test", timeout=timeout) as value:
            yield value
    monkeypatch.setattr(McpServer, "_client", client)
    return McpServer(agent_id="alice")


@pytest.mark.parametrize("phase", ["checkpoint", "inbox", "comments", "checkpoint_frames", "silent"])
async def test_total_deadline_includes_queries_and_keepalives(monkeypatch, phase):
    stream = Stream("checkpoint" if phase == "checkpoint_frames" else phase)
    async def handler(request):
        if request.url.path.endswith("/cursor"):
            if phase == "checkpoint":
                await asyncio.sleep(10)
            return httpx.Response(200, json={"cursor": "initial"})
        if request.url.path.endswith("/messages"):
            if phase == "inbox":
                await asyncio.sleep(10)
            return httpx.Response(200, json={"messages": []})
        return httpx.Response(200, stream=stream)
    start = time.monotonic()
    result = await install(monkeypatch, handler)._wait_for_updates("alice", 1)
    assert result["status"] == "timeout"
    assert .9 <= time.monotonic() - start < 1.7
    if phase not in {"checkpoint", "inbox"}:
        assert stream.closed


@pytest.mark.parametrize("status,code", [(410,"cursor_expired"),(422,"cursor_invalid"),(401,"unauthenticated"),(403,"forbidden"),(503,"hub_error")])
async def test_checkpoint_failures_are_explicit(monkeypatch, status, code):
    server = install(monkeypatch, lambda request: httpx.Response(status, json={"cursor":"fresh"}))
    result = await server._wait_for_updates("alice", 1, "old")
    assert result["code"] == code
    if status == 410:
        assert result["event_cursor"] == "fresh"
        assert result["recovery"] == "read_messages"
        assert server._event_cursors["alice"] == "fresh"


async def test_stream_eof_is_not_reported_as_timeout(monkeypatch):
    stream = Stream("eof")
    def handler(request):
        if request.url.path.endswith("/cursor"):
            return httpx.Response(200, json={"cursor":"initial"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages":[]})
        assert request.headers["Last-Event-ID"] == "initial"
        return httpx.Response(200, stream=stream)
    result = await install(monkeypatch, handler)._wait_for_updates("alice", 1)
    assert result["code"] == "stream_closed"
    assert stream.closed


async def test_external_cancellation_propagates_and_closes_stream(monkeypatch):
    stream = Stream("silent")
    def handler(request):
        if request.url.path.endswith("/cursor"):
            return httpx.Response(200, json={"cursor":"initial"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages":[]})
        return httpx.Response(200, stream=stream)
    task = asyncio.create_task(install(monkeypatch, handler)._wait_for_updates("alice", 120))
    await asyncio.wait_for(stream.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


async def test_event_between_lookup_and_subscription_is_recovered(monkeypatch):
    reads = 0
    stream = Stream("message")
    def handler(request):
        nonlocal reads
        if request.url.path.endswith("/cursor"):
            return httpx.Response(200, json={"cursor":"before_lookup"})
        if request.url.path.endswith("/messages"):
            reads += 1
            return httpx.Response(200, json={"messages": [] if reads == 1 else [{"message_id":"m1"}]})
        assert reads == 1
        assert request.headers["Last-Event-ID"] == "before_lookup"
        return httpx.Response(200, stream=stream)
    result = await install(monkeypatch, handler)._wait_for_updates("alice", 1)
    assert result["status"] == "event_received"
    assert result["messages"] == [{"message_id":"m1"}]
    assert result["event_cursor"] == "delivered"
    assert stream.closed


async def test_bus_down_has_distinct_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("offline", request=request)
    result = await install(monkeypatch, handler)._wait_for_updates("alice", 1)
    assert result["code"] == "bus_unavailable"


@pytest.mark.parametrize("timeout", [0,121,True,"1",1.5])
async def test_invalid_wait_deadline_never_connects(monkeypatch, timeout):
    server = install(monkeypatch, lambda request: pytest.fail("Invalid deadline reached HTTP"))
    with pytest.raises(ValueError):
        await server.execute_tool("wait_for_updates", {"agent_id":"alice","timeout":timeout})


async def test_real_hub_recovers_delivery_in_lookup_subscription_gap(secure_bus, monkeypatch):
    from agent_bus.security import async_bus_client
    server = McpServer(bus_url=secure_bus.url, agent_id="bob")
    injected = []
    async def after_response(response):
        if response.request.url.path == "/inbox/bob/messages" and not injected:
            await response.aread()
            assert response.json()["messages"] == []
            async with async_bus_client("alice", base_url=secure_bus.url) as sender:
                sent = await sender.post("/messages", json={"to_agent":"bob", "body":{"text":"In the gap"}, "idempotency_key":"gap"})
                sent.raise_for_status()
                injected.append(sent.json()["message_id"])
    monkeypatch.setattr(server, "_client", lambda timeout=30: async_bus_client(
        "bob", session=server.session, base_url=secure_bus.url, timeout=timeout,
        event_hooks={"response":[after_response]},
    ))
    result = await server._wait_for_updates("bob", 3)
    assert result["status"] == "event_received"
    assert [message["message_id"] for message in result["messages"]] == injected


async def test_archived_event_does_not_expose_stale_message_as_pending(monkeypatch):
    reads = 0
    def handler(request):
        nonlocal reads
        if request.url.path.endswith("/cursor"):
            return httpx.Response(200, json={"cursor":"before_lookup"})
        if request.url.path.endswith("/messages"):
            reads += 1
            return httpx.Response(200, json={"messages": [] if reads == 1 else [{"message_id":"other"}]})
        return httpx.Response(200, stream=Stream("message"))
    result = await install(monkeypatch, handler)._wait_for_updates("alice", 1)
    assert result["status"] == "pending_messages"
    assert "event" not in result
    assert result["messages"] == [{"message_id":"other"}]


@pytest.mark.parametrize("body", [[], None, {"messages":None}])
async def test_malformed_inbox_response_is_protocol_error(monkeypatch, body):
    def handler(request):
        if request.url.path.endswith("/cursor"):
            return httpx.Response(200, json={"cursor":"initial"})
        return httpx.Response(200, content=json.dumps(body), headers={"content-type":"application/json"})
    result = await install(monkeypatch, handler)._wait_for_updates("alice", 1)
    assert result["code"] == "protocol_error"


async def test_worker_reset_wakes_pending_poll():
    from agent_bus.worker.daemon import WorkerDaemon
    worker = object.__new__(WorkerDaemon)
    worker._wake_event = asyncio.Event()
    await worker._on_sse_event({"event":"reset", "error":"cursor_expired", "recovery":"read_inbox"})
    assert worker._wake_event.is_set()
