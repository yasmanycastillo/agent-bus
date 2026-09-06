"""T08 acceptance through provisioned, authenticated clients and a real temporary hub."""
from __future__ import annotations

import asyncio

from agent_bus.mcp.server import McpServer
from agent_bus.security import async_bus_client
from agent_bus.worker.daemon import WorkerDaemon
from agent_bus.worker.runner import RunnerResult


async def post(client, path, payload):
    response = await client.post(path, json=payload)
    response.raise_for_status()
    return response.json()


async def page(client, agent, **params):
    response = await client.get(f"/inbox/{agent}/messages", params=params)
    response.raise_for_status()
    return response.json()


async def test_send_retry_and_changed_content_are_durable(secure_bus):
    payload = {"to_agent": "bob", "body": {"text": "Review"}, "idempotency_key": "review-1"}
    async with async_bus_client("alice", base_url=secure_bus.url) as alice:
        # Client loses the response; it only knows its retained key.
        await alice.post("/messages", json=payload)
    async with async_bus_client("alice", base_url=secure_bus.url) as retry:
        sent = await post(retry, "/messages", payload)
        assert sent["replayed"] is True
        conflict = await retry.post("/messages", json={**payload, "body": {"text": "Different"}})
        assert conflict.status_code == 409
    async with async_bus_client("bob", base_url=secure_bus.url) as bob:
        messages = (await page(bob, "bob"))["messages"]
        assert [m["message_id"] for m in messages] == [sent["message_id"]]
        assert messages[0]["conversation_id"] == sent["conversation_id"] == sent["message_id"]
        assert (await page(bob, "bob"))["messages"] == messages  # reading has no effects


async def test_broadcast_retry_keeps_original_recipients_and_ack(secure_bus):
    async with (async_bus_client("alice", base_url=secure_bus.url) as alice,
                async_bus_client("bob", base_url=secure_bus.url) as bob,
                async_bus_client("human", base_url=secure_bus.url) as human):
        for agent, client in (("alice", alice), ("bob", bob)):
            await post(client, "/register", {"agent_id": agent, "display_name": agent})
        payload = {"message_type": "broadcast", "body": {"text": "Ready"}, "idempotency_key": "broadcast-1"}
        sent = await post(alice, "/messages", payload)
        assert sent["message_ids"] == [sent["message_id"]]
        ack = {"message_ids": [sent["message_id"]]}
        await post(bob, "/inbox/bob/ack", ack)
        state = (await bob.get(f"/inbox/bob/{sent['message_id']}")).json()
        await post(human, "/register", {"agent_id": "human", "display_name": "Operator"})
        retry = await post(alice, "/messages", payload)
        assert retry["message_ids"] == sent["message_ids"]
        await post(bob, "/inbox/bob/ack", ack)
        assert (await bob.get(f"/inbox/bob/{sent['message_id']}")).json()["acknowledged_at"] == state["acknowledged_at"]
        assert (await page(bob, "bob"))["messages"] == []
        assert (await page(human, "human"))["messages"] == []


async def test_reply_chain_and_atomic_ack_respect_identity(secure_bus):
    async with (async_bus_client("alice", base_url=secure_bus.url) as alice,
                async_bus_client("bob", base_url=secure_bus.url) as bob):
        original = await post(alice, "/messages", {
            "to_agent": "bob", "body": {"text": "Review"}, "reply_needed": True,
            "related_task": "T08", "idempotency_key": "question-1",
        })
        source = original["message_id"]
        reply_path = f"/inbox/bob/{source}/reply"
        payload = {"body": {"text": "Reviewed"}, "idempotency_key": "reply-1", "acknowledge": True}
        assert (await alice.post(reply_path, json=payload)).status_code == 403
        assert (await bob.post(reply_path, json={**payload, "from_agent": "human"})).status_code == 403
        assert (await bob.post(reply_path, json={**payload, "to_agent": "human"})).status_code == 422
        replied = await post(bob, reply_path, payload)
        retried = await post(bob, reply_path, payload)
        assert retried["message_id"] == replied["message_id"]
        assert retried["replayed"]
        assert (await page(bob, "bob"))["messages"] == []
        response = (await page(alice, "alice"))["messages"][0]
        assert (response["from_agent"], response["to_agent"]) == ("bob", "alice")
        assert response["correlation_id"] == source
        assert response["conversation_id"] == original["conversation_id"]
        assert response["related_task"] == "T08"
        followup = await post(alice, f"/inbox/alice/{response['message_id']}/reply", {
            "body": {"text": "Thanks"}, "idempotency_key": "reply-2",
        })
        received = (await page(bob, "bob"))["messages"][0]
        assert received["correlation_id"] == response["message_id"]
        assert received["conversation_id"] == followup["conversation_id"] == source
        # Reply alone did not acknowledge Alice's delivery.
        assert len((await page(alice, "alice"))["messages"]) == 1


async def test_cursor_snapshot_and_batch_ack_have_no_offset_gaps(secure_bus):
    async with (async_bus_client("alice", base_url=secure_bus.url) as alice,
                async_bus_client("bob", base_url=secure_bus.url) as bob,
                async_bus_client("human", base_url=secure_bus.url) as human):
        ids = []
        for index in range(6):
            sent = await post(alice, "/messages", {"to_agent": "bob", "body": {"index": index},
                                                   "idempotency_key": f"page-{index}"})
            ids.append(sent["message_id"])
        first = await page(bob, "bob", limit=2)
        assert [m["message_id"] for m in first["messages"]] == ids[:2]
        cursor = first["next_cursor"]
        assert cursor
        assert (await human.get("/inbox/human/messages", params={"cursor": cursor})).status_code == 422
        assert (await bob.get("/inbox/bob/messages", params={"cursor": cursor + "tampered"})).status_code == 422
        for invalid in (0, 101):
            assert (await bob.get("/inbox/bob/messages", params={"limit": invalid})).status_code == 422
        bad = await bob.post("/inbox/bob/ack", json={"message_ids": [ids[0], "not-in-this-inbox"]})
        assert bad.status_code == 404
        assert len((await page(bob, "bob"))["messages"]) == 6
        await post(bob, "/inbox/bob/ack", {"message_ids": ids[:3]})
        await post(alice, "/messages", {"to_agent": "bob", "body": {"index": "new"}, "idempotency_key": "later"})
        remaining = []
        while cursor:
            result = await page(bob, "bob", cursor=cursor, limit=2)
            remaining.extend(m["message_id"] for m in result["messages"])
            cursor = result["next_cursor"]
        assert remaining == ids[3:]  # new arrival excluded, acked row skipped, no offset gap
        assert len((await page(bob, "bob"))["messages"]) == 4


async def test_worker_failure_survives_restart_then_reply_ack_stops_mcp_wait(secure_bus):
    class Runner:
        agent_id = "bob"
        calls = 0
        succeed = False
        def assemble_prompt(self, **kwargs):
            return "Review and answer"
        async def execute_turn(self, *args, **kwargs):
            self.calls += 1
            return RunnerResult(success=self.succeed, output="Reviewed" if self.succeed else "", error=None if self.succeed else "runner failed")
    runner = Runner()
    async with (async_bus_client("alice", base_url=secure_bus.url) as alice,
                async_bus_client("bob", base_url=secure_bus.url) as bob):
        sent = await post(alice, "/messages", {"to_agent": "bob", "body": {"text": "Review"},
                                                "reply_needed": True, "idempotency_key": "worker-question"})
        daemon = WorkerDaemon("bob", runner, bus_url=secure_bus.url)
        daemon._client = bob
        await daemon._check_and_process_pending()
        assert runner.calls == 1
        state = (await bob.get(f"/inbox/bob/{sent['message_id']}")).json()
        assert not state["acknowledged"] and state["attempts"] >= 1
        assert state["last_error"]
        # New process state; the durable delivery and failure are still recoverable.
        runner.succeed = True
        restarted = WorkerDaemon("bob", runner, bus_url=secure_bus.url)
        restarted._client = bob
        await restarted._check_and_process_pending()
        assert runner.calls == 2
        await restarted._check_and_process_pending()
        assert runner.calls == 2
        answers = (await page(alice, "alice"))["messages"]
        assert len(answers) == 1 and answers[0]["correlation_id"] == sent["message_id"]
        server = McpServer(bus_url=secure_bus.url, agent_id="bob")
        result = await asyncio.wait_for(server.execute_tool("wait_for_updates", {"timeout": 1}), timeout=3)
        assert result["status"] == "timeout"


async def test_message_removed_between_reads_is_not_reported_pending(tmp_db, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from agent_bus.core.bus import MessageBus
    from agent_bus.core.inbox import InboxManager
    from agent_bus.core.registry import AgentRegistry
    from agent_bus.types import Envelope
    monkeypatch.setenv("AGENT_BUS_ALLOW_UNSIGNED", "0")
    inbox = InboxManager(tmp_db)
    bus = MessageBus(tmp_db, AgentRegistry(), inbox)
    session = await bus.sessions.create("bob")
    message = Envelope(from_agent="alice", to_agent="bob", message_type="inbox")
    await inbox.deliver(message)
    await inbox.acknowledgments("bob", [message.message_id])
    original = inbox.delivery_state
    async def cleanup_before_state(agent, identifier):
        await tmp_db.conn.execute("DELETE FROM inbox WHERE to_agent = ? AND message_id = ?", (agent, identifier))
        await tmp_db.conn.commit()
        return await original(agent, identifier)
    monkeypatch.setattr(inbox, "delivery_state", cleanup_before_state)
    async with AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test",
                           headers={"Authorization": f"Bearer {session['token']}"}) as client:
        assert (await client.get(f"/inbox/bob/{message.message_id}")).status_code == 404
