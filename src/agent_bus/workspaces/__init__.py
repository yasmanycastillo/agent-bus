"""Workspace backends. Worktrees are one implementation."""

from agent_bus.workspaces.base import Workspace, WorkspaceBackend, WorkspaceRequest, WorkspaceSnapshot
from agent_bus.workspaces.direct import DirectWorkspaceBackend
from agent_bus.workspaces.fake import FakeWorkspaceBackend
from agent_bus.workspaces.worktree import WorktreeWorkspaceBackend

__all__ = [
    "DirectWorkspaceBackend",
    "FakeWorkspaceBackend",
    "Workspace",
    "WorkspaceBackend",
    "WorkspaceRequest",
    "WorkspaceSnapshot",
    "WorktreeWorkspaceBackend",
]
