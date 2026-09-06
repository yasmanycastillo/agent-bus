"""Regression tests for WorkerDaemon._commit_and_submit_review.

Git subprocesses run for real against temporary repositories; only the
authenticated HTTP client is mocked.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import AgentRunner


def _git(cwd, *args):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def primary(tmp_path):
    """Primary checkout with one commit on main."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@test")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "README.md").write_text("base")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


@pytest.fixture
def linked(primary, tmp_path):
    """Linked git worktree on its own branch."""
    path = tmp_path / "linked-worktree"
    _git(primary, "worktree", "add", "-b", "agent/linked", str(path))
    return path


class _RecordingClient:
    def __init__(self):
        self.posts = []

    async def post(self, url, **kwargs):
        self.posts.append(url)
        return _Response(200)

    async def aclose(self):
        pass


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        pass


def _daemon(worktree: Path, client) -> WorkerDaemon:
    runner = AgentRunner(agent_id="claude", worktree_dir=worktree)
    daemon = WorkerDaemon(agent_id="claude", runner=runner)
    daemon._client = client
    return daemon


def _run(coro):
    return asyncio.run(coro)


def _run_in_dir(worktree: Path, daemon: WorkerDaemon, task_id: str):
    """Run _commit_and_submit_review with the process cwd inside worktree."""

    original_cwd = os.getcwd()

    async def scenario():
        os.chdir(worktree)
        try:
            await daemon._commit_and_submit_review(task_id)
        finally:
            os.chdir(original_cwd)

    asyncio.run(scenario())


def test_linked_worktree_cwd_commits_and_submits(primary, linked):
    """(1) cwd equals the linked worktree: commit + review submission."""
    client = _RecordingClient()
    daemon = _daemon(linked, client)
    (linked / "change.txt").write_text("worker output")
    head_before = _git(linked, "rev-parse", "HEAD")
    status_before = _git(linked, "status", "--porcelain")
    assert "?? change.txt" in status_before

    _run_in_dir(linked, daemon, "T1")

    assert client.posts == ["/tasks/T1/review"]
    log = _git(linked, "log", "-1", "--format=%s")
    assert log == "feat(agent): complete T1"
    assert _git(linked, "rev-parse", "HEAD") != head_before
    assert not _git(linked, "status", "--porcelain")
    # The change is committed, not left staged.
    assert _git(linked, "diff", "--cached", "--name-only") == ""
    assert _git(linked, "diff", "HEAD", "--name-only") == ""
    assert (linked / "change.txt").read_text() == "worker output"


def test_primary_checkout_rejected_even_with_different_cwd(primary, linked):
    """(2) primary checkout is rejected even when process cwd differs.

    Daemon points at the primary checkout while the process cwd lives in the
    linked worktree. Nothing may be staged, committed, or submitted; staged
    and untracked files plus HEAD must be preserved.
    """
    head_before = _git(primary, "rev-parse", "HEAD")
    (primary / "untracked.txt").write_text("keep me")
    (primary / "staged.txt").write_text("staged content")
    _git(primary, "add", "staged.txt")

    client = _RecordingClient()
    daemon = _daemon(primary, client)

    _run_in_dir(linked, daemon, "T2")

    assert client.posts == []
    assert _git(primary, "rev-parse", "HEAD") == head_before
    status = _git(primary, "status", "--porcelain")
    assert "A  staged.txt" in status
    assert "?? untracked.txt" in status


def test_linked_worktree_from_coordinator_cwd(primary, linked, monkeypatch):
    """(3) ordinary linked worktree works even when cwd is elsewhere."""
    client = _RecordingClient()
    daemon = _daemon(linked, client)
    (linked / "feature.txt").write_text("worker output")
    monkeypatch.chdir(primary)  # coordinator cwd, distinct from the worktree

    _run(daemon._commit_and_submit_review("T3"))

    assert client.posts == ["/tasks/T3/review"]
    assert _git(linked, "log", "-1", "--format=%s") == "feat(agent): complete T3"
