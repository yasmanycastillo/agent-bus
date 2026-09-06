from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from agent_bus.config import load_config


def test_default_config():
    config = load_config(Path("/nonexistent"))
    assert config.bus.port == 8420
    assert config.bus.host == "127.0.0.1"
    assert config.consensus.f == 1
    assert config.reputation.decay_factor == 0.95
    assert config.reputation.weights.accuracy == 0.5


def test_load_from_yaml():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(
            {
                "bus": {"port": 9999, "host": "127.0.0.1"},
                "reputation": {"decay_factor": 0.8, "weights": {"accuracy": 0.6}},
            },
            f,
        )
        f.flush()
        config = load_config(Path(f.name))

    assert config.bus.port == 9999
    assert config.bus.host == "127.0.0.1"
    assert config.reputation.decay_factor == 0.8
    assert config.reputation.weights.accuracy == 0.6
    assert config.reputation.weights.honesty == 0.3  # unchanged


def test_empty_yaml_returns_defaults():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("")
        f.flush()
        config = load_config(Path(f.name))

    assert config.bus.port == 8420


def test_config_paths_are_resolved_at_use_time(tmp_path, monkeypatch):
    from agent_bus.config import AppConfig, get_config_dir

    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path))
    assert get_config_dir() == tmp_path
    assert AppConfig().keys.private_key_path == str(tmp_path / "private.key")
    before = load_config()
    (tmp_path / "config.yaml").write_text("")
    assert load_config().database_path == before.database_path
    assert before.database_path == str(tmp_path / "data" / "agent_bus.db")


def test_explicit_database_and_project_override_yaml(tmp_path, monkeypatch):
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml.safe_dump({
        "database_path": str(tmp_path / "yaml.db"),
        "bus": {"project_id": "yaml-project"},
    }))
    assert load_config(config_file).database_path == str(tmp_path / "yaml.db")
    monkeypatch.setenv("AGENT_BUS_DATABASE_PATH", str(tmp_path / "override.db"))
    monkeypatch.setenv("AGENT_BUS_PROJECT_ID", "override-project")
    config = load_config(config_file)
    assert config.database_path == str(tmp_path / "override.db")
    assert config.bus.project_id == "override-project"


def test_existing_legacy_database_is_not_silently_abandoned(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_CONFIG_DIR", str(tmp_path))
    legacy = tmp_path / "agent_bus.db"
    legacy.touch()
    assert load_config().database_path == str(legacy)
    (tmp_path / "config.yaml").write_text("")
    assert load_config().database_path == str(legacy)


def test_explicit_data_directory_is_honored(tmp_path):
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml.safe_dump({"data_dir": str(tmp_path / "project-data")}))
    assert load_config(config_file).database_path == str(tmp_path / "project-data" / "agent_bus.db")
