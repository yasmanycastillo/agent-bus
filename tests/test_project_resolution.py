from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml

from agent_bus.config import get_bus_url, get_config_dir, load_config
from agent_bus.project import (
    create_plan, find_project_dir, generate_agent_protocol, get_checkout_root,
    init_project, load_project_config, project_identity, resolve_project_root,
)


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
         "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def repository(path):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    return path.resolve()


@pytest.fixture
def project_env(monkeypatch):
    for key in ("AGENT_BUS_CONFIG_DIR", "AGENT_BUS_DATABASE_PATH", "AGENT_BUS_PROJECT_ID",
                "AGENT_BUS_PROJECT_ROOT", "AGENT_BUS_URL", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_discovery_from_subdirectory_reuses_parent_marker(tmp_path, project_env):
    root = tmp_path / "plain-project"
    root.mkdir()
    marker = init_project(root)
    nested = root / "src" / "deep"
    nested.mkdir(parents=True)
    assert find_project_dir(nested) == marker
    assert init_project(nested) == marker
    assert not (nested / ".agent-bus").exists()
    assert create_plan("Shared plan", nested) == marker / "plan.md"
    project_env.chdir(nested)
    assert get_config_dir() == marker / "runtime"
    assert load_config().project_root == str(root)
    assert load_config().bus.project_id == load_project_config(root)["project_id"]


def test_uninitialized_git_is_isolated_and_identity_survives_init(tmp_path, project_env):
    root = repository(tmp_path / "repo")
    nested = root / "src"
    nested.mkdir()
    project_env.chdir(nested)
    before = load_config()
    assert before.project_root == str(root)
    assert before.database_path == str(root / ".agent-bus/runtime/data/agent_bus.db")
    assert before.bus.project_id == project_identity(root)
    # Provisioning can create runtime storage before the project marker exists.
    get_config_dir().mkdir(parents=True)
    marker = init_project()
    assert marker == root / ".agent-bus"
    after = load_config()
    assert after.bus.project_id == before.bus.project_id
    assert after.database_path == before.database_path
    assert not (nested / ".agent-bus").exists()


def test_linked_git_worktree_shares_canonical_runtime_and_plan(tmp_path, project_env):
    primary = repository(tmp_path / "primary")
    git(primary, "commit", "--allow-empty", "-m", "base")
    linked = tmp_path / "linked"
    git(primary, "worktree", "add", "--detach", str(linked), "HEAD")
    marker = init_project(primary)
    create_plan("One shared plan", primary)
    nested = linked / "src" / "deep"
    nested.mkdir(parents=True)
    try:
        project_env.chdir(nested)
        assert resolve_project_root() == primary
        assert get_checkout_root() == linked
        assert find_project_dir() == marker
        assert init_project() == marker
        assert get_config_dir() == marker / "runtime"
        assert load_config().bus.project_id == load_project_config(primary)["project_id"]
        generated = generate_agent_protocol("bob")
        assert generated == linked / "BOB.md"
        assert not (primary / "BOB.md").exists()
        assert not (nested / "BOB.md").exists()
        assert not (linked / ".agent-bus").exists()
    finally:
        project_env.chdir(tmp_path)
        git(primary, "worktree", "remove", "--force", str(linked))


def test_nested_repository_does_not_inherit_outer_marker(tmp_path, project_env):
    outer = repository(tmp_path / "outer")
    outer_marker = init_project(outer)
    nested = repository(outer / "vendor" / "inner")
    deeper = nested / "src"
    deeper.mkdir()
    project_env.chdir(deeper)
    assert resolve_project_root() == nested
    assert find_project_dir() is None
    assert get_config_dir() == nested / ".agent-bus/runtime"
    assert load_config().bus.project_id != load_project_config(outer)["project_id"]
    assert init_project() == nested / ".agent-bus"
    assert (outer_marker / "config.yaml").exists()


def test_broken_nested_git_marker_still_blocks_outer_fallback(tmp_path, project_env):
    outer = tmp_path / "outer"
    outer.mkdir()
    init_project(outer)
    nested = outer / "broken-repo"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: missing-metadata\n")
    assert resolve_project_root(nested) == nested
    assert find_project_dir(nested) is None


def test_project_id_and_custom_marker_fields_are_preserved(tmp_path, project_env):
    root = tmp_path / "Name with spaces"
    root.mkdir()
    marker = init_project(root)
    initial = yaml.safe_load((marker / "config.yaml").read_text())
    assert initial["project_id"] == project_identity(root)
    initial.update(project_id="stable-custom-id", bus_url="http://127.0.0.1:9631", custom={"preserve": True})
    (marker / "config.yaml").write_text(yaml.safe_dump(initial))
    init_project(root)
    assert yaml.safe_load((marker / "config.yaml").read_text()) == initial
    project_env.chdir(root)
    assert load_config().bus.project_id == "stable-custom-id"
    assert get_bus_url() == "http://127.0.0.1:9631"


def test_symlinked_paths_share_identity(tmp_path, project_env):
    root = repository(tmp_path / "real")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert resolve_project_root(alias) == root
    assert project_identity(alias) == project_identity(root)


def test_explicit_root_is_absolute_validated_and_has_priority(tmp_path, project_env):
    selected = tmp_path / "selected"
    selected.mkdir()
    ambient = repository(tmp_path / "ambient")
    project_env.chdir(ambient)
    for invalid in ("relative", str(tmp_path / "missing"), ""):
        project_env.setenv("AGENT_BUS_PROJECT_ROOT", invalid)
        with pytest.raises(ValueError, match="absolute directory"):
            get_config_dir()
    project_env.setenv("AGENT_BUS_PROJECT_ROOT", str(selected))
    assert resolve_project_root() == selected
    assert load_config().project_root == str(selected)
    assert get_config_dir() == selected / ".agent-bus/runtime"


def test_explicit_runtime_without_root_never_inherits_ambient_project(tmp_path, project_env):
    root = repository(tmp_path / "ambient")
    marker = init_project(root)
    (marker / "config.yaml").write_text(yaml.safe_dump({"project_id": "ambient-id", "bus_url": "http://127.0.0.1:9632"}))
    isolated = tmp_path / "isolated-config"
    isolated.mkdir()
    project_env.chdir(root)
    project_env.setenv("AGENT_BUS_CONFIG_DIR", str(isolated))
    assert get_config_dir() == isolated
    assert load_config().project_root is None
    assert load_config().bus.project_id == "default"
    assert get_bus_url() == "http://127.0.0.1:8420"
    project_env.setenv("AGENT_BUS_PROJECT_ROOT", str(root))
    assert load_config().project_root == str(root)
    assert load_config().bus.project_id == "ambient-id"
    assert get_bus_url() == "http://127.0.0.1:9632"
    assert get_config_dir() == isolated


def test_runtime_yaml_and_environment_paths_resolve_against_correct_base(tmp_path, project_env):
    root = tmp_path / "invocation"
    root.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_file = runtime / "config.yaml"
    config_file.write_text(yaml.safe_dump({
        "data_dir": "relative-data", "database_path": "../databases/actual.db",
        "keys": {"private_key_path": "keys/private.key", "public_key_path": "keys/public.key"},
        "bus": {"project_id": "runtime-id", "host": "127.0.0.1", "port": 9640},
    }))
    project_env.chdir(root)
    project_env.setenv("AGENT_BUS_CONFIG_DIR", "../runtime")
    config = load_config()
    assert get_config_dir() == runtime
    assert config.data_dir == str(runtime / "relative-data")
    assert config.database_path == str(tmp_path / "databases/actual.db")
    assert config.keys.private_key_path == str(runtime / "keys/private.key")
    assert config.keys.public_key_path == str(runtime / "keys/public.key")
    assert config.bus.project_id == "runtime-id"
    assert get_bus_url() == "http://127.0.0.1:9640"
    project_env.setenv("AGENT_BUS_DATABASE_PATH", "db/env.db")
    project_env.setenv("AGENT_BUS_PROJECT_ID", "env-id")
    assert load_config().database_path == str(root / "db/env.db")
    assert load_config().bus.project_id == "env-id"


def test_bus_url_precedence_and_independent_projects(tmp_path, project_env):
    roots = [repository(tmp_path / name) for name in ("one", "two")]
    for index, root in enumerate(roots):
        marker = init_project(root)
        project = load_project_config(root)
        project["bus_url"] = f"http://127.0.0.1:{9650 + index}"
        (marker / "config.yaml").write_text(yaml.safe_dump(project))
        runtime = marker / "runtime"
        runtime.mkdir()
        (runtime / "config.yaml").write_text(yaml.safe_dump({"bus": {"port": 9699}}))
    identities = []
    for index, root in enumerate(roots):
        project_env.chdir(root)
        identities.append(load_config().bus.project_id)
        assert get_bus_url() == f"http://127.0.0.1:{9650 + index}"
        assert get_config_dir() == root / ".agent-bus/runtime"
    assert identities[0] != identities[1]
    project_env.setenv("AGENT_BUS_URL", "http://127.0.0.1:9660/")
    assert get_bus_url() == "http://127.0.0.1:9660"
    assert get_bus_url("https://example.invalid/bus/") == "https://example.invalid/bus"


def test_runtime_identity_overrides_marker_and_env_overrides_runtime(tmp_path, project_env):
    root = repository(tmp_path / "repo")
    marker = init_project(root)
    runtime = marker / "runtime"
    runtime.mkdir()
    (runtime / "config.yaml").write_text(yaml.safe_dump({"bus": {"project_id": "runtime-id"}}))
    project_env.chdir(root)
    assert load_config().bus.project_id == "runtime-id"
    project_env.setenv("AGENT_BUS_PROJECT_ID", "environment-id")
    assert load_config().bus.project_id == "environment-id"


def test_external_without_project_retains_explicit_legacy_runtime(tmp_path, project_env):
    external = tmp_path / "external"
    external.mkdir()
    runtime = tmp_path / "legacy"
    runtime.mkdir()
    (runtime / "agent_bus.db").touch()
    project_env.chdir(external)
    project_env.setenv("AGENT_BUS_CONFIG_DIR", str(runtime))
    assert find_project_dir() is None
    assert load_config().database_path == str(runtime / "agent_bus.db")
    assert load_config().bus.project_id == "default"


@pytest.mark.parametrize("url", ["", "file:///tmp/socket", "http://user:password@127.0.0.1:8420", "http://localhost:99999", "http://localhost/#fragment"])
def test_bus_url_rejects_invalid_or_credential_urls(url, project_env):
    with pytest.raises(ValueError):
        get_bus_url(url)
