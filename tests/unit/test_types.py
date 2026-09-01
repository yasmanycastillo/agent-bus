from __future__ import annotations

from agent_bus.types import (
    AgentInfo,
    AgentStatus,
    AutonomyLevel,
    Envelope,
    InboxCategory,
    MessageType,
)


def test_envelope_defaults():
    env = Envelope(from_agent="claude", message_type=MessageType.INBOX)
    assert env.from_agent == "claude"
    assert env.message_type == MessageType.INBOX
    assert env.to_agent is None
    assert env.reply_needed is False
    assert env.related_task is None
    assert env.body is None
    assert len(env.message_id) == 36  # UUID format


def test_envelope_with_all_fields():
    env = Envelope(
        from_agent="claude",
        to_agent="codex",
        message_type=MessageType.BLOCKER,
        reply_needed=True,
        related_task="T2.3",
        body={"text": "Test failed on main"},
        metadata={"priority": "high"},
    )
    assert env.to_agent == "codex"
    assert env.reply_needed is True
    assert env.body["text"] == "Test failed on main"


def test_agent_info_defaults():
    info = AgentInfo(agent_id="claude", display_name="Claude")
    assert info.status == AgentStatus.ONLINE
    assert info.autonomy_level == AutonomyLevel.READ_ONLY
    assert info.capabilities == []
    assert info.active_work is None


def test_agent_info_with_capabilities():
    info = AgentInfo(
        agent_id="codex",
        display_name="Codex",
        capabilities=["code", "review"],
        autonomy_level=AutonomyLevel.CORE_DOMAIN,
    )
    assert "code" in info.capabilities
    assert info.autonomy_level == AutonomyLevel.CORE_DOMAIN


def test_envelope_serialization():
    env = Envelope(from_agent="claude", message_type=MessageType.HEARTBEAT)
    data = env.model_dump()
    assert data["from_agent"] == "claude"
    restored = Envelope.model_validate(data)
    assert restored.message_id == env.message_id


def test_message_types():
    assert MessageType.INBOX.value == "inbox"
    assert MessageType.BROADCAST.value == "broadcast"
    assert MessageType.CONSENSUS_PROPOSAL.value == "consensus_proposal"


def test_inbox_categories():
    assert InboxCategory.QUESTION.value == "question"
    assert InboxCategory.BLOCKER.value == "blocker"


def test_agent_status_values():
    assert AgentStatus.ONLINE.value == "online"
    assert AgentStatus.OFFLINE.value == "offline"
