import httpx
import pytest
from httpx import ASGITransport

from agent_bus.core.bus import MessageBus
from agent_bus.core.inbox import InboxManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.reputation.database import Database


AGENTS = [
    {"agent_id": "hermes-01", "provider": "hermes"},
    {"agent_id": "claude-01", "provider": "claude"},
    {"agent_id": "codex-01", "provider": "codex"},
    {"agent_id": "agy-01", "provider": "agy"},
    {"agent_id": "grok-01", "provider": "grok"},
]


@pytest.mark.asyncio
async def test_coordinator_assigns_only_a_confirmed_instruction(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    text = "Corregir el cálculo del impuesto en la factura de venta."
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        refused = await client.post("/instructions", json={
            "agent_id": "coordinator", "instruction": text, "confirmed": False, "agents": AGENTS,
        })
        assert refused.status_code == 422
        assert "confirmed" in refused.json()["error"]
        saved = await client.post("/instructions", json={
            "agent_id": "coordinator", "instruction": text, "confirmed": True, "agents": AGENTS,
        })
        assert saved.status_code == 200, saved.text
        instruction_id = saved.json()["instruction_id"]
        unknown = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "stranger", "role": "implement", "title": "Calcular",
        })
        assert unknown.status_code == 409
        writing = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "claude-01", "role": "implement", "title": "Corregir el cálculo",
        })
        assert writing.status_code == 200, writing.text
        same = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "claude-01", "role": "review", "title": "Revisar el cálculo",
        })
        assert same.status_code == 409
        assert "claude-01 implements part of this instruction" in same.json()["error"]
        assert "reviewer must not implement" in same.json()["error"]
        planning = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "hermes-01", "role": "plan", "title": "Plan del cálculo",
        })
        review = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "codex-01", "role": "review", "title": "Revisar el cálculo",
        })
        assert planning.status_code == 200, planning.text
        assert review.status_code == 200, review.text
        task = (await client.get(f"/tasks/{writing.json()['task_id']}")).json()
        assert task["owner"] == "claude-01"
        assert text in task["description"]
        assert "Corregir el cálculo" in task["description"]
        review_task = (await client.get(f"/tasks/{review.json()['task_id']}")).json()
        assert writing.json()["task_id"] in review_task["independent_from"]
        inbox = (await client.get("/inbox/claude-01/messages")).json()
        assert any(text in ((message.get("body") or {}).get("text") or "") for message in inbox["messages"])

        profile = await client.post("/agents/grok-01/route-profile", json={
            "declared": ["code-review"], "approved": ["code-review"], "can_edit": False,
        })
        assert profile.status_code == 200, profile.text
        free = await client.post("/tasks", json={
            "task_id": "adapt-ar-aging", "title": "aging", "description": "implementation work",
        })
        assert free.status_code == 200, free.text
        stolen = await client.post("/tasks/adapt-ar-aging/claim", json={"agent_id": "grok-01"})
        assert stolen.status_code == 409
        assert "reviewer" in stolen.json()["error"]
        released = await client.post(f"/tasks/{writing.json()['task_id']}/reassign", json={"new_owner": "free"})
        assert released.status_code == 200, released.text
        taken = await client.post(f"/tasks/{writing.json()['task_id']}/claim", json={"agent_id": "grok-01"})
        assert taken.status_code == 409
        assert "claude-01" in taken.json()["error"]
        listed = (await client.get("/agents/claude-01/assignments")).json()
        assert writing.json()["task_id"] in {row["task_id"] for row in listed["assignments"]}
    await db.close()


async def _status(client, task_id):
    return (await client.get(f"/tasks/{task_id}")).json()["status"]


@pytest.mark.asyncio
async def test_review_waits_for_every_implementation_assigned_before_or_after(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        async def submit(text):
            saved = await client.post("/instructions", json={
                "agent_id": "coordinator", "instruction": text, "confirmed": True, "agents": AGENTS,
            })
            return saved.json()["instruction_id"]

        async def assign(instruction_id, agent_id, role):
            response = await client.post(f"/instructions/{instruction_id}/assignments", json={
                "agent_id": agent_id, "role": role, "title": f"{role} {agent_id}",
            })
            assert response.status_code == 200, response.text
            return response.json()["task_id"]

        instruction_id = await submit("Corregir impuestos")
        # Review assigned before any implementation: nothing to review yet.
        review = await assign(instruction_id, "codex-01", "review")
        assert await _status(client, review) == "blocked"
        plan = await assign(instruction_id, "hermes-01", "plan")
        assert (await client.post(f"/tasks/{plan}/done", json={"agent_id": "hermes-01"})).status_code == 200
        assert await _status(client, review) == "blocked"
        # The worker only executes its own in_progress or pending tasks.
        for status in ("in_progress", "pending"):
            owned = (await client.get("/tasks", params={"owner": "codex-01", "status": status})).json()
            assert review not in {task["task_id"] for task in owned}

        first = await assign(instruction_id, "claude-01", "implement")
        second = await assign(instruction_id, "grok-01", "implement")
        task = (await client.get(f"/tasks/{review}")).json()
        assert task["status"] == "blocked"
        assert set(task["depends_on"]) == {first, second}
        assert set(task["independent_from"]) == {first, second}

        assert (await client.post(f"/tasks/{first}/review", json={"agent_id": "claude-01"})).status_code == 200
        assert await _status(client, review) == "blocked"
        assert (await client.post(f"/tasks/{second}/done", json={"agent_id": "grok-01"})).status_code == 200
        assert await _status(client, review) == "in_progress"

        # A new implementation after the review started: it waits again.
        third = await assign(instruction_id, "agy-01", "implement")
        assert await _status(client, review) == "blocked"
        assert (await client.post(f"/tasks/{third}/review", json={"agent_id": "agy-01"})).status_code == 200
        assert await _status(client, review) == "in_progress"
        # Reviewer and implementer stay separate.
        refused = await client.post(f"/instructions/{instruction_id}/assignments", json={
            "agent_id": "claude-01", "role": "review", "title": "self review",
        })
        assert refused.status_code == 409

        # Review assigned after its implementation is already in review starts at once.
        other = await submit("Otro encargo")
        written = await assign(other, "claude-01", "implement")
        assert (await client.post(f"/tasks/{written}/review", json={"agent_id": "claude-01"})).status_code == 200
        late = await assign(other, "codex-01", "review")
        assert await _status(client, late) == "in_progress"
    await db.close()


@pytest.mark.asyncio
async def test_cross_review_covers_only_the_named_implementations(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        saved = await client.post("/instructions", json={
            "agent_id": "coordinator", "instruction": "Backend y frontend", "confirmed": True, "agents": AGENTS,
        })
        instruction_id = saved.json()["instruction_id"]

        async def assign(agent_id, role, **extra):
            return await client.post(f"/instructions/{instruction_id}/assignments", json={
                "agent_id": agent_id, "role": role, "title": f"{role} {agent_id}", **extra,
            })

        backend = (await assign("codex-01", "implement")).json()["task_id"]
        frontend = (await assign("grok-01", "implement")).json()["task_id"]
        # Without a scope the review still covers every implementation.
        assert (await assign("codex-01", "review")).status_code == 409
        foreign = await assign("codex-01", "review", reviews=["ins-x-implement-y"])
        assert foreign.status_code == 422
        assert "ins-x-implement-y" in foreign.json()["error"]
        own = await assign("codex-01", "review", reviews=[backend])
        assert own.status_code == 409
        assert backend in own.json()["error"]
        assert (await assign("codex-01", "review", reviews=[])).status_code == 422

        crossed = await assign("codex-01", "review", reviews=[frontend])
        assert crossed.status_code == 200, crossed.text
        review = crossed.json()["task_id"]
        task = (await client.get(f"/tasks/{review}")).json()
        assert task["status"] == "blocked"
        assert task["independent_from"] == [frontend]
        assert task["depends_on"] == [frontend]
        assert (await assign("grok-01", "review", reviews=[backend])).status_code == 200

        # A scoped reviewer may implement elsewhere; a later implementation joins
        # only reviews without an explicit scope.
        scoped = (await assign("agy-01", "review", reviews=[frontend])).json()["task_id"]
        later = await assign("agy-01", "implement")
        assert later.status_code == 200, later.text
        assert later.json()["task_id"] not in (await client.get(f"/tasks/{review}")).json()["independent_from"]
        assert (await client.get(f"/tasks/{scoped}")).json()["independent_from"] == [frontend]

        # The review waits only for grok's handoff and judges only grok's candidate.
        assert (await client.post(f"/tasks/{frontend}/review", json={"agent_id": "grok-01"})).status_code == 200
        assert await _status(client, review) == "in_progress"
        await db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at,
                outcome, candidate_sha, log_refs)
               VALUES ('att-front', ?, 'dispatch:front', 'completed', 'external:1', '.', ?, 'completed', 'abc123', '[]')""",
            (frontend, "2026-01-01T00:00:00+00:00"),
        )
        await db.conn.commit()
        judged = await client.post(f"/tasks/{review}/verdict", json={
            "agent_id": "codex-01", "verdict": "approve", "reason": "ok",
        })
        assert judged.status_code == 200, judged.text
        assert judged.json()["evidence"]["implementation_task_id"] == frontend
        outside = await client.post(f"/tasks/{review}/verdict", json={
            "agent_id": "codex-01", "verdict": "approve", "implementation_task_id": backend,
        })
        assert outside.status_code == 409
    await db.close()


@pytest.mark.asyncio
async def test_missing_instruction_explains_the_404(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        missing = await client.post("/instructions/ins-nope/assignments", json={
            "agent_id": "codex-01", "role": "implement", "title": "x",
        })
        assert missing.status_code == 404
        detail = missing.json()["error"]
        assert "ins-nope" in detail and "alpha" in detail
    await db.close()


@pytest.mark.asyncio
async def test_approved_review_closes_and_reopens_when_its_approval_goes_stale(tmp_path):
    from agent_bus.types import AgentInfo

    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    stamp = iter(f"2026-01-01T00:00:{second:02d}+00:00" for second in range(60))

    async def deliver(task_id, sha):
        await db.conn.execute(
            """INSERT INTO runtime_attempts
               (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at,
                outcome, candidate_sha, log_refs)
               VALUES (?, ?, ?, 'completed', 'external:1', '.', ?, 'completed', ?, '[]')""",
            (f"att-{sha}", task_id, f"dispatch:{sha}", next(stamp), sha),
        )
        await db.conn.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        saved = await client.post("/instructions", json={
            "agent_id": "coordinator", "instruction": "Backend y frontend", "confirmed": True, "agents": AGENTS,
        })
        instruction_id = saved.json()["instruction_id"]

        async def assign(agent_id, role, **extra):
            response = await client.post(f"/instructions/{instruction_id}/assignments", json={
                "agent_id": agent_id, "role": role, "title": f"{role} {agent_id}", **extra,
            })
            assert response.status_code == 200, response.text
            return response.json()["task_id"]

        async def verdict(review, agent_id, value, implementation):
            response = await client.post(f"/tasks/{review}/verdict", json={
                "agent_id": agent_id, "verdict": value, "implementation_task_id": implementation,
            })
            assert response.status_code == 200, response.text

        backend = await assign("codex-01", "implement")
        frontend = await assign("grok-01", "implement")
        grok_review = await assign("grok-01", "review", reviews=[backend])
        codex_review = await assign("codex-01", "review", reviews=[frontend])
        both = await assign("agy-01", "review")
        for agent_id in ("grok-01", "codex-01"):
            await client.post(f"/agents/{agent_id}/route-profile", json={
                "declared": ["implementation", "code-review"], "approved": ["implementation", "code-review"],
                "can_edit": True, "max_in_progress": 1,
            })
            await bus.registry.register(AgentInfo(agent_id=agent_id, display_name=agent_id))

        await deliver(backend, "b1")
        assert (await client.post(f"/tasks/{backend}/review", json={"agent_id": "codex-01"})).status_code == 200
        await deliver(frontend, "f1")
        assert (await client.post(f"/tasks/{frontend}/review", json={"agent_id": "grok-01"})).status_code == 200
        assert await _status(client, grok_review) == "in_progress"

        # Approving every covered implementation closes the review in the verdict call.
        await verdict(grok_review, "grok-01", "approve", backend)
        assert await _status(client, grok_review) == "done"
        await verdict(both, "agy-01", "approve", backend)
        assert await _status(client, both) == "in_progress"

        # Changes on grok's own work reopen only it; the closed review no longer holds its slot.
        await verdict(codex_review, "codex-01", "changes_requested", frontend)
        assert await _status(client, codex_review) == "blocked"
        assert await _status(client, grok_review) == "done"
        claimed = await client.post(f"/tasks/{frontend}/claim", json={"agent_id": "grok-01"})
        assert claimed.status_code == 200, claimed.text

        # A new candidate for an approved implementation reopens the closed review.
        await verdict(both, "agy-01", "changes_requested", backend)
        assert (await client.post(f"/tasks/{backend}/claim", json={"agent_id": "codex-01"})).status_code == 200
        await deliver(backend, "b2")
        assert await _status(client, grok_review) == "done"
        assert (await client.post(f"/tasks/{backend}/review", json={"agent_id": "codex-01"})).status_code == 200
        assert await _status(client, grok_review) == "in_progress"
        await verdict(grok_review, "grok-01", "approve", backend)
        assert await _status(client, grok_review) == "done"
    await db.close()


async def _deliver(db, task_id, sha, stamp="2026-01-01T00:00:00+00:00"):
    await db.conn.execute(
        """INSERT INTO runtime_attempts
           (attempt_id, task_id, idempotency_key, state, external_ref, workspace_ref, updated_at,
            outcome, candidate_sha, log_refs)
           VALUES (?, ?, ?, 'completed', 'external:1', '.', ?, 'completed', ?, '[]')""",
        (f"att-{sha}", task_id, f"dispatch:{sha}", stamp, sha),
    )
    await db.conn.commit()


@pytest.mark.asyncio
async def test_hub_start_closes_a_review_left_open_after_approving_everything(tmp_path):
    """Reviews approved before the verdict closed them stay in_progress; the next start closes them."""
    path = str(tmp_path / "bus.db")
    db = Database(path)
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        saved = await client.post("/instructions", json={
            "agent_id": "coordinator", "instruction": "Backend", "confirmed": True, "agents": AGENTS,
        })
        instruction_id = saved.json()["instruction_id"]

        async def assign(agent_id, role):
            response = await client.post(f"/instructions/{instruction_id}/assignments", json={
                "agent_id": agent_id, "role": role, "title": f"{role} {agent_id}",
            })
            return response.json()["task_id"]

        backend = await assign("codex-01", "implement")
        review = await assign("grok-01", "review")
        unjudged = await assign("agy-01", "review")
        await _deliver(db, backend, "b1")
        assert (await client.post(f"/tasks/{backend}/review", json={"agent_id": "codex-01"})).status_code == 200
        approved = await client.post(f"/tasks/{review}/verdict", json={"agent_id": "grok-01", "verdict": "approve"})
        assert approved.status_code == 200, approved.text
        # State written by a hub that did not close approved reviews yet.
        await db.conn.execute("UPDATE tasks SET status = 'in_progress' WHERE task_id = ?", (review,))
        await db.conn.commit()
    await db.close()

    restarted = Database(path)
    await restarted.initialize()
    rows = await restarted.conn.execute_fetchall(
        "SELECT task_id, status FROM tasks WHERE task_id IN (?, ?)", (review, unjudged),
    )
    assert {row["task_id"]: row["status"] for row in rows} == {review: "done", unjudged: "in_progress"}
    await restarted.close()


@pytest.mark.asyncio
async def test_coordinator_assigning_itself_gets_no_message_from_itself(tmp_path):
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        saved = await client.post("/instructions", json={
            "agent_id": "claude-01", "instruction": "Planear y construir", "confirmed": True, "agents": AGENTS,
        })
        instruction_id = saved.json()["instruction_id"]
        for agent_id, role in (("claude-01", "plan"), ("codex-01", "implement")):
            response = await client.post(f"/instructions/{instruction_id}/assignments", json={
                "agent_id": agent_id, "role": role, "title": f"{role} {agent_id}",
            })
            assert response.status_code == 200, response.text
        # The task itself is the coordinator's record; a message to itself would sit unacknowledged.
        assert (await client.get("/inbox/claude-01/messages")).json()["messages"] == []
        assert (await client.get(f"/tasks/{instruction_id}-plan-claude-01")).json()["owner"] == "claude-01"
        sent = (await client.get("/inbox/codex-01/messages")).json()["messages"]
        assert [message["from_agent"] for message in sent] == ["claude-01"]
    await db.close()


@pytest.mark.asyncio
async def test_coordinator_closes_an_implementation_only_with_a_current_approve(tmp_path):
    """Integrated outside BranchIntegrator, an approved implementation would stay in_review forever."""
    db = Database(str(tmp_path / "bus.db"))
    await db.initialize()
    bus = MessageBus(db, AgentRegistry(), InboxManager(db), project_id="alpha")
    async with httpx.AsyncClient(transport=ASGITransport(app=bus.app), base_url="http://test") as client:
        saved = await client.post("/instructions", json={
            "agent_id": "coordinator", "instruction": "Backend", "confirmed": True, "agents": AGENTS,
        })
        instruction_id = saved.json()["instruction_id"]

        async def assign(agent_id, role):
            response = await client.post(f"/instructions/{instruction_id}/assignments", json={
                "agent_id": agent_id, "role": role, "title": f"{role} {agent_id}",
            })
            return response.json()["task_id"]

        async def close(agent_id, **extra):
            return await client.post(f"/tasks/{backend}/done", json={"agent_id": agent_id, **extra})

        backend = await assign("codex-01", "implement")
        review = await assign("grok-01", "review")
        await _deliver(db, backend, "b1")
        assert (await client.post(f"/tasks/{backend}/review", json={"agent_id": "codex-01"})).status_code == 200

        unjudged = await close("coordinator")
        assert unjudged.status_code == 409
        assert "approve" in unjudged.json()["error"]
        approved = await client.post(f"/tasks/{review}/verdict", json={"agent_id": "grok-01", "verdict": "approve"})
        assert approved.status_code == 200, approved.text
        assert (await close("agy-01")).status_code == 409
        assert await _status(client, backend) == "in_review"

        # An approve of an older candidate is not current.
        await _deliver(db, backend, "b2", stamp="2026-01-01T00:00:05+00:00")
        assert (await close("coordinator")).status_code == 409
        again = await client.post(f"/tasks/{review}/verdict", json={
            "agent_id": "grok-01", "verdict": "approve", "sha": "b2",
        })
        assert again.status_code == 200, again.text

        closed = await close("coordinator", merged_sha="abc1234")
        assert closed.status_code == 200, closed.text
        assert closed.json()["status"] == "done"
        evidence = (await client.get(f"/tasks/{backend}/evidence")).json()["evidence"][0]
        assert evidence["actor_agent_id"] == "coordinator"
        assert evidence["evidence"] == {"merged_sha": "abc1234", "approved_sha": "b2"}
    await db.close()
