"""Project binding and pinned credentials must survive independent connections."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from agent_bus.reputation.database import Database, ProjectMismatchError
from agent_bus.security import AuthenticationError, SessionStore, async_bus_client, load_session, sync_bus_client


async def test_configured_database_is_bound_before_business_migrations(tmp_path):
    path = str(tmp_path / "bus.db")
    db = Database(path, project_id="project-a")
    await db.initialize()
    await db.conn.execute("INSERT INTO reputation(agent_id, score) VALUES ('alice', 0.8)")
    await db.conn.commit()
    await db.close()
    wrong = Database(path, project_id="project-b")
    with pytest.raises(ProjectMismatchError):
        await wrong.initialize()
    with pytest.raises(RuntimeError, match="not initialized"):
        _ = wrong.conn
    owner = Database(path, project_id="project-a")
    await owner.initialize()
    try:
        rows = await owner.conn.execute_fetchall("SELECT agent_id,score FROM reputation")
        assert [tuple(row) for row in rows] == [("alice", 0.8)]
    finally:
        await owner.close()


async def test_two_connections_race_to_bind_and_create_sessions(tmp_path):
    path = str(tmp_path / "bus.db")
    first, second = Database(path), Database(path)
    await first.initialize()
    await second.initialize()
    try:
        results = await asyncio.gather(
            SessionStore(first, "project-a").create("alice"),
            SessionStore(second, "project-b").create("bob"),
            return_exceptions=True,
        )
        winners = [result for result in results if isinstance(result, dict)]
        assert len(winners) == 1
        assert sum(isinstance(result, ProjectMismatchError) for result in results) == 1
        winner = winners[0]
        rows = await first.conn.execute_fetchall("SELECT project_id FROM sessions")
        assert [row["project_id"] for row in rows] == [winner["project_id"]]
        assert (await SessionStore(second, winner["project_id"]).authenticate(winner["token"])).agent_id == winner["agent_id"]
        loser_project = "project-b" if winner["project_id"] == "project-a" else "project-a"
        with pytest.raises(ProjectMismatchError):
            await SessionStore(first, loser_project).revoke(winner["session_id"])
        assert await SessionStore(second, winner["project_id"]).revoke(winner["session_id"])
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("legacy_projects", [("project-a",), ("project-a", "project-b")])
async def test_legacy_sessions_limit_first_adoption(tmp_path, legacy_projects):
    db = Database(str(tmp_path / "legacy.db"))
    await db.initialize()
    try:
        for index, project in enumerate(legacy_projects):
            await db.conn.execute(
                "INSERT INTO sessions VALUES (?, ?, 'alice', ?, 'agent', ?, 0)",
                (str(index), str(index), project, time.time() + 3600),
            )
        await db.conn.commit()
        with pytest.raises(ProjectMismatchError):
            await db.bind_project("project-c")
        assert not await db.conn.execute_fetchall("SELECT * FROM project_metadata")
        if len(legacy_projects) == 1:
            await db.bind_project("project-a")
            await db.bind_project("project-a")
        else:
            with pytest.raises(ProjectMismatchError):
                await db.bind_project("project-a")
            assert not await db.conn.execute_fetchall("SELECT * FROM project_metadata")
    finally:
        await db.close()


async def test_first_binding_never_commits_unrelated_transaction(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    try:
        await db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('uncommitted')")
        with pytest.raises(RuntimeError, match="committed"):
            await db.bind_project("project-a")
        await db.conn.rollback()
        assert not await db.conn.execute_fetchall("SELECT * FROM reputation")
        await db.bind_project("project-a")
        await db.conn.execute("INSERT INTO reputation(agent_id) VALUES ('still-uncommitted')")
        await db.bind_project("project-a")
        await db.conn.rollback()
        assert not await db.conn.execute_fetchall("SELECT * FROM reputation")
        assert (await db.conn.execute_fetchall("SELECT project_id FROM project_metadata"))[0][0] == "project-a"
    finally:
        await db.close()


def credentials(project="project-a"):
    return dict(token="s" * 43, agent_id="alice", session_id="session-a", project_id=project,
                role="agent", expires_at=time.time() + 3600)


def test_explicit_credential_file_ignores_current_agent_but_not_explicit_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "current_agent").write_text("stale-agent")
    path = tmp_path / "session.json"
    path.write_text(json.dumps(credentials()))
    path.chmod(0o600)
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(path))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "ambient-other")
    assert load_session(project_id="project-a")["agent_id"] == "alice"
    with pytest.raises(AuthenticationError, match="project"):
        load_session()
    with pytest.raises(AuthenticationError, match="identity"):
        load_session("bob", project_id="project-a")
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "bob")
    with pytest.raises(AuthenticationError, match="identity"):
        load_session(project_id="project-a")


@pytest.mark.parametrize("unsigned", [False, True])
async def test_clients_pin_project_header_and_validate_supplied_credentials(monkeypatch, unsigned):
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "ambient-other")
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "1" if unsigned else "0")
    observed = []
    def handler(request):
        observed.append(request)
        return httpx.Response(200)
    options = dict(project_id="project-a", base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(handler))
    if not unsigned:
        options["session"] = credentials()
    with sync_bus_client("alice", **options) as client:
        client.get("/status", headers={"X-Agent-Bus-Project": "forged"})
    async with async_bus_client("alice", **options) as client:
        await client.get("/status", headers={"X-Agent-Bus-Project": "forged"})
    assert len(observed) == 2
    assert all(request.headers["X-Agent-Bus-Project"] == "project-a" for request in observed)
    assert all(("authorization" in request.headers) == (not unsigned) for request in observed)
    with pytest.raises(AuthenticationError, match="project"):
        sync_bus_client("alice", session=credentials("other"), **options_without_session(options))


def options_without_session(options):
    return {key: value for key, value in options.items() if key != "session"}
