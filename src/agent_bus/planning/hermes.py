"""Hermes is one planner strategy. It is not the bus planner."""

from __future__ import annotations

from agent_bus.orchestrator.hermes import HermesOrchestrator
from agent_bus.orchestrator.schema import PlanValidationError, TaskBreakdownPlan
from agent_bus.planning.base import PlanningBackend


class HermesPlanner:
    def __init__(self, backend: PlanningBackend) -> None:
        self.backend = backend
        self._orchestrator = HermesOrchestrator(backend=backend)

    async def plan(self, objective: str, operation_key: str | None = None) -> TaskBreakdownPlan:
        result = await self._orchestrator.breakdown_objective(objective, operation_key=operation_key)
        if not result.success or result.plan is None:
            reason = result.failure.reason if result.failure else "Hermes did not return a plan"
            raise PlanValidationError(reason)
        return result.plan
