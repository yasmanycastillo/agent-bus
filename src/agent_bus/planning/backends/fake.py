"""Canned inference backend for tests. It records which strategy called it."""

from __future__ import annotations


class FakeBackend:
    def __init__(self, content: str, name: str) -> None:
        self.content = content
        self.name = name
        self.calls = 0

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        if not messages:
            raise ValueError("planner sent no messages")
        return self.content
