from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any

from agent_bus.orchestrator.client import (
    InferenceClient,
    InferenceFormatError,
    InferenceNetworkError,
    InferenceStatusError,
    InferenceTimeoutError,
)
from agent_bus.orchestrator.config import OrchestratorConfig
from agent_bus.orchestrator.schema import (
    PlanValidationError,
    TaskBreakdownPlan,
    parse_and_validate_plan,
)

logger = logging.getLogger("agent_bus.orchestrator.hermes")

DEFAULT_SYSTEM_PROMPT = """You are Hermes, the autonomous software engineering architect and orchestrator for agent-bus.
Your responsibility is to analyze software engineering objectives and decompose them into an optimal Directed Acyclic Graph (DAG) of actionable, modular tasks.

Output strictly valid JSON matching this schema:
{
  "objective": "<echo the original objective>",
  "summary": "<brief architecture rationale>",
  "tasks": [
    {
      "task_id": "<unique-kebab-case-id>",
      "title": "<concise title>",
      "description": "<technical implementation details>",
      "acceptance_criteria": ["<verifiable criteria 1>", "<verifiable criteria 2>"],
      "test_cmd": ["pytest", "tests/path/to/test.py"],
      "depends_on": ["<prerequisite-task-id>"]
    }
  ]
}

Rules:
1. Every task must be atomic and testable.
2. Dependencies in 'depends_on' must refer to earlier task_ids in the list, forming a valid DAG with no cycles.
3. Foundational tasks (db, schema, models) must have depends_on: [].
4. Output raw JSON only. Do not wrap in conversational text.
"""


@dataclass
class BreakdownFailureReport:
    """Observable failure report when breakdown or validation fails."""

    objective: str
    operation_key: str | None
    error_type: str
    reason: str
    details: list[str] = field(default_factory=list)
    raw_response: str | None = None
    status: str = "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "operation_key": self.operation_key,
            "error_type": self.error_type,
            "reason": self.reason,
            "details": self.details,
            "raw_response": self.raw_response,
            "status": self.status,
        }


@dataclass
class BreakdownResult:
    """Result of objective breakdown."""

    success: bool
    plan: TaskBreakdownPlan | None = None
    failure: BreakdownFailureReport | None = None


@dataclass
class OrchestrationResult:
    """Result of end-to-end orchestration."""

    success: bool
    plan: TaskBreakdownPlan | None = None
    tasks: list[dict[str, Any]] = field(default_factory=list)
    failure: BreakdownFailureReport | None = None


class HermesOrchestrator:
    """Orchestrator responsible for breaking down objectives using LLM inference and publishing tasks to agent-bus."""

    def __init__(
        self,
        config: OrchestratorConfig | None = None,
        client: InferenceClient | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self.config = config or OrchestratorConfig.from_env()
        self.client = client or InferenceClient(self.config)
        self.system_prompt = system_prompt

    async def breakdown_objective(
        self,
        objective_text: str,
        operation_key: str | None = None,
    ) -> BreakdownResult:
        """Call inference to break down objective, and validate with strict JSON schema.

        If schema is invalid or inference fails, catches the error and returns an observable failure report.
        """
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    f"Objective to break down:\n{objective_text}\n\n"
                    f"Operation key: {operation_key or 'none'}\n"
                    "Provide the task breakdown plan in valid JSON."
                ),
            },
        ]

        raw_response: str | None = None
        try:
            raw_response = await self.client.chat_completion(
                messages,
                response_format={"type": "json_object"},
            )
            plan = parse_and_validate_plan(raw_response, operation_key=operation_key)
            return BreakdownResult(success=True, plan=plan)

        except (InferenceTimeoutError, TimeoutError) as exc:
            logger.warning("Inference timeout during objective breakdown: %s", exc)
            failure = BreakdownFailureReport(
                objective=objective_text,
                operation_key=operation_key,
                error_type="timeout",
                reason=f"Inference request timed out: {exc}",
                status="blocked",
            )
            return BreakdownResult(success=False, failure=failure)

        except InferenceNetworkError as exc:
            logger.warning("Inference network error during objective breakdown: %s", exc)
            failure = BreakdownFailureReport(
                objective=objective_text,
                operation_key=operation_key,
                error_type="network_error",
                reason=f"Network error communicating with model endpoint: {exc}",
                status="blocked",
            )
            return BreakdownResult(success=False, failure=failure)

        except InferenceStatusError as exc:
            logger.warning("Inference status error during objective breakdown: %s", exc)
            failure = BreakdownFailureReport(
                objective=objective_text,
                operation_key=operation_key,
                error_type="status_error",
                reason=f"Model provider returned HTTP {exc.status_code}: {exc}",
                raw_response=exc.body,
                status="blocked",
            )
            return BreakdownResult(success=False, failure=failure)

        except InferenceFormatError as exc:
            logger.warning("Inference format error during objective breakdown: %s", exc)
            failure = BreakdownFailureReport(
                objective=objective_text,
                operation_key=operation_key,
                error_type="format_error",
                reason=f"Invalid provider response structure: {exc}",
                raw_response=raw_response,
                status="blocked",
            )
            return BreakdownResult(success=False, failure=failure)

        except PlanValidationError as exc:
            logger.warning("JSON Schema / DAG validation failed for objective breakdown: %s", exc)
            failure = BreakdownFailureReport(
                objective=objective_text,
                operation_key=operation_key,
                error_type="schema_validation_error",
                reason=exc.message,
                details=exc.errors,
                raw_response=raw_response,
                status="blocked",
            )
            return BreakdownResult(success=False, failure=failure)

        except Exception as exc:
            logger.exception("Unexpected error during objective breakdown: %s", exc)
            failure = BreakdownFailureReport(
                objective=objective_text,
                operation_key=operation_key,
                error_type="unexpected_error",
                reason=f"Unexpected error: {exc}",
                raw_response=raw_response,
                status="blocked",
            )
            return BreakdownResult(success=False, failure=failure)

    async def publish_breakdown(
        self,
        plan: TaskBreakdownPlan,
        bus_client: Any,
    ) -> list[dict[str, Any]]:
        """Publish tasks from plan to agent-bus via POST /tasks/batch using operation_key."""
        payload = {
            "operation_key": plan.operation_key,
            "tasks": [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "description": t.description,
                    "acceptance_criteria": t.acceptance_criteria,
                    "test_cmd": t.test_cmd,
                    "depends_on": t.depends_on,
                    "operation_key": plan.operation_key,
                }
                for t in plan.tasks
            ],
        }

        # Case 1: bus_client is a MessageBus instance directly
        if hasattr(bus_client, "tasks") and hasattr(bus_client.tasks, "create_batch"):
            tasks = await bus_client.tasks.create_batch(payload["tasks"], operation_key=plan.operation_key)
            return [t.model_dump(mode="json") for t in tasks]

        # Case 2: HTTP client (AsyncClient or Client)
        if hasattr(bus_client, "post"):
            res = bus_client.post("/tasks/batch", json=payload)
            if inspect.iscoroutine(res):
                res = await res

            if res.status_code in (200, 201):
                data = res.json()
                if isinstance(data, list):
                    return data
                return [data]
            else:
                error_detail = res.text
                try:
                    error_detail = res.json().get("error", res.text)
                except Exception:
                    pass
                raise RuntimeError(f"Bus rejected task batch (HTTP {res.status_code}): {error_detail}")

        raise TypeError(f"Unsupported bus_client type: {type(bus_client).__name__}")

    async def orchestrate(
        self,
        objective_text: str,
        operation_key: str | None = None,
        bus_client: Any = None,
    ) -> OrchestrationResult:
        """End-to-end orchestration: breakdown, schema validation, and bus publication."""
        breakdown_res = await self.breakdown_objective(objective_text, operation_key=operation_key)

        if not breakdown_res.success or breakdown_res.plan is None:
            failure = breakdown_res.failure or BreakdownFailureReport(
                objective=objective_text,
                operation_key=operation_key,
                error_type="unknown_failure",
                reason="Breakdown failed without explicit error report",
                status="blocked",
            )
            # Notify bus if client available
            if bus_client is not None:
                await self._notify_failure(bus_client, failure)
            return OrchestrationResult(success=False, failure=failure)

        # Breakdown succeeded; publish if bus_client provided
        created_tasks: list[dict[str, Any]] = []
        if bus_client is not None:
            try:
                created_tasks = await self.publish_breakdown(breakdown_res.plan, bus_client)
                await self._notify_success(bus_client, breakdown_res.plan, created_tasks)
            except Exception as exc:
                logger.error("Failed to publish breakdown to bus: %s", exc)
                publish_failure = BreakdownFailureReport(
                    objective=objective_text,
                    operation_key=operation_key,
                    error_type="publish_error",
                    reason=f"Failed to publish breakdown tasks to bus: {exc}",
                    status="blocked",
                )
                await self._notify_failure(bus_client, publish_failure)
                return OrchestrationResult(
                    success=False,
                    plan=breakdown_res.plan,
                    failure=publish_failure,
                )

        return OrchestrationResult(
            success=True,
            plan=breakdown_res.plan,
            tasks=created_tasks,
        )

    async def _notify_failure(self, bus_client: Any, failure: BreakdownFailureReport) -> None:
        """Broadcast failure notification to the bus."""
        msg = {
            "from_agent": "hermes-orchestrator",
            "to_agent": "*",
            "message_type": "inbox",
            "body": {
                "event": "breakdown_failed",
                "objective": failure.objective,
                "reason": failure.reason,
                "error_type": failure.error_type,
                "details": failure.details,
                "status": failure.status,
            },
            "reply_needed": False,
        }
        try:
            if hasattr(bus_client, "post"):
                res = bus_client.post("/messages", json=msg)
                if inspect.iscoroutine(res):
                    await res
        except Exception:
            logger.debug("Failed to emit breakdown failure notice to bus", exc_info=True)

    async def _notify_success(
        self,
        bus_client: Any,
        plan: TaskBreakdownPlan,
        tasks: list[dict[str, Any]],
    ) -> None:
        """Broadcast success notification to the bus."""
        msg = {
            "from_agent": "hermes-orchestrator",
            "to_agent": "*",
            "message_type": "inbox",
            "body": {
                "event": "breakdown_published",
                "objective": plan.objective,
                "tasks_count": len(tasks),
                "task_ids": [t.get("task_id") for t in tasks],
            },
            "reply_needed": False,
        }
        try:
            if hasattr(bus_client, "post"):
                res = bus_client.post("/messages", json=msg)
                if inspect.iscoroutine(res):
                    await res
        except Exception:
            logger.debug("Failed to emit breakdown success notice to bus", exc_info=True)
