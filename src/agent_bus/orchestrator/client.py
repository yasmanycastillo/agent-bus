from __future__ import annotations

import logging
from typing import Any
import httpx

from agent_bus.orchestrator.config import OrchestratorConfig

logger = logging.getLogger("agent_bus.orchestrator.client")


class InferenceError(Exception):
    """Base error for LLM inference failures."""


class InferenceTimeoutError(InferenceError):
    """Request to inference provider timed out."""


class InferenceNetworkError(InferenceError):
    """Network connection error communicating with inference provider."""


class InferenceStatusError(InferenceError):
    """Provider returned non-200 HTTP status code."""

    def __init__(self, status_code: int, message: str, body: str = "") -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class InferenceFormatError(InferenceError):
    """Provider response body was missing expected fields or malformed."""


class InferenceClient:
    """Async inference client supporting OpenAI-compatible chat completions with structured outputs."""

    def __init__(
        self,
        config: OrchestratorConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or OrchestratorConfig.from_env()
        self._custom_client = http_client
        self._client: httpx.AsyncClient | None = http_client

    async def __aenter__(self) -> InferenceClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._custom_client is None and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        if self.config.provider == "openrouter":
            headers.setdefault("HTTP-Referer", "https://github.com/agent-bus")
            headers.setdefault("X-Title", "agent-bus")

        headers.update(self.config.extra_headers)
        return headers

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Execute chat completion request and return message content string."""
        url = f"{self.config.base_url}/chat/completions"
        headers = self._get_headers()

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
        }

        tokens = self.config.max_tokens if max_tokens is None else max_tokens
        if tokens is not None:
            payload["max_tokens"] = tokens

        if response_format is not None:
            payload["response_format"] = response_format

        close_after = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self.config.timeout)
            close_after = True

        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"Request to {url} timed out: {exc}") from exc
        except (httpx.NetworkError, httpx.ConnectError) as exc:
            raise InferenceNetworkError(f"Network error connecting to {url}: {exc}") from exc
        except Exception as exc:
            raise InferenceError(f"Unexpected error communicating with {url}: {exc}") from exc
        finally:
            if close_after:
                await client.aclose()

        if response.status_code != 200:
            raise InferenceStatusError(
                response.status_code,
                response.reason_phrase or "Error response from provider",
                body=response.text,
            )

        try:
            data = response.json()
        except Exception as exc:
            raise InferenceFormatError(f"Response from {url} is not valid JSON: {response.text[:200]}") from exc

        if not isinstance(data, dict):
            raise InferenceFormatError(f"Expected JSON object response from {url}, got {type(data).__name__}")

        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            raise InferenceFormatError(f"Response from {url} missing 'choices' list: {data}")

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise InferenceFormatError("Invalid choice item in response")

        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise InferenceFormatError("Missing or invalid 'message' in response choice")

        content = message.get("content")
        if content is None:
            raise InferenceFormatError("Missing 'content' in message choice")

        return str(content)
