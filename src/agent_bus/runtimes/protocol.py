"""Runtime contract. Provider-specific processes stay behind this protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RuntimeStartRequest:
    attempt_id: str
    task_id: str
    idempotency_key: str
    agent_id: str
    workspace_ref: str | None = None


@dataclass(frozen=True)
class RuntimeSession:
    attempt_id: str
    task_id: str
    external_ref: str
    workspace_ref: str | None
    state: str


@dataclass(frozen=True)
class RuntimeMessage:
    attempt_id: str
    text: str


class AttemptConflict(RuntimeError):
    """An unknown attempt must be reconciled before another start."""


class AgentRuntime(Protocol):
    async def start(self, request: RuntimeStartRequest) -> RuntimeSession: ...

    async def send(self, session: RuntimeSession, message: str) -> RuntimeMessage: ...

    async def cancel(self, session: RuntimeSession) -> None: ...

    async def status(self, attempt_id: str) -> RuntimeSession: ...
