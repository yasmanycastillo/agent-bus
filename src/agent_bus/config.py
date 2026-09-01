from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


DEFAULT_CONFIG_DIR = Path.home() / ".agent-bus"


@dataclass
class BusConfig:
    host: str = "0.0.0.0"
    port: int = 8420
    heartbeat_interval_seconds: int = 30
    heartbeat_miss_threshold: int = 3


@dataclass
class InboxConfig:
    max_age_days: int = 30
    archive_read: bool = False


@dataclass
class ConsensusConfig:
    default_rounds: int = 2
    f: int = 1
    lambda_threshold: float = 0.3
    round_timeout_ms: int = 30000


@dataclass
class ReputationWeights:
    accuracy: float = 0.5
    honesty: float = 0.3
    energy: float = 0.2


@dataclass
class ReputationConfig:
    decay_factor: float = 0.95
    initial_score: float = 0.5
    weights: ReputationWeights = field(default_factory=ReputationWeights)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"


@dataclass
class KeysConfig:
    private_key_path: str = str(DEFAULT_CONFIG_DIR / "private.key")
    public_key_path: str = str(DEFAULT_CONFIG_DIR / "public.key")


@dataclass
class AppConfig:
    bus: BusConfig = field(default_factory=BusConfig)
    inbox: InboxConfig = field(default_factory=InboxConfig)
    consensus: ConsensusConfig = field(default_factory=ConsensusConfig)
    reputation: ReputationConfig = field(default_factory=ReputationConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    keys: KeysConfig = field(default_factory=KeysConfig)
    data_dir: str = str(DEFAULT_CONFIG_DIR / "data")
    database_path: str = str(DEFAULT_CONFIG_DIR / "agent_bus.db")


def load_config(path: Path | None = None) -> AppConfig:
    """Load config from YAML file, falling back to defaults."""
    config = AppConfig()
    if path is None:
        path = DEFAULT_CONFIG_DIR / "config.yaml"
    if not path.exists():
        return config

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    if "bus" in raw:
        for k, v in raw["bus"].items():
            if hasattr(config.bus, k):
                setattr(config.bus, k, v)

    if "inbox" in raw:
        for k, v in raw["inbox"].items():
            if hasattr(config.inbox, k):
                setattr(config.inbox, k, v)

    if "consensus" in raw:
        for k, v in raw["consensus"].items():
            if hasattr(config.consensus, k):
                setattr(config.consensus, k, v)

    if "reputation" in raw:
        rep = raw["reputation"]
        for k, v in rep.items():
            if k == "weights" and isinstance(v, dict):
                for wk, wv in v.items():
                    if hasattr(config.reputation.weights, wk):
                        setattr(config.reputation.weights, wk, wv)
            elif hasattr(config.reputation, k):
                setattr(config.reputation, k, v)

    if "logging" in raw:
        for k, v in raw["logging"].items():
            if hasattr(config.logging, k):
                setattr(config.logging, k, v)

    if "keys" in raw:
        for k, v in raw["keys"].items():
            if hasattr(config.keys, k):
                setattr(config.keys, k, v)

    # Override database path relative to data_dir
    config.database_path = str(Path(config.data_dir) / "agent_bus.db")

    return config
