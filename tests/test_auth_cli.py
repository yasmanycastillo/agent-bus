from __future__ import annotations

import asyncio
import json
import stat

from click.testing import CliRunner

from agent_bus.cli.auth_cmds import auth
from agent_bus.reputation.database import Database
from agent_bus.security import SessionStore


def test_local_provision_and_revoke(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BUS_DATABASE_PATH", str(tmp_path / "bus.db"))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "test-project")
    runner = CliRunner()
    result = runner.invoke(auth, ["create", "--agent", "alice", "--role", "admin"])
    assert result.exit_code == 0, result.output
    path = tmp_path / "credentials/alice.json"
    session = json.loads(path.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert session["token"] not in result.output
    assert session["project_id"] == "test-project"
    assert runner.invoke(auth, ["create", "--agent", "alice"]).exit_code != 0
    assert json.loads(path.read_text()) == session
    async def verify():
        db = Database(str(tmp_path / "bus.db"))
        await db.initialize()
        try:
            assert (await SessionStore(db, "test-project").authenticate(session["token"])).is_admin
        finally:
            await db.close()
    asyncio.run(verify())
    assert session["token"].encode() not in (tmp_path / "bus.db").read_bytes()
    result = runner.invoke(auth, ["revoke", "--session", session["session_id"]])
    assert result.exit_code == 0 and "revoked" in result.output
    assert runner.invoke(auth, ["create", "--agent", "../escape"]).exit_code != 0


def test_provider_generates_independent_operational_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BUS_DATABASE_PATH", str(tmp_path / "bus.db"))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "provider-project")
    runner = CliRunner()
    results = [runner.invoke(auth, ["create", "--provider", "claude"]) for _ in range(2)]
    assert all(result.exit_code == 0 for result in results), [result.output for result in results]
    paths = list((tmp_path / "credentials").glob("*.json"))
    assert len(paths) == 2
    sessions = [json.loads(path.read_text()) for path in paths]
    assert len({session["agent_id"] for session in sessions}) == 2
    assert all(session["agent_id"].startswith("claude-") and session["provider"] == "claude" for session in sessions)
    output = "".join(result.output for result in results)
    for path, session in zip(paths, sessions):
        assert path.stem == session["agent_id"]
        assert session["agent_id"] in output
        assert session["token"] not in output
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert runner.invoke(auth, ["create"]).exit_code != 0
    assert runner.invoke(auth, ["create", "--provider", "../invalid"]).exit_code != 0


def test_explicit_agent_rotation_and_project_mismatch_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_BUS_DATABASE_PATH", str(tmp_path / "bus.db"))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "project-a")
    runner = CliRunner()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for path in (first, second):
        result = runner.invoke(auth, ["create", "--agent", "shared-alice", "--provider", "claude", "--output", str(path)])
        assert result.exit_code == 0, result.output
    sessions = [json.loads(path.read_text()) for path in (first, second)]
    assert sessions[0]["agent_id"] == sessions[1]["agent_id"] == "shared-alice"
    assert sessions[0]["session_id"] != sessions[1]["session_id"]
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "project-b")
    rejected = tmp_path / "rejected.json"
    result = runner.invoke(auth, ["create", "--provider", "claude", "--output", str(rejected)])
    assert result.exit_code != 0 and "another project" in result.output
    assert not rejected.exists()
    assert all(session["token"] not in result.output for session in sessions)
