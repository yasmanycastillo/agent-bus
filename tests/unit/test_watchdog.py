"""The watchdog reports what is stuck, once per episode, without a model."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from click.testing import CliRunner

from agent_bus.cli.main import app
from agent_bus.core.tasks import REVIEW_WAIT
from agent_bus.worker.watchdog import Finding, findings, new_since

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def task(task_id, status, minutes_ago, **extra):
    return {"task_id": task_id, "status": status, "owner": "alice",
            "updated_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), **extra}


def kinds(tasks, integrator_running=None):
    found = findings(tasks, NOW, review_minutes=30, stuck_minutes=60, integrator_running=integrator_running)
    return sorted(finding.key for finding in found)


def test_reports_stalled_reviews_stuck_claims_and_blocks_that_need_a_human():
    assert kinds([
        task("R-old", "in_review", 45), task("R-new", "in_review", 5),
        task("P-old", "in_progress", 90), task("P-new", "in_progress", 10),
        task("B-human", "blocked", 1, blocked_reason="Exceeded max retries (3)"),
        task("B-deps", "blocked", 1, blocked_reason=None),
        task("B-review", "blocked", 1, blocked_reason=REVIEW_WAIT),
        task("D", "done", 999),
    ]) == ["claim_stuck:P-old", "needs_human:B-human", "review_stalled:R-old"]


def test_integrator_down_only_when_known_dead_and_reviews_wait():
    waiting = [task("R", "in_review", 1)]
    assert kinds(waiting, integrator_running=False) == ["integrator_down:integrator"]
    assert kinds(waiting, integrator_running=None) == []  # never started here: reviews may close manually
    assert kinds(waiting, integrator_running=True) == []
    assert kinds([], integrator_running=False) == []


def test_each_finding_is_reported_once_per_episode(tmp_path):
    state = tmp_path / "watchdog.json"
    stuck = Finding("claim_stuck", "T1", "T1 stuck")
    assert new_since([stuck], state) == [stuck]
    assert new_since([stuck], state) == []
    assert new_since([], state) == []           # resolved
    assert new_since([stuck], state) == [stuck]  # a new episode


def test_watchdog_once_against_a_real_hub(live_bus_url, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_URL", live_bus_url)
    with httpx.Client(base_url=live_bus_url) as client:
        client.post("/tasks", json={"task_id": "T1", "title": "Work"}).raise_for_status()
        client.post("/tasks/T1/claim", json={"agent_id": "alice"}).raise_for_status()
    command = ["watchdog", "--once", "--no-desktop", "--stuck-minutes", "0"]
    first = CliRunner().invoke(app, command)
    assert first.exit_code == 0, first.output
    assert "[claim_stuck] T1 de alice" in first.output
    second = CliRunner().invoke(app, command)
    assert second.exit_code == 0 and "claim_stuck" not in second.output
