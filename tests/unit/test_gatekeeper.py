from __future__ import annotations


from agent_bus.worker.gatekeeper import (
    APPROVE,
    BLOCKED,
    CHANGES_REQUESTED,
    CodeReviewGatekeeper,
    ReviewRequest,
    Verdict,
)


CLEAN_DIFF = """diff --git a/src/agent_bus/feature.py b/src/agent_bus/feature.py
index 1234567..89abcde 100644
--- a/src/agent_bus/feature.py
+++ b/src/agent_bus/feature.py
@@ -1,3 +1,5 @@
 def existing_code():
     return True
+def new_feature():
+    return "ok"
"""

SECRET_DIFF = """diff --git a/src/agent_bus/config.py b/src/agent_bus/config.py
--- a/src/agent_bus/config.py
+++ b/src/agent_bus/config.py
@@ -10,3 +10,4 @@
+aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
"""

PRIVATE_KEY_DIFF = """diff --git a/src/agent_bus/keys.py b/src/agent_bus/keys.py
--- a/src/agent_bus/keys.py
+++ b/src/agent_bus/keys.py
@@ -1,2 +1,3 @@
+-----BEGIN PRIVATE KEY-----
+MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC7
"""

DELETED_CRITICAL_FILE_DIFF = """diff --git a/pyproject.toml b/pyproject.toml
deleted file mode 100644
index 1234567..0000000
--- a/pyproject.toml
+++ /dev/null
@@ -1,50 +0,0 @@
-[project]
"""


def test_gatekeeper_verdict_approve():
    gatekeeper = CodeReviewGatekeeper()
    req = ReviewRequest(
        task_id="T-100",
        sha="abc12345def67890",
        diff=CLEAN_DIFF,
        test_passed=True,
        test_output="5 passed in 0.2s",
        acceptance_criteria=["Feature implementation"],
        reviewer_agent_id="integrator",
        reviewer_session_id="sess-001",
    )
    decision = gatekeeper.evaluate(req)

    assert decision.verdict == APPROVE
    assert decision.verdict == Verdict.APPROVE
    assert decision.task_id == "T-100"
    assert decision.sha == "abc12345def67890"
    assert decision.reviewer_agent_id == "integrator"
    assert decision.reviewer_session_id == "sess-001"
    assert "All gatekeeper checks passed" in decision.reason

    # Evidence verification
    evidence = decision.evidence
    assert evidence["diff_stats"]["files_changed"] == 1
    assert evidence["diff_stats"]["insertions"] == 2
    assert evidence["diff_stats"]["total_lines"] == 2
    assert evidence["security_checklist"]["passed"] is True
    assert evidence["security_checklist"]["secret_detected"] is False
    assert evidence["security_checklist"]["critical_files_deleted"] == []
    assert evidence["test_results"]["passed"] is True
    assert len(evidence["acceptance_criteria_checks"]) == 1
    assert evidence["acceptance_criteria_checks"][0]["passed"] is True


def test_gatekeeper_verdict_changes_requested_failing_tests():
    gatekeeper = CodeReviewGatekeeper()
    req = ReviewRequest(
        task_id="T-101",
        sha="abc12345",
        diff=CLEAN_DIFF,
        test_passed=False,
        test_output="FAILED test_something.py::test_fail - AssertionError",
    )
    decision = gatekeeper.evaluate(req)

    assert decision.verdict == CHANGES_REQUESTED
    assert decision.verdict == Verdict.CHANGES_REQUESTED
    assert "Automated test suite failed" in decision.reason
    assert decision.evidence["test_results"]["passed"] is False


def test_gatekeeper_verdict_changes_requested_empty_diff():
    gatekeeper = CodeReviewGatekeeper()
    req = ReviewRequest(
        task_id="T-102",
        sha="abc12345",
        diff="",
        test_passed=True,
        test_output="Passed",
    )
    decision = gatekeeper.evaluate(req)

    assert decision.verdict == CHANGES_REQUESTED
    assert "Empty diff" in decision.reason
    assert decision.evidence["diff_stats"]["total_lines"] == 0


def test_gatekeeper_verdict_changes_requested_diff_size():
    gatekeeper = CodeReviewGatekeeper(max_diff_lines=5)
    req = ReviewRequest(
        task_id="T-103",
        sha="abc12345",
        diff=CLEAN_DIFF,  # CLEAN_DIFF has 2 insertions
        test_passed=True,
    )
    # 2 lines is under max 5
    assert gatekeeper.evaluate(req).verdict == APPROVE

    # Exceed max lines
    huge_diff = "diff --git a/foo.py b/foo.py\n" + "\n".join(f"+line {i}" for i in range(10))
    req_huge = ReviewRequest(task_id="T-103", sha="abc12345", diff=huge_diff, test_passed=True)
    decision_huge = gatekeeper.evaluate(req_huge)

    assert decision_huge.verdict == CHANGES_REQUESTED
    assert "exceeds maximum threshold" in decision_huge.reason


def test_gatekeeper_verdict_changes_requested_unmet_acceptance_criteria():
    gatekeeper = CodeReviewGatekeeper()
    req = ReviewRequest(
        task_id="T-104",
        sha="abc12345",
        diff=CLEAN_DIFF,
        test_passed=True,
        acceptance_criteria=["require_file: not_in_diff.py"],
    )
    decision = gatekeeper.evaluate(req)

    assert decision.verdict == CHANGES_REQUESTED
    assert "Acceptance criteria not satisfied" in decision.reason
    assert decision.evidence["acceptance_criteria_checks"][0]["passed"] is False


def test_gatekeeper_verdict_blocked_secret_detected():
    gatekeeper = CodeReviewGatekeeper()

    # AWS secret
    req_aws = ReviewRequest(
        task_id="T-105",
        sha="abc12345",
        diff=SECRET_DIFF,
        test_passed=True,
    )
    decision_aws = gatekeeper.evaluate(req_aws)
    assert decision_aws.verdict == BLOCKED
    assert decision_aws.verdict == Verdict.BLOCKED
    assert "Security violation detected" in decision_aws.reason
    assert decision_aws.evidence["security_checklist"]["secret_detected"] is True
    assert decision_aws.evidence["security_checklist"]["passed"] is False

    # Private key
    req_pk = ReviewRequest(
        task_id="T-106",
        sha="abc12345",
        diff=PRIVATE_KEY_DIFF,
        test_passed=True,
    )
    decision_pk = gatekeeper.evaluate(req_pk)
    assert decision_pk.verdict == BLOCKED
    assert decision_pk.evidence["security_checklist"]["secret_detected"] is True


def test_gatekeeper_verdict_blocked_deleted_critical_file():
    gatekeeper = CodeReviewGatekeeper()
    req = ReviewRequest(
        task_id="T-107",
        sha="abc12345",
        diff=DELETED_CRITICAL_FILE_DIFF,
        test_passed=True,
    )
    decision = gatekeeper.evaluate(req)

    assert decision.verdict == BLOCKED
    assert "Deleted critical file: pyproject.toml" in decision.reason
    assert "pyproject.toml" in decision.evidence["security_checklist"]["critical_files_deleted"]
    assert decision.evidence["security_checklist"]["passed"] is False


def test_gatekeeper_audit_fields_structure():
    req = ReviewRequest(
        task_id="T-108",
        sha="1234567890abcdef1234567890abcdef12345678",
        diff=CLEAN_DIFF,
        test_passed=True,
        test_output="Passed",
        reviewer_agent_id="gatekeeper-agent",
        reviewer_session_id="session-xyz",
    )
    gatekeeper = CodeReviewGatekeeper()
    decision = gatekeeper.evaluate(req)

    # Verify model fields
    dump = decision.model_dump(mode="json")
    for key in (
        "review_id",
        "task_id",
        "sha",
        "verdict",
        "reason",
        "evidence",
        "test_results",
        "reviewer_agent_id",
        "reviewer_session_id",
        "created_at",
    ):
        assert key in dump

    assert dump["verdict"] == "approve"
    assert dump["sha"] == "1234567890abcdef1234567890abcdef12345678"
    assert dump["reviewer_agent_id"] == "gatekeeper-agent"
    assert dump["reviewer_session_id"] == "session-xyz"
    assert dump["test_results"]["passed"] is True
    assert isinstance(dump["evidence"], dict)
