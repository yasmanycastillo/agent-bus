"""Planner strategies and inference backends are separate."""

from __future__ import annotations

from typing import Protocol

from agent_bus.orchestrator.schema import TaskBreakdownPlan


class PlanningBackend(Protocol):
    async def complete(self, messages: list[dict[str, str]]) -> str:
        """Return the raw model text. A static planner does not use this."""


class Planner(Protocol):
    async def plan(self, objective: str, operation_key: str | None = None) -> TaskBreakdownPlan:
        """Return a validated plan. The bus, not the planner, stores the tasks."""
