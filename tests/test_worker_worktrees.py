"""Tests de worker/worktrees.py (T4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_bus.worker.worktrees import WorktreeManager, WorktreeInfo


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Repo git mínimo con un commit en main."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@test")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "README.md").write_text("base")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_create_worktree(repo):
    mgr = WorktreeManager(repo_root=repo)
    info = mgr.create("claude")
    assert info.branch == "agent/claude"
    assert (info.path / ".git").exists()
    branches = _git(repo, "branch", "--list")
    assert "agent/claude" in branches


def test_create_idempotente(repo):
    mgr = WorktreeManager(repo_root=repo)
    first = mgr.create("claude")
    (first.path / "file.txt").write_text("x")
    second = mgr.create("claude")
    assert second.path == first.path
    assert (first.path / "file.txt").exists()  # no se recreó ni perdió datos


def test_multiple_agentes_aislados(repo):
    mgr = WorktreeManager(repo_root=repo)
    mgr.create("claude")
    mgr.create("agy")
    # un cambio en el worktree de claude no aparece en el de agy ni en main
    (mgr.path_for("claude") / "solo-claude.txt").write_text("x")
    assert not (mgr.path_for("agy") / "solo-claude.txt").exists()
    assert not (repo / "solo-claude.txt").exists()


def test_commit_all_y_diff(repo):
    mgr = WorktreeManager(repo_root=repo)
    mgr.create("claude")
    assert not mgr.has_changes("claude")
    assert mgr.commit_all("claude", "vacio") is None
    (mgr.path_for("claude") / "new.txt").write_text("hola")
    assert mgr.has_changes("claude")
    sha = mgr.commit_all("claude", "feat: new")
    assert sha
    assert "new.txt" in mgr.current_diff("claude")
    # el commit inicial de main es ancestro de la rama del agente
    main_sha = _git(repo, "rev-parse", "main")
    assert _git(mgr.path_for("claude"), "merge-base", "--is-ancestor", main_sha, "HEAD") == ""


def test_sync_con_main(repo):
    mgr = WorktreeManager(repo_root=repo)
    mgr.create("claude")
    # nuevo commit en main después de creado el worktree
    (repo / "base.md").write_text("cambio en main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main avanza")
    out = mgr.sync_with_base("claude")
    # tras rebase, el cambio de main es visible en el worktree
    assert (mgr.path_for("claude") / "base.md").exists()


def test_remove(repo):
    mgr = WorktreeManager(repo_root=repo)
    mgr.create("claude")
    mgr.remove("claude")
    assert not mgr.exists("claude")
    assert "agent/claude" not in _git(repo, "branch", "--list")


def test_list_all(repo):
    mgr = WorktreeManager(repo_root=repo)
    assert mgr.list_all() == []
    mgr.create("claude")
    mgr.create("agy")
    ids = [w.agent_id for w in mgr.list_all()]
    assert ids == ["agy", "claude"]


def test_no_repo_error(tmp_path):
    mgr = WorktreeManager(repo_root=tmp_path)
    assert not mgr.is_repo()
    with pytest.raises(RuntimeError, match="not a git repository"):
        mgr.create("claude")


def test_worktree_info_dict():
    info = WorktreeInfo("claude", Path("/tmp/x"), "agent/claude")
    assert info.to_dict() == {"agent_id": "claude", "path": "/tmp/x", "branch": "agent/claude"}
