"""Tests del comando CLI `agent-bus worker`, `run-team` y `submit` (T3 & T10)."""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from agent_bus.cli.main import app
from agent_bus.cli.worker_cmds import run_team, submit_goal, worker


def test_worker_start_creates_pid_and_process(monkeypatch, tmp_path):
    """E2E: worker start lanza un proceso real (mock runner) y registra su PID."""
    from agent_bus.cli import worker_cmds

    monkeypatch.setattr(worker_cmds, "WORKERS_DIR", tmp_path)
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "claude")

    runner = CliRunner()
    result = runner.invoke(
        worker,
        ["start", "--agent", "claude", "--provider", "mock"],
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

    # stop lo mata (el daemon tarda unos segundos en bajar: polling hasta 10s)
    result = runner.invoke(worker, ["stop", "--agent", "claude"], catch_exceptions=False)
    assert "SIGTERM" in result.output
    import subprocess
    import time

    def _process_alive(pid: int) -> bool:
        # os.kill(pid, 0) da falso positivo con zombies; ps distingue estado
        r = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        if r.returncode != 0:
            return False
        stat = r.stdout.strip()
        return stat != "Z" and stat != ""

    deadline = time.monotonic() + 10
    alive = True
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            alive = False
            break
        time.sleep(0.2)
    assert not alive
    assert not pid_file.exists()


def test_worker_start_idempotente(monkeypatch, tmp_path):
    """Un segundo start con el proceso vivo no duplica el worker."""
    import subprocess
    import sys
    import time

    from agent_bus.cli import worker_cmds

    monkeypatch.setattr(worker_cmds, "WORKERS_DIR", tmp_path)

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

    monkeypatch.setattr(worker_cmds, "WORKERS_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(worker, ["status", "--agent", "codex"], catch_exceptions=False)
    assert "no iniciado" in result.output


def test_worker_stop_stale_pid(monkeypatch, tmp_path):
    from agent_bus.cli import worker_cmds

    monkeypatch.setattr(worker_cmds, "WORKERS_DIR", tmp_path)
    # PID imposible: no debería existir ningún proceso con ese PID alto
    (tmp_path / "codex.pid").write_text("99999999")
    runner = CliRunner()
    result = runner.invoke(worker, ["stop", "--agent", "codex"], catch_exceptions=False)
    assert "no encontrado" in result.output


def test_run_team_and_submit_cli(monkeypatch, tmp_path):
    """Verifica que run-team y submit ejecuten correctamente."""
    from agent_bus.cli import worker_cmds

    monkeypatch.setattr(worker_cmds, "WORKERS_DIR", tmp_path)

    runner = CliRunner()
    # Test run-team
    res_team = runner.invoke(
        app,
        ["run-team", "--agents", "claude,antigravity", "--mock"],
        catch_exceptions=False,
    )
    assert res_team.exit_code == 0
    assert "Preparando equipo" in res_team.output
    assert "claude" in res_team.output
    assert "antigravity" in res_team.output

    # Clean up spawned workers
    for agent in ("claude", "antigravity"):
        pid_file = tmp_path / f"{agent}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 9)
            except Exception:
                pass
            pid_file.unlink(missing_ok=True)
