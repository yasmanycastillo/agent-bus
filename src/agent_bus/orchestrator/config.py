from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
import yaml

ProviderType = Literal["openai", "vllm", "ollama", "openrouter", "custom"]

DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "vllm": "http://localhost:8000/v1",
    "ollama": "http://localhost:11434/v1",
    "custom": "http://localhost:8000/v1",
}

DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "openrouter": "nousresearch/hermes-3-llama-3.1-405b",
    "vllm": "hermes-3",
    "ollama": "hermes3",
    "custom": "hermes-3",
}

SECRET_CONFIG_KEYS = {"api_key", "secret", "bearer_token", "auth_token", "key"}


def is_tracked_by_git(file_path: Path) -> bool:
    """Return True if file is tracked by git or git-trackable (not gitignored)."""
    try:
        path = file_path.resolve()
        # 1. Check if git ls-files recognizes it
        res = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            capture_output=True,
            cwd=path.parent,
        )
        if res.returncode == 0:
            return True

        # 2. Check if git check-ignore ignores it
        res_ignore = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            capture_output=True,
            cwd=path.parent,
        )
        # If check-ignore returns 0, it IS ignored, so NOT tracked.
        # If returncode != 0, it is NOT ignored (so it could be tracked in git).
        if res_ignore.returncode != 0:
            # Check if it's inside a git repository
            res_repo = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                cwd=path.parent,
            )
            if res_repo.returncode == 0 and res_repo.stdout.strip() == b"true":
                return True
        return False
    except Exception:
        return False


@dataclass
class OrchestratorConfig:
    provider: ProviderType = "openai"
    base_url: str = field(default="")
    model: str = "hermes-3"
    api_key: str | None = None
    timeout: float = 60.0
    max_retries: int = 2
    temperature: float = 0.2
    max_tokens: int | None = 4096
    extra_headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider not in DEFAULT_BASE_URLS and self.provider != "custom":
            raise ValueError(f"Unsupported provider: {self.provider}")
        if not self.base_url:
            self.base_url = DEFAULT_BASE_URLS.get(self.provider, "https://api.openai.com/v1")
        self.base_url = self.base_url.rstrip("/")

    @classmethod
    def from_env(
        cls,
        provider: ProviderType | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> OrchestratorConfig:
        """Create OrchestratorConfig resolving from environment variables."""
        resolved_provider: ProviderType = (
            provider
            or os.environ.get("HERMES_PROVIDER")  # type: ignore[assignment]
            or os.environ.get("ORCHESTRATOR_PROVIDER")  # type: ignore[assignment]
            or "openai"
        )
        if resolved_provider not in DEFAULT_BASE_URLS and resolved_provider != "custom":
            resolved_provider = "custom"

        resolved_base_url = (
            base_url
            or os.environ.get("HERMES_BASE_URL")
            or os.environ.get("HERMES_ENDPOINT")
            or os.environ.get("ORCHESTRATOR_ENDPOINT")
            or os.environ.get("OPENAI_BASE_URL")
            or DEFAULT_BASE_URLS.get(resolved_provider, "https://api.openai.com/v1")
        )

        resolved_model = (
            model
            or os.environ.get("HERMES_MODEL")
            or os.environ.get("ORCHESTRATOR_MODEL")
            or DEFAULT_MODELS.get(resolved_provider, "hermes-3")
        )

        # Resolve API key from environment variables
        resolved_api_key = api_key
        if resolved_api_key is None:
            # 1. Global hermes key
            resolved_api_key = os.environ.get("HERMES_API_KEY")
        if resolved_api_key is None:
            if resolved_provider == "openai":
                resolved_api_key = os.environ.get("OPENAI_API_KEY")
            elif resolved_provider == "openrouter":
                resolved_api_key = os.environ.get("OPENROUTER_API_KEY")
            elif resolved_provider == "vllm":
                resolved_api_key = os.environ.get("VLLM_API_KEY")
            elif resolved_provider == "ollama":
                resolved_api_key = os.environ.get("OLLAMA_API_KEY")
        if resolved_api_key is None:
            resolved_api_key = os.environ.get("LLM_API_KEY")

        timeout = float(os.environ.get("HERMES_TIMEOUT", kwargs.get("timeout", 60.0)))
        temperature = float(os.environ.get("HERMES_TEMPERATURE", kwargs.get("temperature", 0.2)))
        max_tokens_val = os.environ.get("HERMES_MAX_TOKENS")
        max_tokens = int(max_tokens_val) if max_tokens_val else kwargs.get("max_tokens", 4096)

        return cls(
            provider=resolved_provider,
            base_url=resolved_base_url,
            model=resolved_model,
            api_key=resolved_api_key,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_headers=kwargs.get("extra_headers", {}),
        )

    @classmethod
    def from_file(
        cls,
        path: Path | str,
        allow_tracked_secrets: bool = False,
    ) -> OrchestratorConfig:
        """Load config from a YAML or JSON file.

        Rejects raw secrets if the config file is tracked or trackable in Git.
        """
        config_path = Path(path).resolve()
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        raw = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError("Configuration file must contain a YAML/JSON mapping")

        # Check for secrets in tracked files
        if not allow_tracked_secrets:
            contains_secret = any(k in raw and raw[k] for k in SECRET_CONFIG_KEYS)
            if contains_secret and is_tracked_by_git(config_path):
                raise ValueError(
                    f"Refusing to load secrets from git-tracked file: {config_path}. "
                    "Secrets must be loaded from environment variables (e.g. HERMES_API_KEY, "
                    "OPENAI_API_KEY) or non-versioned files ignored by Git."
                )

        provider = raw.get("provider", "openai")
        base_url = raw.get("base_url") or raw.get("endpoint")
        model = raw.get("model")
        api_key = raw.get("api_key")

        # If api_key not in file, fallback to env
        cfg = cls.from_env(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=float(raw.get("timeout", 60.0)),
            temperature=float(raw.get("temperature", 0.2)),
            max_tokens=raw.get("max_tokens", 4096),
            extra_headers=raw.get("extra_headers", {}),
        )
        return cfg
