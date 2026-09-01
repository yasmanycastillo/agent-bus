from __future__ import annotations

from agent_bus.core.skills import SkillRegistry
from agent_bus.reputation.database import Database


async def test_assign_and_get(tmp_db: Database):
    sr = SkillRegistry(tmp_db)
    skill = await sr.assign_role(
        "claude",
        "developer",
        responsibilities=["write code", "fix bugs"],
        audit_questions=["Do tests pass?", "Is the code reviewed?"],
    )
    assert skill.agent_id == "claude"
    assert skill.role == "developer"
    assert len(skill.responsibilities) == 2

    roles = await sr.get_roles("claude")
    assert len(roles) == 1
    assert roles[0].role == "developer"


async def test_multiple_roles(tmp_db: Database):
    sr = SkillRegistry(tmp_db)
    await sr.assign_role("claude", "developer", ["code"])
    await sr.assign_role("claude", "reviewer", ["review PRs"])
    roles = await sr.get_roles("claude")
    assert len(roles) == 2


async def test_audit_questions(tmp_db: Database):
    sr = SkillRegistry(tmp_db)
    await sr.assign_role("claude", "developer", audit_questions=["Tests?", "Lint?"])
    await sr.assign_role("claude", "reviewer", audit_questions=["Security?"])
    questions = await sr.get_audit_questions("claude")
    assert len(questions) == 3


async def test_no_roles(tmp_db: Database):
    sr = SkillRegistry(tmp_db)
    assert await sr.get_roles("unknown") == []
