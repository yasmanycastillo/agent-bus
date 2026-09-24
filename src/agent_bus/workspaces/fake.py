"""In-memory workspace backend for tests."""

from __future__ import annotations

from agent_bus.workspaces.base import Workspace, WorkspaceRequest, WorkspaceSnapshot


class FakeWorkspaceBackend:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.created: list[str] = []
        self.opened: list[str] = []
        self.cleaned: list[str] = []

    async def create(self, request: WorkspaceRequest) -> Workspace:
        self.created.append(request.agent_id)
        return self.workspace

    async def open(self, request: WorkspaceRequest) -> Workspace:
        self.opened.append(request.agent_id)
        return self.workspace

    async def cleanup(self, workspace: Workspace) -> None:
        self.cleaned.append(workspace.workspace_id)

    async def snapshot(self, workspace: Workspace) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(workspace.workspace_id, "candidate", "baseline")
