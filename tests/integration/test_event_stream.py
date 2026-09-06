"""Durable SSE replay and bounded wake hints across real ASGI connections."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from agent_bus.core.bus import MessageBus
from agent_bus.core.events import EventLog
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.types import Envelope, MessageType


@pytest.fixture
async def event_bus(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "0")
    bus = MessageBus(tmp_db, AgentRegistry(), InboxManager(tmp_db))
    sessions = {}
    for agent, role in (("alice", "agent"), ("bob", "agent"), ("all", "agent"), ("lead", "admin")):
        sessions[agent] = await bus.sessions.create(agent, role=role)
    return bus, sessions


def headers(sessions, agent="alice"):
    return {"Authorization": f"Bearer {sessions[agent]['token']}"}


async def deliver(bus, recipient="alice", text="Hello", push=True):
    envelope = Envelope(from_agent="bob", to_agent=recipient, message_type=MessageType.INBOX, body={"text": text})
    await bus.inbox.deliver(envelope)
    if push:
        await bus._push_to_agent(recipient, envelope)
    return envelope


class Stream:
    """Drive ASGI without buffering an infinite HTTP response in a test client."""
    def __init__(self, bus, path, auth, *, blocked_send=None):
        self.bus, self.path, self.auth = bus, path, auth
        self.blocked_send = blocked_send
        self.started = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.messages = asyncio.Queue()
        self.status = None
        self.body = b""
        self.task = None

    async def __aenter__(self):
        first = True
        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await self.disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                self.status = message["status"]
                self.started.set()
            body = message.get("body", b"")
            if self.blocked_send and b"event: message" in body:
                await self.blocked_send.wait()
            if body:
                self.body += body
                for block in body.decode().replace("\r\n", "\n").split("\n\n"):
                    values = {}
                    for line in block.splitlines():
                        key, _, value = line.partition(":")
                        if key in ("id", "event", "data"):
                            values[key] = value.lstrip()
                    if "event" in values:
                        await self.messages.put(values)
        url = urlsplit(self.path)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": url.path, "raw_path": url.path.encode(),
                 "query_string": url.query.encode(), "headers": [(key.lower().encode(), value.encode()) for key, value in self.auth.items()],
                 "server": ("test", 80), "client": ("test", 123), "root_path": ""}
        self.task = asyncio.create_task(self.bus.app(scope, receive, send))
        await asyncio.wait_for(self.started.wait(), 2)
        return self

    async def next(self, kind=None):
        async def find():
            while True:
                value = await self.messages.get()
                if kind is None or value["event"] == kind:
                    return value
        return await asyncio.wait_for(find(), 3)

    async def __aexit__(self, *exc):
        self.disconnect.set()
        try:
            await asyncio.wait_for(self.task, 2)
        finally:
            if not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)


async def test_checkpoint_validation_preserves_cursor_and_scopes(event_bus):
    bus, sessions = event_bus
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        checkpoint = (await client.get("/inbox/alice/events/cursor", headers=headers(sessions))).json()["cursor"]
        await deliver(bus)
        same = await client.get("/inbox/alice/events/cursor", params={"cursor": checkpoint}, headers=headers(sessions))
        assert same.json()["cursor"] == checkpoint
        assert (await client.get("/events/alice/cursor", params={"cursor": checkpoint}, headers=headers(sessions))).json()["cursor"] == checkpoint
        assert (await client.get("/inbox/bob/events/cursor", params={"cursor": checkpoint}, headers=headers(sessions, "bob"))).status_code == 422
        assert (await client.get("/events/all/cursor", headers=headers(sessions))).status_code == 403
        assert (await client.get("/events/all/cursor", headers=headers(sessions, "lead"))).status_code == 200
        assert (await client.get("/inbox/all/events/cursor", headers=headers(sessions, "all"))).status_code == 200
        assert (await client.get("/inbox/all/events/cursor", headers=headers(sessions))).status_code == 403


async def test_replay_after_bus_restart_and_last_event_id_precedence(event_bus):
    bus, sessions = event_bus
    checkpoint = await bus.events.checkpoint("alice")
    first = await deliver(bus, text="First")
    second = await deliver(bus, text="Second")
    await bus.db.close()
    reopened = Database(bus.db.db_path)
    await reopened.initialize()
    try:
        restarted = MessageBus(reopened, AgentRegistry(), InboxManager(reopened))
        auth = dict(headers(sessions), **{"Last-Event-ID": checkpoint})
        async with Stream(restarted, "/inbox/alice/events?cursor=invalid-ignored", auth) as stream:
            assert stream.status == 200
            a, b = await stream.next("message"), await stream.next("message")
            assert json.loads(a["data"])["message_id"] == first.message_id
            assert json.loads(b["data"])["message_id"] == second.message_id
            assert a["id"] != b["id"]
        async with Stream(restarted, "/events/alice?" + urlencode({"cursor": a["id"]}), headers(sessions)) as stream:
            replayed = await stream.next("message")
            assert json.loads(replayed["data"])["message_id"] == second.message_id
        assert not restarted._sse_subscribers["alice"]
    finally:
        await reopened.close()


async def test_commit_without_push_is_recovered_by_polling(event_bus):
    bus, sessions = event_bus
    async with Stream(bus, "/inbox/alice/events", headers(sessions)) as stream:
        assert stream.status == 200
        initial = await stream.next("checkpoint")
        assert initial["id"] == json.loads(initial["data"])["cursor"]
        envelope = await deliver(bus, push=False)
        event = await stream.next("message")
        assert json.loads(event["data"])["message_id"] == envelope.message_id
    assert not bus._sse_subscribers["alice"]


async def test_commit_between_validation_and_stream_iteration_is_not_lost(event_bus, monkeypatch):
    bus, sessions = event_bus
    checkpoint = await bus.events.checkpoint("alice")
    original = bus.events.read
    injected = []
    async def read(scope, cursor, limit=50):
        page = await original(scope, cursor, limit=limit)
        if not injected:
            injected.append(await deliver(bus, text="In subscription gap", push=False))
        return page
    monkeypatch.setattr(bus.events, "read", read)
    async with Stream(bus, "/inbox/alice/events?" + urlencode({"cursor": checkpoint}), headers(sessions)) as stream:
        event = await stream.next("message")
        assert json.loads(event["data"])["message_id"] == injected[0].message_id


async def test_invalid_cursor_is_rejected_before_sse_headers(event_bus):
    bus, sessions = event_bus
    async with Stream(bus, "/inbox/alice/events?cursor=malformed", headers(sessions)) as stream:
        assert stream.status == 422
        await stream.task
        assert json.loads(stream.body)["error"] == "cursor_invalid"
    assert not bus._sse_subscribers["alice"]


async def test_scope_gaps_emit_checkpoint_not_foreign_messages(event_bus):
    bus, sessions = event_bus
    async with Stream(bus, "/inbox/alice/events", headers(sessions)) as stream:
        before = await stream.next("checkpoint")
        await deliver(bus, recipient="bob", text="Private bob")
        after = await stream.next("checkpoint")
        assert after["id"] != before["id"]
        assert b"Private bob" not in stream.body


async def test_slow_reader_keeps_only_one_wake_hint(event_bus):
    bus, sessions = event_bus
    release_send = asyncio.Event()
    async with Stream(bus, "/inbox/alice/events", headers(sessions), blocked_send=release_send) as stream:
        await stream.next("checkpoint")
        envelope = await deliver(bus)
        for _ in range(200):
            await bus._push_to_agent("alice", envelope)
        assert len(bus._sse_subscribers["alice"]) == 1
        wake = next(iter(bus._sse_subscribers["alice"]))
        assert isinstance(wake, asyncio.Event)
        release_send.set()
        assert json.loads((await stream.next("message"))["data"])["message_id"] == envelope.message_id
    assert not bus._sse_subscribers["alice"]


async def test_session_revocation_closes_idle_stream_and_unsubscribes(event_bus):
    bus, sessions = event_bus
    async with Stream(bus, "/inbox/alice/events", headers(sessions)) as stream:
        await stream.next("checkpoint")
        await bus.sessions.revoke(sessions["alice"]["session_id"])
        await asyncio.wait_for(stream.task, 2)
    assert not bus._sse_subscribers["alice"]


async def test_expired_cursor_returns_recovery_before_headers(event_bus):
    bus, sessions = event_bus
    bus.events = EventLog(bus.db, max_events=1)
    checkpoint = await bus.events.checkpoint("alice")
    await deliver(bus, text="Old")
    await deliver(bus, text="New")
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        for path in ("/inbox/alice/events", "/inbox/alice/events/cursor"):
            response = await client.get(path, params={"cursor": checkpoint}, headers=headers(sessions))
            assert response.status_code == 410
            data = response.json()
            assert data["error"] == "cursor_expired"
            assert data["recovery"] == "read_inbox"
            assert data["cursor"] != checkpoint
            assert (await client.get("/inbox/alice/events/cursor", params={"cursor": data["cursor"]}, headers=headers(sessions))).status_code == 200
    assert len(await bus.inbox.get_inbox("alice")) == 2
    assert not bus._sse_subscribers["alice"]


async def test_expiration_during_stream_emits_reset_and_closes(event_bus, monkeypatch):
    bus, sessions = event_bus
    bus.events = EventLog(bus.db, max_events=1)
    entered, release_read = asyncio.Event(), asyncio.Event()
    original = bus.events.read
    async def read(scope, cursor, limit=50):
        if limit == 50:
            entered.set()
            await release_read.wait()
        return await original(scope, cursor, limit=limit)
    monkeypatch.setattr(bus.events, "read", read)
    async with Stream(bus, "/inbox/alice/events", headers(sessions)) as stream:
        await stream.next("checkpoint")
        await asyncio.wait_for(entered.wait(), 2)
        await deliver(bus, text="Old", push=False)
        await deliver(bus, text="New", push=False)
        release_read.set()
        reset = await stream.next("reset")
        assert json.loads(reset["data"])["error"] == "cursor_expired"
        assert json.loads(reset["data"])["recovery"] == "read_inbox"
        await asyncio.wait_for(stream.task, 2)
    assert not bus._sse_subscribers["alice"]


async def test_agent_named_all_has_personal_stream_distinct_from_global(event_bus):
    bus, sessions = event_bus
    async with Stream(bus, "/inbox/all/events", headers(sessions, "all")) as stream:
        await stream.next("checkpoint")
        await deliver(bus, recipient="alice", text="Private Alice")
        own = await deliver(bus, recipient="all", text="Own message")
        event = await stream.next("message")
        assert json.loads(event["data"])["message_id"] == own.message_id
        assert b"Private Alice" not in stream.body


async def test_replay_drains_multiple_bounded_pages_without_duplicates(event_bus, monkeypatch):
    bus, sessions = event_bus
    checkpoint = await bus.events.checkpoint("alice")
    expected = [(await deliver(bus, text=str(index), push=False)).message_id for index in range(53)]
    limits = []
    original = bus.events.read
    async def read(scope, cursor, limit=50):
        limits.append(limit)
        return await original(scope, cursor, limit=limit)
    monkeypatch.setattr(bus.events, "read", read)
    async with Stream(bus, "/inbox/alice/events?" + urlencode({"cursor": checkpoint}), headers(sessions)) as stream:
        actual = [json.loads((await stream.next("message"))["data"])["message_id"] for _ in expected]
        assert actual == expected
    assert max(limits) == 50
    assert limits.count(50) >= 2
