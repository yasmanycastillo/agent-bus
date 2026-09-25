import subprocess
from pathlib import Path

import pytest

from agent_bus.core.reviews import ReviewLog
from agent_bus.core.tasks import TaskManager
from agent_bus.reputation.database import Database
from agent_bus.runtimes.openhands import OpenHandsRuntime
from agent_bus.runtimes.protocol import RuntimeStartRequest


class FakeSandbox:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.ref = "sandbox-1"
        self.commands: list[str] = []
        self.closed = False

    async def execute(self, command: str) -> tuple[int, str]:
        self.commands.append(command)
        result = subprocess.run(command, cwd=self.cwd, shell=True, capture_output=True, text=True)
        return result.returncode, result.stdout + result.stderr

    async def cleanup(self) -> None:
        self.closed = True


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_openhands_runtime_keeps_task_state_and_does_not_restart(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "agent/openhands")
    base = git(repo, "rev-parse", "main")
    sandbox = FakeSandbox(repo)
    opened = 0

    async def open_sandbox():
        nonlocal opened
        opened += 1
        return sandbox

    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    await TaskManager(db).create("T-oh", "Remote feature")
    runtime = OpenHandsRuntime(db, open_sandbox)
    request = RuntimeStartRequest("att-oh", "T-oh", "openhands:T-oh", "kimi", str(repo))
    session = await runtime.start(request)
    assert session.external_ref == "openhands:sandbox-1"
    sent = await runtime.send(session, "printf 'feature\\n' > feature.txt && git add feature.txt && git commit -m feature")
    assert sent.text == "" or "feature" in sent.text or sent.attempt_id == "att-oh"
    report = await runtime.finish(session)
    assert report.state == "completed"
    assert report.candidate_sha == git(repo, "rev-parse", "HEAD")
    assert report.candidate_sha != base
    assert sandbox.closed is True
    task = await TaskManager(db).get("T-oh")
    assert task is not None and task.status.value == "pending"
    review = (await ReviewLog(db).list_for_task("T-oh"))[0]
    assert review.sha == report.candidate_sha
    assert review.evidence["attempt_id"] == "att-oh"

    again = OpenHandsRuntime(db, open_sandbox)
    same = await again.start(request)
    assert same.attempt_id == "att-oh"
    assert opened == 1
    await db.close()
