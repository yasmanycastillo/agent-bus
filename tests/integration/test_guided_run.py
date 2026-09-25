import asyncio
import subprocess
import sys

import click
import httpx
import pytest
from httpx import ASGITransport

from agent_bus.cli.guided_run import collect_answers, drive_run
from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database
from agent_bus.types import AgentInfo


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_prompts_keep_reviewer_apart():
    def prompt(text, default=None, type=None):
        answers = {
            "Nombre de la corrida": "odoo-1",
            "Implementador": "impl",
            "Revisor": "reviewer",
            "Capacidades del implementador": "preset",
            "Capacidades del revisor": "preset",
            "Runtime del implementador": "external",
            "Runtime del revisor": "native",
            "CLI del implementador": "otro",
            "Comando del implementador": "echo impl",
            "Comando de test del módulo. Vacío usa uv run pytest -q": "",
        }
        assert text in answers
        return answers[text]

    answers = collect_answers(prompt, lambda text, default=False: default)
    assert "code-review" not in answers["impl_caps"]
    assert "implementation" not in answers["review_caps"]
    assert answers["test_cmd"] == []
    assert answers["impl_command"] == ["echo", "impl"]
    assert answers["review_runtime"] == "native"
    with pytest.raises(click.ClickException, match="otro agente"):
        collect_answers(
            lambda text, default=None, type=None: {"Implementador": "impl", "Revisor": "impl"}.get(text, default),
            lambda text, default=False: default,
        )


class _SyncApp:
    def __init__(self, app) -> None:
        self.app = app

    def _request(self, method: str, url: str, payload: dict | None = None):
        async def once():
            async with httpx.AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
                return await client.request(method, url, json=payload)
        return asyncio.run(once())

    def post(self, url: str, json: dict | None = None):
        return self._request("POST", url, json)

    def get(self, url: str):
        return self._request("GET", url)


def test_run_asks_for_a_verdict_and_integrates(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    script = tmp_path / "impl.py"
    script.write_text(
        "import subprocess\nfrom pathlib import Path\n"
        "path = Path('feature.txt')\n"
        "path.write_text((path.read_text() if path.exists() else '') + 'x\\n')\n"
        "subprocess.check_call(['git', 'add', 'feature.txt'])\n"
        "subprocess.check_call(['git', 'commit', '-m', 'feature'])\n"
    )
    answers = {
        "instance": "odoo-1",
        "implementer": "impl",
        "reviewer": "reviewer",
        "impl_caps": [
            "repository-analysis", "long-context", "architecture", "implementation", "tests", "python", "odoo",
        ],
        "review_caps": ["code-review", "odoo"],
        "impl_runtime": "external",
        "impl_command": [sys.executable, str(script)],
        "review_runtime": "external",
        "review_command": [sys.executable, "-c", "print('review')"],
        "test_cmd": [sys.executable, "-c", "print('suite ok')"],
    }
    calls = {"n": 0}

    def ask():
        calls["n"] += 1
        if calls["n"] == 1:
            return "changes_requested", "otra vuelta"
        return "approve", "ok"

    async def open_bus():
        database = Database(str(tmp_path / "bus.db"))
        await database.initialize()
        opened = MessageBus(database, AgentRegistry(), InboxManager(database), project_id="alpha")
        await opened.registry.register(AgentInfo(agent_id="placeholder", display_name="Placeholder"))
        return database, opened

    db, bus = asyncio.run(open_bus())
    client = _SyncApp(bus.app)
    result = drive_run(client, answers, repo=repo, ask=ask)
    assert result["status"] == "integrated"
    assert calls["n"] == 2
    assert git(repo, "rev-parse", "HEAD^2") == git(repo / ".worktrees" / "impl", "rev-parse", "HEAD")
    stored = client.get("/tasks/feature-development-odoo-1-integration").json()
    assert stored["status"] == "done"
    asyncio.run(db.close())
