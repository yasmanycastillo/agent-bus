from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from agent_bus.reputation.database import Database, ProjectMismatchError
from agent_bus.security import (
    AuthenticationError, SessionStore, async_bus_client, load_session, sync_bus_client,
)
from agent_bus.worker.auth import WorkerAuth


async def test_sessions_persist_hashes_expire_revoke_and_project(tmp_db):
    store = SessionStore(tmp_db)
    session = await store.create("alice", "admin")
    principal = await store.authenticate(session["token"])
    assert principal.agent_id == "alice" and principal.is_admin
    rows = await tmp_db.conn.execute_fetchall("SELECT * FROM sessions")
    assert session["token"] not in str([tuple(row) for row in rows])
    assert len(rows[0]["token_hash"]) == 64
    other = Database(tmp_db.db_path)
    await other.initialize()
    try:
        assert await SessionStore(other).authenticate(session["token"]) == principal
        with pytest.raises(ProjectMismatchError):
            await SessionStore(other, "other").authenticate(session["token"])
        with pytest.raises(ProjectMismatchError):
            await SessionStore(other, "other").revoke(session["session_id"])
        assert await store.revoke(session["session_id"])
        assert not await store.revoke(session["session_id"])
        with pytest.raises(AuthenticationError):
            await SessionStore(other).authenticate(session["token"])
        expired = await store.create("alice")
        await tmp_db.conn.execute("UPDATE sessions SET expires_at=0")
        await tmp_db.conn.commit()
        with pytest.raises(AuthenticationError):
            await store.authenticate(expired["token"])
        with pytest.raises(AuthenticationError):
            await store.authenticate("x" * 43)
    finally:
        await other.close()


@pytest.mark.parametrize("agent,role,ttl", [("free", "agent", 1), ("../alice", "agent", 1), ("alice", "root", 1), ("alice", "agent", 0), ("alice", "agent", 2592001), ("alice", "agent", True)])
async def test_session_validation(tmp_db, agent, role, ttl):
    with pytest.raises(AuthenticationError):
        await SessionStore(tmp_db).create(agent, role, ttl)


@pytest.fixture
def credential(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_BUS_ALLOW_UNSIGNED", raising=False)
    monkeypatch.delenv("AGENT_BUS_AGENT_ID", raising=False)
    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "default")
    session = dict(token="s" * 43, agent_id="alice", session_id="session-1", project_id="default", role="agent", expires_at=time.time()+3600)
    path = tmp_path / "session.json"
    path.write_text(json.dumps(session))
    path.chmod(0o600)
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(path))
    return session, path


def test_session_file_validation(credential, monkeypatch):
    session, path = credential
    assert load_session("alice") == session
    with pytest.raises(AuthenticationError, match="identity"):
        load_session("bob")
    path.chmod(0o644)
    with pytest.raises(AuthenticationError, match="0600"):
        load_session()
    path.chmod(0o600)
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "other")
    with pytest.raises(AuthenticationError, match="project"):
        load_session()


def test_sync_client_bearer_origin_and_redirect(credential):
    session, _ = credential
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/"})
        return httpx.Response(200)
    with sync_bus_client("alice", base_url="http://localhost:8420", transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        client.get("/inbox/alice")
        assert requests[0].headers["authorization"] == "Bearer " + session["token"]
        with pytest.raises(AuthenticationError, match="origin"):
            client.get("https://evil.example/")
        with pytest.raises(AuthenticationError, match="origin"):
            client.get("/redirect")
    assert len(requests) == 2
    with pytest.raises(AuthenticationError, match="HTTPS"):
        sync_bus_client("alice", base_url="http://example.com")


async def test_async_client_snapshot_and_expiry(credential, monkeypatch):
    session, path = credential
    captured = []
    async def handler(request):
        captured.append(request.headers["authorization"])
        return httpx.Response(200)
    async with async_bus_client("alice", session=session, transport=httpx.MockTransport(handler)) as client:
        path.unlink()
        await client.get("/status")
        assert captured == ["Bearer " + session["token"]]
        monkeypatch.setattr("agent_bus.security.time.time", lambda: session["expires_at"] + 1)
        with pytest.raises(AuthenticationError, match="expired"):
            await client.get("/status")


def test_fail_closed_and_explicit_development_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_BUS_SESSION_FILE", raising=False)
    monkeypatch.delenv("AGENT_BUS_ALLOW_UNSIGNED", raising=False)
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path))
    with pytest.raises(AuthenticationError):
        sync_bus_client("alice")
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1")
    with sync_bus_client("alice", transport=httpx.MockTransport(lambda req: httpx.Response(200))) as client:
        response = client.get("/status")
        assert "authorization" not in response.request.headers
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(tmp_path / "missing"))
    with pytest.raises(AuthenticationError):
        sync_bus_client("alice")


def test_legacy_signature_cannot_choose_trust_key(tmp_path):
    trusted = WorkerAuth(tmp_path / "trusted")
    attacker = WorkerAuth(tmp_path / "attacker")
    trusted.get_or_create("alice")
    headers = attacker.sign_operation("alice", "POST", "/x", {})
    assert not trusted.verify_operation("alice", "POST", "/x", {}, headers["X-Agent-Signature"], headers["X-Agent-Public-Key"])
    assert not trusted.verify_operation("unknown", "POST", "/x", {}, headers["X-Agent-Signature"], headers["X-Agent-Public-Key"])


def test_session_file_expiry_symlink_and_path_override(credential, tmp_path):
    session, path = credential
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(AuthenticationError):
        load_session("alice", session_file=alias)
    assert load_session("alice", session_file=path) == session
    session["expires_at"] = 0
    path.write_text(json.dumps(session))
    with pytest.raises(AuthenticationError, match="expired"):
        load_session("alice")


async def test_authenticated_clients_ignore_ambient_proxy(credential, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    # HTTPX mounts proxy transports before sending requests. No environment
    # transport may be installed for our authenticated loopback clients.
    with sync_bus_client("alice") as client:
        assert not client._mounts
    async with async_bus_client("alice") as client:
        assert not client._mounts
