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
