from __future__ import annotations

import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.worker.gatekeeper import CodeReviewGatekeeper, ReviewRequest
from tests.spikes.external_command_probe import AttemptError, ExternalCommandProbe


@contextmanager
def hub(db_path: Path, ports: set[int]):
    db = Database(str(db_path))
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
        bus.app, host="127.0.0.1", port=0, log_level="error", timeout_graceful_shutdown=2,
    ))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        ports.add(port)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=3)
            ports.discard(port)
            assert not thread.is_alive()


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


def create_task(client: httpx.Client, task_id: str) -> None:
    response = client.post("/tasks", json={"task_id": task_id, "title": task_id, "owner": "probe"})
    assert response.status_code in (200, 201), response.text


def test_restart_does_not_execute_twice(tmp_path: Path, allowed_test_ports: set[int], monkeypatch):
    monkeypatch.setenv("AGENT_BUS_URL", "http://bus.invalid")
    work = repo(tmp_path / "repo")
    script = tmp_path / "run.py"
    script.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "if any(name.startswith('AGENT_BUS') for name in os.environ):\n"
        "    sys.exit(2)\n"
        "path = Path('runs.txt')\n"
        "path.write_text(path.read_text() + '1\\n' if path.exists() else '1\\n')\n"
    )
    db = tmp_path / "bus.db"
    command = [sys_executable(), str(script)]

    with hub(db, allowed_test_ports) as url:
        with httpx.Client(base_url=url, trust_env=False) as client:
            create_task(client, "T-restart")
            attempt = ExternalCommandProbe(client).launch("T-restart", command, str(work), timeout=5)
            assert attempt.state == "completed"
    with hub(db, allowed_test_ports) as url:
        with httpx.Client(base_url=url, trust_env=False) as client:
            probe = ExternalCommandProbe(client)
            with pytest.raises(AttemptError, match="completed"):
                probe.launch("T-restart", command, str(work), timeout=5)
            status = client.get("/tasks/T-restart").json()["status"]
    assert (work / "runs.txt").read_text() == "1\n"
    assert status != "done"


def test_timeout_is_reconciled_before_retry(tmp_path: Path, allowed_test_ports: set[int]):
    work = repo(tmp_path / "repo")
    with hub(tmp_path / "bus.db", allowed_test_ports) as url:
        with httpx.Client(base_url=url, trust_env=False) as client:
            create_task(client, "T-timeout")
            probe = ExternalCommandProbe(client)
            unknown = probe.launch("T-timeout", ["sleep", "30"], str(work), timeout=0.2)
            assert unknown.state == "unknown"
            with pytest.raises(AttemptError, match="reconciled"):
                probe.launch("T-timeout", ["sleep", "30"], str(work), timeout=0.2)
            settled = probe.reconcile(unknown.attempt_id)
            assert settled.state == "cancelled"
            completed = probe.launch("T-timeout", ["true"], str(work), timeout=5)
            assert completed.state == "completed"


def test_cancel_reaches_one_terminal_state(tmp_path: Path, allowed_test_ports: set[int]):
    work = repo(tmp_path / "repo")
    with hub(tmp_path / "bus.db", allowed_test_ports) as url:
        with httpx.Client(base_url=url, trust_env=False) as client:
            create_task(client, "T-cancel")
            probe = ExternalCommandProbe(client)
            started = probe.begin("T-cancel", ["sleep", "30"], str(work))
            cancelled = probe.cancel(started.attempt_id)
            assert cancelled.state == "cancelled"
            assert probe.latest("T-cancel") == [cancelled]
            process = probe._processes[started.attempt_id]
            assert process.poll() is not None


def test_command_cannot_govern_the_candidate(tmp_path: Path, allowed_test_ports: set[int], monkeypatch):
    monkeypatch.setenv("AGENT_BUS_URL", "http://bus.invalid")
    work = repo(tmp_path / "repo")
    git(work, "checkout", "-b", "agent/probe")
    main_sha = git(work, "rev-parse", "main")
    script = tmp_path / "feature.py"
    script.write_text(
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "if any(name.startswith('AGENT_BUS') for name in os.environ):\n"
        "    sys.exit(2)\n"
        "Path('feature.txt').write_text('feature\\n')\n"
        "subprocess.check_call(['git', 'add', 'feature.txt'])\n"
        "subprocess.check_call(['git', 'commit', '-m', 'feature'])\n"
    )
    with hub(tmp_path / "bus.db", allowed_test_ports) as url:
        with httpx.Client(base_url=url, trust_env=False) as client:
            create_task(client, "T-candidate")
            attempt = ExternalCommandProbe(client).launch(
                "T-candidate", [sys_executable(), str(script)], str(work), timeout=5,
            )
            assert attempt.state == "completed"
            assert attempt.candidate_sha == git(work, "rev-parse", "HEAD")
            assert git(work, "rev-parse", "main") == main_sha
            diff = git(work, "diff", "main...HEAD")
            decision = CodeReviewGatekeeper().evaluate(ReviewRequest(
                task_id="T-candidate", sha=attempt.candidate_sha or "", diff=diff, test_passed=True,
            ))
            assert decision.sha == attempt.candidate_sha
            assert decision.verdict.value == "approve"
            assert client.get("/tasks/T-candidate").json()["status"] != "done"
            unsigned = client.post("/tasks/T-candidate/done")
            assert unsigned.status_code == 422
            stranger = client.post("/tasks/T-candidate/done", json={"agent_id": "stranger"})
            assert stranger.status_code == 409
            assert client.get("/tasks/T-candidate").json()["status"] != "done"


def sys_executable() -> str:
    import sys
    return sys.executable
