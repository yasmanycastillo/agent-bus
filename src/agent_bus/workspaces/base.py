"""Workspace contract. The integrator receives one of these, not an agent id."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class WorkspaceRequest:
    task_id: str
    agent_id: str
    base_ref: str = "main"
    path: Path | None = None
    branch: str | None = None


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    path: Path
    branch: str
    kind: str


@dataclass(frozen=True)
class WorkspaceSnapshot:
    workspace_id: str
    candidate_sha: str
    target_sha: str


class WorkspaceBackend(Protocol):
    async def create(self, request: WorkspaceRequest) -> Workspace:
        """Create the isolated checkout."""

    async def open(self, request: WorkspaceRequest) -> Workspace:
        """Return an existing checkout without inventing one."""

    async def cleanup(self, workspace: Workspace) -> None:
        """Release backend-owned resources. The caller decides when."""

    async def snapshot(self, workspace: Workspace) -> WorkspaceSnapshot:
        """Read the candidate and target SHAs."""
