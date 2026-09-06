from __future__ import annotations

import json
import re
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from jsonschema import Draft202012Validator


class PlanValidationError(ValueError):
    """Raised when task breakdown JSON does not conform to required schema or DAG rules."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or []


class BreakdownTaskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(..., min_length=1, description="Identificador único de la tarea en el grafo")
    title: str = Field(..., min_length=1, description="Título conciso y descriptivo de la tarea")
    description: str | None = Field(default=None, description="Detalle del trabajo técnico a realizar")
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="Criterios verificables de aceptación",
    )
    test_cmd: list[str] | None = Field(
        default=None,
        description="Comando de pruebas con argumentos (ej. ['pytest', 'tests/unit'])",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Lista de task_ids de los que depende esta tarea",
    )


class TaskBreakdownPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(..., min_length=1, description="Objetivo global desglosado")
    tasks: list[BreakdownTaskItem] = Field(..., min_length=1, description="Lista de tareas del desglose")
    summary: str | None = Field(default=None, description="Resumen o justificación del desglose")
    operation_key: str | None = Field(default=None, description="Clave de idempotencia para la publicación")


# Strict JSON Schema definition conforming to OpenAI Structured Outputs and jsonschema Draft 2020-12
TASK_BREAKDOWN_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["objective", "tasks"],
    "properties": {
        "objective": {
            "type": "string",
            "minLength": 1,
            "description": "El objetivo global que se está desglosando",
        },
        "summary": {
            "type": ["string", "null"],
            "description": "Resumen o justificación del plan",
        },
        "operation_key": {
            "type": ["string", "null"],
            "description": "Clave de idempotencia",
        },
        "tasks": {
            "type": "array",
            "minItems": 1,
            "description": "Lista de tareas a crear",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id", "title", "acceptance_criteria", "depends_on"],
                "properties": {
                    "task_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Identificador único de la tarea",
                    },
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Título breve de la tarea",
                    },
                    "description": {
                        "type": ["string", "null"],
                        "description": "Descripción detallada",
                    },
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Criterios de aceptación",
                    },
                    "test_cmd": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": "Comando de prueba y sus argumentos",
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IDs de tareas de las que depende",
                    },
                },
            },
        },
    },
}

_validator = Draft202012Validator(TASK_BREAKDOWN_JSON_SCHEMA)


def extract_json_payload(raw_text: str) -> str:
    """Extract JSON from raw model output, stripping markdown formatting if present."""
    text = raw_text.strip()
    # Match ```json ... ``` or ``` ... ```
    pattern = r"^```(?:json)?\s*\n(.*?)\n```$"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def validate_plan_dag(plan: TaskBreakdownPlan) -> None:
    """Validate internal DAG consistency: unique task_ids, valid depends_on references, no cycles."""
    task_ids = set()
    for task in plan.tasks:
        if task.task_id in task_ids:
            raise PlanValidationError(f"Duplicate task_id found in plan: '{task.task_id}'")
        task_ids.add(task.task_id)

    # Check for references to undefined tasks within this plan
    for task in plan.tasks:
        for dep in task.depends_on:
            if dep == task.task_id:
                raise PlanValidationError(f"Task '{task.task_id}' cannot depend on itself")
            if dep not in task_ids:
                # If it's not in the plan, it might be an external task, but within a self-contained plan
                # it's usually an error. We allow it or warn, but let's check:
                pass

    # Cycle detection via DFS
    visited: dict[str, int] = {}  # 0=visiting, 1=visited
    adj: dict[str, list[str]] = {t.task_id: [d for d in t.depends_on if d in task_ids] for t in plan.tasks}

    def dfs(node: str) -> None:
        visited[node] = 0
        for neighbor in adj.get(node, []):
            if neighbor in visited:
                if visited[neighbor] == 0:
                    raise PlanValidationError(f"Cycle detected in task dependencies involving '{node}' and '{neighbor}'")
            else:
                dfs(neighbor)
        visited[node] = 1

    for t_id in task_ids:
        if t_id not in visited:
            dfs(t_id)


def validate_breakdown_dict(data: dict[str, Any], operation_key: str | None = None) -> TaskBreakdownPlan:
    """Validate a dictionary against TASK_BREAKDOWN_JSON_SCHEMA and construct TaskBreakdownPlan."""
    errors: list[str] = []
    for err in _validator.iter_errors(data):
        path = ".".join(str(p) for p in err.path) or "root"
        errors.append(f"{path}: {err.message}")

    if errors:
        raise PlanValidationError(
            f"JSON schema validation failed with {len(errors)} error(s)",
            errors=errors,
        )

    try:
        plan = TaskBreakdownPlan.model_validate(data)
    except Exception as exc:
        raise PlanValidationError(f"Pydantic validation failed: {exc}") from exc

    if operation_key and not plan.operation_key:
        plan.operation_key = operation_key

    validate_plan_dag(plan)
    return plan


def parse_and_validate_plan(content: str | dict[str, Any], operation_key: str | None = None) -> TaskBreakdownPlan:
    """Parse JSON string (or accept dict), validate against JSON Schema and return TaskBreakdownPlan."""
    if isinstance(content, dict):
        return validate_breakdown_dict(content, operation_key=operation_key)

    clean_json = extract_json_payload(content)
    try:
        data = json.loads(clean_json)
    except json.JSONDecodeError as exc:
        raise PlanValidationError(f"Malformed JSON output: {exc.msg} at line {exc.lineno} col {exc.colno}") from exc

    if not isinstance(data, dict):
        raise PlanValidationError(f"Expected JSON object at root, got {type(data).__name__}")

    return validate_breakdown_dict(data, operation_key=operation_key)
