from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("agent_bus.worker.integrator")


@dataclass
class IntegratorResult:
    success: bool
    merged: bool
    status: str
    output: str = ""
    error: str | None = None
    retry_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class BranchIntegrator:
    """Automated Integrator / Tech Lead agent that tests candidate branches and manages merge semantics."""

    def __init__(
        self,
        repo_dir: Path | None = None,
        bus_url: str = "http://localhost:8420",
        agent_id: str = "integrator",
        max_retries_per_task: int = 2,
    ) -> None:
        self.repo_dir = repo_dir or Path.cwd()
        self.bus_url = bus_url.rstrip("/")
        self.agent_id = agent_id
        self.max_retries_per_task = max_retries_per_task
        self._retry_counts: dict[str, int] = {}  # task_id -> retry count

    async def run_tests(self, worktree_dir: Path, test_cmd: list[str] | None = None) -> tuple[bool, str]:
        """Runs test suite inside candidate worktree directory."""
        cmd = test_cmd or ["uv", "run", "pytest", "-q"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(worktree_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            output = stdout_bytes.decode("utf-8", errors="replace") + stderr_bytes.decode("utf-8", errors="replace")
            return (proc.returncode == 0, output)
        except Exception as exc:
            return (False, str(exc))

    async def integrate_task(
        self,
        task_id: str,
        author_agent: str,
        worktree_dir: Path,
        candidate_branch: str,
        target_branch: str = "main",
        test_cmd: list[str] | None = None,
    ) -> IntegratorResult:
        """Verifies candidate worktree, runs tests, and merges if green, or rejects with feedback."""
        # 1. Run tests in candidate worktree
        tests_passed, test_output = await self.run_tests(worktree_dir, test_cmd)

        if not tests_passed:
            retries = self._retry_counts.get(task_id, 0) + 1
            self._retry_counts[task_id] = retries

            if retries > self.max_retries_per_task:
                await self._notify_bus_blocked(task_id, author_agent, test_output)
                return IntegratorResult(
                    success=False,
                    merged=False,
                    status="blocked",
                    output=test_output,
                    error=f"Task {task_id} failed integration after {retries} retries. Marked as blocked.",
                    retry_count=retries,
                )

            # Send feedback message to author requiring reply
            await self._notify_author_failure(task_id, author_agent, test_output, retries)
            return IntegratorResult(
                success=False,
                merged=False,
                status="retry_requested",
                output=test_output,
                error="Tests failed in candidate branch. Feedback sent to author.",
                retry_count=retries,
            )

        # 2. Attempt git merge into target branch
        merge_ok, merge_output = await self._merge_branches(candidate_branch, target_branch)
        if not merge_ok:
            retries = self._retry_counts.get(task_id, 0) + 1
            self._retry_counts[task_id] = retries
            await self._notify_author_failure(task_id, author_agent, f"Merge conflict:\n{merge_output}", retries)
            return IntegratorResult(
                success=False,
                merged=False,
                status="conflict",
                output=merge_output,
                error="Merge conflict detected. Sent feedback to author to rebase.",
                retry_count=retries,
            )

        # 3. Mark task done and clear retries
        self._retry_counts.pop(task_id, None)
        await self._mark_task_completed(task_id)

        return IntegratorResult(
            success=True,
            merged=True,
            status="integrated",
            output=merge_output,
        )

    async def _merge_branches(self, candidate_branch: str, target_branch: str) -> tuple[bool, str]:
        """Merges candidate branch to target branch in main repo."""
        try:
            # Check merge possibility
            proc = await asyncio.create_subprocess_exec(
                "git", "merge", "--no-commit", "--no-ff", candidate_branch,
                cwd=str(self.repo_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                # Abort incomplete merge
                await asyncio.create_subprocess_exec("git", "merge", "--abort", cwd=str(self.repo_dir))
                return (False, out)

            # Commit merge
            proc_commit = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", f"chore(merge): integrate {candidate_branch} into {target_branch}",
                cwd=str(self.repo_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc_commit.communicate()
            return (True, "Merge completed successfully.")

        except Exception as exc:
            return (False, str(exc))

    async def _notify_author_failure(self, task_id: str, author_agent: str, details: str, retry: int) -> None:
        """Sends a high priority feedback message to author on the bus."""
        async with httpx.AsyncClient(base_url=self.bus_url, timeout=10.0) as client:
            try:
                # Ensure task stays in_progress for author
                await client.post(f"/tasks/{task_id}/reassign", json={"new_owner": author_agent})

                msg_text = (
                    f"Integration test/merge failed for task {task_id} (Attempt {retry}/{self.max_retries_per_task}).\n"
                    f"Output:\n{details[:500]}\n"
                    "Please fix the failing tests in your branch and update task."
                )
                await client.post(
                    "/messages",
                    json={
                        "from_agent": self.agent_id,
                        "to_agent": author_agent,
                        "message_type": "inbox",
                        "body": {"text": msg_text},
                        "reply_needed": True,
                        "related_task": task_id,
                    },
                )
            except Exception as exc:
                logger.error(f"Failed to notify author {author_agent}: {exc}")

    async def _notify_bus_blocked(self, task_id: str, author_agent: str, details: str) -> None:
        """Alerts that a task exceeded max integration retries and is blocked."""
        async with httpx.AsyncClient(base_url=self.bus_url, timeout=10.0) as client:
            try:
                await client.post(
                    "/messages",
                    json={
                        "from_agent": self.agent_id,
                        "to_agent": author_agent,
                        "message_type": "blocker",
                        "body": {
                            "text": f"Task {task_id} blocked: exceeded max integration retries ({self.max_retries_per_task}).",
                            "details": details[:300],
                        },
                        "reply_needed": True,
                        "related_task": task_id,
                    },
                )
            except Exception as exc:
                logger.error(f"Failed to post blocker for {task_id}: {exc}")

    async def _mark_task_completed(self, task_id: str) -> None:
        async with httpx.AsyncClient(base_url=self.bus_url, timeout=10.0) as client:
            try:
                await client.post(f"/tasks/{task_id}/done")
            except Exception as exc:
                logger.error(f"Failed to mark task {task_id} done: {exc}")
