"""Deterministic capability routing. No model chooses the agent."""

from __future__ import annotations

from dataclasses import dataclass, field

COST_RANK = {"low": 0, "medium": 1, "high": 2}
EDIT_REQUIREMENT = "implementation"


@dataclass(frozen=True)
class AgentCandidate:
    agent_id: str
    declared: frozenset[str]
    approved: frozenset[str]
    heartbeat_age_seconds: float | None
    in_progress: int
    max_in_progress: int = 1
    priority: int = 0
    cost_class: str = "medium"
    can_edit: bool = True


@dataclass(frozen=True)
class RouteDecision:
    selected_agent: str | None
    eligible: tuple[str, ...]
    reasons: dict[str, str] = field(default_factory=dict)


def route(
    requires: list[str],
    agents: list[AgentCandidate],
    *,
    heartbeat_limit_seconds: float,
) -> RouteDecision:
    needed = set(requires)
    needs_edit = EDIT_REQUIREMENT in needed
    reasons: dict[str, str] = {}
    eligible: list[AgentCandidate] = []
    for agent in agents:
        reason = _reject(agent, needed, needs_edit, heartbeat_limit_seconds)
        if reason is None:
            eligible.append(agent)
        else:
            reasons[agent.agent_id] = reason
    eligible.sort(key=lambda agent: (
        -agent.priority,
        agent.in_progress,
        COST_RANK.get(agent.cost_class, 1),
        agent.agent_id,
    ))
    selected = eligible[0].agent_id if eligible else None
    return RouteDecision(selected, tuple(agent.agent_id for agent in eligible), reasons)


def _reject(
    agent: AgentCandidate,
    needed: set[str],
    needs_edit: bool,
    heartbeat_limit_seconds: float,
) -> str | None:
    if needed and not needed <= set(agent.declared):
        return "missing declared capability"
    if needed and not needed <= set(agent.approved):
        return "capability is not project-approved"
    if agent.heartbeat_age_seconds is None or agent.heartbeat_age_seconds > heartbeat_limit_seconds:
        return "heartbeat is not recent"
    if agent.in_progress >= agent.max_in_progress:
        return "execution capacity is full"
    if needs_edit and not agent.can_edit:
        return "policy does not allow implementation"
    return None
