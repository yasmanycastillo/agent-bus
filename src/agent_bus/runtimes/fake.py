"""In-memory runtime for protocol tests. It never launches a process."""

from __future__ import annotations

from dataclasses import dataclass

from agent_bus.runtimes.protocol import (
    AttemptConflict, RuntimeMessage, RuntimeResult, RuntimeSession, RuntimeStartRequest, RuntimeStatus,
)

TERMINAL = {"completed", "failed", "cancelled"}


@dataclass
class _Attempt:
    session: RuntimeSession
    outcome: str | None = None
    candidate_sha: str | None = None
    log_refs: tuple[str, ...] = ()
    epoch: str | None = None


class FakeRuntime:
    def __init__(self) -> None:
        self._by_key: dict[str, _Attempt] = {}
        self._by_id: dict[str, _Attempt] = {}
        self.sent: list[tuple[str, str]] = []

    async def start(self, request: RuntimeStartRequest) -> RuntimeSession:
        existing = self._by_key.get(request.idempotency_key)
        if existing is not None:
            return existing.session
        for attempt in self._by_id.values():
            if attempt.session.task_id == request.task_id and attempt.session.state == "unknown":
                raise AttemptConflict(f"reconcile attempt {attempt.session.attempt_id} before starting another")
        created = _Attempt(RuntimeSession(
            request.attempt_id, request.task_id, f"fake:{request.agent_id}", request.workspace_ref, "started",
        ))
        self._by_key[request.idempotency_key] = created
        self._by_id[created.session.attempt_id] = created
        return created.session

    async def send(self, session: RuntimeSession, message: str) -> RuntimeMessage:
        self.sent.append((session.attempt_id, message))
        return RuntimeMessage(session.attempt_id, message)

    async def cancel(self, session: RuntimeSession) -> None:
        self._replace(session.attempt_id, "cancelled")

    async def status(self, session: RuntimeSession) -> RuntimeStatus:
        attempt = self._by_id[session.attempt_id]
        return RuntimeStatus(
            attempt.session.attempt_id, attempt.session.state, attempt.outcome,
            attempt.candidate_sha, attempt.log_refs,
        )

    async def complete(self, session: RuntimeSession, result: RuntimeResult) -> RuntimeStatus:
        if result.outcome not in TERMINAL:
            raise ValueError(f"outcome must be one of {sorted(TERMINAL)}")
        attempt = self._by_id[session.attempt_id]
        if attempt.session.state not in TERMINAL:
            attempt.outcome = result.outcome
            attempt.candidate_sha = result.candidate_sha
            attempt.log_refs = result.log_refs
            self._replace(session.attempt_id, result.outcome)
        return await self.status(self._by_id[session.attempt_id].session)

    async def claim_execution(self, attempt_id: str, epoch: str) -> bool:
        attempt = self._by_id[attempt_id]
        if attempt.session.state in TERMINAL or attempt.session.state == "unknown":
            return False
        if attempt.epoch and attempt.epoch != epoch:
            self._replace(attempt_id, "unknown")
            return False
        if not attempt.epoch:
            attempt.epoch = epoch
        return True

    async def mark_unknown(self, attempt_id: str) -> None:
        self._replace(attempt_id, "unknown")

    async def reconcile(self, attempt_id: str, outcome: str) -> RuntimeStatus:
        if outcome not in TERMINAL:
            raise ValueError(f"outcome must be one of {sorted(TERMINAL)}")
        attempt = self._by_id[attempt_id]
        if attempt.session.state in TERMINAL:
            return await self.status(attempt.session)
        if attempt.session.state != "unknown":
            raise AttemptConflict(f"attempt {attempt_id} is {attempt.session.state}")
        attempt.outcome = outcome
        self._replace(attempt_id, outcome)
        return await self.status(self._by_id[attempt_id].session)

    def _replace(self, attempt_id: str, state: str) -> RuntimeSession:
        current = self._by_id[attempt_id]
        updated = RuntimeSession(
            current.session.attempt_id, current.session.task_id, current.session.external_ref,
            current.session.workspace_ref, state,
        )
        current.session = updated
        for key, attempt in list(self._by_key.items()):
            if attempt.session.attempt_id == attempt_id:
                self._by_key[key] = current
        return updated
