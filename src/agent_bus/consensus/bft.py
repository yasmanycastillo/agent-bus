from __future__ import annotations

from dataclasses import dataclass

from agent_bus.consensus.types import Proposal, VoteRecord
from agent_bus.reputation.manager import ReputationManager


@dataclass
class BFTConfig:
    f: int = 1
    lambda_threshold: float = 0.3
    round_timeout_ms: int = 30000
    min_proposals: int = 2


class BFTConsensusError(Exception):
    pass


class BFTAdaptiveConsensus:
    def __init__(self, config: BFTConfig, reputation: ReputationManager) -> None:
        self.config = config
        self._reputation = reputation

    async def run_round(self, proposals: list[Proposal], votes: list[VoteRecord]) -> Proposal:
        if len(proposals) < self.config.min_proposals:
            raise BFTConsensusError(
                f"Need at least {self.config.min_proposals} proposals, got {len(proposals)}"
            )

        # Filter by reputation threshold
        eligible = []
        for p in proposals:
            score = await self._reputation.get_score(p.agent_id)
            if score >= self.config.lambda_threshold:
                eligible.append(p)

        if not eligible:
            raise BFTConsensusError("No proposals meet the reputation threshold")

        # Verify no self-votes and collect valid votes
        valid_votes: dict[str, list[VoteRecord]] = {}
        for v in votes:
            if v.voter_id == v.nominee_id:
                continue
            # Check nominee is in eligible proposals
            nominee_ids = {p.agent_id for p in eligible}
            if v.nominee_id not in nominee_ids:
                continue
            valid_votes.setdefault(v.nominee_id, []).append(v)

        # Count weighted votes
        vote_counts: dict[str, float] = {}
        for nominee_id, nominee_votes in valid_votes.items():
            total = 0.0
            for v in nominee_votes:
                rep_score = await self._reputation.get_score(v.voter_id)
                total += rep_score * v.confidence
            vote_counts[nominee_id] = total

        # Require supermajority (2f+1)
        total_votes = sum(len(v) for v in valid_votes.values())
        required = 2 * self.config.f + 1

        if total_votes < required:
            # Fall back to highest reputation proposal
            best = max(eligible, key=lambda p: (p.confidence, p.agent_id))
            return best

        winner_id = max(vote_counts, key=vote_counts.get) if vote_counts else None
        if not winner_id:
            best = max(eligible, key=lambda p: p.confidence)
            return best

        return next(p for p in eligible if p.agent_id == winner_id)

    async def verify_proposal(
        self, proposal: Proposal, public_key: str | None = None
    ) -> bool:
        if not proposal.signature or not public_key:
            return True  # Unsigned proposals are allowed but unverified
        from agent_bus.crypto import verify_signature
        from agent_bus.types import Envelope, MessageType

        envelope = Envelope(
            from_agent=proposal.agent_id,
            message_type=MessageType.CONSENSUS_PROPOSAL,
            body={"content": proposal.content, "confidence": proposal.confidence},
        )
        return verify_signature(public_key, proposal.signature, envelope)
