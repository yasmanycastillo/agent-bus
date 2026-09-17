from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
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

# Untracked operational directories created by agent-bus itself on the target
# checkout. `git update-ref` / `git reset --hard` never touch them, so they are
# explicitly allowed by the dirty-target safety gate. Any other untracked path
# or any tracked modification still blocks integration.
ALLOWED_UNTRACKED_OPERATIONAL_DIRS = frozenset({".agent-bus/", ".worktrees/"})


def _blocking_target_status(porcelain_status: str) -> str:
    """Return the blocking subset of a `git status --porcelain` output.

    Ignores only the known agent-bus operational directory entries; everything
    else (tracked modifications, other untracked files) is blocking.
    """
    lines = [line for line in porcelain_status.splitlines() if line.strip()]
    blocking = [
        line for line in lines
        if not (line.startswith("?? ") and line[3:].strip() in ALLOWED_UNTRACKED_OPERATIONAL_DIRS)
    ]
    return "\n".join(blocking)


@dataclass
class PublicationOutcome:
    """Explicit model of a publication attempt on the target branch.

    ``published``: our tested integration commit was fast-forwarded onto the
    target. ``reconciliation_required``: a concurrent advance landed after our
    tested commit was published; the newer state must be reconciled by a human,
    never erased, and the author must NOT be reassigned or asked to retry.
    """

    published: bool
    reconciliation_required: bool
    output: str = ""
    error: str | None = None


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
        # The tested integration commit is kept reachable under a private ref
        # until publish or abort. Ownership is LOCAL to this call (never a
        # shared instance attribute) so concurrent integrate_task invocations
        # cannot clobber each other's cleanup. The finally runs shielded so the
        # ref is deleted even on exceptions or task cancellation.
        ref_box: dict[str, str] = {}
        try:
            return await self._integrate_task_impl(
                task_id, author_agent, worktree_dir, candidate_branch,
                target_branch=target_branch, test_cmd=test_cmd,
                acceptance_criteria=acceptance_criteria,
                require_approval=require_approval, ref_box=ref_box,
            )
        finally:
            pending_ref = ref_box.get("ref", "")
            if pending_ref:
                try:
                    await asyncio.shield(self._delete_ref(pending_ref))
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - cleanup best effort
                    logger.warning("Failed to delete pending integration ref %s", pending_ref)

    async def _integrate_task_impl(
        self,
        task_id: str,
        author_agent: str,
        worktree_dir: Path,
        candidate_branch: str,
        target_branch: str = "main",
        test_cmd: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        require_approval: bool | None = None,
        ref_box: dict[str, str] | None = None,
    ) -> IntegratorResult:
        preflight_error = await self._preflight(worktree_dir, candidate_branch, target_branch)
        if preflight_error:
            return IntegratorResult(False, False, "rejected", error=preflight_error, output=preflight_error)

        # Pin the exact reviewed commit SHA: a movable branch ref could advance
        # between review and merge (TOCTOU), so everything downstream operates
        # on this immutable SHA. (Mirrors _preflight: non-git dirs skip git checks.)
        candidate_sha = candidate_branch
        target_sha = ""
        integration_sha = ""
        if await self._is_git_repo():
            candidate_sha = await self._git_output(
                "rev-parse", f"refs/heads/{candidate_branch}", cwd=worktree_dir
            )
            if not candidate_sha:
                return IntegratorResult(
                    False, False, "rejected",
                    error=f"Candidate branch '{candidate_branch}' has no resolvable commit SHA.",
                    output="Could not resolve candidate branch HEAD.",
                )
            target_sha = await self._git_output("rev-parse", f"refs/heads/{target_branch}")

            # Build and test a temporary candidate worktree that combines the
            # pinned candidate SHA with the captured target SHA (never the
            # movable branch name), so the tested state is the state that will
            # actually be integrated. The combined commit is pinned under a
            # private ref so it cannot be garbage-collected before publication.
            integration_sha, integration_ref, tests_passed, test_output, diff = await self._test_candidate(
                candidate_sha, target_sha, worktree_dir, test_cmd, ref_box=ref_box
            )
        else:
            tests_passed, test_output = await self.run_tests(worktree_dir, test_cmd)
            # Legacy non-git path (also exercised by tests that stub _git_output):
            # resolve SHA and diff from the worktree like the pre-P0 flow did.
            candidate_sha = (
                await self._git_output("rev-parse", "HEAD", cwd=worktree_dir) or candidate_sha
            )
            diff = await self._git_output("diff", f"{target_branch}...{candidate_branch}", cwd=worktree_dir)
            if not diff and worktree_dir.exists():
                diff = await self._git_output("diff", f"{target_branch}...HEAD", cwd=worktree_dir)

        # Retrieve acceptance criteria if not provided. A fetch failure (None)
        # is distinct from an empty list: fail closed rather than review with
        # unknown criteria.
        if acceptance_criteria is None:
            acceptance_criteria = await self._fetch_acceptance_criteria(task_id)
            if acceptance_criteria is None:
                return IntegratorResult(
                    success=False,
                    merged=False,
                    status="error",
                    output=test_output,
                    error=f"Failed to fetch acceptance criteria for task {task_id}; refusing to review blind.",
                    metadata={"candidate_sha": candidate_sha, "integration_sha": integration_sha or None},
                )

        # Invoke Gatekeeper to evaluate diff and test results. The review stays
        # tied to the author's candidate SHA; the tested combined SHA travels in
        # metadata so audits point at what the author wrote.
        review_request = ReviewRequest(
            task_id=task_id,
            sha=candidate_sha,
            diff=diff,
            test_passed=tests_passed,
            test_output=test_output,
            test_results={"passed": tests_passed, "output_snippet": test_output[:1000]},
            acceptance_criteria=acceptance_criteria or [],
            reviewer_agent_id=self.agent_id,
        )
        decision = self.gatekeeper.evaluate(review_request)
        if decision.sha != candidate_sha:
            return IntegratorResult(
                success=False,
                merged=False,
                status="blocked",
                output=test_output,
                error="Gatekeeper reviewed a different commit; integration rejected.",
                metadata={"candidate_sha": candidate_sha, "integration_sha": integration_sha or None},
            )

        # Record the review decision in the bus audit trail (POST /reviews).
        # The review MUST be persisted before mutating main: if persistence
        # fails, the merged result would be unauditable, so fail closed here.
        # The bus response is authoritative (review_id/identity/created_at):
        # replace the local decision with the persisted one so every
        # downstream IntegratorResult metadata and reconciliation references
        # the record that actually exists in GET /reviews.
        persisted_decision = await self._record_review(decision)
        if persisted_decision is None:
            return IntegratorResult(
                success=False,
                merged=False,
                status="error",
                output=test_output,
                error=f"Failed to persist review decision for task {task_id}; refusing to merge unaudited.",
                metadata={"review": decision.model_dump(mode="json")},
            )
        decision = persisted_decision

        effective_require_approval = (
            self.require_approval if require_approval is None else require_approval
        )

        # If verdict is BLOCKED, DO NOT MERGE under any policy
        if decision.verdict == Verdict.BLOCKED:
            blocked = await self._mark_task_blocked(task_id, f"Gatekeeper review blocked: {decision.reason}")
            notified = await self._notify_bus_blocked(
                task_id, author_agent, f"Task {task_id} blocked by gatekeeper: {decision.reason}"
            )
            return IntegratorResult(
                success=False,
                merged=False,
                status="blocked",
                output=test_output,
                error=f"Task {task_id} blocked by gatekeeper: {decision.reason}",
                metadata={
                    "review": decision.model_dump(mode="json"),
                    "task_blocked": blocked,
                    "author_notified": notified,
                },
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
                    notified = await self._notify_bus_blocked(task_id, author_agent, test_output)
                    return IntegratorResult(
                        success=False,
                        merged=False,
                        status="blocked",
                        output=test_output,
                        error=f"Task {task_id} failed integration after {retries} retries. Gatekeeper: {decision.reason}",
                        retry_count=retries,
                        metadata={"review": decision.model_dump(mode="json"), "author_notified": notified},
                    )

                # Send feedback to author with reason and evidence, and reassign task to author
                feedback_sent = await self._notify_author_review_feedback(task_id, author_agent, decision, retries)
                return IntegratorResult(
                    success=False,
                    merged=False,
                    status="retry_requested",
                    output=test_output,
                    error=(
                        f"Gatekeeper requested changes: {decision.reason}"
                        if feedback_sent
                        else f"Gatekeeper requested changes: {decision.reason}; FAILED to deliver feedback to author — manual follow-up required"
                    ),
                    retry_count=retries,
                    metadata={"review": decision.model_dump(mode="json"), "feedback_sent": feedback_sent},
                )

            # Verdict is APPROVE: integrate the exact tested state.
            if integration_sha:
                publication = await self._publish(integration_sha, target_branch, target_sha)
                if publication.reconciliation_required:
                    # Our tested commit is published AND a concurrent writer
                    # has since advanced the target. Their state survives; we
                    # do NOT reassign the author or request a retry.
                    return IntegratorResult(
                        success=False,
                        merged=True,
                        status="reconciliation_required",
                        output=publication.output,
                        error=publication.error,
                        metadata={
                            "review": decision.model_dump(mode="json"),
                            "candidate_sha": candidate_sha,
                            "integration_sha": integration_sha,
                            "reconciliation_required": True,
                        },
                    )
                merge_ok = publication.published
                merge_output = publication.output or (publication.error or "")
            else:
                merge_ok, merge_output = await self._merge_branches(candidate_sha, target_branch)
            if not merge_ok:
                retries = self._retry_counts.get(task_id, 0) + 1
                self._retry_counts[task_id] = retries
                feedback_sent = await self._notify_author_failure(task_id, author_agent, f"Merge conflict:\n{merge_output}", retries)
                return IntegratorResult(
                    success=False,
                    merged=False,
                    status="conflict",
                    output=merge_output,
                    error=(
                        "Merge conflict detected. Sent feedback to author to rebase."
                        if feedback_sent
                        else "Merge conflict detected; FAILED to deliver feedback to author — manual follow-up required"
                    ),
                    retry_count=retries,
                    metadata={"review": decision.model_dump(mode="json"), "feedback_sent": feedback_sent},
                )

            self._retry_counts.pop(task_id, None)
            # Task completion MUST succeed before reporting merged success;
            # otherwise the bus would still show the task as in_review.
            completed, completion_error = await self._finalize_merge(task_id)
            if not completed:
                return await self._handle_done_failure(
                    task_id, author_agent, candidate_sha, integration_sha,
                    target_branch, target_sha, merge_output, decision, completion_error,
                )
            return IntegratorResult(
                success=True,
                merged=True,
                status="integrated",
                output=merge_output,
                metadata={"review": decision.model_dump(mode="json"), "candidate_sha": candidate_sha, "integration_sha": integration_sha or None},
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
                    notified = await self._notify_bus_blocked(task_id, author_agent, test_output)
                    return IntegratorResult(
                        success=False,
                        merged=False,
                        status="blocked",
                        output=test_output,
                        error=f"Task {task_id} failed integration after {retries} retries. Marked as blocked.",
                        retry_count=retries,
                        metadata={"review": decision.model_dump(mode="json"), "author_notified": notified},
                    )

                # Send feedback message to author requiring reply
                feedback_sent = await self._notify_author_failure(task_id, author_agent, test_output, retries)
                return IntegratorResult(
                    success=False,
                    merged=False,
                    status="retry_requested",
                    output=test_output,
                    error=(
                        "Tests failed in candidate branch. Feedback sent to author."
                        if feedback_sent
                        else "Tests failed in candidate branch; FAILED to deliver feedback to author — manual follow-up required"
                    ),
                    retry_count=retries,
                    metadata={"review": decision.model_dump(mode="json"), "feedback_sent": feedback_sent},
                )

            if integration_sha:
                publication = await self._publish(integration_sha, target_branch, target_sha)
                if publication.reconciliation_required:
                    # Published but concurrently advanced: preserve the newer
                    # state, never reassign the author, never request a retry.
                    return IntegratorResult(
                        success=False,
                        merged=True,
                        status="reconciliation_required",
                        output=publication.output,
                        error=publication.error,
                        metadata={
                            "review": decision.model_dump(mode="json"),
                            "candidate_sha": candidate_sha,
                            "integration_sha": integration_sha,
                            "reconciliation_required": True,
                        },
                    )
                merge_ok = publication.published
                merge_output = publication.output or (publication.error or "")
            else:
                merge_ok, merge_output = await self._merge_branches(candidate_sha, target_branch)
            if not merge_ok:
                retries = self._retry_counts.get(task_id, 0) + 1
                self._retry_counts[task_id] = retries
                feedback_sent = await self._notify_author_failure(task_id, author_agent, f"Merge conflict:\n{merge_output}", retries)
                return IntegratorResult(
                    success=False,
                    merged=False,
                    status="conflict",
                    output=merge_output,
                    error=(
                        "Merge conflict detected. Sent feedback to author to rebase."
                        if feedback_sent
                        else "Merge conflict detected; FAILED to deliver feedback to author — manual follow-up required"
                    ),
                    retry_count=retries,
                    metadata={"review": decision.model_dump(mode="json"), "feedback_sent": feedback_sent},
                )

            self._retry_counts.pop(task_id, None)
            # Task completion MUST succeed before reporting merged success;
            # otherwise the bus would still show the task as in_review.
            completed, completion_error = await self._finalize_merge(task_id)
            if not completed:
                return await self._handle_done_failure(
                    task_id, author_agent, candidate_sha, integration_sha,
                    target_branch, target_sha, merge_output, decision, completion_error,
                )
            return IntegratorResult(
                success=True,
                merged=True,
                status="integrated",
                output=merge_output,
                metadata={"review": decision.model_dump(mode="json"), "candidate_sha": candidate_sha, "integration_sha": integration_sha or None},
            )

    async def _test_candidate(
        self,
        sha: str,
        target_sha: str,
        worktree_dir: Path,
        test_cmd: list[str] | None,
        ref_box: dict[str, str] | None = None,
    ) -> tuple[str, str, bool, str, str]:
        """Build and test the exact candidate SHA combined with the captured target SHA.

        Creates a throwaway worktree detached at ``target_sha`` (never at the
        movable branch name), merges the pinned ``sha`` into it, runs the test
        suite, and captures the diff of the merged result versus ``target_sha``.
        The combined commit is pinned under a unique private ref so it stays
        reachable (GC-safe) until publish or abort.
        Returns ``(integration_sha, integration_ref, tests_passed, test_output,
        diff)``. On infrastructure failure tests are reported as failed.
        """
        work_root = Path(tempfile.mkdtemp(prefix="integrator-candidate-"))
        candidate_path: Path | None = None
        integration_ref = f"refs/integrator/pending-{uuid.uuid4().hex[:12]}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", "--detach", str(work_root / "candidate"), target_sha,
                cwd=str(self.repo_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                out = (stdout + stderr).decode("utf-8", errors="replace")
                return sha, "", False, f"Failed to prepare candidate worktree: {out}", ""
            candidate_path = work_root / "candidate"

            merge_ok, merge_output = await self._merge_in_worktree(candidate_path, sha)
            if not merge_ok:
                return sha, "", False, f"Candidate merge conflict against target {target_sha}:\n{merge_output}", ""

            integration_sha = await self._git_output("rev-parse", "HEAD", cwd=candidate_path)
            if not integration_sha:
                return sha, "", False, "Failed to resolve combined commit SHA.", ""

            # Pin the combined commit under the private ref before anything else,
            # so it cannot be garbage-collected between test and publish.
            # Register ownership in ref_box IMMEDIATELY so a cancellation at
            # any later await still triggers the wrapper's shielded cleanup.
            if not await self._update_ref(integration_ref, integration_sha, expected_old=""):
                return sha, "", False, f"Failed to pin integration commit under {integration_ref}.", ""
            if ref_box is not None:
                ref_box["ref"] = integration_ref

            diff = await self._git_output(
                "diff", f"{target_sha}...HEAD", cwd=candidate_path
            )
            tests_passed, test_output = await self.run_tests(candidate_path, test_cmd)
            return integration_sha, integration_ref, tests_passed, test_output, diff
        finally:
            if candidate_path is not None:
                prune = await asyncio.create_subprocess_exec(
                    "git", "worktree", "remove", "--force", str(candidate_path),
                    cwd=str(self.repo_dir),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await prune.communicate()
            shutil.rmtree(work_root, ignore_errors=True)

    async def _merge_in_worktree(self, worktree: Path, sha: str) -> tuple[bool, str]:
        """Merge the pinned ``sha`` into the detached target worktree."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "merge", "--no-ff", "--no-edit", sha,
                cwd=str(worktree),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                abort = await asyncio.create_subprocess_exec(
                    "git", "merge", "--abort", cwd=str(worktree),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await abort.communicate()
                return (False, out + ("\nMerge abort failed." if abort.returncode else ""))
            return (True, out)
        except Exception as exc:
            return (False, str(exc))

    async def _is_git_repo(self) -> bool:
        """True if repo_dir is a Git repository (worktrees expose '.git' as a file)."""
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--git-dir", cwd=str(self.repo_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def _integrate(
        self, integration_sha: str, target_branch: str, target_sha: str
    ) -> tuple[bool, str]:
        """Publish the tested combined commit safely, never erasing concurrent advances.

        Publication uses ``git merge --ff-only <integration_sha>`` on the
        checked-out target so Git updates the ref, index and worktree
        coherently under its own locking. The target SHA and a clean, idle
        checkout are prevalidated first. A concurrent advance of the target
        makes the ff-only merge fail (non-fast-forward) BEFORE anything is
        published: that is a plain conflict (merged=False). If the ref moved
        AFTER our tested commit was already published, the exact postcondition
        check detects it and reports reconciliation_required with
        published=True — the newer state is never erased and the author is
        never reassigned.
        """
        outcome = await self._publish(integration_sha, target_branch, target_sha)
        return (outcome.published, outcome.output if outcome.published else (outcome.error or outcome.output or ""))

    async def _publish(
        self, integration_sha: str, target_branch: str, target_sha: str
    ) -> PublicationOutcome:
        """Safe checked-out-target publication primitive (see _integrate)."""
        try:
            current = await self._git_output("symbolic-ref", "--short", "HEAD")
            if current != target_branch:
                return PublicationOutcome(
                    False, False,
                    error=f"Target checkout is on '{current or 'detached'}', expected '{target_branch}'.",
                )

            # Prevalidate: dirty tree, in-progress merge/rebase/cherry-pick, or
            # a moved ref/head all abort publication before any mutation.
            status = _blocking_target_status(await self._git_output("status", "--porcelain"))
            if status:
                return PublicationOutcome(
                    False, False,
                    error=f"Target worktree is dirty; refusing to publish:\n{status[:500]}",
                )
            for state_file in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
                if (self._git_dir() / state_file).exists():
                    return PublicationOutcome(
                        False, False,
                        error=f"Git operation in progress ({state_file}); refusing to publish.",
                    )
            head = await self._git_output("rev-parse", "HEAD")
            if target_sha and head != target_sha:
                return PublicationOutcome(
                    False, False,
                    error=(
                        f"Target '{target_branch}' moved from {target_sha} to {head} during integration; "
                        "please retry to recombine with the updated target."
                    ),
                )
            ref_now = await self._git_output("rev-parse", f"refs/heads/{target_branch}")
            if target_sha and ref_now != target_sha:
                return PublicationOutcome(
                    False, False,
                    error=(
                        f"Target ref '{target_branch}' moved from {target_sha} to {ref_now}; "
                        "please retry to recombine with the updated target."
                    ),
                )

            # Single coherent publication step: ff-only merge updates ref,
            # index and worktree together under Git's own locks. It refuses
            # non-fast-forwards, so a concurrent advance of the target can
            # never be overwritten here.
            merge = await asyncio.create_subprocess_exec(
                "git", "merge", "--ff-only", integration_sha,
                cwd=str(self.repo_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            m_out, m_err = await merge.communicate()
            m_text = m_out.decode("utf-8", errors="replace") + m_err.decode("utf-8", errors="replace")
            if merge.returncode != 0:
                # The ff-only merge failed atomically: either the target had
                # already advanced (plain pre-publication conflict, merged=False)
                # or the local checkout diverged. Nothing was published.
                return PublicationOutcome(False, False, output=m_text, error=f"Fast-forward publication failed: {m_text}")

            # Exact postcondition verification. A concurrent advance that
            # landed after our tested commit was published leaves the ref
            # pointing past integration_sha: report reconciliation, never
            # rewind, never reassign the author.
            ref_after = await self._git_output("rev-parse", f"refs/heads/{target_branch}")
            head_after = await self._git_output("rev-parse", "HEAD")
            tree_head = await self._git_output("rev-parse", "HEAD^{tree}")
            tree_expected = await self._git_output("rev-parse", f"{integration_sha}^{{tree}}")
            if ref_after == integration_sha and head_after == integration_sha and tree_head == tree_expected:
                return PublicationOutcome(
                    True, False, output=f"Published tested commit {integration_sha} onto '{target_branch}'.\n{m_text}"
                )
            # We published, but a concurrent writer has since advanced the
            # target. Their state must survive untouched.
            return PublicationOutcome(
                published=True,
                reconciliation_required=True,
                output=f"Published {integration_sha} onto '{target_branch}' but the target has since advanced "
                       f"(ref={ref_after}, head={head_after}); concurrent state preserved — reconciliation required.",
                error=(
                    f"Post-publish verification: ref={ref_after} head={head_after} "
                    f"expected={integration_sha} tree={tree_head} expected_tree={tree_expected}; "
                    "concurrent target activity detected — reconciliation required, nothing erased."
                ),
            )
        except Exception as exc:
            return PublicationOutcome(False, False, error=str(exc))

    async def _handle_done_failure(
        self,
        task_id: str,
        author_agent: str,
        candidate_sha: str,
        integration_sha: str,
        target_branch: str,
        target_sha: str,
        merge_output: str,
        decision: ReviewDecision,
        completion_error: str | None,
    ) -> IntegratorResult:
        """POST /done failed after publication: leave the tested commit published.

        No checkout reset or ref rewind is attempted: rewinding the target
        after publication races with concurrent writers and could erase their
        advances. Instead the verified tested commit stays published and the
        result demands explicit reconciliation with the exact SHAs.
        """
        await self._notify_bus_blocked(
            task_id, author_agent,
            f"Task {task_id} integrated as {integration_sha} but bus completion failed; "
            "manual reconciliation required.",
        )
        return IntegratorResult(
            success=False,
            merged=True,
            status="error",
            output=merge_output,
            error=(
                f"Published {integration_sha} onto '{target_branch}' and POST /tasks/{task_id}/done failed "
                f"({completion_error}). Target left at the verified tested commit; manual reconciliation "
                "of the bus task state is required."
            ),
            metadata={
                "review": decision.model_dump(mode="json"),
                "candidate_sha": candidate_sha,
                "integration_sha": integration_sha or None,
                "reconciliation_required": True,
            },
        )

    def _git_dir(self) -> Path:
        """Best-effort resolve of the .git dir (plain dir or worktree file)."""
        git_path = self.repo_dir / ".git"
        if git_path.is_file():
            text = git_path.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                target = Path(text[len("gitdir:"):].strip())
                return target if target.is_absolute() else (self.repo_dir / target)
        return git_path

    async def _update_ref(self, ref: str, new: str, expected_old: str = "") -> bool:
        args = ["update-ref", ref, new] + ([expected_old] if expected_old else [])
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(self.repo_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def _delete_ref(self, ref: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "git", "update-ref", "-d", ref, cwd=str(self.repo_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def _merge_branches(self, sha: str, target_branch: str) -> tuple[bool, str]:
        """Merge the pinned reviewed commit SHA into target branch in main repo."""
        try:
            current = await self._git_output("symbolic-ref", "--short", "HEAD")
            if current != target_branch:
                return False, f"Target checkout is on '{current or 'detached'}', expected '{target_branch}'."
            # Check merge possibility
            proc = await asyncio.create_subprocess_exec(
                "git", "merge", "--no-commit", "--no-ff", sha,
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
                "git", "commit", "-m", f"chore(merge): integrate {sha} into {target_branch}",
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

    async def _finalize_merge(self, task_id: str) -> tuple[bool, str | None]:
        """Mark the task completed, surfacing persistence failures to the caller."""
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                resp = await client.post(f"/tasks/{task_id}/done")
                resp.raise_for_status()
            return (True, None)
        except Exception as exc:
            logger.error(f"Failed to mark task {task_id} done: {exc}")
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
        # Target checkout must be clean and idle before we test against it.
        target_status = _blocking_target_status(await self._git_output("status", "--porcelain"))
        if target_status:
            return f"Target worktree is dirty; commit or stash before integrating:\n{target_status[:500]}"
        for state_file in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            if (self._git_dir() / state_file).exists():
                return f"Git operation in progress on target ({state_file}); refusing to integrate."
        return None

    async def _notify_author_failure(self, task_id: str, author_agent: str, details: str, retry: int) -> bool:
        """Send a high-priority feedback message to the author; False if it failed."""
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                # Ensure task stays in_progress for author
                reassigned = await client.post(f"/tasks/{task_id}/reassign", json={"new_owner": author_agent})
                reassigned.raise_for_status()

                msg_text = (
                    f"Integration test/merge failed for task {task_id} (Attempt {retry}/{self.max_retries_per_task}).\n"
                    f"Output:\n{details[:500]}\n"
                    "Please fix the failing tests in your branch and update task."
                )
                sent = await client.post(
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
                sent.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Failed to notify author {author_agent}: {exc}")
            return False

    async def _notify_bus_blocked(self, task_id: str, author_agent: str, details: str) -> bool:
        """Alert that a task was blocked; False if the bus rejected the post."""
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                resp = await client.post(
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
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Failed to post blocker for {task_id}: {exc}")
            return False

    async def _mark_task_completed(self, task_id: str) -> None:
        """Deprecated: superseded by _finalize_merge, kept for callers/tests."""
        await self._finalize_merge(task_id)

    async def _fetch_acceptance_criteria(self, task_id: str) -> list[str] | None:
        """Fetch acceptance criteria; None signals fetch failure (4xx/5xx/network).

        An actual empty list is a valid answer and is returned as ``[]``;
        ``None`` means the criteria could not be read, letting the caller
        decide instead of silently treating an error as "no criteria".
        """
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                resp = await client.get(f"/tasks/{task_id}")
                resp.raise_for_status()
                return list(resp.json().get("acceptance_criteria", []) or [])
        except Exception as exc:
            logger.error(f"Failed to fetch acceptance criteria for task {task_id}: {exc}")
            return None

    async def _record_review(self, decision: ReviewDecision) -> ReviewDecision | None:
        """Persist the review decision and return the authoritative persisted record.

        POST /reviews returns the persisted ReviewDecision (authoritative
        review_id/identity/created_at). The response is validated into a
        ReviewDecision; None means the audit trail was not written (HTTP
        failure or an unvalidatable response) and the caller must fail closed.
        """
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                resp = await client.post("/reviews", json=decision.model_dump(mode="json"))
                resp.raise_for_status()
                persisted = ReviewDecision.model_validate(resp.json())
            if not persisted.review_id:
                logger.error(f"Bus returned a review without review_id for task {decision.task_id}")
                return None
            return persisted
        except Exception as exc:
            logger.error(f"Failed to record review for task {decision.task_id}: {exc}")
            return None

    async def _mark_task_blocked(self, task_id: str, reason: str = "") -> bool:
        """Mark the task blocked; False if the bus rejected the request."""
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                resp = await client.post(f"/tasks/{task_id}/block", json={"reason": reason})
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Failed to mark task {task_id} blocked: {exc}")
            return False

    async def _notify_author_review_feedback(
        self, task_id: str, author_agent: str, decision: ReviewDecision, retry: int
    ) -> bool:
        """Send review feedback to the author; False if any post was rejected."""
        try:
            async with async_bus_client(self.agent_id, base_url=self.bus_url, timeout=10.0) as client:
                # Ensure task stays in_progress and assigned to author
                reassigned = await client.post(f"/tasks/{task_id}/reassign", json={"new_owner": author_agent})
                reassigned.raise_for_status()

                msg_text = (
                    f"Gatekeeper review: changes requested for task {task_id} (Attempt {retry}/{self.max_retries_per_task}).\n"
                    f"Reason: {decision.reason}\n"
                    f"Evidence: {json.dumps(decision.evidence, indent=2)}\n"
                    "Please address the review comments and update your branch."
                )
                sent = await client.post(
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
                sent.raise_for_status()
            return True
        except Exception as exc:
            logger.error(f"Failed to notify author {author_agent} of review feedback: {exc}")
            return False
