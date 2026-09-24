"""Deterministic planner. It has no inference backend."""

from __future__ import annotations

from typing import Any

from agent_bus.orchestrator.schema import PLAN_VERSION, TaskBreakdownPlan, validate_breakdown_dict


class StaticPlanner:
    """Emit a validated TaskBreakdownPlan from an explicit task list."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self._tasks = tasks

    def breakdown(self, objective: str, operation_key: str | None = None) -> TaskBreakdownPlan:
        payload: dict[str, Any] = {
            "plan_version": PLAN_VERSION,
            "objective": objective,
            "tasks": self._tasks,
        }
        if operation_key is not None:
            payload["operation_key"] = operation_key
        return validate_breakdown_dict(payload, operation_key=operation_key)
