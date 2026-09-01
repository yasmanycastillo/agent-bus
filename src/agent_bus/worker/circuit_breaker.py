"""Circuit breakers del worker (sección 6 de la arquitectura).

Controla: límite de turnos por tarea (max turn cap), presupuesto/tokens por
objetivo, y detección de locks estancados y deadlocks (ciclos de espera).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("agent_bus.worker.circuit_breaker")


class BreakerReason(str, Enum):
    TURN_CAP = "turn_cap"
    BUDGET = "budget_exceeded"
    STALE_LOCK = "stale_lock"
    DEADLOCK = "deadlock"


@dataclass
class BreakerTripped:
    """Un circuit breaker se disparó; el caller debe detener/escalar."""

    reason: BreakerReason
    subject: str  # task_id, agent_id o path del lock
    detail: str


@dataclass
class _TaskUsage:
    turns: int = 0
    tokens: int = 0
    messages: list[tuple[str, float]] = field(default_factory=list)


class TaskTurnBreaker:
    """Max turn cap: N intercambios sobre una tarea sin progreso => alerta."""

    def __init__(self, max_turns: int = 10, max_messages: int = 10) -> None:
        self.max_turns = max_turns
        self.max_messages = max_messages
        self._usage: dict[str, _TaskUsage] = {}

    def record_turn(self, task_id: str, tokens: int = 0) -> BreakerTripped | None:
        usage = self._usage.setdefault(task_id, _TaskUsage())
        usage.turns += 1
        usage.tokens += tokens
        if usage.turns > self.max_turns:
            detail = f"Task {task_id} exceeded {self.max_turns} turns without completing"
            logger.warning(detail)
            return BreakerTripped(BreakerReason.TURN_CAP, task_id, detail)
        return None

    def record_message(self, task_id: str, from_agent: str) -> BreakerTripped | None:
        """Dos agentes intercambiando > N mensajes sobre la misma tarea sin progreso."""
        usage = self._usage.setdefault(task_id, _TaskUsage())
        usage.messages.append((from_agent, time.monotonic()))
        if len(usage.messages) > self.max_messages:
            detail = (
                f"Task {task_id} exceeded {self.max_messages} messages between agents"
            )
            logger.warning(detail)
            return BreakerTripped(BreakerReason.TURN_CAP, task_id, detail)
        return None

    def record_progress(self, task_id: str) -> None:
        """Progreso observable (status change): resetea los contadores."""
        self._usage.pop(task_id, None)


class BudgetBreaker:
    """Presupuesto máximo de tokens por objetivo/tarea."""

    def __init__(self, max_tokens: int = 1_000_000) -> None:
        self.max_tokens = max_tokens
        self._spent: dict[str, int] = {}

    def record_usage(self, subject: str, tokens: int) -> BreakerTripped | None:
        self._spent[subject] = self._spent.get(subject, 0) + tokens
        if self._spent[subject] > self.max_tokens:
            detail = (
                f"Budget for '{subject}' exceeded: {self._spent[subject]} > {self.max_tokens} tokens"
            )
            logger.warning(detail)
            return BreakerTripped(BreakerReason.BUDGET, subject, detail)
        return None

    def remaining(self, subject: str) -> int:
        return max(0, self.max_tokens - self._spent.get(subject, 0))


class StaleLockDetector:
    """Locks sin heartbeat por > ttl => estancados; se reportan para revocar."""

    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._last_activity: dict[str, float] = {}
        self._lock_owner: dict[str, str] = {}

    def observe(self, file_path: str, owner: str) -> None:
        """Registra actividad (heartbeat o lock recién adquirido)."""
        self._last_activity[file_path] = time.monotonic()
        self._lock_owner[file_path] = owner

    def forget(self, file_path: str) -> None:
        self._last_activity.pop(file_path, None)
        self._lock_owner.pop(file_path, None)

    def stale_locks(self) -> list[BreakerTripped]:
        now = time.monotonic()
        tripped: list[BreakerTripped] = []
        for path, last in list(self._last_activity.items()):
            if now - last > self.ttl_seconds:
                owner = self._lock_owner.get(path, "?")
                detail = f"Lock on '{path}' (owner {owner}) stale for {now - last:.0f}s"
                tripped.append(BreakerTripped(BreakerReason.STALE_LOCK, path, detail))
        return tripped


def detect_deadlock(
    wait_graph: dict[str, str],
) -> BreakerTripped | None:
    """Detecta ciclos en el grafo de espera (agente -> archivo que espera).

    ``wait_graph`` mapea cada lock activo ``file_path -> agent_id_dueño``;
    se construye el grafo agente-espera-a-agente a partir de qué archivos
    cada agente quiere usar y quién los posee. Aquí aceptamos directamente
    ``agent -> agent`` para simplicidad del detector.
    """
    visited: dict[str, str] = {}  # node -> desde dónde llegó
    for start in wait_graph:
        if start in visited:
            continue
        path = [start]
        node = start
        while node in wait_graph:
            nxt = wait_graph[node]
            if nxt in path:
                cycle = path[path.index(nxt):] + [nxt]
                detail = " -> ".join(cycle)
                logger.warning("Deadlock detected: %s", detail)
                return BreakerTripped(BreakerReason.DEADLOCK, start, detail)
            if nxt in visited:
                break
            path.append(nxt)
            node = nxt
        for n in path:
            visited[n] = start
    return None
