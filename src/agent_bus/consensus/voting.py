from __future__ import annotations

from agent_bus.consensus.types import Proposal
from agent_bus.reputation.manager import ReputationManager


async def weighted_vote(
    proposals: list[Proposal],
    reputation: ReputationManager,
) -> Proposal | None:
    """Reputation-weighted vote across proposals. Returns highest-weighted proposal."""
    if not proposals:
        return None

    best: Proposal | None = None
    best_score = -1.0

    for proposal in proposals:
        rep_score = await reputation.get_score(proposal.agent_id)
        combined = rep_score * 0.6 + proposal.confidence * 0.4
        if combined > best_score:
            best_score = combined
            best = proposal

    return best
