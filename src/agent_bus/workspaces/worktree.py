"""Git worktree backend. Agent-id paths stay inside this implementation."""

from __future__ import annotations

from pathlib import Path

from agent_bus.worker.worktrees import WorktreeManager
from agent_bus.workspaces.base import Workspace, WorkspaceRequest, WorkspaceSnapshot
from agent_bus.workspaces.direct import _rev_parse


class WorktreeWorkspaceBackend:
    def __init__(self, repo_dir: Path) -> None:
        self._manager = WorktreeManager(repo_dir)

    async def create(self, request: WorkspaceRequest) -> Workspace:
        info = self._manager.create(request.agent_id, request.base_ref)
        return Workspace(request.agent_id, info.path, info.branch, "worktree")

    async def open(self, request: WorkspaceRequest) -> Workspace:
        if not self._manager.exists(request.agent_id):
            raise FileNotFoundError(f"No worktree for agent {request.agent_id}")
        return Workspace(
            request.agent_id,
            self._manager.path_for(request.agent_id),
            self._manager.branch_for(request.agent_id),
            "worktree",
        )

    async def cleanup(self, workspace: Workspace) -> None:
        return None

    async def snapshot(self, workspace: Workspace) -> WorkspaceSnapshot:
        candidate = await _rev_parse(workspace.path, "HEAD")
        target = await _rev_parse(self._manager.repo_root, "HEAD")
        return WorkspaceSnapshot(workspace.workspace_id, candidate, target)
