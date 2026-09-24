from agent_bus.routing import AgentCandidate, route


def _agent(agent_id, **kwargs):
    defaults = dict(
        declared=frozenset({"implementation", "python"}),
        approved=frozenset({"implementation", "python"}),
        heartbeat_age_seconds=1,
        in_progress=0,
    )
    defaults.update(kwargs)
    return AgentCandidate(agent_id, **defaults)


def test_router_filters_and_ranks_deterministically():
    decision = route(
        ["implementation", "python"],
        [
            _agent("low-priority", priority=1, cost_class="low"),
            _agent("missing", declared=frozenset({"python"})),
            _agent("unapproved", approved=frozenset({"python"})),
            _agent("stale", heartbeat_age_seconds=90),
            _agent("busy", in_progress=1),
            _agent("reader", can_edit=False),
            _agent("high-priority", priority=5, cost_class="high", in_progress=0),
            _agent("same-priority-cheaper", priority=5, cost_class="low", in_progress=0),
        ],
        heartbeat_limit_seconds=30,
    )
    assert decision.selected_agent == "same-priority-cheaper"
    assert decision.eligible == ("same-priority-cheaper", "high-priority", "low-priority")
    assert decision.reasons["missing"] == "missing declared capability"
    assert decision.reasons["unapproved"] == "capability is not project-approved"
    assert decision.reasons["stale"] == "heartbeat is not recent"
    assert decision.reasons["busy"] == "execution capacity is full"
    assert decision.reasons["reader"] == "policy does not allow implementation"


def test_empty_requirements_keep_available_agents():
    decision = route([], [_agent("one", declared=frozenset(), approved=frozenset())], heartbeat_limit_seconds=30)
    assert decision.selected_agent == "one"
    assert decision.eligible == ("one",)
