from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database


@pytest.fixture(autouse=True)
def isolated_agent_config(tmp_path, monkeypatch):
    """Never read/write the developer's agent credentials or CLI session state."""
    from agent_bus import config
    from agent_bus.cli import display, main, worker_cmds
    from agent_bus.worker import auth

    config_dir = tmp_path / "agent-config"
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("AGENT_BUS_DATABASE_PATH", raising=False)
    monkeypatch.delenv("AGENT_BUS_PROJECT_ID", raising=False)
    monkeypatch.delenv("AGENT_BUS_SESSION_FILE", raising=False)
    for module in (config, main, auth):
        monkeypatch.setattr(module, "DEFAULT_CONFIG_DIR", config_dir)
    monkeypatch.setattr(display, "CURRENT_AGENT_FILE", config_dir / "current_agent")
    monkeypatch.setattr(worker_cmds, "WORKERS_DIR", config_dir / "workers")
    monkeypatch.delenv("AGENT_BUS_AGENT_ID", raising=False)
    # Individual authentication tests can override this explicitly.
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")


@pytest.fixture(autouse=True)
def allowed_test_ports(monkeypatch):
    """Real HTTP may only reach servers explicitly owned by this test."""
    ports = set()
    sync_request = httpx.HTTPTransport.handle_request
    async_request = httpx.AsyncHTTPTransport.handle_async_request

    def check(request):
        assert request.url.host == "127.0.0.1" and request.url.port in ports, (
            f"Unisolated HTTP request: {request.method} {request.url}"
        )

    def guarded_sync(self, request):
        check(request)
        return sync_request(self, request)

    async def guarded_async(self, request):
        check(request)
        return await async_request(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_async)
    return ports


@pytest.fixture
async def tmp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, "test.db"))
        await db.initialize()
        try:
            yield db
        finally:
            await db.close()


@pytest.fixture
def live_bus_url(tmp_path, allowed_test_ports):
    """A real HTTP/SSE hub on an OS-assigned port, with its own SQLite database."""
    db = Database(str(tmp_path / "live-bus.db"))
    bus = MessageBus(db, AgentRegistry(), InboxManager(db))

    @asynccontextmanager
    async def lifespan(app):
        await db.initialize()
        try:
            yield
        finally:
            await db.close()

    bus.app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(
        bus.app, host="127.0.0.1", port=0, log_level="error",
        timeout_graceful_shutdown=2,
    ))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        allowed_test_ports.add(port)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "Temporary hub failed to start"
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=3)
            allowed_test_ports.discard(port)
            assert not thread.is_alive(), "Temporary hub failed to stop"


@pytest.fixture
def unavailable_bus_url(allowed_test_ports):
    """Reserve a port without listening, guaranteeing a connection refusal."""
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        allowed_test_ports.add(port)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            allowed_test_ports.discard(port)
