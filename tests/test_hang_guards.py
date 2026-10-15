"""A hung test suite or a blocked reader must not stall the serialized hub."""
import os
import sys
import time

import pytest

from agent_bus.reputation.database import Database
from agent_bus.worker.integrator import run_test_command


@pytest.mark.asyncio
async def test_hung_test_command_fails_and_kills_its_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    passed, output = await run_test_command([sys.executable, "-c", script], str(tmp_path), timeout=1)
    assert not passed
    assert "timed out after 1s" in output
    assert time.monotonic() - started < 10
    child = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild survived the timeout")


@pytest.mark.asyncio
async def test_test_command_reports_exit_status(tmp_path):
    assert await run_test_command([sys.executable, "-c", "print('ok')"], str(tmp_path)) == (True, "ok\n")
    passed, _ = await run_test_command([sys.executable, "-c", "raise SystemExit(3)"], str(tmp_path))
    assert not passed


@pytest.mark.asyncio
async def test_file_database_uses_wal(tmp_path):
    # Reopen too: the migration snapshot must still back up a WAL database.
    for _ in range(2):
        database = Database(str(tmp_path / "bus.db"))
        await database.initialize()
        try:
            mode = await database.transaction(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0], write=False)
            assert mode == "wal"
        finally:
            await database.close()
