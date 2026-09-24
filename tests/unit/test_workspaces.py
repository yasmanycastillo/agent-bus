import subprocess
from pathlib import Path

import pytest

from agent_bus.worker.integrator import BranchIntegrator
from agent_bus.workspaces import (
    DirectWorkspaceBackend,
    FakeWorkspaceBackend,
    Workspace,
    WorkspaceRequest,
    WorktreeWorkspaceBackend,
)


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_run_once_receives_workspace_instead_of_an_agent_path(tmp_path):
    workspace = Workspace("ws-1", tmp_path / "explicit", "feature/explicit", "fake")
    backend = FakeWorkspaceBackend(workspace)
    integrator = BranchIntegrator(repo_dir=tmp_path)

    async def process_pending(resolver, **kwargs):
        located = await resolver({"task_id": "T1", "owner": "alice"})
        return [located]

    integrator.process_pending = process_pending  # type: ignore[method-assign]
    located = (await integrator.run_once(workspace_backend=backend))[0]
    assert located.path == tmp_path / "explicit"
    assert located.branch == "feature/explicit"
    assert backend.opened == ["alice"]
    assert backend.created == []


@pytest.mark.asyncio
async def test_worktree_backend_owns_the_agent_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    backend = WorktreeWorkspaceBackend(repo)
    created = await backend.create(WorkspaceRequest("T1", "alice"))
    assert created.path == repo / ".worktrees" / "alice"
    assert created.branch == "agent/alice"
    opened = await backend.open(WorkspaceRequest("T1", "alice"))
    assert opened.path == created.path
    snapshot = await backend.snapshot(opened)
    assert snapshot.candidate_sha
    assert snapshot.target_sha
    direct = await DirectWorkspaceBackend().open(WorkspaceRequest("T1", "alice", path=repo, branch="main"))
    assert direct.kind == "direct"
    assert direct.path == repo


def test_path_checkout_requires_an_explicit_branch():
    with pytest.raises(ValueError, match="candidate_branch"):
        BranchIntegrator._checkout_from(Path("/tmp/work"), {"owner": "alice"})
    path, branch = BranchIntegrator._checkout_from(Path("/tmp/work"), {"candidate_branch": "feature/explicit"})
    assert path == Path("/tmp/work")
    assert branch == "feature/explicit"
