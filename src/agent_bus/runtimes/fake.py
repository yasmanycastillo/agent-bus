"""In-memory runtime for protocol tests. It never launches a process."""

from __future__ import annotations

from agent_bus.runtimes.protocol import (
    AttemptConflict, RuntimeMessage, RuntimeSession, RuntimeStartRequest,
)

TERMINAL = {"completed", "failed", "cancelled"}


class FakeRuntime:
    def __init__(self) -> None:
        self._by_key: dict[str, RuntimeSession] = {}
        self._by_id: dict[str, RuntimeSession] = {}
        self.sent: list[tuple[str, str]] = []

    async def start(self, request: RuntimeStartRequest) -> RuntimeSession:
        existing = self._by_key.get(request.idempotency_key)
        if existing is not None:
            return existing
        for session in self._by_id.values():
            if session.task_id == request.task_id and session.state == "unknown":
                raise AttemptConflict(f"reconcile attempt {session.attempt_id} before starting another")
        created = RuntimeSession(
            request.attempt_id, request.task_id, f"fake:{request.agent_id}", request.workspace_ref, "started",
        )
        self._by_key[request.idempotency_key] = created
        self._by_id[created.attempt_id] = created
        return created

    async def send(self, session: RuntimeSession, message: str) -> RuntimeMessage:
        self.sent.append((session.attempt_id, message))
        return RuntimeMessage(session.attempt_id, message)

    async def cancel(self, session: RuntimeSession) -> None:
        self._replace(session.attempt_id, "cancelled")

    async def status(self, attempt_id: str) -> RuntimeSession:
        try:
            return self._by_id[attempt_id]
        except KeyError:
            raise KeyError(attempt_id) from None

    async def mark_unknown(self, attempt_id: str) -> None:
        self._replace(attempt_id, "unknown")

    async def reconcile(self, attempt_id: str, outcome: str) -> RuntimeSession:
        if outcome not in TERMINAL:
            raise ValueError(f"outcome must be one of {sorted(TERMINAL)}")
        current = await self.status(attempt_id)
        if current.state in TERMINAL:
            return current
        if current.state != "unknown":
            raise AttemptConflict(f"attempt {attempt_id} is {current.state}")
        return self._replace(attempt_id, outcome)

    def _replace(self, attempt_id: str, state: str) -> RuntimeSession:
        current = self._by_id[attempt_id]
        updated = RuntimeSession(
            current.attempt_id, current.task_id, current.external_ref, current.workspace_ref, state,
        )
        self._by_id[attempt_id] = updated
        for key, session in list(self._by_key.items()):
            if session.attempt_id == attempt_id:
                self._by_key[key] = updated
        return updated
