"""Tests del comando CLI `agent-bus worker`, `run-team` y `submit` (T3 & T10)."""

from __future__ import annotations

import os
import subprocess

import pytest
from click.testing import CliRunner

from agent_bus.cli.main import app
from agent_bus.cli.worker_cmds import worker


@pytest.fixture
def spawned_processes(monkeypatch):
    """Reap every process even when a CLI assertion fails."""
    processes = []
    original = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        proc = original(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", tracked_popen)
    try:
        yield processes
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_worker_start_creates_pid_and_process(
    monkeypatch, tmp_path, live_bus_url, spawned_processes,
):
    """E2E: worker start lanza un proceso real (mock runner) y registra su PID."""
    from agent_bus.cli import worker_cmds

    monkeypatch.setattr(worker_cmds, "_workers_dir", lambda: tmp_path)
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "claude")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        worker,
        ["start", "--agent", "claude", "--provider", "mock", "--bus-url", live_bus_url],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "iniciado" in result.output

    pid_file = tmp_path / "claude.pid"
    assert pid_file.exists()
    pid = int(pid_file.read_text().strip())
    os.kill(pid, 0)  # sigue vivo

    # status lo reporta corriendo
    result = runner.invoke(worker, ["status", "--agent", "claude"], catch_exceptions=False)
    assert "corriendo" in result.output

    # Reap the actual child, rather than treating a zombie PID as a live worker.
    result = runner.invoke(worker, ["stop", "--agent", "claude"], catch_exceptions=False)
    assert "SIGTERM" in result.output
    process = next(proc for proc in spawned_processes if proc.pid == pid)
    assert process.wait(timeout=5) is not None
    assert not pid_file.exists()


def test_worker_start_foreground_runs_daemon_without_pid(monkeypatch, tmp_path):
    from agent_bus.cli import worker_cmds

    started = []

    class FakeDaemon:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def start(self):
            started.append(self.kwargs)

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    monkeypatch.setattr(worker_cmds, "worker_environment", lambda *args, **kwargs: {
        "AGENT_BUS_AGENT_ID": "worker-claude",
        "AGENT_BUS_SESSION_FILE": "/tmp/worker-session.json",
        "AGENT_BUS_PROJECT_ROOT": str(tmp_path),
    })
    monkeypatch.setattr(worker_cmds, "_worker_provider", lambda *args, **kwargs: "mock")
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "interactive-claude")
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", "/tmp/interactive-session.json")
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", "/tmp/interactive-runtime")
    monkeypatch.setattr("agent_bus.worker.daemon.WorkerDaemon", FakeDaemon)
    monkeypatch.setattr("agent_bus.worker.runner.AgentRunner", FakeRunner)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        worker,
        ["start", "--agent", "claude", "--provider", "mock", "--foreground"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert len(started) == 1
    assert started[0]["agent_id"] == "claude"
    assert os.environ["AGENT_BUS_AGENT_ID"] == "interactive-claude"
    assert os.environ["AGENT_BUS_SESSION_FILE"] == "/tmp/interactive-session.json"
    assert os.environ["AGENT_BUS_CONFIG_DIR"] == "/tmp/interactive-runtime"
    assert not (tmp_path / "claude.pid").exists()



def test_worker_start_foreground_restores_environment_on_failure(monkeypatch, tmp_path):
    from agent_bus.cli import worker_cmds

    class FailingDaemon:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            raise RuntimeError("worker startup failed")

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(worker_cmds, "worker_environment", lambda *args, **kwargs: {
        "AGENT_BUS_AGENT_ID": "worker-claude",
        "AGENT_BUS_SESSION_FILE": "/tmp/worker-session.json",
    })
    monkeypatch.setattr(worker_cmds, "_worker_provider", lambda *args, **kwargs: "mock")
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "interactive-claude")
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", "/tmp/interactive-session.json")
    monkeypatch.setattr("agent_bus.worker.daemon.WorkerDaemon", FailingDaemon)
    monkeypatch.setattr("agent_bus.worker.runner.AgentRunner", FakeRunner)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="worker startup failed"):
        CliRunner().invoke(
            worker,
            ["start", "--agent", "claude", "--provider", "mock", "--foreground"],
            catch_exceptions=False,
        )

    assert os.environ["AGENT_BUS_AGENT_ID"] == "interactive-claude"
    assert os.environ["AGENT_BUS_SESSION_FILE"] == "/tmp/interactive-session.json"


def test_worker_start_idempotente(monkeypatch, tmp_path, spawned_processes):
    """Un segundo start con el proceso vivo no duplica el worker."""
    import subprocess
    import sys
    import time

    from agent_bus.cli import worker_cmds

    monkeypatch.setattr(worker_cmds, "_workers_dir", lambda: tmp_path)

    # proceso dormido de larga vida
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(0.2)
    (tmp_path / "claude.pid").write_text(str(proc.pid))

    runner = CliRunner()
    result = runner.invoke(worker, ["start", "--agent", "claude"], catch_exceptions=False)
    assert "ya corriendo" in result.output
    proc.terminate()
    proc.wait()


def test_worker_status_sin_pid(monkeypatch, tmp_path):
    from agent_bus.cli import worker_cmds

    monkeypatch.setattr(worker_cmds, "_workers_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(worker, ["status", "--agent", "codex"], catch_exceptions=False)
    assert "no iniciado" in result.output


def test_worker_stop_stale_pid(monkeypatch, tmp_path):
    from agent_bus.cli import worker_cmds

    monkeypatch.setattr(worker_cmds, "_workers_dir", lambda: tmp_path)
    # PID imposible: no debería existir ningún proceso con ese PID alto
    (tmp_path / "codex.pid").write_text("99999999")
    runner = CliRunner()
    result = runner.invoke(worker, ["stop", "--agent", "codex"], catch_exceptions=False)
    assert "no encontrado" in result.output


def test_run_team_and_submit_cli(monkeypatch, tmp_path, live_bus_url, spawned_processes):
    """Verifica que run-team y submit ejecuten correctamente."""
    from agent_bus.cli import worker_cmds

    monkeypatch.setattr(worker_cmds, "_workers_dir", lambda: tmp_path)
    # This command otherwise creates worktrees in the checkout running pytest.
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    # Test run-team
    res_team = runner.invoke(
        app,
        ["run-team", "--agents", "claude,antigravity", "--mock", "--bus-url", live_bus_url],
        catch_exceptions=False,
    )
    assert res_team.exit_code == 0
    assert "Preparando equipo" in res_team.output
    assert "claude" in res_team.output
    assert "antigravity" in res_team.output

    res_submit = runner.invoke(
        app, ["submit", "Review temporary project", "--bus-url", live_bus_url],
        catch_exceptions=False,
    )
    assert res_submit.exit_code == 0
    assert "Objetivo transmitido" in res_submit.output
    assert "Error comunicando" not in res_submit.output
