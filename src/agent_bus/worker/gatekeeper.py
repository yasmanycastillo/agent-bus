from __future__ import annotations

import enum
import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Verdict(str, enum.Enum):
    APPROVE = "approve"
    CHANGES_REQUESTED = "changes_requested"
    BLOCKED = "blocked"


# Aliases for convenience and compatibility
ReviewVerdict = Verdict
APPROVE = Verdict.APPROVE
CHANGES_REQUESTED = Verdict.CHANGES_REQUESTED
BLOCKED = Verdict.BLOCKED

DEFAULT_CRITICAL_FILES = frozenset({
    "pyproject.toml",
    "README.md",
    "AGENTS.md",
    ".gitignore",
    "src/agent_bus/__init__.py",
})

# Patterns targeting leaked secrets/credentials in diff additions
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (?:[A-Z0-9 ]+)?PRIVATE KEY-----"), "Private key detected"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key detected"),
    (
        re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?"),
        "AWS secret access key detected",
    ),
    (
        re.compile(
            r"(?i)(?:password|secret_key|api_key|access_token|auth_token|private_key)\s*[:=]\s*['\"][A-Za-z0-9_\-\.\$\/]{8,}['\"]"
        ),
        "Hardcoded secret/password token detected",
    ),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}\b"), "GitHub personal access token detected"),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), "Google API key detected"),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b"), "OpenAI/API secret key detected"),
    (
        re.compile(r"\bxox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,32}\b"),
        "Slack token detected",
    ),
]


class ReviewRequest(BaseModel):
    task_id: str
    sha: str = ""
    diff: str = ""
    test_passed: bool = True
    test_output: str = ""
    test_results: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)
    verdict: Verdict | None = None
    reason: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    reviewer_agent_id: str = "gatekeeper"
    reviewer_session_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReviewDecision(BaseModel):
    review_id: str = Field(default_factory=lambda: f"rev-{uuid.uuid4().hex[:8]}")
    task_id: str
    sha: str
    verdict: Verdict
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    test_results: dict[str, Any] = Field(default_factory=dict)
    reviewer_agent_id: str = "gatekeeper"
    reviewer_session_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Gatekeeper(ABC):
    """Abstract base class for review and merge authorization gatekeepers."""

    @abstractmethod
    def evaluate(self, request: ReviewRequest) -> ReviewDecision:
        """Evaluate candidate changes and tests, returning a ReviewDecision."""
        raise NotImplementedError


class CodeReviewGatekeeper(Gatekeeper):
    """Evaluates diff metrics, security checklist, acceptance criteria, and test outcomes."""

    def __init__(
        self,
        max_diff_lines: int = 2000,
        critical_files: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.max_diff_lines = max_diff_lines
        self.critical_files = critical_files or DEFAULT_CRITICAL_FILES

    def evaluate(self, request: ReviewRequest) -> ReviewDecision:
        diff_stats = self._analyze_diff_stats(request.diff)
        security_checklist = self._check_security(request.diff)
        criteria_checks = self._check_acceptance_criteria(request.diff, request.acceptance_criteria)

        test_results = request.test_results or {
            "passed": request.test_passed,
            "output_snippet": request.test_output[:1000] if request.test_output else "",
        }
        if "passed" not in test_results:
            test_results["passed"] = request.test_passed

        evidence = {
            "diff_stats": diff_stats,
            "test_results": test_results,
            "security_checklist": security_checklist,
            "acceptance_criteria_checks": criteria_checks,
        }

        # 1. Security violations always trigger BLOCKED
        if not security_checklist["passed"]:
            issues_summary = "; ".join(security_checklist["issues"])
            return ReviewDecision(
                task_id=request.task_id,
                sha=request.sha,
                verdict=Verdict.BLOCKED,
                reason=f"Security violation detected: {issues_summary}",
                evidence=evidence,
                test_results=test_results,
                reviewer_agent_id=request.reviewer_agent_id,
                reviewer_session_id=request.reviewer_session_id,
            )

        # 2. Test failures trigger CHANGES_REQUESTED
        if not test_results.get("passed", False):
            snippet = test_results.get("output_snippet", "")
            return ReviewDecision(
                task_id=request.task_id,
                sha=request.sha,
                verdict=Verdict.CHANGES_REQUESTED,
                reason=f"Automated test suite failed: {snippet[:200].strip() or 'tests did not pass'}",
                evidence=evidence,
                test_results=test_results,
                reviewer_agent_id=request.reviewer_agent_id,
                reviewer_session_id=request.reviewer_session_id,
            )

        # 3. Empty diff triggers CHANGES_REQUESTED
        if diff_stats["files_changed"] == 0 or diff_stats["total_lines"] == 0:
            return ReviewDecision(
                task_id=request.task_id,
                sha=request.sha,
                verdict=Verdict.CHANGES_REQUESTED,
                reason="Empty diff: no changes detected to integrate.",
                evidence=evidence,
                test_results=test_results,
                reviewer_agent_id=request.reviewer_agent_id,
                reviewer_session_id=request.reviewer_session_id,
            )

        # 4. Excessive diff size triggers CHANGES_REQUESTED
        if diff_stats["total_lines"] > self.max_diff_lines:
            return ReviewDecision(
                task_id=request.task_id,
                sha=request.sha,
                verdict=Verdict.CHANGES_REQUESTED,
                reason=(
                    f"Diff size exceeds maximum threshold "
                    f"({diff_stats['total_lines']} > {self.max_diff_lines} lines). Please split task."
                ),
                evidence=evidence,
                test_results=test_results,
                reviewer_agent_id=request.reviewer_agent_id,
                reviewer_session_id=request.reviewer_session_id,
            )

        # 5. Acceptance criteria check failures trigger CHANGES_REQUESTED
        failed_criteria = [c for c in criteria_checks if not c.get("passed", True)]
        if failed_criteria:
            reasons = [f"'{c['criterion']}': {c.get('details', 'unmet')}" for c in failed_criteria]
            return ReviewDecision(
                task_id=request.task_id,
                sha=request.sha,
                verdict=Verdict.CHANGES_REQUESTED,
                reason=f"Acceptance criteria not satisfied: {'; '.join(reasons)}",
                evidence=evidence,
                test_results=test_results,
                reviewer_agent_id=request.reviewer_agent_id,
                reviewer_session_id=request.reviewer_session_id,
            )

        # 6. All checks clean -> APPROVE
        return ReviewDecision(
            task_id=request.task_id,
            sha=request.sha,
            verdict=Verdict.APPROVE,
            reason="All gatekeeper checks passed: clean diff, green tests, security verified.",
            evidence=evidence,
            test_results=test_results,
            reviewer_agent_id=request.reviewer_agent_id,
            reviewer_session_id=request.reviewer_session_id,
        )

    def _analyze_diff_stats(self, diff: str) -> dict[str, int]:
        if not diff or not diff.strip():
            return {"files_changed": 0, "insertions": 0, "deletions": 0, "total_lines": 0}

        files_changed = 0
        insertions = 0
        deletions = 0

        for line in diff.splitlines():
            if line.startswith("diff --git "):
                files_changed += 1
            elif line.startswith("+++ ") or line.startswith("--- "):
                continue
            elif line.startswith("+"):
                insertions += 1
            elif line.startswith("-"):
                deletions += 1

        # Fallback if diff doesn't contain 'diff --git' headers but has +/- lines
        if files_changed == 0 and (insertions > 0 or deletions > 0):
            files_changed = 1

        return {
            "files_changed": files_changed,
            "insertions": insertions,
            "deletions": deletions,
            "total_lines": insertions + deletions,
        }

    def _check_security(self, diff: str) -> dict[str, Any]:
        issues: list[str] = []
        critical_deleted: list[str] = []
        secret_detected = False

        if not diff:
            return {
                "passed": True,
                "secret_detected": False,
                "critical_files_deleted": [],
                "issues": [],
            }

        lines = diff.splitlines()
        current_file: str | None = None
        is_deletion = False

        for i, line in enumerate(lines):
            if line.startswith("diff --git "):
                parts = line.split(" ")
                if len(parts) >= 4:
                    current_file = parts[2].removeprefix("a/")
                is_deletion = False
            elif line.startswith("deleted file mode "):
                is_deletion = True
            elif line.startswith("--- a/") and i + 1 < len(lines) and lines[i + 1].startswith("+++ /dev/null"):
                is_deletion = True
                current_file = line.removeprefix("--- a/")

            if is_deletion and current_file:
                for crit in self.critical_files:
                    if current_file == crit or current_file.endswith("/" + crit):
                        critical_deleted.append(current_file)
                        issues.append(f"Deleted critical file: {current_file}")
                        break
                is_deletion = False

            # Secret check on added lines only
            if line.startswith("+") and not line.startswith("+++"):
                added_content = line[1:].strip()
                for pattern, desc in SECRET_PATTERNS:
                    if pattern.search(added_content):
                        if re.search(r"\b(?:dummy|placeholder|example|<REDACTED>|mock)\b", added_content, re.IGNORECASE):
                            continue
                        secret_detected = True
                        issues.append(f"{desc} in added line: {added_content[:60]}")
                        break

        passed = len(issues) == 0
        return {
            "passed": passed,
            "secret_detected": secret_detected,
            "critical_files_deleted": list(set(critical_deleted)),
            "issues": issues,
        }

    def _check_acceptance_criteria(
        self, diff: str, criteria: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for criterion in criteria:
            criterion_clean = criterion.strip()
            if not criterion_clean:
                continue

            passed = True
            details = "Verified"

            if criterion_clean.lower().startswith("require_file:"):
                req_path = criterion_clean.split(":", 1)[1].strip()
                if req_path not in diff:
                    passed = False
                    details = f"Required file or path '{req_path}' not found in diff"
            elif criterion_clean.lower().startswith("fail_if:"):
                forbidden = criterion_clean.split(":", 1)[1].strip()
                if forbidden in diff:
                    passed = False
                    details = f"Forbidden pattern '{forbidden}' found in diff"

            results.append({
                "criterion": criterion_clean,
                "passed": passed,
                "details": details,
            })
        return results
