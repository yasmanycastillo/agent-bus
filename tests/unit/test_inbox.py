from __future__ import annotations

import pytest

from agent_bus.core.inbox import InboxManager
from agent_bus.reputation.database import Database
from agent_bus.types import Envelope, MessageType


async def test_deliver_and_get(tmp_db: Database):
    inbox = InboxManager(tmp_db)
    env = Envelope(
        from_agent="claude",
        to_agent="codex",
        message_type=MessageType.INBOX,
        body={"text": "Review this PR"},
        reply_needed=True,
    )
    msg_id = await inbox.deliver(env)
    assert msg_id == env.message_id

    messages = await inbox.get_inbox("codex")
    assert len(messages) == 1
    assert messages[0].from_agent == "claude"
    assert messages[0].body["text"] == "Review this PR"


async def test_archive_message(tmp_db: Database):
    inbox = InboxManager(tmp_db)
    env = Envelope(
        from_agent="claude",
        to_agent="codex",
        message_type=MessageType.INBOX,
    )
    await inbox.deliver(env)
    await inbox.archive("codex", env.message_id)

    messages = await inbox.get_inbox("codex")
    assert len(messages) == 0

    archived = await inbox.get_archived("codex")
    assert len(archived) == 1


async def test_pending_count(tmp_db: Database):
    inbox = InboxManager(tmp_db)
    for i in range(3):
        await inbox.deliver(
            Envelope(
                from_agent="claude",
                to_agent="codex",
                message_type=MessageType.INBOX,
                body={"idx": i},
            )
        )
    assert await inbox.pending_count("codex") == 3
    assert await inbox.pending_count("claude") == 0


async def test_get_specific_message(tmp_db: Database):
    inbox = InboxManager(tmp_db)
    env = Envelope(
        from_agent="claude",
        to_agent="codex",
        message_type=MessageType.BLOCKER,
        body={"error": "build failed"},
    )
    await inbox.deliver(env)
    result = await inbox.get_message("codex", env.message_id)
    assert result is not None
    assert result.body["error"] == "build failed"


async def test_get_message_not_found(tmp_db: Database):
    inbox = InboxManager(tmp_db)
    result = await inbox.get_message("codex", "nonexistent")
    assert result is None


async def test_deliver_without_to_agent_raises(tmp_db: Database):
    inbox = InboxManager(tmp_db)
    env = Envelope(
        from_agent="claude",
        message_type=MessageType.BROADCAST,
    )
    with pytest.raises(ValueError, match="to_agent"):
        await inbox.deliver(env)
