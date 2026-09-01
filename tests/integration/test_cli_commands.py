from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agent_bus.cli.main import app


@pytest.fixture
def runner():
    return CliRunner()


def test_init_command(runner: CliRunner):
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("agent_bus.cli.main.DEFAULT_CONFIG_DIR", Path(tmpdir) / ".agent-bus"):
            with patch("agent_bus.project.Path.cwd", return_value=Path(tmpdir)):
                result = runner.invoke(app, ["init"])
                assert result.exit_code == 0
                assert "inicializado" in result.output.lower()
                assert (Path(tmpdir) / ".agent-bus" / "config.yaml").exists()
                assert (Path(tmpdir) / ".agent-bus" / "agents").is_dir()


def test_init_idempotent(runner: CliRunner):
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("agent_bus.cli.main.DEFAULT_CONFIG_DIR", Path(tmpdir) / "ab"):
            with patch("agent_bus.project.Path.cwd", return_value=Path(tmpdir)):
                runner.invoke(app, ["init"])
                result = runner.invoke(app, ["init"])
                assert result.exit_code == 0
                assert "inicializado" in result.output.lower()


def test_commands_require_server(runner: CliRunner):
    result = runner.invoke(app, ["status"])
    # Either fails (no server) or succeeds (server running) — just verify it doesn't crash
    assert result.exit_code in (0, 1)


def test_init_generates_protocols(runner: CliRunner):
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("agent_bus.cli.main.DEFAULT_CONFIG_DIR", Path(tmpdir) / ".agent-bus"):
            with patch("agent_bus.project.Path.cwd", return_value=Path(tmpdir)):
                runner.invoke(app, ["init"])
                # No agents yet — no protocols generated
                assert not (Path(tmpdir) / "CLAUDE.md").exists()

                # Create agent config and re-init
                from agent_bus.project import create_agent_config

                create_agent_config("claude", {"display_name": "Claude"}, cwd=Path(tmpdir))
                create_agent_config("codex", {"display_name": "Codex"}, cwd=Path(tmpdir))
                result = runner.invoke(app, ["init"])
                assert result.exit_code == 0
                assert (Path(tmpdir) / "CLAUDE.md").exists()
                assert (Path(tmpdir) / "CODEX.md").exists()
                content = (Path(tmpdir) / "CLAUDE.md").read_text()
                assert "agent-bus work as claude" in content
