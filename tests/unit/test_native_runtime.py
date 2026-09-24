import pytest

from agent_bus.reputation.database import Database
from agent_bus.runtimes.native import NativeRuntime
from agent_bus.runtimes.protocol import AttemptConflict, RuntimeStartRequest


@pytest.mark.asyncio
async def test_native_attempt_survives_a_new_runtime_and_blocks_unknown(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    runtime = NativeRuntime(db)
    request = RuntimeStartRequest("att-1", "T1", "native:T1", "worker")
    started = await runtime.start(request)
    assert started.state == "started"
    assert started.external_ref == "native:worker"

    again = NativeRuntime(db)
    same = await again.start(request)
    assert same.attempt_id == "att-1"
    assert (await again.status(same)).state == "started"
    assert await again.claim_execution("att-1", "epoch-a")
    assert not await again.claim_execution("att-1", "epoch-b")
    assert (await again.status(same)).state == "unknown"

    await again.mark_unknown("att-1")
    with pytest.raises(AttemptConflict):
        await again.start(RuntimeStartRequest("att-2", "T1", "native:T1:2", "worker"))
    settled = await again.reconcile("att-1", "cancelled")
    assert settled.state == "cancelled"
    restarted = await again.start(RuntimeStartRequest("att-2", "T1", "native:T1:2", "worker"))
    assert restarted.state == "started"
    await db.close()
