from __future__ import annotations

from agent_bus.core.kickoff import KickoffManager
from agent_bus.reputation.database import Database


async def test_start_creates_9_steps(tmp_db: Database):
    km = KickoffManager(tmp_db)
    steps = await km.start()
    assert len(steps) == 9
    assert steps[0].name == "project_brief"
    assert steps[8].name == "ready_signal"
    assert all(s.status == "pending" for s in steps)


async def test_complete_step(tmp_db: Database):
    km = KickoffManager(tmp_db)
    await km.start()
    step = await km.complete_step(0, result={"brief": "Build agent bus"}, completed_by="human")
    assert step is not None
    assert step.status == "done"
    assert step.result == {"brief": "Build agent bus"}
    assert step.completed_by == "human"


async def test_is_complete(tmp_db: Database):
    km = KickoffManager(tmp_db)
    await km.start()
    assert not await km.is_complete()

    for i in range(9):
        await km.complete_step(i)
    assert await km.is_complete()


async def test_get_progress(tmp_db: Database):
    km = KickoffManager(tmp_db)
    await km.start()
    await km.complete_step(0, completed_by="human")
    progress = await km.get_progress()
    assert progress[0].status == "done"
    assert progress[1].status == "pending"


async def test_restart_clears_previous(tmp_db: Database):
    km = KickoffManager(tmp_db)
    await km.start()
    await km.complete_step(0)

    # Restart
    steps = await km.start()
    assert steps[0].status == "pending"
