"""Under require_distinct_provider the same model under another agent_id cannot approve itself."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_bus.core.evidence import EvidenceLog
from agent_bus.core.reviews import ReviewLog, VerdictError
from agent_bus.core.tasks import TaskManager
from agent_bus.security import SessionStore

SHA = "a" * 40


@pytest.fixture
async def review(tmp_db):
    tasks = TaskManager(tmp_db)
    await tasks.create("IMPL", "Implementation", owner="alice")
    await tasks.create("REV", "Review", owner="bob", independent_from=["IMPL"])
    await tmp_db.conn.execute(
        "INSERT INTO runtime_attempts (attempt_id, task_id, idempotency_key, state, updated_at, candidate_sha) "
        "VALUES ('A1', 'IMPL', 'k1', 'completed', ?, ?)", (datetime.now(timezone.utc).isoformat(), SHA),
    )
    await tmp_db.conn.commit()
    return tmp_db


async def sessions(db, **providers):
    store = SessionStore(db, "default")
    for agent, provider in providers.items():
        await store.create(agent, provider=provider)


async def require_distinct(db, enabled=True):
    await EvidenceLog(db, "default").set_policy(
        "IMPL", strict=True, require_review=True, require_sha_match=True, require_distinct_provider=enabled,
    )


async def verdict(db):
    return await ReviewLog(db).record_task_verdict("REV", "bob", SHA, "approve")


async def test_same_provider_cannot_review(review):
    await sessions(review, alice="claude", bob="claude")
    await require_distinct(review)
    with pytest.raises(VerdictError, match="both use claude"):
        await verdict(review)


async def test_unknown_provider_fails_closed(review):
    await sessions(review, alice="claude", bob=None)
    await require_distinct(review)
    with pytest.raises(VerdictError, match="provider is unknown"):
        await verdict(review)


async def test_other_provider_can_review(review):
    await sessions(review, alice="claude", bob="codex")
    await require_distinct(review)
    assert (await verdict(review)).verdict.value == "approve"


async def test_rule_is_opt_in(review):
    await sessions(review, alice="claude", bob="claude")
    await require_distinct(review, enabled=False)
    assert (await verdict(review)).verdict.value == "approve"


async def test_only_an_admin_can_change_the_evidence_policy(review):
    from httpx import ASGITransport, AsyncClient

    from agent_bus.core.bus import MessageBus
    from agent_bus.core.inbox import InboxManager
    from agent_bus.core.registry import AgentRegistry

    bus = MessageBus(db=review, registry=AgentRegistry(), inbox=InboxManager(review))
    agent = await bus.sessions.create("alice")
    admin = await bus.sessions.create("root", role="admin")
    body = {"strict": False, "require_distinct_provider": False}

    async def post(token):
        async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://bus",
                               headers={"Authorization": f"Bearer {token}"}) as client:
            return await client.post("/tasks/IMPL/evidence-policy", json=body)

    assert (await post(agent["token"])).status_code == 403
    assert (await post(admin["token"])).status_code == 200
