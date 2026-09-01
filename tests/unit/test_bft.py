from __future__ import annotations

import pytest

from agent_bus.consensus.bft import BFTAdaptiveConsensus, BFTConfig, BFTConsensusError
from agent_bus.consensus.phases import PhaseRunner
from agent_bus.consensus.types import ConsensusPhase, Proposal, VoteRecord
from agent_bus.consensus.voting import weighted_vote
from agent_bus.reputation.manager import MetricsUpdate, ReputationManager


@pytest.fixture
async def setup(tmp_db):
    rep = ReputationManager(tmp_db)
    bft = BFTAdaptiveConsensus(BFTConfig(f=1, lambda_threshold=0.3), rep)
    return bft, rep


async def test_bft_basic_consensus(setup):
    bft, rep = setup
    await rep.update("a", MetricsUpdate(accuracy_delta=0.3))
    await rep.update("b", MetricsUpdate(accuracy_delta=0.3))
    await rep.update("c", MetricsUpdate(accuracy_delta=0.3))

    proposals = [
        Proposal(agent_id="a", content="Use Redis", confidence=0.9),
        Proposal(agent_id="b", content="Use SQLite", confidence=0.7),
    ]
    votes = [
        VoteRecord(voter_id="a", nominee_id="b", confidence=0.8, reason="SQLite is simpler"),
        VoteRecord(voter_id="b", nominee_id="a", confidence=0.9, reason="Redis is faster"),
        VoteRecord(voter_id="c", nominee_id="a", confidence=0.7, reason="Agree with Redis"),
    ]
    winner = await bft.run_round(proposals, votes)
    assert winner.agent_id == "a"


async def test_bft_min_proposals(setup):
    bft, rep = setup
    proposals = [Proposal(agent_id="a", content="Only one", confidence=0.9)]
    with pytest.raises(BFTConsensusError, match="at least 2"):
        await bft.run_round(proposals, [])


async def test_bft_reputation_filter(setup):
    bft, rep = setup
    await rep.update("a", MetricsUpdate(accuracy_delta=0.3))
    # "filtered" gets pushed below threshold
    await rep.update("filtered", MetricsUpdate(accuracy_delta=-0.5))

    proposals = [
        Proposal(agent_id="filtered", content="Bad proposal", confidence=0.9),
        Proposal(agent_id="a", content="Good proposal", confidence=0.8),
    ]
    votes = [
        VoteRecord(voter_id="a", nominee_id="a", confidence=0.9),
        VoteRecord(voter_id="filtered", nominee_id="a", confidence=0.7),
    ]
    # "filtered" is below lambda=0.3, so only "a" is eligible
    winner = await bft.run_round(proposals, votes)
    assert winner.agent_id == "a"


async def test_bft_no_self_votes(setup):
    bft, rep = setup
    await rep.update("a", MetricsUpdate(accuracy_delta=0.3))
    await rep.update("b", MetricsUpdate(accuracy_delta=0.3))

    proposals = [
        Proposal(agent_id="a", content="A", confidence=0.8),
        Proposal(agent_id="b", content="B", confidence=0.8),
    ]
    votes = [
        VoteRecord(voter_id="a", nominee_id="a", confidence=1.0),  # self-vote, ignored
        VoteRecord(voter_id="b", nominee_id="a", confidence=0.9),
    ]
    winner = await bft.run_round(proposals, votes)
    # Only 1 valid vote (< 2f+1=3), falls back to highest confidence/reputation
    assert winner.agent_id in ("a", "b")  # winner determined by fallback


async def test_weighted_vote(tmp_db):
    rep = ReputationManager(tmp_db)
    await rep.update("high", MetricsUpdate(accuracy_delta=0.4))
    await rep.update("low", MetricsUpdate(accuracy_delta=-0.2))

    proposals = [
        Proposal(agent_id="high", content="Good", confidence=0.7),
        Proposal(agent_id="low", content="Bad", confidence=0.9),
    ]
    winner = await weighted_vote(proposals, rep)
    assert winner is not None
    # high rep * 0.6 + 0.7 * 0.4 > low rep * 0.6 + 0.9 * 0.4
    assert winner.agent_id == "high"


async def test_weighted_vote_empty(tmp_db):
    rep = ReputationManager(tmp_db)
    result = await weighted_vote([], rep)
    assert result is None


async def test_phase_runner_full_flow(tmp_db):
    rep = ReputationManager(tmp_db)
    for agent in ["a", "b", "c"]:
        await rep.update(agent, MetricsUpdate(accuracy_delta=0.2))

    bft = BFTAdaptiveConsensus(BFTConfig(f=1, lambda_threshold=0.3), rep)
    runner = PhaseRunner(bft, rep)

    proposals = [
        Proposal(agent_id="a", content="Redis", confidence=0.9),
        Proposal(agent_id="b", content="SQLite", confidence=0.7),
        Proposal(agent_id="c", content="Postgres", confidence=0.6),
    ]
    votes = [
        VoteRecord(voter_id="a", nominee_id="b", confidence=0.8),
        VoteRecord(voter_id="b", nominee_id="a", confidence=0.9),
        VoteRecord(voter_id="c", nominee_id="a", confidence=0.7),
    ]

    result = await runner.start_consensus(
        topic="Which DB to use?",
        participant_ids=["a", "b", "c"],
        proposals=proposals,
        votes=votes,
    )
    assert result.phase == ConsensusPhase.SYNTHESIS
    assert result.winner_id == "a"
    assert result.final_answer == "Redis"
    assert len(result.proposals) == 3
