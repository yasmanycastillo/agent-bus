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
from agent_bus.workspaces.base import Workspace
from agent_bus.workspaces.worktree import WorktreeWorkspaceBackend

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


class MergeInvariantError(RuntimeError):
    """The created integration commit does not match the reviewed snapshot."""


class BranchIntegrator:
    """Automated Integrator / Tech Lead agent that tests candidate branches and manages merge semantics."""

    def __init__(
        self,
        repo_dir: Path | None = None,
        bus_url: str | None = None,
        agent_id: str = "integrator",
        max_retries_per_task: int = 2,
        require_approval: bool = True,
        gatekeeper: Gatekeeper | None = None,
        client_factory=None,
    ) -> None:
        self.repo_dir = repo_dir or Path.cwd()
        from agent_bus.config import get_bus_url
        self.bus_url = get_bus_url(bus_url)
        self._client_factory = client_factory
        self.agent_id = agent_id
        self.max_retries_per_task = max_retries_per_task
        self.require_approval = require_approval
        self.gatekeeper = gatekeeper or CodeReviewGatekeeper()
        self._retry_counts: dict[str, int] = {}

    def _open_bus(self, timeout: float):
        if self._client_factory is not None:
            return self._client_factory(timeout)
        return async_bus_client(self.agent_id, base_url=self.bus_url, timeout=timeout)

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
        async with self._open_bus(30.0) as client:
            response = await client.get("/tasks", params={"status": "in_review"})
            response.raise_for_status()
            tasks = response.json()
        results: list[IntegratorResult] = []
        for task in tasks:
            located = worktree_for(task)
            if asyncio.iscoroutine(located):
                located = await located
            worktree, branch = self._checkout_from(located, task)
            acceptance_criteria = task.get("acceptance_criteria", [])
            results.append(await self.integrate_task(
                task["task_id"], task.get("owner", "unknown"), worktree, branch,
                target_branch=target_branch, test_cmd=test_cmd,
                acceptance_criteria=acceptance_criteria,
            ))
        return results

    @staticmethod
    def _checkout_from(located: Workspace | Path, task: dict[str, Any]) -> tuple[Path, str]:
        if isinstance(located, Workspace):
            return located.path, located.branch
        branch = str(task.get("candidate_branch") or "")
        if not branch:
            raise ValueError("candidate_branch is required when the checkout is only a path")
        return Path(located), branch

    async def run_once(
        self,
        test_cmd: list[str] | None = None,
        target_branch: str = "main",
        workspace_backend=None,
    ) -> list[IntegratorResult]:
        """Process the review queue using a workspace backend."""
        backend = workspace_backend or WorktreeWorkspaceBackend(self.repo_dir)

        async def resolve(task: dict[str, Any]) -> Workspace:
            from agent_bus.workspaces.base import WorkspaceRequest
            return await backend.open(WorkspaceRequest(
                task_id=str(task.get("task_id") or ""),
                agent_id=str(task.get("owner") or ""),
                base_ref=target_branch,
            ))

        return await self.process_pending(
            resolve,
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

        try:
            snapshot = await self._snapshot(worktree_dir, candidate_branch, target_branch)
        except ValueError as exc:
            return IntegratorResult(False, False, "rejected", error=str(exc))

        if await self._candidate_is_merged(snapshot["sha"], target_branch):
            return await self._finish_recorded_merge(task_id, "candidate already on target")

        # Test and review the same immutable candidate and target baseline.
        tests_passed, test_output = await self.run_tests(worktree_dir, test_cmd)
        try:
            after_tests = await self._snapshot(worktree_dir, candidate_branch, target_branch)
            if after_tests != snapshot:
                raise ValueError("Candidate or target changed during validation; run validation again.")
        except ValueError as exc:
            return IntegratorResult(False, False, "rejected", error=str(exc), output=test_output)

        # 2. Retrieve candidate SHA and diff against target branch
        sha = snapshot["sha"]
        diff = await self._git_output("diff", f"{snapshot['target_sha']}...{sha}", cwd=worktree_dir)

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
        if decision.sha != sha:
            return IntegratorResult(False, False, "blocked", output=test_output,
                                    error="Gatekeeper reviewed a different commit; integration rejected.")

        # 5. Record the review decision in the bus audit trail (POST /reviews)
        await self._record_review(decision)

        effective_require_approval = (
            self.require_approval if require_approval is None else require_approval
        )

        # If verdict is BLOCKED, DO NOT MERGE under any policy
        evidence_gap = await self._evidence_gap(task_id, sha, snapshot["target_sha"], decision.verdict.value)
        if evidence_gap:
            reason = f"Evidence policy rejected integration: {evidence_gap}"
            await self._mark_task_blocked(task_id, reason)
            await self._notify_bus_blocked(task_id, author_agent, reason, test_output)
            return IntegratorResult(
                success=False, merged=False, status="blocked", output=test_output, error=reason,
                metadata={"review": decision.model_dump(mode="json")},
            )

        if decision.verdict == Verdict.BLOCKED:
            reason = f"Gatekeeper review blocked: {decision.reason}"
            await self._mark_task_blocked(task_id, reason)
            await self._notify_bus_blocked(
                task_id, author_agent, reason
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
                    task_reason = (
                        f"Exceeded max retries ({self.max_retries_per_task}). "
                        f"Gatekeeper: {decision.reason}"
                    )
                    inbox_reason = (
                        f"Exceeded max retries ({self.max_retries_per_task}) "
                        "after Gatekeeper requested changes."
                    )
                    await self._mark_task_blocked(task_id, task_reason)
                    await self._notify_bus_blocked(task_id, author_agent, inbox_reason, test_output)
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
            if await self._candidate_is_merged(sha, target_branch):
                return await self._record_merge(task_id, decision, "candidate already on target", sha, target_branch)
            try:
                merge_ok, merge_output = await self._merge_branches(
                    sha, target_branch, expected_target_sha=snapshot["target_sha"]
                )
            except MergeInvariantError as exc:
                return await self._block_merge_invariant(task_id, author_agent, decision, test_output, exc)
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

            return await self._record_merge(task_id, decision, merge_output, sha, target_branch)

        else:
            # Explicit advisory-review mode: record the verdict and merge on
            # passing tests unless Gatekeeper blocks the candidate.
            if not tests_passed:
                retries = self._retry_counts.get(task_id, 0) + 1
                self._retry_counts[task_id] = retries

                if retries > self.max_retries_per_task:
                    reason = f"Exceeded max integration retries ({self.max_retries_per_task})."
                    await self._mark_task_blocked(task_id, reason)
                    await self._notify_bus_blocked(task_id, author_agent, reason, test_output)
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

            if await self._candidate_is_merged(sha, target_branch):
                return await self._record_merge(task_id, decision, "candidate already on target", sha, target_branch)
            try:
                merge_ok, merge_output = await self._merge_branches(
                    sha, target_branch, expected_target_sha=snapshot["target_sha"]
                )
            except MergeInvariantError as exc:
                return await self._block_merge_invariant(task_id, author_agent, decision, test_output, exc)
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

            return await self._record_merge(task_id, decision, merge_output, sha, target_branch)

    async def _record_merge(
        self, task_id: str, decision: ReviewDecision, merge_output: str, sha: str, target_branch: str,
    ) -> IntegratorResult:
        del sha, target_branch
        self._retry_counts.pop(task_id, None)
        result = await self._finish_recorded_merge(task_id, merge_output)
        result.metadata = {"review": decision.model_dump(mode="json")}
        return result

    async def _finish_recorded_merge(self, task_id: str, output: str) -> IntegratorResult:
        recorded = bool(await self._mark_task_completed(task_id))
        if not recorded:
            return IntegratorResult(
                success=False, merged=True, status="merged_unrecorded", output=output,
                error="integration merged but the task was not marked done",
            )
        return IntegratorResult(success=True, merged=True, status="integrated", output=output)

    async def _candidate_is_merged(self, sha: str, target_branch: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "git", "merge-base", "--is-ancestor", sha, target_branch,
            cwd=str(self.repo_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0

    async def _block_merge_invariant(
        self, task_id: str, author_agent: str, decision: ReviewDecision,
        test_output: str, error: MergeInvariantError,
    ) -> IntegratorResult:
        message = f"Merge invariant failed: {error}. Inspect the target checkout before retrying."
        logger.error(message)
        await self._mark_task_blocked(task_id, message)
        await self._notify_bus_blocked(task_id, author_agent, message)
        return IntegratorResult(
            success=False, merged=True, status="blocked", output=test_output,
            error=message,
            metadata={
                "review": decision.model_dump(mode="json"),
                "merge_commit_sha": await self._git_output("rev-parse", "HEAD"),
            },
        )

    async def _merge_branches(self, candidate_sha: str, target_branch: str, *, expected_target_sha: str) -> tuple[bool, str]:
        """Merge the reviewed commit ID, never a movable candidate branch."""
        try:
            current = await self._git_output("symbolic-ref", "--short", "HEAD")
            if current != target_branch:
                return False, f"Target checkout is on '{current or 'detached'}', expected '{target_branch}'."
            if await self._git_output("rev-parse", "HEAD") != expected_target_sha:
                return False, "Target changed after validation; run validation again."
            if await self._git_output("status", "--porcelain"):
                return False, "Target checkout is dirty; refusing to merge."
            # Check merge possibility
            proc = await asyncio.create_subprocess_exec(
                "git", "merge", "--no-commit", "--no-ff", candidate_sha,
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

            if await self._git_output("rev-parse", "HEAD") != expected_target_sha:
                abort = await asyncio.create_subprocess_exec(
                    "git", "merge", "--abort", cwd=str(self.repo_dir),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await abort.communicate()
                return False, "Target changed during merge; integration rejected."

            # Commit merge
            proc_commit = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", f"chore(merge): integrate {candidate_sha} into {target_branch}",
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
            first_parent = await self._git_output("rev-parse", "HEAD^1")
            second_parent = await self._git_output("rev-parse", "HEAD^2")
            if first_parent != expected_target_sha or second_parent != candidate_sha:
                raise MergeInvariantError(
                    f"expected parents ({expected_target_sha}, {candidate_sha}), "
                    f"found ({first_parent or 'missing'}, {second_parent or 'missing'})"
                )
            return (True, "Merge completed successfully.\n" + commit_text)

        except MergeInvariantError:
            raise
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
            return "Integrator requires a Git checkout."
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

    async def _snapshot(self, worktree_dir: Path, candidate_branch: str, target_branch: str) -> dict[str, str]:
        """Fail closed on ambiguous checkouts, dirty files, or a different repository."""
        common = await self._git_output("rev-parse", "--path-format=absolute", "--git-common-dir")
        candidate_common = await self._git_output("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree_dir)
        if not common or common != candidate_common:
            raise ValueError("Candidate and target must belong to the same Git repository.")
        sha = await self._git_output("rev-parse", "--verify", "HEAD", cwd=worktree_dir)
        candidate_sha = await self._git_output("rev-parse", "--verify", f"refs/heads/{candidate_branch}")
        target_sha = await self._git_output("rev-parse", "--verify", f"refs/heads/{target_branch}")
        if not sha or not target_sha or sha != candidate_sha:
            raise ValueError("Candidate worktree HEAD must match the candidate branch.")
        if await self._git_output("symbolic-ref", "--short", "HEAD") != target_branch:
            raise ValueError("Integrator checkout must be on the target branch.")
        for directory in (self.repo_dir, worktree_dir):
            if await self._git_output("status", "--porcelain", cwd=directory):
                raise ValueError("Git checkout changed or contains uncommitted files; validation rejected.")
        return {"sha": sha, "target_sha": target_sha}

    async def _notify_author_failure(self, task_id: str, author_agent: str, details: str, retry: int) -> None:
        """Sends a high priority feedback message to author on the bus."""
        async with self._open_bus(10.0) as client:
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

    async def _notify_bus_blocked(
        self, task_id: str, author_agent: str, reason: str, details: str = ""
    ) -> None:
        """Alert the author to a blocked integration with its specific reason."""
        async with self._open_bus(10.0) as client:
            try:
                await client.post(
                    "/messages",
                    json={
                        "from_agent": self.agent_id,
                        "to_agent": author_agent,
                        "message_type": "blocker",
                        "body": {
                            "text": f"Task {task_id} blocked: {reason[:300]}",
                            "details": details[:300],
                        },
                        "reply_needed": True,
                        "related_task": task_id,
                    },
                )
            except Exception as exc:
                logger.error(f"Failed to post blocker for {task_id}: {exc}")

    async def _task_owner(self, task_id: str) -> str:
        try:
            async with self._open_bus(10.0) as client:
                response = await client.get(f"/tasks/{task_id}")
                if response.status_code == 200:
                    owner = response.json().get("owner")
                    if isinstance(owner, str) and owner:
                        return owner
        except Exception as exc:
            logger.debug(f"Could not fetch owner for {task_id}: {exc}")
        return self.agent_id

    async def _mark_task_completed(self, task_id: str) -> bool:
        try:
            async with self._open_bus(10.0) as client:
                response = await client.post(
                    f"/tasks/{task_id}/integrated", json={"agent_id": self.agent_id},
                )
        except Exception as exc:
            logger.error(f"Failed to mark task {task_id} done: {exc}")
            return False
        if response.status_code != 200:
            logger.error(f"Failed to mark task {task_id} done: {response.status_code}")
            return False
        return response.json().get("status") == "done"

    async def _fetch_acceptance_criteria(self, task_id: str) -> list[str]:
        try:
            async with self._open_bus(10.0) as client:
                resp = await client.get(f"/tasks/{task_id}")
                if resp.status_code == 200:
                    return resp.json().get("acceptance_criteria", []) or []
        except Exception as exc:
            logger.debug(f"Could not fetch task {task_id} details: {exc}")
        return []

    async def _evidence_gap(self, task_id: str, candidate_sha: str, target_sha: str, verdict: str) -> str | None:
        try:
            async with self._open_bus(1.0) as client:
                policy = await client.get(f"/tasks/{task_id}/evidence-policy")
                if policy.status_code == 404:
                    return None
                if policy.status_code != 200:
                    return f"evidence policy unavailable ({policy.status_code})"
                if not policy.json().get("strict"):
                    return None
                checked = await client.post(f"/tasks/{task_id}/evidence-check", json={
                    "candidate_sha": candidate_sha,
                    "target_sha": target_sha,
                    "verdict": verdict,
                })
                if checked.status_code != 200:
                    return "evidence check failed"
                body = checked.json()
                if not body.get("ok"):
                    return body.get("error") or "required evidence is missing"
                return None
        except Exception as exc:
            return f"evidence policy unavailable: {type(exc).__name__}"

    async def _record_review(self, decision: ReviewDecision) -> None:
        try:
            async with self._open_bus(10.0) as client:
                await client.post("/reviews", json=decision.model_dump(mode="json"))
        except Exception as exc:
            logger.error(f"Failed to record review for task {decision.task_id}: {exc}")

    async def _mark_task_blocked(self, task_id: str, reason: str = "") -> None:
        try:
            owner = await self._task_owner(task_id)
            async with self._open_bus(10.0) as client:
                await client.post(f"/tasks/{task_id}/block", json={"reason": reason, "agent_id": owner})
        except Exception as exc:
            logger.error(f"Failed to mark task {task_id} blocked: {exc}")

    async def _notify_author_review_feedback(
        self, task_id: str, author_agent: str, decision: ReviewDecision, retry: int
    ) -> None:
        """Sends a high-priority feedback message to author on the bus with Gatekeeper evidence."""
        try:
            async with self._open_bus(10.0) as client:
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
