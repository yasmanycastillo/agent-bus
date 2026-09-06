from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable


from agent_bus.security import async_bus_client
from agent_bus.worker.execution import ExecutionGuard
from agent_bus.worker.gatekeeper import (
    CodeReviewGatekeeper,
    Gatekeeper,
    ReviewDecision,
    ReviewRequest,
    Verdict,
)
from agent_bus.worker.worktrees import WorktreeManager

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
        bus_url: str | None = None,
        agent_id: str = "integrator",
        max_retries_per_task: int = 2,
        require_approval: bool = False,
        gatekeeper: Gatekeeper | None = None,
    ) -> None:
        self.repo_dir = repo_dir or Path.cwd()
        from agent_bus.config import get_bus_url
        self.bus_url = get_bus_url(bus_url)
        self.agent_id = agent_id
        self.max_retries_per_task = max_retries_per_task
        self.require_approval = require_approval
        self.gatekeeper = gatekeeper or CodeReviewGatekeeper()
        self._retry_counts: dict[str, int] = {}  # task_id -> retry count

    async def process_pending(
        self,
        worktree_for: Callable[[dict[str, Any]], Path | Awaitable[Path]],
        test_cmd: list[str] | None = None,
        target_branch: str = "main",
    ) -> list[IntegratorResult]:
        """Consume the durable ``in_review`` queue once, serially.

        The resolver owns the project policy for mapping a task to its dedicated
        checkout; the integrator never guesses a worktree from an agent name.
        """
        async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=30.0) as client:
            response = await client.get("/tasks", params={"status": "in_review"})
            response.raise_for_status()
            tasks = response.json()
        results: list[IntegratorResult] = []
        for task in tasks:
            worktree = worktree_for(task)
            if asyncio.iscoroutine(worktree):
                worktree = await worktree
            branch = str(task.get("candidate_branch") or f"agent/{task.get('owner', '')}")
            acceptance_criteria = task.get("acceptance_criteria", [])
            results.append(await self.integrate_task(
                task["task_id"], task.get("owner", "unknown"), Path(worktree), branch,
                target_branch=target_branch, test_cmd=test_cmd,
                acceptance_criteria=acceptance_criteria,
            ))
        return results

    async def run_once(self, test_cmd: list[str] | None = None, target_branch: str = "main") -> list[IntegratorResult]:
        """Process the conventional ``.worktrees/<owner>`` queue once."""
        manager = WorktreeManager(self.repo_dir)
        return await self.process_pending(
            lambda task: manager.path_for(str(task.get("owner", ""))),
            test_cmd=test_cmd,
            target_branch=target_branch,
        )

    async def run_forever(self, poll_interval_seconds: float = 5.0, test_cmd: list[str] | None = None) -> None:
        """Run a serialized integration loop guarded per repository."""
        with ExecutionGuard(self.agent_id, kind="integrator"):
            while True:
                await self.run_once(test_cmd=test_cmd)
                await asyncio.sleep(poll_interval_seconds)

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
        acceptance_criteria: list[str] | None = None,
        require_approval: bool | None = None,
    ) -> IntegratorResult:
        """Verifies candidate worktree, runs tests, and merges if green, or rejects with feedback."""
        preflight_error = await self._preflight(worktree_dir, candidate_branch, target_branch)
        if preflight_error:
            return IntegratorResult(False, False, "rejected", error=preflight_error, output=preflight_error)

        # 1. Run tests in candidate worktree
        tests_passed, test_output = await self.run_tests(worktree_dir, test_cmd)

        # 2. Retrieve candidate SHA and diff against target branch
        sha = await self._git_output("rev-parse", "HEAD", cwd=worktree_dir) or "HEAD"
        diff = await self._git_output("diff", f"{target_branch}...{candidate_branch}", cwd=worktree_dir)
        if not diff and worktree_dir.exists():
            diff = await self._git_output("diff", f"{target_branch}...HEAD", cwd=worktree_dir)

        # 3. Retrieve acceptance criteria if not provided
        if acceptance_criteria is None:
            acceptance_criteria = await self._fetch_acceptance_criteria(task_id)

        # 4. Invoke Gatekeeper to evaluate diff and test results
        review_request = ReviewRequest(
            task_id=task_id,
            sha=sha,
            diff=diff,
            test_passed=tests_passed,
            test_output=test_output,
            test_results={"passed": tests_passed, "output_snippet": test_output[:1000]},
            acceptance_criteria=acceptance_criteria or [],
            reviewer_agent_id=self.agent_id,
        )
        decision = self.gatekeeper.evaluate(review_request)

        # 5. Record the review decision in the bus audit trail (POST /reviews)
        await self._record_review(decision)

        effective_require_approval = (
            self.require_approval if require_approval is None else require_approval
        )

        # If verdict is BLOCKED, DO NOT MERGE under any policy
        if decision.verdict == Verdict.BLOCKED:
            await self._mark_task_blocked(task_id, f"Gatekeeper review blocked: {decision.reason}")
            await self._notify_bus_blocked(
                task_id, author_agent, f"Task {task_id} blocked by gatekeeper: {decision.reason}"
            )
            return IntegratorResult(
                success=False,
                merged=False,
                status="blocked",
                output=test_output,
                error=f"Task {task_id} blocked by gatekeeper: {decision.reason}",
                metadata={"review": decision.model_dump(mode="json")},
            )

        if effective_require_approval:
            # Under require_approval=True:
            if decision.verdict == Verdict.CHANGES_REQUESTED:
                retries = self._retry_counts.get(task_id, 0) + 1
                self._retry_counts[task_id] = retries

                if retries > self.max_retries_per_task:
                    await self._mark_task_blocked(
                        task_id, f"Exceeded max retries ({self.max_retries_per_task}). Gatekeeper: {decision.reason}"
                    )
                    await self._notify_bus_blocked(task_id, author_agent, test_output)
                    return IntegratorResult(
                        success=False,
                        merged=False,
                        status="blocked",
                        output=test_output,
                        error=f"Task {task_id} failed integration after {retries} retries. Gatekeeper: {decision.reason}",
                        retry_count=retries,
                        metadata={"review": decision.model_dump(mode="json")},
                    )

                # Send feedback to author with reason and evidence, and reassign task to author
                await self._notify_author_review_feedback(task_id, author_agent, decision, retries)
                return IntegratorResult(
                    success=False,
                    merged=False,
                    status="retry_requested",
                    output=test_output,
                    error=f"Gatekeeper requested changes: {decision.reason}",
                    retry_count=retries,
                    metadata={"review": decision.model_dump(mode="json")},
                )

            # Verdict is APPROVE: proceed with git merge!
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
                    metadata={"review": decision.model_dump(mode="json")},
                )

            self._retry_counts.pop(task_id, None)
            await self._mark_task_completed(task_id)
            return IntegratorResult(
                success=True,
                merged=True,
                status="integrated",
                output=merge_output,
                metadata={"review": decision.model_dump(mode="json")},
            )

        else:
            # Under require_approval=False:
            # Record audit decision, merge if tests passed and not explicitly blocked
            if not tests_passed:
                retries = self._retry_counts.get(task_id, 0) + 1
                self._retry_counts[task_id] = retries

                if retries > self.max_retries_per_task:
                    await self._mark_task_blocked(
                        task_id, f"Exceeded max integration retries ({self.max_retries_per_task})."
                    )
                    await self._notify_bus_blocked(task_id, author_agent, test_output)
                    return IntegratorResult(
                        success=False,
                        merged=False,
                        status="blocked",
                        output=test_output,
                        error=f"Task {task_id} failed integration after {retries} retries. Marked as blocked.",
                        retry_count=retries,
                        metadata={"review": decision.model_dump(mode="json")},
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
                    metadata={"review": decision.model_dump(mode="json")},
                )

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
                    metadata={"review": decision.model_dump(mode="json")},
                )

            self._retry_counts.pop(task_id, None)
            await self._mark_task_completed(task_id)
            return IntegratorResult(
                success=True,
                merged=True,
                status="integrated",
                output=merge_output,
                metadata={"review": decision.model_dump(mode="json")},
            )

    async def _merge_branches(self, candidate_branch: str, target_branch: str) -> tuple[bool, str]:
        """Merges candidate branch to target branch in main repo."""
        try:
            current = await self._git_output("symbolic-ref", "--short", "HEAD")
            if current != target_branch:
                return False, f"Target checkout is on '{current or 'detached'}', expected '{target_branch}'."
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
                abort = await asyncio.create_subprocess_exec("git", "merge", "--abort", cwd=str(self.repo_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await abort.communicate()
                return (False, out + ("\nMerge abort failed." if abort.returncode else ""))

            # Commit merge
            proc_commit = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", f"chore(merge): integrate {candidate_branch} into {target_branch}",
                cwd=str(self.repo_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            commit_out, commit_err = await proc_commit.communicate()
            commit_text = (commit_out + commit_err).decode("utf-8", errors="replace")
            if proc_commit.returncode != 0:
                abort = await asyncio.create_subprocess_exec("git", "merge", "--abort", cwd=str(self.repo_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await abort.communicate()
                return False, "Merge commit failed: " + commit_text
            return (True, "Merge completed successfully.\n" + commit_text)

        except Exception as exc:
            return (False, str(exc))

    async def _git_output(self, *args: str, cwd: Path | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd or self.repo_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace").strip() if proc.returncode == 0 else ""

    async def _preflight(self, worktree_dir: Path, candidate_branch: str, target_branch: str) -> str | None:
        """Reject ambiguous Git state before tests or mutation."""
        if not (self.repo_dir / ".git").exists():
            return None
        if candidate_branch == target_branch:
            return "Candidate and target branches must be different."
        status = await self._git_output("status", "--porcelain", cwd=worktree_dir)
        if status:
            return f"Candidate worktree is dirty; preserve uncommitted work before integrating:\n{status[:500]}"
        if not await self._git_output("rev-parse", "--verify", f"refs/heads/{candidate_branch}", cwd=worktree_dir):
            return f"Candidate branch '{candidate_branch}' does not exist."
        if not await self._git_output("rev-parse", "--verify", f"refs/heads/{target_branch}"):
            return f"Target branch '{target_branch}' does not exist."
        return None

    async def _notify_author_failure(self, task_id: str, author_agent: str, details: str, retry: int) -> None:
        """Sends a high priority feedback message to author on the bus."""
        async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
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
        async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
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
        async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
            try:
                await client.post(f"/tasks/{task_id}/done")
            except Exception as exc:
                logger.error(f"Failed to mark task {task_id} done: {exc}")

    async def _fetch_acceptance_criteria(self, task_id: str) -> list[str]:
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                resp = await client.get(f"/tasks/{task_id}")
                if resp.status_code == 200:
                    return resp.json().get("acceptance_criteria", []) or []
        except Exception as exc:
            logger.debug(f"Could not fetch task {task_id} details: {exc}")
        return []

    async def _record_review(self, decision: ReviewDecision) -> None:
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                await client.post("/reviews", json=decision.model_dump(mode="json"))
        except Exception as exc:
            logger.error(f"Failed to record review for task {decision.task_id}: {exc}")

    async def _mark_task_blocked(self, task_id: str, reason: str = "") -> None:
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                await client.post(f"/tasks/{task_id}/block", json={"reason": reason})
        except Exception as exc:
            logger.error(f"Failed to mark task {task_id} blocked: {exc}")

    async def _notify_author_review_feedback(
        self, task_id: str, author_agent: str, decision: ReviewDecision, retry: int
    ) -> None:
        """Sends a high-priority feedback message to author on the bus with Gatekeeper evidence."""
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                # Ensure task stays in_progress and assigned to author
                await client.post(f"/tasks/{task_id}/reassign", json={"new_owner": author_agent})

                msg_text = (
                    f"Gatekeeper review: changes requested for task {task_id} (Attempt {retry}/{self.max_retries_per_task}).\n"
                    f"Reason: {decision.reason}\n"
                    f"Evidence: {json.dumps(decision.evidence, indent=2)}\n"
                    "Please address the review comments and update your branch."
                )
                await client.post(
                    "/messages",
                    json={
                        "from_agent": self.agent_id,
                        "to_agent": author_agent,
                        "message_type": "inbox",
                        "body": {
                            "text": msg_text,
                            "verdict": decision.verdict.value if hasattr(decision.verdict, "value") else str(decision.verdict),
                            "reason": decision.reason,
                            "evidence": decision.evidence,
                        },
                        "reply_needed": True,
                        "related_task": task_id,
                    },
                )
        except Exception as exc:
            logger.error(f"Failed to notify author {author_agent} of review feedback: {exc}")

