from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

import yaml


DEFAULT_CONFIG_DIR = Path.home() / ".agent-bus"


def _project_root() -> Path | None:
    # Explicit runtime configuration is self-contained. In particular, test and
    # operator configs must not inherit the repository from which they run.
    if "AGENT_BUS_CONFIG_DIR" in os.environ and "AGENT_BUS_PROJECT_ROOT" not in os.environ:
        return None
    from agent_bus.project import resolve_project_root
    return resolve_project_root()


def _absolute(value: str | Path, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else (base or Path.cwd()) / path).resolve()


def get_config_dir() -> Path:
    """Project runtime by default; explicit overrides resolve at invocation cwd."""
    if "AGENT_BUS_CONFIG_DIR" in os.environ:
        if not os.environ["AGENT_BUS_CONFIG_DIR"]:
            raise ValueError("AGENT_BUS_CONFIG_DIR must not be empty")
        return _absolute(os.environ["AGENT_BUS_CONFIG_DIR"])
    root = _project_root()
    return root / ".agent-bus" / "runtime" if root else _absolute(DEFAULT_CONFIG_DIR)


@dataclass
class BusConfig:
    host: str = "127.0.0.1"
    port: int = 8420
    project_id: str = "default"
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
    private_key_path: str = field(default_factory=lambda: str(get_config_dir() / "private.key"))
    public_key_path: str = field(default_factory=lambda: str(get_config_dir() / "public.key"))


@dataclass
class AppConfig:
    project_root: str | None = None
    bus: BusConfig = field(default_factory=BusConfig)
    inbox: InboxConfig = field(default_factory=InboxConfig)
    consensus: ConsensusConfig = field(default_factory=ConsensusConfig)
    reputation: ReputationConfig = field(default_factory=ReputationConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    keys: KeysConfig = field(default_factory=KeysConfig)
    data_dir: str = field(default_factory=lambda: str(get_config_dir() / "data"))
    database_path: str = field(default_factory=lambda: str(get_config_dir() / "data" / "agent_bus.db"))


def load_config(path: Path | None = None) -> AppConfig:
    """Resolve configuration and every filesystem path before child cwd changes."""
    from agent_bus.project import project_identity

    root = _project_root()
    config_dir = get_config_dir()
    config = AppConfig(
        project_root=str(root) if root else None,
        keys=KeysConfig(private_key_path=str(config_dir / "private.key"),
                        public_key_path=str(config_dir / "public.key")),
        data_dir=str(config_dir / "data"), database_path=str(config_dir / "data" / "agent_bus.db"),
    )
    project = {}
    if root and (root / ".agent-bus" / "config.yaml").exists():
        project = yaml.safe_load((root / ".agent-bus" / "config.yaml").read_text()) or {}
        if not isinstance(project, dict):
            raise ValueError("Project config must contain a YAML mapping")
    config.bus.project_id = project.get("project_id") or (project_identity(root) if root else "default")
    path = _absolute(path) if path is not None else config_dir / "config.yaml"
    raw = yaml.safe_load(path.read_text()) or {} if path.exists() else {}
    if not isinstance(raw, dict):
        raise ValueError("Runtime config must contain a YAML mapping")

    for section in ("bus", "inbox", "consensus", "logging", "keys"):
        values = raw.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"Configuration section {section} must be a mapping")
        target = getattr(config, section)
        for key, value in values.items():
            if hasattr(target, key):
                setattr(target, key, value)
    for key, value in raw.get("reputation", {}).items():
        if key == "weights" and isinstance(value, dict):
            for weight, amount in value.items():
                if hasattr(config.reputation.weights, weight):
                    setattr(config.reputation.weights, weight, amount)
        elif hasattr(config.reputation, key):
            setattr(config.reputation, key, value)

    config.data_dir = str(_absolute(raw.get("data_dir", config.data_dir), path.parent))
    for key in ("private_key_path", "public_key_path"):
        setattr(config.keys, key, str(_absolute(getattr(config.keys, key), path.parent)))
    configured_db = raw.get("database_path")
    default_db = Path(config.data_dir) / "agent_bus.db"
    legacy_db = config_dir / "agent_bus.db"
    if not configured_db and "data_dir" not in raw and legacy_db.exists() and not default_db.exists():
        default_db = legacy_db
    if "AGENT_BUS_DATABASE_PATH" in os.environ:
        config.database_path = str(_absolute(os.environ["AGENT_BUS_DATABASE_PATH"]))
    else:
        config.database_path = str(_absolute(configured_db or default_db, path.parent))
    config.bus.project_id = os.environ.get("AGENT_BUS_PROJECT_ID", config.bus.project_id)
    return config


def get_bus_url(explicit: str | None = None) -> str:
    """Argument > environment > project marker > runtime host/port > defaults."""
    from urllib.parse import urlsplit

    value = explicit if explicit is not None else os.environ.get("AGENT_BUS_URL")
    if value is None:
        config = load_config()
        project = {}
        if config.project_root:
            path = Path(config.project_root) / ".agent-bus" / "config.yaml"
            if path.exists():
                project = yaml.safe_load(path.read_text()) or {}
        value = project.get("bus_url")
        if value is None:
            host = config.bus.host
            if host == "0.0.0.0":
                host = "127.0.0.1"
            elif host == "::":
                host = "::1"
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            value = f"http://{host}:{config.bus.port}"
    if not isinstance(value, str) or not value:
        raise ValueError("Bus URL must be a nonempty HTTP(S) URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Bus URL must be an HTTP(S) origin without credentials")
    if parsed.query or parsed.fragment or (parsed.port is not None and not 1 <= parsed.port <= 65535):
        raise ValueError("Invalid bus URL")
    return value.rstrip("/")
