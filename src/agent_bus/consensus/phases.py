from __future__ import annotations

import uuid

from agent_bus.consensus.bft import BFTAdaptiveConsensus
from agent_bus.consensus.types import ConsensusPhase, ConsensusRound, Proposal, VoteRecord
from agent_bus.reputation.manager import MetricsUpdate, ReputationManager


class PhaseRunner:
    def __init__(
        self,
        bft: BFTAdaptiveConsensus,
        reputation: ReputationManager,
    ) -> None:
        self._bft = bft
        self._reputation = reputation
        self._active_rounds: dict[str, ConsensusRound] = {}

    async def start_consensus(
        self,
        topic: str,
        participant_ids: list[str],
        proposals: list[Proposal],
        votes: list[VoteRecord],
        rounds: int = 2,
    ) -> ConsensusRound:
        round_id = str(uuid.uuid4())
        current_round = ConsensusRound(
            round_id=round_id,
            topic=topic,
            participant_ids=participant_ids,
            phase=ConsensusPhase.PROPOSAL,
            proposals=proposals,
            votes=votes,
        )
        self._active_rounds[round_id] = current_round

        # Phase 1: Proposals already collected
        current_round.phase = ConsensusPhase.ARGUMENT

        # Phase 2: Argument (proposals stand as arguments)
        current_round.phase = ConsensusPhase.VOTE

        # Phase 3: Vote
        current_round.phase = ConsensusPhase.REVEAL

        # Phase 4: BFT Reveal - determine winner
        winner = await self._bft.run_round(current_round.proposals, current_round.votes)
        current_round.winner_id = winner.agent_id

        # Phase 5: Synthesis
        current_round.phase = ConsensusPhase.SYNTHESIS
        current_round.final_answer = winner.content

        # Update reputation based on outcome
        for proposal in current_round.proposals:
            if proposal.agent_id == winner.agent_id:
                await self._reputation.update(
                    proposal.agent_id, MetricsUpdate(accuracy_delta=0.05)
                )
            else:
                await self._reputation.update(
                    proposal.agent_id, MetricsUpdate(accuracy_delta=-0.02)
                )

        return current_round

    def get_round(self, round_id: str) -> ConsensusRound | None:
        return self._active_rounds.get(round_id)
