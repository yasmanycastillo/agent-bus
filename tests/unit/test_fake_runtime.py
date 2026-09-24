import pytest

from agent_bus.runtimes.fake import FakeRuntime
from agent_bus.runtimes.protocol import AttemptConflict, RuntimeStartRequest


@pytest.mark.asyncio
async def test_fake_runtime_covers_the_protocol():
    runtime = FakeRuntime()
    first = RuntimeStartRequest("att-1", "T1", "key-1", "worker")
    started = await runtime.start(first)
    assert (await runtime.start(first)).attempt_id == started.attempt_id
    sent = await runtime.send(started, "continue")
    assert sent.text == "continue"
    assert runtime.sent == [("att-1", "continue")]
    await runtime.mark_unknown("att-1")
    with pytest.raises(AttemptConflict):
        await runtime.start(RuntimeStartRequest("att-2", "T1", "key-2", "worker"))
    await runtime.reconcile("att-1", "cancelled")
    second = await runtime.start(RuntimeStartRequest("att-2", "T1", "key-2", "worker"))
    await runtime.cancel(second)
    assert (await runtime.status(second)).state == "cancelled"
