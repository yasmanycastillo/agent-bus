"""OpenAI-compatible chat backend. Provider HTTP stays here, not in task code."""

from __future__ import annotations

from agent_bus.orchestrator.client import InferenceClient


class OpenAICompatibleBackend:
    def __init__(self, client: InferenceClient) -> None:
        self._client = client

    async def complete(self, messages: list[dict[str, str]]) -> str:
        return await self._client.chat_completion(messages, response_format={"type": "json_object"})
