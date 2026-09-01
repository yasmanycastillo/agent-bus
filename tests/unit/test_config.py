from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from agent_bus.config import load_config


def test_default_config():
    config = load_config(Path("/nonexistent"))
    assert config.bus.port == 8420
    assert config.bus.host == "0.0.0.0"
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
