"""Actual CLI subprocesses talking MCP over pipes and HTTP to a provisioned hub."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters, stdio_client

import agent_bus
from agent_bus.core.bus import MessageBus
from agent_bus.security import async_bus_client


def child_parameters(secure_bus):
    # Follow the package under test, including when integration runs this file
    # from a separate worktree. Never silently test the developer's global CLI.
    source = Path(agent_bus.__file__).resolve().parent.parent
    env = os.environ.copy()
    env.update(PYTHONPATH=str(source), AGENT_BUS_AGENT_ID="bob", AGENT_BUS_ALLOW_UNSIGNED="0",
               AGENT_BUS_SESSION_FILE=str(secure_bus.paths["bob"]), PYTHONUNBUFFERED="1")
    args = ["-c", "from agent_bus.cli.main import app; app()", "mcp-server", "--agent", "bob",
            "--bus-url", secure_bus.url]
    return StdioServerParameters(command=sys.executable, args=args, env=env,
                                 cwd=str(secure_bus.paths["bob"].parent))


def payload(result):
    assert not result.is_error, result
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(block.text for block in result.content if block.type == "text"))


async def eventually(predicate, timeout=4):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
def observed_streams(monkeypatch):
    """Observe real hub subscriptions, without replacing any transport/handler."""
    seen = []
    original = MessageBus._event_response
    async def observe(self, *args, **kwargs):
        if self not in seen:
            seen.append(self)
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(MessageBus, "_event_response", observe)
    return lambda: sum(len(bus._sse_subscribers.get("bob", set())) for bus in seen)


class RawPeer:
    def __init__(self, process):
        self.process = process
        self.frames = []
        self.stdout_errors = []
        self.queue = asyncio.Queue()
        self.pending = {}
        self.reader = asyncio.create_task(self._read_stdout())
        self.stderr = asyncio.create_task(process.stderr.read())

    async def _read_stdout(self):
        try:
            while line := await self.process.stdout.readline():
                frame = json.loads(line)
                assert isinstance(frame, dict) and frame.get("jsonrpc") == "2.0", frame
                self.frames.append(frame)
                await self.queue.put(frame)
        except Exception as error:
            self.stdout_errors.append(error)
            await self.queue.put(error)
        finally:
            await self.queue.put(EOFError("MCP stdout closed"))

    async def send(self, method, *, request_id=None, params=None):
        frame = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            frame["id"] = request_id
        if params is not None:
            frame["params"] = params
        self.process.stdin.write((json.dumps(frame) + "\n").encode())
        await self.process.stdin.drain()

    async def receive(self, request_id, timeout=4):
        if request_id in self.pending:
            return self.pending.pop(request_id)
        async with asyncio.timeout(timeout):
            while True:
                frame = await self.queue.get()
                if isinstance(frame, Exception):
                    raise frame
                if "id" not in frame:
                    continue
                if frame["id"] == request_id:
                    return frame
                self.pending[frame["id"]] = frame

    async def initialize(self, version="2025-06-18"):
        await self.send("initialize", request_id=1, params={
            "protocolVersion": version, "capabilities": {},
            "clientInfo": {"name": "agent-bus-acceptance", "version": "1"},
        })
        result = await self.receive(1)
        assert result["result"]["protocolVersion"] == version
        await self.send("notifications/initialized")
        return result["result"]


@asynccontextmanager
async def raw_peer(secure_bus):
    parameters = child_parameters(secure_bus)
    process = await asyncio.create_subprocess_exec(
        parameters.command, *parameters.args, env=parameters.env, cwd=parameters.cwd,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    peer = RawPeer(process)
    try:
        yield peer
    finally:
        if not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=2)
        if not peer.reader.done():
            peer.reader.cancel()
        await asyncio.gather(peer.reader, return_exceptions=True)
        assert not peer.stdout_errors, peer.stdout_errors
        stderr = await asyncio.wait_for(peer.stderr, timeout=2)
        output = json.dumps(peer.frames).encode() + stderr
        assert all(session["token"].encode() not in output for session in secure_bus.sessions.values())


async def test_official_sdk_discovers_lists_and_calls_actual_cli(secure_bus, tmp_path):
    parameters = child_parameters(secure_bus)
    errlog_path = tmp_path / "mcp-stderr.log"
    with errlog_path.open("w") as errlog:
        async with asyncio.timeout(15):
            async with Client(stdio_client(parameters, errlog=errlog)) as client:
                assert client.protocol_version == "2026-07-28"
                assert client.server_info.name == "agent-bus"
                listed = await client.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                assert {"post_message", "read_messages", "ack_messages", "reply_message", "wait_for_updates"} <= tools.keys()
                for tool in tools.values():
                    assert not {"agent_id", "from_agent", "decided_by"} & tool.input_schema.get("properties", {}).keys()
                sent = payload(await client.call_tool("post_message", {
                    "to_agent": "alice", "text": "Real SDK subprocess", "idempotency_key": "sdk-stdio-send",
                }))
                assert sent["message_id"]
                async with async_bus_client("alice", session=secure_bus.sessions["alice"], base_url=secure_bus.url) as http:
                    response = await http.get("/inbox/alice")
                    response.raise_for_status()
                    delivered = response.json()
                    assert delivered[0]["from_agent"] == "bob"
                    assert delivered[0]["body"]["text"] == "Real SDK subprocess"
                    response = await http.post("/messages", json={
                        "to_agent": "bob", "body": {"text": "Back to SDK"}, "idempotency_key": "sdk-stdio-receive",
                    })
                    response.raise_for_status()
                received = payload(await client.call_tool("read_messages", {"limit": 10}))
                assert received["messages"][0]["from_agent"] == "alice"
                assert received["messages"][0]["body"]["text"] == "Back to SDK"
    assert all(session["token"] not in errlog_path.read_text() for session in secure_bus.sessions.values())


@pytest.mark.parametrize("version", ["2024-11-05", "2025-06-18"])
async def test_legacy_versions_negotiate_without_stdout_noise(secure_bus, version):
    async with raw_peer(secure_bus) as peer:
        initialized = await peer.initialize(version)
        assert initialized["serverInfo"]["name"] == "agent-bus"
        await peer.send("tools/list", request_id=2)
        assert (await peer.receive(2))["result"]["tools"]
        await peer.send("ping", request_id=3)
        assert (await peer.receive(3))["result"] == {}
        peer.process.stdin.close()
        assert await asyncio.wait_for(peer.process.wait(), timeout=4) == 0
        assert len(peer.frames) == 3  # Notifications do not generate responses.


async def test_wait_does_not_block_ping_or_tools_and_cancellation_closes_sse(secure_bus, observed_streams):
    async with raw_peer(secure_bus) as peer:
        await peer.initialize()
        await peer.send("tools/call", request_id=2, params={"name": "wait_for_updates", "arguments": {"timeout": 120}})
        await eventually(lambda: observed_streams() == 1)
        await peer.send("ping", request_id=3)
        await peer.send("tools/list", request_id=4)
        assert (await peer.receive(3, timeout=2))["result"] == {}
        assert (await peer.receive(4, timeout=2))["result"]["tools"]
        assert not any(frame.get("id") == 2 for frame in peer.frames)
        await peer.send("notifications/cancelled", params={"requestId": 2, "reason": "Acceptance test cancellation"})
        await eventually(lambda: observed_streams() == 0)
        await peer.send("tools/call", request_id=5, params={"name": "get_project_status", "arguments": {}})
        assert not (await peer.receive(5))["result"].get("isError", False)
        assert not any(frame.get("id") == 2 for frame in peer.frames)


async def test_protocol_errors_are_distinct_from_tool_failures(secure_bus):
    async with raw_peer(secure_bus) as peer:
        await peer.initialize()
        await peer.send("not/a/protocol/method", request_id=2)
        protocol = await peer.receive(2)
        assert protocol["error"]["code"] == -32601 and "result" not in protocol
        await peer.send("tools/call", request_id=3, params={"name": "post_message", "arguments": {"to_agent": "alice"}})
        invalid = await peer.receive(3)
        assert invalid["result"]["isError"] is True and "error" not in invalid
        await peer.send("tools/call", request_id=4, params={"name": "ack_messages", "arguments": {"message_ids": ["missing"]}})
        failure = await peer.receive(4)
        assert failure["result"]["isError"] is True and "error" not in failure
        peer.process.stdin.write(b"{malformed-json}\n")
        await peer.process.stdin.drain()
        malformed = await peer.receive(None)
        assert malformed["error"]["code"] == -32700 and "result" not in malformed
        await peer.send("ping", request_id=5)
        assert (await peer.receive(5))["result"] == {}


@pytest.mark.parametrize("broken_pipe", [False, True], ids=["stdin-eof", "stdout-closed"])
async def test_disconnect_while_waiting_stops_process_and_hub_stream(secure_bus, observed_streams, broken_pipe):
    async with raw_peer(secure_bus) as peer:
        await peer.initialize()
        await peer.send("tools/call", request_id=2, params={"name": "wait_for_updates", "arguments": {"timeout": 120}})
        await eventually(lambda: observed_streams() == 1)
        if broken_pipe:
            # Close only our child's stdout read end. A subsequent reply forces
            # the server to notice the broken pipe while another tool is waiting.
            peer.process._transport.get_pipe_transport(1).close()
            await peer.send("ping", request_id=3)
        else:
            peer.process.stdin.close()
        returncode = await asyncio.wait_for(peer.process.wait(), timeout=4)
        if not broken_pipe:
            assert returncode == 0
        await eventually(lambda: observed_streams() == 0)


async def test_stdin_eof_stops_child_even_when_stdout_is_backpressured(secure_bus):
    parameters = child_parameters(secure_bus)
    process = await asyncio.create_subprocess_exec(
        parameters.command, *parameters.args, env=parameters.env, cwd=parameters.cwd,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stderr_reader = asyncio.create_task(process.stderr.read())
    try:
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "backpressure-test", "version": "1"},
        }}
        process.stdin.write((json.dumps(initialize) + "\n").encode())
        await process.stdin.drain()
        frame = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=4))
        assert frame["result"]["protocolVersion"] == "2025-06-18"
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        for request_id in range(2, 202):
            process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}) + "\n").encode())
        await asyncio.wait_for(process.stdin.drain(), timeout=3)
        # No reads: fill the parent's StreamReader until its OS pipe pauses.
        # The child must stop on EOF even with its stdout writer awaiting drain.
        await eventually(lambda: not process.stdout._transport.is_reading())
        await asyncio.sleep(0.1)
        process.stdin.close()
        await eventually(lambda: process.returncode is not None)
        assert process.returncode == 0
    finally:
        if not process.stdin.is_closing():
            process.stdin.close()
        # Process.wait() can itself await pipe EOF when the parent has stopped
        # reading. Check the child's returncode above, then release our pipe.
        process._transport.get_pipe_transport(1).close()
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=2)
        stderr = await asyncio.wait_for(stderr_reader, timeout=2)
        assert all(session["token"].encode() not in stderr for session in secure_bus.sessions.values())


async def test_oversized_stdio_frame_stops_child_without_delivery(secure_bus):
    async with raw_peer(secure_bus) as peer:
        await peer.initialize()
        frame = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "post_message", "arguments": {
                "to_agent": "alice", "text": "x" * (4 * 1024 * 1024),
                "idempotency_key": "oversized-stdio-frame",
            },
        }}
        peer.process.stdin.write((json.dumps(frame) + "\n").encode())
        try:
            await asyncio.wait_for(peer.process.stdin.drain(), timeout=4)
        except (BrokenPipeError, ConnectionResetError):
            pass
        await asyncio.wait_for(peer.process.wait(), timeout=4)
        async with async_bus_client("alice", session=secure_bus.sessions["alice"], base_url=secure_bus.url) as client:
            response = await client.get("/inbox/alice")
            response.raise_for_status()
            assert response.json() == []
