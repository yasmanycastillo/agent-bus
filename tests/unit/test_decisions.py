from __future__ import annotations

from agent_bus.core.decisions import DecisionLog
from agent_bus.reputation.database import Database


async def test_add_and_get(tmp_db: Database):
    log = DecisionLog(tmp_db)
    d = await log.add("D1", "Use FastAPI", "Need a web framework", "FastAPI", "claude")
    assert d.decision_id == "D1"
    assert d.decided_by == "claude"

    fetched = await log.get("D1")
    assert fetched is not None
    assert fetched.title == "Use FastAPI"


async def test_list_all_ordered(tmp_db: Database):
    log = DecisionLog(tmp_db)
    await log.add("D1", "First", "ctx", "dec", "claude")
    await log.add("D2", "Second", "ctx", "dec", "codex")
    decisions = await log.list_all()
    assert len(decisions) == 2
    assert decisions[0].decision_id == "D1"


async def test_supersede(tmp_db: Database):
    log = DecisionLog(tmp_db)
    await log.add("D1", "Old", "ctx", "dec", "claude")
    await log.add("D2", "New", "ctx", "dec2", "codex", supersedes="D1")
    await log.supersede("D1", "D2")

    # D1 itself is unchanged; D2 has supersedes=D1
    new = await log.get("D2")
    assert new is not None
    assert new.supersedes == "D1"


async def test_with_alternatives(tmp_db: Database):
    log = DecisionLog(tmp_db)
    d = await log.add(
        "D1", "DB", "ctx", "SQLite",
        decided_by="consensus",
        alternatives=["PostgreSQL", "Redis"],
        consequences="Limited to single-node",
    )
    assert d.alternatives == ["PostgreSQL", "Redis"]
    assert d.consequences == "Limited to single-node"
