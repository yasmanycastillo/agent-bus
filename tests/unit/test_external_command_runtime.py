import subprocess
import sys
from pathlib import Path

import pytest

from agent_bus.core.reviews import ReviewLog
from agent_bus.core.tasks import TaskManager
from agent_bus.reputation.database import Database
from agent_bus.runtimes.external import ExternalCommandRuntime
from agent_bus.runtimes.protocol import AttemptConflict, RuntimeStartRequest


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "probe@example.com")
    git(path, "config", "user.name", "probe")
    (path / "README").write_text("base\n")
    git(path, "add", "README")
    git(path, "commit", "-m", "base")
    return path


@pytest.mark.asyncio
async def test_external_command_exports_candidate_without_owning_the_task(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_URL", "http://bus.invalid")
    work = repo(tmp_path / "repo")
    git(work, "checkout", "-b", "agent/probe")
    base = git(work, "rev-parse", "main")
    script = tmp_path / "feature.py"
    script.write_text(
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "if any(name.startswith('AGENT_BUS') for name in os.environ):\n"
        "    sys.exit(2)\n"
        "path = Path('runs.txt')\n"
        "path.write_text(path.read_text() + '1\\n' if path.exists() else '1\\n')\n"
        "Path('feature.txt').write_text('feature\\n')\n"
        "subprocess.check_call(['git', 'add', 'feature.txt', 'runs.txt'])\n"
        "subprocess.check_call(['git', 'commit', '-m', 'feature'])\n"
    )
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    await TaskManager(db).create("T-ext", "External feature")
    runtime = ExternalCommandRuntime(db, [sys.executable, str(script)])
    request = RuntimeStartRequest("att-ext", "T-ext", "external:T-ext", "probe", str(work))
    session = await runtime.start(request)
    report = await runtime.wait(session, timeout=10)
    assert report.state == "completed"
    assert report.candidate_sha == git(work, "rev-parse", "HEAD")
    assert report.candidate_sha != base
    assert git(work, "rev-parse", "main") == base
    assert (work / "runs.txt").read_text() == "1\n"
    task = await TaskManager(db).get("T-ext")
    assert task is not None and task.status.value == "pending"
    reviews = await ReviewLog(db).list_for_task("T-ext")
    assert reviews[0].sha == report.candidate_sha
    assert reviews[0].evidence["attempt_id"] == "att-ext"

    restarted = ExternalCommandRuntime(db, [sys.executable, str(script)])
    same = await restarted.start(request)
    assert same.attempt_id == "att-ext"
    assert (await restarted.wait(same, timeout=5)).state == "completed"
    assert (work / "runs.txt").read_text() == "1\n"
    assert work.exists()
    await db.close()


@pytest.mark.asyncio
async def test_external_timeout_and_cancel(tmp_path):
    work = repo(tmp_path / "repo")
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    runtime = ExternalCommandRuntime(db, ["sleep", "30"])
    started = await runtime.start(RuntimeStartRequest("att-timeout", "T-time", "external:time", "probe", str(work)))
    unknown = await runtime.wait(started, timeout=0.2)
    assert unknown.state == "unknown"
    with pytest.raises(AttemptConflict):
        await runtime.start(RuntimeStartRequest("att-retry", "T-time", "external:time:2", "probe", str(work)))
    await runtime.reconcile("att-timeout", "cancelled")

    other = ExternalCommandRuntime(db, ["sleep", "30"])
    running = await other.start(RuntimeStartRequest("att-cancel", "T-cancel", "external:cancel", "probe", str(work)))
    await other.cancel(running)
    assert (await other.status(running)).state == "cancelled"
    await db.close()
