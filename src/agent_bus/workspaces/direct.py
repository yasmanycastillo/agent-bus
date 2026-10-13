"""Use an explicit checkout. The path does not come from the agent id."""

from __future__ import annotations

from agent_bus.workspaces.base import Workspace, WorkspaceRequest, WorkspaceSnapshot


class DirectWorkspaceBackend:
    async def create(self, request: WorkspaceRequest) -> Workspace:
        return await self.open(request)

    async def open(self, request: WorkspaceRequest) -> Workspace:
        if request.path is None or request.branch is None:
            raise ValueError("A direct workspace needs an explicit path and branch")
        return Workspace(request.task_id or request.path.name, request.path, request.branch, "direct")

    async def cleanup(self, workspace: Workspace) -> None:
        return None

    async def snapshot(self, workspace: Workspace) -> WorkspaceSnapshot:
        candidate = await _rev_parse(workspace.path, "HEAD")
        target = await _rev_parse(workspace.path, workspace.branch)
        return WorkspaceSnapshot(workspace.workspace_id, candidate, target)


async def _rev_parse(path, ref: str) -> str:
    import asyncio
    process = await asyncio.create_subprocess_exec(
        "git", "rev-parse", ref, cwd=str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(err.decode().strip() or f"git rev-parse {ref} failed")
    return out.decode().strip()
