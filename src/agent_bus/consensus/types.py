from __future__ import annotations

import enum
from pydantic import BaseModel, Field


class ConsensusPhase(str, enum.Enum):
    PROPOSAL = "proposal"
    ARGUMENT = "argument"
    REBUTTAL = "rebuttal"
    VOTE = "vote"
    REVEAL = "reveal"
    SYNTHESIS = "synthesis"


class Proposal(BaseModel):
    agent_id: str
    content: str
    confidence: float = 0.5
    latency_ms: float = 0.0
    signature: str | None = None
    round_number: int = 0


class VoteRecord(BaseModel):
    voter_id: str
    nominee_id: str
    reason: str = ""
    confidence: float = 0.5
    signature: str | None = None
    round_number: int = 0


class ConsensusRound(BaseModel):
    round_id: str
    round_number: int = 0
    topic: str
    proposals: list[Proposal] = Field(default_factory=list)
    votes: list[VoteRecord] = Field(default_factory=list)
    winner_id: str | None = None
    final_answer: str | None = None
    phase: ConsensusPhase = ConsensusPhase.PROPOSAL
    participant_ids: list[str] = Field(default_factory=list)
