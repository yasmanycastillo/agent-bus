"""Deterministic workflow compiler. A model may write the YAML; only this validates it."""

from __future__ import annotations

from typing import Any

import yaml

FEATURE_DEVELOPMENT = """
workflow: feature-development
version: 1
steps:
  - id: discovery
    requires: [repository-analysis, long-context]
  - id: design
    requires: [architecture]
    depends_on: [discovery]
  - id: implementation
    requires: [implementation, tests]
    depends_on: [design]
  - id: review
    requires: [code-review]
    depends_on: [implementation]
    policy:
      independent_from: [implementation]
  - id: integration
    depends_on: [review]
    gate:
      tests: passed
      review: approved
"""


class WorkflowError(ValueError):
    pass


def load_workflow(source: str, *, name: str | None = None, advisory: bool = False) -> dict[str, Any]:
    if name and not source:
        if name != "feature-development":
            raise WorkflowError(f"unknown workflow {name}")
        source = FEATURE_DEVELOPMENT
    try:
        document = yaml.safe_load(source) if source else None
    except yaml.YAMLError as exc:
        raise WorkflowError(f"invalid workflow YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise WorkflowError("workflow must be a mapping")
    return validate_workflow(document, advisory=advisory)


def validate_workflow(document: dict[str, Any], *, advisory: bool = False) -> dict[str, Any]:
    workflow = document.get("workflow")
    if not isinstance(workflow, str) or not workflow:
        raise WorkflowError("workflow name is required")
    if document.get("version") != 1:
        raise WorkflowError("only workflow version 1 is accepted")
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowError("workflow steps are required")
    steps = []
    seen: set[str] = set()
    for raw in raw_steps:
        step = {"id": raw} if isinstance(raw, str) else dict(raw)
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            raise WorkflowError("every step needs an id")
        if step_id in seen:
            raise WorkflowError(f"duplicate step {step_id}")
        depends = step.get("depends_on") or []
        requires = step.get("requires") or []
        if not isinstance(depends, list) or not all(isinstance(item, str) for item in depends):
            raise WorkflowError(f"{step_id} depends_on must be a list of step ids")
        if any(item not in seen for item in depends):
            raise WorkflowError(f"{step_id} depends on a step that is not defined earlier")
        if not isinstance(requires, list) or not all(isinstance(item, str) and item for item in requires):
            raise WorkflowError(f"{step_id} requires must be a list of capabilities")
        gate = step.get("gate") or {}
        if gate and not isinstance(gate, dict):
            raise WorkflowError(f"{step_id} gate must be a mapping")
        if gate.get("review") not in (None, "approved"):
            raise WorkflowError(f"{step_id} review gate must be approved")
        if gate.get("tests") not in (None, "passed"):
            raise WorkflowError(f"{step_id} tests gate must be passed")
        if advisory and gate.get("review") == "approved":
            raise WorkflowError(f"{step_id} requires review: approved and cannot run in advisory mode")
        independent = (step.get("policy") or {}).get("independent_from") or []
        if any(item not in seen for item in independent):
            raise WorkflowError(f"{step_id} independent_from names an unknown step")
        seen.add(step_id)
        steps.append({**step, "depends_on": depends, "requires": requires, "gate": gate, "independent_from": independent})
    return {"workflow": workflow, "version": 1, "steps": steps}


def compile_tasks(document: dict[str, Any], instance_id: str) -> list[dict[str, Any]]:
    if not instance_id:
        raise WorkflowError("instance_id is required")
    tasks = []
    for step in document["steps"]:
        task_id = f"{document['workflow']}-{instance_id}-{step['id']}"
        acceptance = []
        if step["gate"].get("tests"):
            acceptance.append(f"tests: {step['gate']['tests']}")
        if step["gate"].get("review"):
            acceptance.append(f"review: {step['gate']['review']}")
        description = f"workflow {document['workflow']} instance {instance_id} step {step['id']}"
        if step["independent_from"]:
            description += f"; independent_from {', '.join(step['independent_from'])}"
        tasks.append({
            "task_id": task_id,
            "title": step["id"],
            "description": description,
            "depends_on": [f"{document['workflow']}-{instance_id}-{item}" for item in step["depends_on"]],
            "requirements": list(step["requires"]),
            "acceptance_criteria": acceptance,
            "strict_review": step["gate"].get("review") == "approved",
        })
    return tasks
