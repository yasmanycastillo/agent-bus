"""Deterministic watchdog: what is stuck in the hub, reported once per episode. No model involved."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_bus.core.tasks import REVIEW_WAIT


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str
    message: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.subject}"


def _age(task: dict[str, Any], now: datetime) -> timedelta | None:
    try:
        return now - datetime.fromisoformat(str(task.get("updated_at")))
    except (TypeError, ValueError):
        return None


def findings(
    tasks: list[dict[str, Any]], now: datetime, *, review_minutes: float, stuck_minutes: float,
    integrator_running: bool | None,
) -> list[Finding]:
    """integrator_running=None means unknown (another machine): no integrator finding."""
    found: list[Finding] = []
    in_review = [task for task in tasks if task.get("status") == "in_review"]
    if in_review and integrator_running is False:
        found.append(Finding("integrator_down", "integrator",
                             f"{len(in_review)} tarea(s) en in_review y el integrador no está corriendo"))
    for task in tasks:
        task_id, status, age = str(task.get("task_id")), task.get("status"), _age(task, now)
        minutes = int(age.total_seconds() // 60) if age is not None else None
        if status == "in_review" and minutes is not None and minutes >= review_minutes:
            found.append(Finding("review_stalled", task_id, f"{task_id} lleva {minutes} min en in_review"))
        elif status == "in_progress" and minutes is not None and minutes >= stuck_minutes:
            found.append(Finding("claim_stuck", task_id,
                                 f"{task_id} de {task.get('owner')} lleva {minutes} min sin cambios en in_progress"))
        elif status == "blocked" and task.get("blocked_reason") and task.get("blocked_reason") != REVIEW_WAIT:
            found.append(Finding("needs_human", task_id, f"{task_id} bloqueada: {task.get('blocked_reason')}"))
    return found


def new_since(current: list[Finding], state_file: Path) -> list[Finding]:
    """Findings absent from the previous run; the state file keeps only the current ones."""
    try:
        previous = set(json.loads(state_file.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        previous = set()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(sorted(finding.key for finding in current)), encoding="utf-8")
    return [finding for finding in current if finding.key not in previous]
