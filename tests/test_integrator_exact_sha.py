"""Real Git acceptance: integrate only the candidate that passed validation."""
import subprocess
from unittest.mock import AsyncMock

import pytest

from agent_bus.worker.integrator import BranchIntegrator


def git(path, *args):
    result = subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
async def candidate(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'base').write_text('base')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    wt = tmp_path / 'worker'
    git(repo, 'worktree', 'add', '-b', 'agent/alice', str(wt))
    (wt / 'feature').write_text('reviewed')
    git(wt, 'add', '.')
    git(wt, 'commit', '-m', 'feature')
    integrator = BranchIntegrator(repo_dir=repo, require_approval=True)
    integrator.run_tests = AsyncMock(return_value=(True, 'tests passed'))
    integrator._record_review = AsyncMock()
    integrator._mark_task_completed = AsyncMock()
    integrator._notify_author_failure = AsyncMock()
    return integrator, repo, wt


async def integrate(integrator, wt):
    return await integrator.integrate_task('T1', 'alice', wt, 'agent/alice', acceptance_criteria=[])


async def test_success_merges_the_reviewed_sha(candidate):
    integrator, repo, wt = candidate
    sha = git(wt, 'rev-parse', 'HEAD')
    result = await integrate(integrator, wt)
    assert result.success, result
    assert result.metadata['review']['sha'] == sha
    assert git(repo, 'rev-parse', 'HEAD^2') == sha


async def test_commit_created_during_tests_is_rejected(candidate):
    integrator, repo, wt = candidate
    initial = git(repo, 'rev-parse', 'HEAD')
    async def tests(*args):
        (wt / 'feature').write_text('not the original code')
        git(wt, 'commit', '-am', 'changed during tests')
        return True, 'passed'
    integrator.run_tests = tests
    result = await integrate(integrator, wt)
    assert result.status == 'rejected' and not result.merged
    assert git(repo, 'rev-parse', 'HEAD') == initial
    integrator._record_review.assert_not_called()


async def test_branch_moves_after_review_merges_only_original_sha(candidate):
    integrator, repo, wt = candidate
    sha = git(wt, 'rev-parse', 'HEAD')
    async def record(decision):
        (wt / 'feature').write_text('unreviewed successor')
        git(wt, 'commit', '-am', 'new unreviewed commit')
    integrator._record_review = record
    result = await integrate(integrator, wt)
    assert result.success, result
    assert git(repo, 'rev-parse', 'HEAD^2') == sha
    assert (repo / 'feature').read_text() == 'reviewed'


async def test_target_moves_after_review_refuses_merge(candidate):
    integrator, repo, wt = candidate
    async def record(decision):
        (repo / 'other').write_text('new target')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'target advanced')
    integrator._record_review = record
    result = await integrate(integrator, wt)
    assert not result.merged
    assert 'Target changed' in result.output
    assert not (repo / 'feature').exists()


async def test_dirty_candidate_after_tests_refuses_review(candidate):
    integrator, repo, wt = candidate
    async def tests(*args):
        (wt / 'feature').write_text('uncommitted edits')
        return True, 'passed'
    integrator.run_tests = tests
    result = await integrate(integrator, wt)
    assert result.status == 'rejected'
    integrator._record_review.assert_not_called()


async def test_non_git_checkout_fails_closed(tmp_path):
    integrator = BranchIntegrator(repo_dir=tmp_path)
    result = await integrate(integrator, tmp_path)
    assert result.status == 'rejected'


async def test_gatekeeper_cannot_approve_another_sha(candidate):
    integrator, repo, wt = candidate
    initial = git(repo, 'rev-parse', 'HEAD')
    original = integrator.gatekeeper.evaluate
    integrator.gatekeeper.evaluate = lambda request: original(request).model_copy(update={'sha': 'f' * 40})
    result = await integrate(integrator, wt)
    assert result.status == 'blocked' and not result.merged
    assert git(repo, 'rev-parse', 'HEAD') == initial
