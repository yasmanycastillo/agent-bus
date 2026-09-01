from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class MessageType(str, enum.Enum):
    INBOX = "inbox"
    BROADCAST = "broadcast"
    CONSENSUS_PROPOSAL = "consensus_proposal"
    CONSENSUS_VOTE = "consensus_vote"
    CONSENSUS_REVEAL = "consensus_reveal"
    HANDOFF = "handoff"
    HEARTBEAT = "heartbeat"
    STATUS_UPDATE = "status_update"
    REVIEW_REQUEST = "review_request"
    BLOCKER = "blocker"
    FYI = "fyi"


class InboxCategory(str, enum.Enum):
    QUESTION = "question"
    ANSWER = "answer"
    HANDOFF = "handoff"
    REVIEW_REQUEST = "review-request"
    STATUS = "status"
    BLOCKER = "blocker"
    FYI = "fyi"


class AgentStatus(str, enum.Enum):
    ONLINE = "online"
    AWAY = "away"
    OFFLINE = "offline"
    BUSY = "busy"


class AutonomyLevel(int, enum.Enum):
    READ_ONLY = 0
    AUDIT = 1
    DOCS_TESTS = 2
    RUNTIME_NON_CORE = 3
    CORE_DOMAIN = 4


class Envelope(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str | None = None
    from_agent: str
    to_agent: str | None = None
    message_type: MessageType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    signature: str | None = None
    reply_needed: bool = False
    related_task: str | None = None
    body: dict | None = None
    metadata: dict = Field(default_factory=dict)


class AgentInfo(BaseModel):
    agent_id: str
    display_name: str
    capabilities: list[str] = Field(default_factory=list)
    status: AgentStatus = AgentStatus.ONLINE
    autonomy_level: AutonomyLevel = AutonomyLevel.READ_ONLY
    endpoint: str | None = None
    public_key: str | None = None
    registered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_heartbeat: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    active_work: dict | None = None


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


class Task(BaseModel):
    task_id: str
    title: str
    description: str | None = None
    owner: str = "free"
    status: TaskStatus = TaskStatus.PENDING
    locked_files: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Decision(BaseModel):
    decision_id: str
    title: str
    context: str
    decision: str
    alternatives: list[str] = Field(default_factory=list)
    consequences: str | None = None
    decided_by: str
    supersedes: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentSkill(BaseModel):
    agent_id: str
    role: str
    responsibilities: list[str] = Field(default_factory=list)
    audit_questions: list[str] = Field(default_factory=list)


class Lock(BaseModel):
    file_path: str
    locked_by: str
    locked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reason: str | None = None


class KickoffStep(BaseModel):
    step: int
    name: str
    status: str = "pending"
    result: dict | None = None
    completed_by: str | None = None
    completed_at: datetime | None = None


KICKOFF_STEPS: list[tuple[int, str]] = [
    (0, "project_brief"),
    (1, "agent_cards"),
    (2, "peer_questions"),
    (3, "peer_answers"),
    (4, "team_charter"),
    (5, "human_approval"),
    (6, "task_breakdown"),
    (7, "ownership_assignment"),
    (8, "ready_signal"),
]


class MessageBody(BaseModel):
    type: str = "fyi"
    topic: str | None = None
    detail: str | None = None
    expects: str | None = None
    context: dict | None = None


class HandoffContext(BaseModel):
    task_id: str
    from_agent: str
    to_agent: str
    summary: str
    files_touched: list[str] = Field(default_factory=list)
    decisions_made: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    state: dict = Field(default_factory=dict)
