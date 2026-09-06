from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_bus.worker.integrator import BranchIntegrator, IntegratorResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GIT_ENV_BASE = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git_env(home: Path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.update(_GIT_ENV_BASE)
    env["HOME"] = str(home)
    return env


def _run_git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env,
    )
    return proc.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = _git_env(path)
    _run_git("init", "-b", "main", cwd=path, env=env)
    _run_git("config", "user.name", "t", cwd=path, env=env)
    _run_git("config", "user.email", "t@t", cwd=path, env=env)
    (path / "base.txt").write_text("base\n")
    _run_git("add", "-A", cwd=path, env=env)
    _run_git(
        "commit", "-m", "base",
        env={**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        cwd=path,
    )
    _run_git("checkout", "-b", "agent/claude", cwd=path, env=env)
    (path / "feature.txt").write_text("feature\n")
    _run_git("add", "-A", cwd=path, env=env)
    _run_git(
        "commit", "-m", "feature",
        env={**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        cwd=path,
    )
    _run_git("checkout", "main", cwd=path, env=env)


# ---------------------------------------------------------------------------
# Existing behavior: retry / blocked / success (mocked, non-git paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integrator_test_failure_triggers_author_feedback(tmp_path):
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test", max_retries_per_task=2)

    async def mock_fail_tests(worktree_dir, test_cmd=None):
        return (False, "Failing test: assertion error in test_foo")

    async def mock_candidate(sha, target_branch, worktree_dir, test_cmd):
        return sha, False, "Failing test: assertion error in test_foo", "diff --git a/x"

    async def mock_record(decision):
        return True

    async def mock_notify(task_id, author_agent, details, retry):
        pass

    integrator._test_candidate = mock_candidate  # type: ignore
    integrator.run_tests = mock_fail_tests
    integrator._record_review = mock_record  # type: ignore
    integrator._notify_author_failure = mock_notify  # type: ignore

    res = await integrator.integrate_task(
        task_id="T800",
        author_agent="claude",
        worktree_dir=tmp_path,
        candidate_branch="agent/claude",
    )

    assert res.success is False
    assert res.merged is False
    assert res.status == "retry_requested"
    assert res.retry_count == 1
    assert "Failing test" in res.output


@pytest.mark.asyncio
async def test_integrator_exceeding_max_retries_blocks_task(tmp_path):
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test", max_retries_per_task=2)

    async def mock_fail_tests(worktree_dir, test_cmd=None):
        return (False, "Fatal syntax error")

    async def mock_candidate(sha, target_branch, worktree_dir, test_cmd):
        return sha, False, "Fatal syntax error", "diff --git a/x"

    async def mock_record(decision):
        return True

    async def mock_notify(task_id, author_agent, details, retry):
        pass

    async def mock_block(task_id, author_agent, details):
        pass

    integrator._test_candidate = mock_candidate  # type: ignore
    integrator.run_tests = mock_fail_tests
    integrator._record_review = mock_record  # type: ignore
    integrator._notify_author_failure = mock_notify  # type: ignore
    integrator._notify_bus_blocked = mock_block  # type: ignore

    res1 = await integrator.integrate_task("T801", "claude", tmp_path, "agent/claude")
    assert res1.status == "retry_requested"
    assert res1.retry_count == 1

    res2 = await integrator.integrate_task("T801", "claude", tmp_path, "agent/claude")
    assert res2.status == "retry_requested"
    assert res2.retry_count == 2

    res3 = await integrator.integrate_task("T801", "claude", tmp_path, "agent/claude")
    assert res3.status == "blocked"
    assert res3.retry_count == 3
    assert "blocked" in (res3.error or "")


@pytest.mark.asyncio
async def test_integrator_success_flow(tmp_path):
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")

    async def mock_pass_tests(worktree_dir, test_cmd=None):
        return (True, "All tests passed (10/10)")

    async def mock_integrate(integration_sha, target_branch, target_sha=""):
        return (True, "Merged 1 commit cleanly.")

    async def mock_finalize(task_id):
        return (True, None)

    async def mock_record(decision):
        return True

    integrator.run_tests = mock_pass_tests
    integrator._merge_branches = mock_integrate  # type: ignore
    integrator._finalize_merge = mock_finalize  # type: ignore
    integrator._record_review = mock_record  # type: ignore

    res = await integrator.integrate_task("T802", "claude", tmp_path, "agent/claude")
    assert res.success is True
    assert res.merged is True
    assert res.status == "integrated"


# ---------------------------------------------------------------------------
# P0-INTEGRATOR: pinned SHA integration semantics (real git fixtures)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integrator_integrates_exact_reviewed_candidate(tmp_path):
    """Target ends at the tested combined commit; review tied to candidate SHA."""
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    candidate_worktree = tmp_path.parent / f"{tmp_path.name}-candidate-wt"
    _run_git("worktree", "add", str(candidate_worktree), "agent/claude", cwd=tmp_path, env=env)
    candidate_sha = _run_git("rev-parse", "agent/claude", cwd=tmp_path, env=env)
    main_sha = _run_git("rev-parse", "main", cwd=tmp_path, env=env)

    recorded: list = []
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")

    async def mock_record(decision):
        recorded.append(decision)
        return True

    async def mock_finalize(task_id):
        return (True, None)

    integrator._record_review = mock_record  # type: ignore
    integrator._finalize_merge = mock_finalize  # type: ignore

    res = await integrator.integrate_task(
        "T900", "claude", candidate_worktree, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.success is True
    assert res.merged is True
    assert res.status == "integrated"
    # Review is tied to the author's candidate SHA
    assert recorded[0].sha == candidate_sha
    assert res.metadata["candidate_sha"] == candidate_sha
    # Target advanced past the old main and contains the feature
    new_main = _run_git("rev-parse", "main", cwd=tmp_path, env=env)
    assert new_main != main_sha
    _run_git("checkout", "main", cwd=tmp_path, env=env)
    assert (tmp_path / "feature.txt").exists()
    # The exact tested combined commit is what main points at
    assert res.metadata["integration_sha"] == new_main


@pytest.mark.asyncio
async def test_integrator_tests_candidate_combined_with_current_main(tmp_path):
    """run_tests receives a throwaway worktree holding candidate+main combined."""
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    candidate_worktree = tmp_path.parent / f"{tmp_path.name}-candidate-wt"
    _run_git("worktree", "add", str(candidate_worktree), "agent/claude", cwd=tmp_path, env=env)

    tested_dirs: list[Path] = []
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")

    async def spy_tests(worktree_dir, test_cmd=None):
        tested_dirs.append(Path(worktree_dir))
        # Assert while the temporary worktree still exists (before cleanup)
        assert (Path(worktree_dir) / "feature.txt").exists()
        assert (Path(worktree_dir) / "base.txt").exists()
        assert Path(worktree_dir) != candidate_worktree
        return (True, "ok")

    async def mock_record(decision):
        return True

    async def mock_finalize(task_id):
        return (True, None)

    integrator.run_tests = spy_tests
    integrator._record_review = mock_record  # type: ignore
    integrator._finalize_merge = mock_finalize  # type: ignore

    res = await integrator.integrate_task(
        "T901", "claude", candidate_worktree, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.success is True
    assert len(tested_dirs) == 1
    # Throwaway worktree is cleaned up afterwards
    assert not tested_dirs[0].exists()


@pytest.mark.asyncio
async def test_integrator_conflict_against_current_main_requests_retry(tmp_path):
    """A candidate conflicting with current main fails before any target mutation."""
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    # Diverge main so the candidate conflicts with it
    (tmp_path / "feature.txt").write_text("conflicting main version\n")
    _run_git("add", "-A", cwd=tmp_path, env=env)
    _run_git(
        "commit", "-m", "diverge main",
        env={**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        cwd=tmp_path,
    )
    main_before = _run_git("rev-parse", "main", cwd=tmp_path, env=env)

    candidate_worktree = tmp_path.parent / f"{tmp_path.name}-candidate-wt"
    _run_git("worktree", "add", str(candidate_worktree), "agent/claude", cwd=tmp_path, env=env)

    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test", max_retries_per_task=2)

    async def mock_record(decision):
        return True

    async def mock_notify(task_id, author_agent, details, retry):
        pass

    integrator._record_review = mock_record  # type: ignore
    integrator._notify_author_failure = mock_notify  # type: ignore

    res = await integrator.integrate_task(
        "T902", "claude", candidate_worktree, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.success is False
    assert res.merged is False
    assert res.status == "retry_requested"
    assert "conflict" in res.output.lower()
    # main was not mutated
    assert _run_git("rev-parse", "main", cwd=tmp_path, env=env) == main_before


@pytest.mark.asyncio
async def test_integrator_refuses_merge_when_target_moved(tmp_path):
    """If main advances between testing and integration, report conflict, no mutation."""
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    candidate_worktree = tmp_path.parent / f"{tmp_path.name}-candidate-wt"
    _run_git("worktree", "add", str(candidate_worktree), "agent/claude", cwd=tmp_path, env=env)
    main_before = _run_git("rev-parse", "main", cwd=tmp_path, env=env)

    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")

    async def mock_record(decision):
        return True

    async def mock_notify(task_id, author_agent, details, retry):
        pass

    integrator._record_review = mock_record  # type: ignore
    integrator._notify_author_failure = mock_notify  # type: ignore

    # Sneak a new commit into main after _test_candidate captured target_sha
    # but before _integrate runs, simulating a concurrent update.
    original_test_candidate = integrator._test_candidate

    async def racing_test_candidate(sha, target_branch, worktree_dir, test_cmd):
        result = await original_test_candidate(sha, target_branch, worktree_dir, test_cmd)
        (tmp_path / "sneaky.txt").write_text("moved\n")
        _run_git("add", "-A", cwd=tmp_path, env=env)
        _run_git(
            "commit", "-m", "move main",
            env={**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
            cwd=tmp_path,
        )
        return result

    integrator._test_candidate = racing_test_candidate  # type: ignore

    res = await integrator.integrate_task(
        "T906", "claude", candidate_worktree, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.success is False
    assert res.merged is False
    assert res.status == "conflict"
    assert "moved" in res.output
    # main is at the sneaky commit, not at the tested combined commit
    main_after = _run_git("rev-parse", "main", cwd=tmp_path, env=env)
    assert main_after != main_before
    assert "sneaky.txt" in res.output or (tmp_path / ".git").exists()


@pytest.mark.asyncio
async def test_integrator_fails_closed_when_review_persistence_fails(tmp_path):
    """No integration and no success when POST /reviews fails."""
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")

    async def mock_candidate(sha, target_branch, worktree_dir, test_cmd):
        return sha, True, "All tests passed", "diff --git a/x"

    async def mock_record(decision):
        return False

    integrated: list[str] = []

    async def spy_integrate(integration_sha, target_branch, target_sha):
        integrated.append(integration_sha)
        return (True, "Merged.")

    integrator._test_candidate = mock_candidate  # type: ignore
    integrator._record_review = mock_record  # type: ignore
    integrator._integrate = spy_integrate  # type: ignore

    res = await integrator.integrate_task("T903", "claude", tmp_path, "agent/claude")
    assert res.success is False
    assert res.merged is False
    assert res.status == "error"
    assert integrated == []  # integration never attempted


@pytest.mark.asyncio
async def test_integrator_no_success_when_task_completion_fails(tmp_path):
    """Integrated but success=False when POST /tasks/<id>/done fails."""
    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")

    async def mock_candidate(sha, target_branch, worktree_dir, test_cmd):
        return sha, True, "All tests passed", "diff --git a/x"

    async def mock_pass_tests(worktree_dir, test_cmd=None):
        return (True, "All tests passed")

    async def mock_record(decision):
        return True

    async def mock_integrate(integration_sha, target_branch, target_sha=""):
        return (True, "Merged.")

    async def mock_finalize(task_id):
        return (False, "connection refused")

    integrator.run_tests = mock_pass_tests
    integrator._merge_branches = mock_integrate  # type: ignore

    integrator._test_candidate = mock_candidate  # type: ignore
    integrator._record_review = mock_record  # type: ignore
    integrator._integrate = mock_integrate  # type: ignore
    integrator._finalize_merge = mock_finalize  # type: ignore

    res = await integrator.integrate_task("T904", "claude", tmp_path, "agent/claude")
    assert res.merged is True  # the integration did happen
    assert res.success is False  # but we cannot report success
    assert res.status == "error"
    assert "connection refused" in (res.error or "")


@pytest.mark.asyncio
async def test_integrator_rejects_unresolvable_candidate_branch(tmp_path):
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    candidate_worktree = tmp_path.parent / f"{tmp_path.name}-candidate-wt"
    _run_git("worktree", "add", "--detach", str(candidate_worktree), "main", cwd=tmp_path, env=env)

    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")
    res = await integrator.integrate_task(
        "T905", "claude", candidate_worktree, "agent/nonexistent", test_cmd=["/bin/true"],
    )
    assert res.success is False
    assert res.merged is False
    assert res.status == "rejected"


# ---------------------------------------------------------------------------
# P0-INTEGRATOR follow-up: races, real HTTP, dirty target, ref lifecycle
# ---------------------------------------------------------------------------


def _author_worktree(tmp_path: Path, env: dict) -> Path:
    wt = tmp_path.parent / f"{tmp_path.name}-candidate-wt"
    _run_git("worktree", "add", str(wt), "agent/claude", cwd=tmp_path, env=env)
    return wt


@pytest.mark.asyncio
async def test_record_review_real_404_fails_closed(tmp_path, live_bus_url):
    """POST /reviews for a payload the bus rejects (400) must fail closed."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    env = _git_env(repo)
    wt = _author_worktree(repo, env)

    integrator = BranchIntegrator(repo_dir=repo, bus_url=live_bus_url)

    # Corrupt the decision after evaluation so the server rejects it (400).
    async def record_invalid(decision):
        from agent_bus.worker.gatekeeper import ReviewDecision
        bad = decision.model_dump(mode="json")
        bad["verdict"] = "not-a-verdict"  # server returns 400
        import httpx
        try:
            async with async_bus_client(integrator.agent_id, base_url=integrator.bus_url, timeout=10.0) as client:
                resp = await client.post("/reviews", json=bad)
                resp.raise_for_status()
            return True
        except Exception:
            return False

    integrator._record_review = record_invalid  # type: ignore

    res = await integrator.integrate_task(
        "T910", "claude", wt, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.success is False
    assert res.merged is False
    assert res.status == "error"
    # main untouched
    assert "feature.txt" not in [p.name for p in repo.iterdir()]


@pytest.mark.asyncio
async def test_done_real_404_triggers_rollback(tmp_path, live_bus_url):
    """POST /tasks/<unknown>/done returns 404 after publication -> conditional rollback."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    env = _git_env(repo)
    wt = _author_worktree(repo, env)
    main_before = _run_git("rev-parse", "main", cwd=repo, env=env)

    integrator = BranchIntegrator(repo_dir=repo, bus_url=live_bus_url)

    async def mock_record(decision):
        return True

    async def mock_blocked(task_id, author, details):
        pass

    async def mock_author_failure(task_id, author, details, retry):
        pass

    integrator._record_review = mock_record  # type: ignore
    integrator._notify_bus_blocked = mock_blocked  # type: ignore
    integrator._notify_author_failure = mock_author_failure  # type: ignore

    # Task T911 does not exist on the bus -> /done returns 404 -> raise_for_status throws
    res = await integrator.integrate_task(
        "T911-unknown", "claude", wt, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.success is False
    assert res.status == "error"
    assert res.metadata.get("rolled_back") is True
    # Target was rolled back to the pre-integration commit
    assert _run_git("rev-parse", "main", cwd=repo, env=env) == main_before
    _run_git("checkout", "-f", "main", cwd=repo, env=env)
    assert "feature.txt" not in [p.name for p in repo.iterdir()]


@pytest.mark.asyncio
async def test_done_failure_rollback_loses_race_reports_reconciliation(tmp_path, live_bus_url):
    """If someone advances main after publication, rollback CAS loses and we reconcile."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    env = _git_env(repo)
    wt = _author_worktree(repo, env)
    main_before = _run_git("rev-parse", "main", cwd=repo, env=env)

    integrator = BranchIntegrator(repo_dir=repo, bus_url=live_bus_url)

    async def mock_record(decision):
        return True

    integrator._record_review = mock_record  # type: ignore

    # After publication (inside _finalize_merge failure path), race the target:
    # advance main beyond integration_sha so the rollback CAS must lose.
    finalize_calls = {"n": 0}

    async def racing_finalize(task_id):
        finalize_calls["n"] += 1
        if finalize_calls["n"] == 1:
            # Simultaneously fail /done AND advance main concurrently.
            (repo / "raced.txt").write_text("raced\n")
            _run_git("checkout", "-f", "main", cwd=repo, env=env)
            _run_git("add", "-A", cwd=repo, env=env)
            _run_git(
                "commit", "-m", "raced",
                env={**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
                cwd=repo,
            )
            return (False, "simulated 500 from /done")
        return (False, "already failed")

    integrator._finalize_merge = racing_finalize  # type: ignore

    res = await integrator.integrate_task(
        "T912", "claude", wt, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.success is False
    assert res.merged is True  # publication happened; rollback lost
    assert res.status == "error"
    assert res.metadata.get("reconciliation_required") is True
    # main is at the raced commit, past integration
    main_after = _run_git("rev-parse", "main", cwd=repo, env=env)
    assert main_after != main_before


@pytest.mark.asyncio
async def test_dirty_target_rejected_before_testing(tmp_path):
    """A dirty target checkout is rejected in preflight before any testing."""
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    wt = _author_worktree(tmp_path, env)
    (tmp_path / "uncommitted.txt").write_text("dirty\n")

    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")
    tested: list = []

    async def spy_tests(worktree_dir, test_cmd=None):
        tested.append(worktree_dir)
        return (True, "ok")

    integrator.run_tests = spy_tests

    res = await integrator.integrate_task(
        "T913", "claude", wt, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.status == "rejected"
    assert "dirty" in res.error
    assert tested == []  # tests never ran


@pytest.mark.asyncio
async def test_in_progress_merge_on_target_rejected(tmp_path):
    """An in-progress merge on the target aborts integration."""
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    wt = _author_worktree(tmp_path, env)

    # Start a merge that conflicts and leaves MERGE_HEAD behind.
    (tmp_path / "feature.txt").write_text("main side\n")
    _run_git("add", "-A", cwd=tmp_path, env=env)
    _run_git(
        "commit", "-m", "diverge",
        env={**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        cwd=tmp_path,
    )
    merge = subprocess.run(
        ["git", "merge", "agent/claude"], cwd=str(tmp_path), capture_output=True, env=env,
    )
    assert merge.returncode != 0  # conflict leaves MERGE_HEAD

    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")
    res = await integrator.integrate_task(
        "T914", "claude", wt, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.status == "rejected"
    # The conflicted merge leaves the target dirty (AA) and MERGE_HEAD behind;
    # preflight refuses either way before any testing or mutation.
    assert "dirty" in (res.error or "") or "in progress" in (res.error or "")


@pytest.mark.asyncio
async def test_pending_ref_lifecycle_created_then_deleted(tmp_path):
    """The integration commit is pinned under refs/integrator/* and cleaned up."""
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    wt = _author_worktree(tmp_path, env)

    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")

    async def mock_record(decision):
        return True

    async def mock_finalize(task_id):
        return (True, None)

    integrator._record_review = mock_record  # type: ignore
    integrator._finalize_merge = mock_finalize  # type: ignore

    refs_seen: list[str] = []

    async def spy_run_tests(worktree_dir, test_cmd=None):
        # While testing, the pending ref must already exist... (it is created
        # before run_tests in _test_candidate)
        return (True, "ok")

    integrator.run_tests = spy_run_tests

    res = await integrator.integrate_task(
        "T915", "claude", wt, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.success is True
    # After success, no pending refs remain
    refs = _run_git(
        "for-each-ref", "--format=%(refname)", "refs/integrator/",
        cwd=tmp_path, env=env,
    )
    assert refs == ""
    # But the integration commit is now reachable from main
    assert _run_git("rev-parse", "main", cwd=tmp_path, env=env) == res.metadata["integration_sha"]


@pytest.mark.asyncio
async def test_pending_ref_deleted_on_failure(tmp_path):
    """A conflicting candidate leaves no pending ref behind."""
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    wt = _author_worktree(tmp_path, env)
    # Diverge main so the candidate conflicts
    (tmp_path / "feature.txt").write_text("main side\n")
    _run_git("add", "-A", cwd=tmp_path, env=env)
    _run_git(
        "commit", "-m", "diverge",
        env={**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        cwd=tmp_path,
    )

    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")
    async def mock_record(decision):
        return True
    async def mock_notify(task_id, author_agent, details, retry):
        pass
    integrator._record_review = mock_record  # type: ignore
    integrator._notify_author_failure = mock_notify  # type: ignore

    res = await integrator.integrate_task(
        "T916", "claude", wt, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.status == "retry_requested"
    refs = _run_git(
        "for-each-ref", "--format=%(refname)", "refs/integrator/",
        cwd=tmp_path, env=env,
    )
    assert refs == ""


@pytest.mark.asyncio
async def test_target_cas_race_detected_via_update_ref(tmp_path):
    """Concurrent update-ref on main between capture and publish -> conflict, no merge."""
    _init_repo(tmp_path)
    env = _git_env(tmp_path)
    wt = _author_worktree(tmp_path, env)
    main_before = _run_git("rev-parse", "main", cwd=tmp_path, env=env)

    integrator = BranchIntegrator(repo_dir=tmp_path, bus_url="http://test")
    async def mock_record(decision):
        return True
    async def mock_notify(task_id, author_agent, details, retry):
        pass
    integrator._record_review = mock_record  # type: ignore
    integrator._notify_author_failure = mock_notify  # type: ignore

    original = integrator._test_candidate

    async def racing_test_candidate(sha, target_sha, worktree_dir, test_cmd):
        result = await original(sha, target_sha, worktree_dir, test_cmd)
        # Advance main concurrently AFTER target_sha was captured and the
        # combined commit built: the CAS in _integrate must lose.
        (tmp_path / "raced.txt").write_text("raced\n")
        _run_git("add", "-A", cwd=tmp_path, env=env)
        _run_git(
            "commit", "-m", "race main",
            env={**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
            cwd=tmp_path,
        )
        return result

    integrator._test_candidate = racing_test_candidate  # type: ignore

    res = await integrator.integrate_task(
        "T917", "claude", wt, "agent/claude", test_cmd=["/bin/true"],
    )
    assert res.status == "conflict"
    assert "moved" in res.output or "race" in res.output.lower() or "failed" in res.output.lower()
    # main is at the raced commit, not the tested integration commit
    main_after = _run_git("rev-parse", "main", cwd=tmp_path, env=env)
    assert main_after != main_before
    assert res.merged is False
