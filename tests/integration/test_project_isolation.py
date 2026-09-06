"""Two live configured hubs must never share project state or client identity."""
from __future__ import annotations

import json
from contextlib import ExitStack
from types import SimpleNamespace

import httpx
import pytest
import yaml
from click.testing import CliRunner
from mcp import Client

from agent_bus.cli.auth_cmds import auth
from agent_bus.mcp.server import McpServer
from tests.conftest import running_bus


@pytest.fixture
def two_projects(tmp_path, allowed_test_ports, monkeypatch):
    projects = []
    with ExitStack() as servers:
        for name in ("alpha", "beta"):
            root = tmp_path / name
            runtime = root / "runtime"
            runtime.mkdir(parents=True)
            project_id = f"isolated-{name}"
            database = root / "data" / "bus.db"
            sessions, paths = {}, {}
            with monkeypatch.context() as env:
                env.setenv("AGENT_BUS_ALLOW_UNSIGNED", "0")
                env.setenv("AGENT_BUS_PROJECT_ROOT", str(root))
                env.setenv("AGENT_BUS_CONFIG_DIR", str(runtime))
                env.setenv("AGENT_BUS_PROJECT_ID", project_id)
                env.setenv("AGENT_BUS_DATABASE_PATH", str(database))
                env.delenv("AGENT_BUS_SESSION_FILE", raising=False)
                env.delenv("AGENT_BUS_AGENT_ID", raising=False)
                env.delenv("AGENT_ID", raising=False)
                for agent, role in (("alice", "agent"), ("bob", "agent"), ("human", "admin")):
                    result = CliRunner().invoke(auth, ["create", "--agent", agent, "--role", role])
                    assert result.exit_code == 0, result.output
                    paths[agent] = runtime / "credentials" / f"{agent}.json"
                    sessions[agent] = json.loads(paths[agent].read_text())
                    assert sessions[agent]["token"] not in result.output
                url = servers.enter_context(running_bus(root, allowed_test_ports, configured=True))
                # Explicit credential selection wins over stale interactive state.
                (runtime / "current_agent").write_text("bob")
                env.setenv("AGENT_BUS_SESSION_FILE", str(paths["alice"]))
                env.setenv("AGENT_BUS_URL", url)
                mcp = McpServer()
                assert mcp.agent_id == "alice"
                projects.append(SimpleNamespace(
                    root=root, runtime=runtime, database=database, project_id=project_id,
                    sessions=sessions, paths=paths, url=url, mcp=mcp,
                ))
        # Both servers stay alive while environment and cwd can change freely.
        yield tuple(projects)


def headers(project, agent="alice"):
    return {"Authorization": f"Bearer {project.sessions[agent]['token']}",
            "X-Agent-Bus-Project": project.project_id}


def request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def test_live_hubs_isolate_same_names_and_reject_foreign_credentials(two_projects):
    first, second = two_projects
    with ExitStack() as stack:
        clients = [stack.enter_context(httpx.Client(base_url=project.url, headers=headers(project)))
                   for project in two_projects]
        sent = []
        for project, client in zip(two_projects, clients):
            assert request(client, "GET", "/health")["project_id"] == project.project_id
            task = request(client, "POST", "/tasks", json={"task_id": "SAME-TASK", "title": project.project_id})
            assert task["owner"] == "free"
            request(client, "POST", "/tasks/SAME-TASK/claim", json={})
            locked = request(client, "POST", "/locks/acquire", json={"file_path": "same.py", "reason": project.project_id})
            assert locked["locked_by"] == "alice"
            sent.append(request(client, "POST", "/messages", json={
                "to_agent": "bob", "body": {"text": project.project_id}, "idempotency_key": "same-key",
            }))
        assert sent[0]["message_id"] != sent[1]["message_id"]
        request(clients[0], "POST", "/tasks/SAME-TASK/done")
        request(clients[0], "POST", "/locks/release", json={"file_path": "same.py"})
        assert request(clients[0], "GET", "/tasks/SAME-TASK")["status"] == "done"
        assert request(clients[1], "GET", "/tasks/SAME-TASK")["status"] == "in_progress"
        assert request(clients[0], "GET", "/locks") == []
        assert len(request(clients[1], "GET", "/locks")) == 1
        for project, client, message in zip(two_projects, clients, sent):
            page = request(client, "GET", "/inbox/bob/messages", headers=headers(project, "bob"))
            assert len(page["messages"]) == 1
            assert page["messages"][0]["message_id"] == message["message_id"]
            assert page["messages"][0]["body"]["text"] == project.project_id
        request(clients[0], "POST", "/inbox/bob/ack", headers=headers(first, "bob"), json={"message_ids": [sent[0]["message_id"]]})
        assert request(clients[0], "GET", "/inbox/bob/messages", headers=headers(first, "bob"))["messages"] == []
        assert len(request(clients[1], "GET", "/inbox/bob/messages", headers=headers(second, "bob"))["messages"]) == 1

        for target, foreign, client in ((first, second, clients[0]), (second, first, clients[1])):
            # A correct local token cannot override the hub's configured project.
            mismatched_header = headers(target)
            mismatched_header["X-Agent-Bus-Project"] = foreign.project_id
            assert client.get("/tasks", headers=mismatched_header).status_code == 403
            # Claiming the target project does not make another DB's token valid.
            foreign_token = headers(foreign)
            foreign_token["X-Agent-Bus-Project"] = target.project_id
            response = client.post("/tasks", headers=foreign_token, json={"task_id": "INJECTED", "title": "Forbidden"})
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert client.get("/tasks/INJECTED").status_code == 404


def test_live_context_paths_remain_project_local_after_cwd_changes(two_projects, tmp_path, monkeypatch):
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(elsewhere / "runtime"))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "unrelated")
    for project in two_projects:
        with httpx.Client(base_url=project.url, headers=headers(project, "human")) as client:
            request(client, "POST", "/project/context", json={"field": "description", "value": project.project_id})
    for project in two_projects:
        with httpx.Client(base_url=project.url, headers=headers(project)) as client:
            assert request(client, "GET", "/project/context")["description"] == project.project_id
        assert yaml.safe_load((project.runtime / "context.yaml").read_text())["description"] == project.project_id
        assert project.database.exists()
    assert not (elsewhere / "runtime").exists()
    assert not (elsewhere / ".agent-bus").exists()


async def test_mcp_snapshots_project_url_and_session_across_environment_changes(two_projects, monkeypatch):
    first, second = two_projects
    monkeypatch.chdir(second.root)
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(second.runtime))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ROOT", str(second.root))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", second.project_id)
    monkeypatch.setenv("AGENT_BUS_DATABASE_PATH", str(second.database))
    monkeypatch.setenv("AGENT_BUS_URL", second.url)
    monkeypatch.setenv("AGENT_BUS_SESSION_FILE", str(second.paths["bob"]))
    monkeypatch.setenv("AGENT_BUS_AGENT_ID", "bob")
    for project in two_projects:
        async with Client(project.mcp.sdk_server()) as client:
            result = await client.call_tool("post_message", {
                "to_agent": "bob", "text": project.project_id, "idempotency_key": "same-mcp-key",
            })
            assert not result.is_error, result
            status = await client.call_tool("get_project_status", {})
            assert not status.is_error, status
            assert status.structured_content["server"]["project_id"] == project.project_id
        async with httpx.AsyncClient(base_url=project.url, headers=headers(project, "bob")) as client:
            response = await client.get("/inbox/bob/messages")
            assert response.status_code == 200
            messages = response.json()["messages"]
            assert len(messages) == 1
            assert messages[0]["from_agent"] == "alice"
            assert messages[0]["body"]["text"] == project.project_id
