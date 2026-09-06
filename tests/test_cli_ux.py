from __future__ import annotations

from click.testing import CliRunner
import pytest

from agent_bus.cli.main import app


def test_top_cmd_once(live_bus_url, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_URL", live_bus_url)
    runner = CliRunner()
    res = runner.invoke(app, ["top", "--once"])
    assert res.exit_code == 0
    assert "top" in res.output
    assert "Error" not in res.output


def test_quickstart_help():
    runner = CliRunner()
    res = runner.invoke(app, ["quickstart", "--help"])
    assert res.exit_code == 0
    assert "Onboarding en 1 solo paso" in res.output
    assert "--agents" in res.output
    assert "--mock" in res.output
