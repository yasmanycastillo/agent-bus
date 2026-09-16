"""Acceptance of the packaged demo: real hub, MCP children and Python test processes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import agent_bus


def invoke(tmp_path, *args, input=None):
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(agent_bus.__file__).resolve().parent.parent)
    # Deliberately poison inherited configuration: demo must ignore all of it.
    env.update(AGENT_BUS_PROJECT_ROOT=str(tmp_path),
               AGENT_BUS_DATABASE_PATH=str(tmp_path / 'must-not-exist.db'),
               AGENT_BUS_URL='http://127.0.0.1:1', AGENT_BUS_PROJECT_ID='not-the-demo')
    return subprocess.run([sys.executable, '-m', 'agent_bus.cli.main', 'demo', *args],
                          cwd=tmp_path, env=env, input=input, text=True, capture_output=True, timeout=60)


@pytest.mark.parametrize('automated', [False, True])
def test_demo_real_coordination_and_correction(tmp_path, automated):
    report_path = tmp_path / 'evidence.json'
    result = invoke(tmp_path, *(['--yes'] if automated else []), '--report', str(report_path), input=None if automated else 'y\n')
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text())
    assert report['status'] == 'completed'
    assert report['mode'] == 'scripted_actors_real_mcp'
    assert report['decision_source'] == ('simulated' if automated else 'terminal_operator')
    assert report['human_interventions'] == (0 if automated else 1)
    assert report['model_calls'] == 0
    assert report['conflict_http_status'] == 409
    assert report['partial_locks_after_conflict'] == 0
    assert report['handoff_replay_same_message'] is True
    assert [run['returncode'] for run in report['test_runs']] == [0, 1, 0, 0]
    assert set(report['final_tasks'].values()) == {'done'}
    assert report['remaining_locks'] == 0
    assert report['temporary_project_removed']
    assert 'Bearer' not in report_path.read_text() and 'acquisition_id' not in report_path.read_text()
    assert list(tmp_path.iterdir()) == [report_path]  # no runtime, DB, or code in caller's project


def test_demo_human_rejection_prevents_correction(tmp_path):
    report_path = tmp_path / 'rejected.json'
    result = invoke(tmp_path, '--report', str(report_path), input='n\n')
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text())
    assert report['status'] == 'declined' and report['decision'] == 'reject'
    assert len(report['test_runs']) == 2
    assert 'final_source' not in report
    assert report['temporary_project_removed']


def test_demo_will_not_overwrite_report(tmp_path):
    existing = tmp_path / 'report.json'
    existing.write_text('keep this')
    result = invoke(tmp_path, '--yes', '--report', str(existing))
    assert result.returncode != 0
    assert existing.read_text() == 'keep this'
    assert list(tmp_path.iterdir()) == [existing]
