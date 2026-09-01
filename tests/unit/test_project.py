from __future__ import annotations

from pathlib import Path

import yaml

from agent_bus.project import (
    create_agent_config,
    create_plan,
    find_project_dir,
    generate_agent_protocol,
    generate_all_agent_protocols,
    get_agents_dir,
    get_plan_path,
    init_project,
    load_project_config,
)


def test_init_project(tmp_path):
    project = init_project(cwd=tmp_path)
    assert project.exists()
    assert (project / "config.yaml").exists()
    assert (project / "agents").is_dir()

    config = yaml.safe_load((project / "config.yaml").read_text())
    assert "bus_url" in config


def test_init_project_idempotent(tmp_path):
    init_project(cwd=tmp_path)
    init_project(cwd=tmp_path)
    assert find_project_dir(tmp_path) is not None


def test_find_project_dir(tmp_path):
    assert find_project_dir(tmp_path) is None
    init_project(cwd=tmp_path)
    assert find_project_dir(tmp_path) is not None


def test_load_project_config(tmp_path):
    assert load_project_config(tmp_path) == {}
    init_project(cwd=tmp_path)
    config = load_project_config(tmp_path)
    assert "bus_url" in config


def test_get_agents_dir(tmp_path):
    assert get_agents_dir(tmp_path) is None
    init_project(cwd=tmp_path)
    agents = get_agents_dir(tmp_path)
    assert agents is not None
    assert agents.is_dir()


def test_create_agent_config(tmp_path):
    init_project(cwd=tmp_path)
    path = create_agent_config("claude", {"display_name": "Claude", "capabilities": ["python"]}, cwd=tmp_path)
    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert data["display_name"] == "Claude"


def test_create_agent_config_without_init(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        create_agent_config("claude", {}, cwd=tmp_path)


def test_create_and_get_plan(tmp_path):
    init_project(cwd=tmp_path)
    assert get_plan_path(tmp_path) is None
    path = create_plan("# My Plan\nDo things", cwd=tmp_path)
    assert path.exists()
    assert get_plan_path(tmp_path) is not None


def test_generate_agent_protocol(tmp_path):
    init_project(cwd=tmp_path)
    create_agent_config("claude", {"display_name": "Claude"}, cwd=tmp_path)
    path = generate_agent_protocol("claude", cwd=tmp_path)
    assert path is not None
    assert path.name == "CLAUDE.md"
    content = path.read_text()
    assert "agent-bus" in content
    assert "claude" in content
    assert "agent-bus work as claude" in content
    assert "agent-bus work check" in content
    assert "When to check inbox" in content


def test_generate_agent_protocol_without_init(tmp_path):
    path = generate_agent_protocol("claude", cwd=tmp_path)
    assert path is None


def test_generate_all_agent_protocols(tmp_path):
    init_project(cwd=tmp_path)
    create_agent_config("claude", {"display_name": "Claude"}, cwd=tmp_path)
    create_agent_config("codex", {"display_name": "Codex"}, cwd=tmp_path)
    paths = generate_all_agent_protocols(cwd=tmp_path)
    assert len(paths) == 2
    names = {p.name for p in paths}
    assert names == {"CLAUDE.md", "CODEX.md"}


def test_generate_all_protocols_no_agents(tmp_path):
    init_project(cwd=tmp_path)
    paths = generate_all_agent_protocols(cwd=tmp_path)
    assert paths == []
