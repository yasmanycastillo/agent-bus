"""Tests de worker/circuit_breaker.py (T7)."""

from __future__ import annotations

from agent_bus.worker.circuit_breaker import (
    BudgetBreaker,
    BreakerReason,
    StaleLockDetector,
    TaskTurnBreaker,
    detect_deadlock,
)


def test_turn_cap_se_dispara():
    breaker = TaskTurnBreaker(max_turns=3)
    assert breaker.record_turn("T1") is None
    assert breaker.record_turn("T1") is None
    assert breaker.record_turn("T1") is None
    tripped = breaker.record_turn("T1")
    assert tripped is not None
    assert tripped.reason == BreakerReason.TURN_CAP
    assert tripped.subject == "T1"


def test_turn_cap_resetea_con_progreso():
    breaker = TaskTurnBreaker(max_turns=2)
    breaker.record_turn("T1")
    breaker.record_progress("T1")  # status cambió
    assert breaker.record_turn("T1") is None  # contador reseteado


def test_message_cap_entre_agentes():
    breaker = TaskTurnBreaker(max_messages=3)
    for i in range(3):
        assert breaker.record_message("T1", "claude" if i % 2 else "agy") is None
    tripped = breaker.record_message("T1", "claude")
    assert tripped is not None and tripped.reason == BreakerReason.TURN_CAP


def test_budget_breaker():
    budget = BudgetBreaker(max_tokens=1000)
    assert budget.record_usage("goal-1", 600) is None
    assert budget.remaining("goal-1") == 400
    tripped = budget.record_usage("goal-1", 500)
    assert tripped is not None
    assert tripped.reason == BreakerReason.BUDGET
    assert budget.remaining("goal-1") == 0


def test_stale_lock_detector(monkeypatch):
    import agent_bus.worker.circuit_breaker as cb

    detector = StaleLockDetector(ttl_seconds=100.0)
    detector.observe("src/a.py", "claude")
    assert detector.stale_locks() == []

    # avanzar el reloj más allá del ttl
    real_monotonic = cb.time.monotonic
    monkeypatch.setattr(cb.time, "monotonic", lambda: real_monotonic() + 200)
    stale = detector.stale_locks()
    assert len(stale) == 1
    assert stale[0].reason == BreakerReason.STALE_LOCK
    assert "src/a.py" in stale[0].subject

    # al olvidar el lock liberado ya no se reporta
    detector.forget("src/a.py")
    assert detector.stale_locks() == []


def test_deadlock_ciclo_simple():
    # claude espera a agy, agy espera a claude
    tripped = detect_deadlock({"claude": "agy", "agy": "claude"})
    assert tripped is not None
    assert tripped.reason == BreakerReason.DEADLOCK
    assert "claude" in tripped.detail and "agy" in tripped.detail


def test_deadlock_ciclo_largo():
    g = {"a": "b", "b": "c", "c": "d", "d": "b"}  # ciclo b->c->d->b
    tripped = detect_deadlock(g)
    assert tripped is not None
    assert tripped.reason == BreakerReason.DEADLOCK


def test_sin_deadlock_cadena_lineal():
    g = {"a": "b", "b": "c", "c": "main"}  # c espera a main, main no espera
    assert detect_deadlock(g) is None


def test_sin_deadlock_grafo_vacio():
    assert detect_deadlock({}) is None
