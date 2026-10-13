"""Inference backends. Static planning does not live here."""

from agent_bus.planning.backends.fake import FakeBackend
from agent_bus.planning.backends.openai_compatible import OpenAICompatibleBackend

__all__ = ["FakeBackend", "OpenAICompatibleBackend"]
