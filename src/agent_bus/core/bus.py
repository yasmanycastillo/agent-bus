from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone

import os
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent_bus.core.decisions import DecisionLog
from agent_bus.core.inbox import InboxManager
from agent_bus.core.kickoff import KickoffManager
from agent_bus.core.locks import LockError, LockManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.core.skills import SkillRegistry
from agent_bus.core.tasks import TaskManager
from agent_bus.reputation.database import Database
from agent_bus.types import AgentInfo, AutonomyLevel, Envelope, MessageType
from agent_bus.worker.auth import WorkerAuth


class SendMessageRequest(BaseModel):
    from_agent: str
    to_agent: str | None = None
    message_type: MessageType = MessageType.INBOX
    body: dict | None = None
    reply_needed: bool = False
    related_task: str | None = None
    signature: str | None = None
    correlation_id: str | None = None
    metadata: dict | None = None


class RegisterRequest(BaseModel):
    agent_id: str
    display_name: str
    capabilities: list[str] | None = None
    autonomy_level: int = 0
    endpoint: str | None = None
    public_key: str | None = None


class TaskRequest(BaseModel):
    task_id: str
    title: str
    description: str | None = None
    owner: str = "free"


class ClaimRequest(BaseModel):
    agent_id: str


class ReassignRequest(BaseModel):
    new_owner: str


class LockRequest(BaseModel):
    file_path: str
    agent_id: str
    reason: str | None = None


class ReleaseRequest(BaseModel):
    file_path: str
    agent_id: str


class DecisionRequest(BaseModel):
    decision_id: str
    title: str
    context: str
    decision: str
    decided_by: str
    alternatives: list[str] | None = None
    consequences: str | None = None
    supersedes: str | None = None


class SkillRequest(BaseModel):
    role: str
    responsibilities: list[str] | None = None
    audit_questions: list[str] | None = None


class KickoffStepRequest(BaseModel):
    result: dict | None = None
    completed_by: str | None = None


class MessageBus:
    def __init__(self, db: Database, registry: AgentRegistry, inbox: InboxManager) -> None:
        self.db = db
        self.registry = registry
        self.inbox = inbox
        self.tasks = TaskManager(db)
        self.decisions = DecisionLog(db)
        self.locks = LockManager(db)
        self.skills = SkillRegistry(db)
        self.kickoff = KickoffManager(db)
        self.app = FastAPI(title="agent-bus", version="0.1.0")
        self._sse_subscribers: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)
        self._global_sse_subscribers: set[asyncio.Queue[str]] = set()
        self._ws_connections: dict[str, WebSocket] = {}
        self._setup_routes()

    def _setup_routes(self) -> None:
        auth_validator = WorkerAuth()

        @self.app.middleware("http")
        async def verify_signature_middleware(request: Request, call_next):
            allow_unsigned = os.environ.get("AGENT_BUS_ALLOW_UNSIGNED", "1") == "1"
            if request.method in ("POST", "PUT", "DELETE"):
                path = request.url.path
                if not (path == "/register" or "/heartbeat" in path or path.startswith("/agents/")):
                    agent_id = request.headers.get("x-agent-id")
                    signature = request.headers.get("x-agent-signature")
                    public_key = request.headers.get("x-agent-public-key")

                    if agent_id and signature:
                        body_bytes = await request.body()
                        body_json = None
                        if body_bytes:
                            try:
                                body_json = json.loads(body_bytes.decode())
                            except Exception:
                                pass

                        async def receive():
                            return {"type": "http.request", "body": body_bytes}

                        request._receive = receive

                        valid = auth_validator.verify_operation(
                            agent_id=agent_id,
                            method=request.method,
                            path=path,
                            body=body_json,
                            signature_hex=signature,
                            public_key_hex=public_key,
                        )
                        if not valid:
                            return JSONResponse({"error": "Invalid Ed25519 signature"}, status_code=401)
                    elif not allow_unsigned:
                        return JSONResponse({"error": "Authentication required: missing signature"}, status_code=401)

            return await call_next(request)

        # --- Agent endpoints ---

        @self.app.post("/register")
        async def register(req: RegisterRequest):
            info = AgentInfo(
                agent_id=req.agent_id,
                display_name=req.display_name,
                capabilities=req.capabilities or [],
                autonomy_level=AutonomyLevel(req.autonomy_level),
                endpoint=req.endpoint,
                public_key=req.public_key,
            )
            existing = await self.registry.get(req.agent_id)
            if existing:
                return JSONResponse({"error": "Agent already registered"}, status_code=409)
            await self.registry.register(info)
            return JSONResponse(info.model_dump(mode="json"), status_code=201)

        @self.app.get("/agents")
        async def list_agents():
            agents = await self.registry.list_all()
            return [a.model_dump(mode="json") for a in agents]

        @self.app.get("/status")
        async def status():
            agents = await self.registry.list_all()
            return {
                "bus_version": "0.1.0",
                "agents_online": sum(1 for a in agents if a.status.value == "online"),
                "agents_total": len(agents),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # --- Message endpoints ---

        @self.app.post("/messages")
        async def post_message(req: SendMessageRequest):
            envelope = Envelope(
                from_agent=req.from_agent,
                to_agent=req.to_agent,
                message_type=req.message_type,
                body=req.body,
                reply_needed=req.reply_needed,
                related_task=req.related_task,
                signature=req.signature,
                correlation_id=req.correlation_id,
                metadata=req.metadata or {},
            )
            if req.to_agent and req.message_type != MessageType.BROADCAST:
                msg_id = await self.inbox.deliver(envelope)
                await self._push_to_agent(req.to_agent, envelope)
                return {"message_id": msg_id, "status": "delivered"}
            else:
                agents = await self.registry.list_all()
                results = []
                for agent in agents:
                    if agent.agent_id == req.from_agent:
                        continue
                    env_copy = envelope.model_copy(update={"to_agent": agent.agent_id})
                    msg_id = await self.inbox.deliver(env_copy)
                    await self._push_to_agent(agent.agent_id, env_copy)
                    results.append(msg_id)
                return {"message_ids": results, "status": "broadcast"}

        @self.app.get("/inbox/{agent_id}")
        async def get_inbox(agent_id: str):
            messages = await self.inbox.get_inbox(agent_id)
            return [m.model_dump(mode="json") for m in messages]

        @self.app.get("/inbox/{agent_id}/pending")
        async def pending_inbox(agent_id: str):
            messages = await self.inbox.get_inbox(agent_id)
            count = len(messages)
            reply_needed = [m for m in messages if m.reply_needed]
            return {
                "count": count,
                "reply_needed": len(reply_needed),
                "latest_senders": list({m.from_agent for m in messages[-5:]}),
                "latest_summary": [
                    {"from": m.from_agent, "text": str(m.body)[:60]}
                    for m in messages[-3:]
                ],
            }

        @self.app.get("/inbox/{agent_id}/{message_id}")
        async def get_message(agent_id: str, message_id: str):
            msg = await self.inbox.get_message(agent_id, message_id)
            if not msg:
                return JSONResponse({"error": "Message not found"}, status_code=404)
            return msg.model_dump(mode="json")

        @self.app.post("/inbox/{agent_id}/{message_id}/archive")
        async def archive_message(agent_id: str, message_id: str):
            await self.inbox.archive(agent_id, message_id)
            return {"status": "archived"}

        @self.app.get("/events/all")
        async def sse_all_stream():
            queue: asyncio.Queue[str] = asyncio.Queue()
            self._global_sse_subscribers.add(queue)

            async def event_generator():
                try:
                    while True:
                        data = await queue.get()
                        yield {"event": "message", "data": data}
                except (asyncio.CancelledError, GeneratorExit):
                    pass
                finally:
                    self._global_sse_subscribers.discard(queue)

            return EventSourceResponse(event_generator())

        @self.app.get("/events/{agent_id}")
        async def sse_stream(agent_id: str):
            queue: asyncio.Queue[str] = asyncio.Queue()
            self._sse_subscribers[agent_id].add(queue)

            async def event_generator():
                try:
                    while True:
                        data = await queue.get()
                        yield {"event": "message", "data": data}
                except (asyncio.CancelledError, GeneratorExit):
                    pass
                finally:
                    self._sse_subscribers[agent_id].discard(queue)

            return EventSourceResponse(event_generator())

        @self.app.websocket("/ws/{agent_id}")
        async def websocket_endpoint(websocket: WebSocket, agent_id: str):
            await websocket.accept()
            self._ws_connections[agent_id] = websocket
            try:
                while True:
                    raw = await websocket.receive_text()
                    data = json.loads(raw)
                    envelope = Envelope.model_validate(data)
                    if envelope.message_type == MessageType.HEARTBEAT:
                        await self.registry.heartbeat(agent_id)
                        await websocket.send_json({"type": "heartbeat_ack"})
                        continue
                    if envelope.to_agent:
                        msg_id = await self.inbox.deliver(envelope)
                        await self._push_to_agent(envelope.to_agent, envelope)
                        await websocket.send_json({"type": "delivered", "message_id": msg_id})
                    else:
                        agents = await self.registry.list_all()
                        for agent in agents:
                            if agent.agent_id == agent_id:
                                continue
                            env_copy = envelope.model_copy(update={"to_agent": agent.agent_id})
                            await self.inbox.deliver(env_copy)
                            await self._push_to_agent(agent.agent_id, env_copy)
                        await websocket.send_json({"type": "broadcast_sent"})
            except WebSocketDisconnect:
                pass
            finally:
                self._ws_connections.pop(agent_id, None)

        # --- Task endpoints ---

        @self.app.post("/tasks")
        async def create_task(req: TaskRequest):
            task = await self.tasks.create(
                req.task_id, req.title, req.description, req.owner
            )
            return task.model_dump(mode="json")

        @self.app.get("/tasks")
        async def list_tasks(status: str | None = None, owner: str | None = None):
            from agent_bus.types import TaskStatus

            ts = TaskStatus(status) if status else None
            task_list = await self.tasks.list_all(status=ts, owner=owner)
            return [t.model_dump(mode="json") for t in task_list]

        @self.app.get("/tasks/{task_id}")
        async def get_task(task_id: str):
            task = await self.tasks.get(task_id)
            if not task:
                return JSONResponse({"error": "Task not found"}, status_code=404)
            return task.model_dump(mode="json")

        @self.app.post("/tasks/{task_id}/claim")
        async def claim_task(task_id: str, req: ClaimRequest):
            task = await self.tasks.claim(task_id, req.agent_id)
            if not task:
                return JSONResponse({"error": "Task not found or already owned"}, status_code=409)
            return task.model_dump(mode="json")

        @self.app.post("/tasks/{task_id}/reassign")
        async def reassign_task(task_id: str, req: ReassignRequest):
            task = await self.tasks.reassign(task_id, req.new_owner)
            if not task:
                return JSONResponse({"error": "Task not found"}, status_code=404)
            return task.model_dump(mode="json")

        @self.app.post("/tasks/{task_id}/done")
        async def complete_task(task_id: str):
            task = await self.tasks.complete(task_id)
            if not task:
                return JSONResponse({"error": "Task not found"}, status_code=404)
            return task.model_dump(mode="json")

        @self.app.post("/tasks/{task_id}/lock-files")
        async def lock_task_files(task_id: str, req: dict):
            paths = req.get("files", [])
            task = await self.tasks.lock_files(task_id, paths)
            if not task:
                return JSONResponse({"error": "Task not found"}, status_code=404)
            return task.model_dump(mode="json")

        # --- Decision endpoints ---

        @self.app.post("/decisions")
        async def add_decision(req: DecisionRequest):
            d = await self.decisions.add(
                req.decision_id, req.title, req.context, req.decision,
                req.decided_by, req.alternatives, req.consequences, req.supersedes,
            )
            if req.supersedes:
                await self.decisions.supersede(req.supersedes, req.decision_id)
            return d.model_dump(mode="json")

        @self.app.get("/decisions")
        async def list_decisions():
            return [d.model_dump(mode="json") for d in await self.decisions.list_all()]

        @self.app.get("/decisions/{decision_id}")
        async def get_decision(decision_id: str):
            d = await self.decisions.get(decision_id)
            if not d:
                return JSONResponse({"error": "Decision not found"}, status_code=404)
            return d.model_dump(mode="json")

        # --- Lock endpoints ---

        @self.app.post("/locks/acquire")
        async def acquire_lock(req: LockRequest):
            try:
                lock = await self.locks.acquire(req.file_path, req.agent_id, req.reason)
                return lock.model_dump(mode="json")
            except LockError as e:
                return JSONResponse({"error": str(e)}, status_code=409)

        @self.app.post("/locks/release")
        async def release_lock(req: ReleaseRequest):
            try:
                await self.locks.release(req.file_path, req.agent_id)
                return {"status": "released"}
            except LockError as e:
                return JSONResponse({"error": str(e)}, status_code=403)

        @self.app.get("/locks")
        async def list_locks():
            return [lk.model_dump(mode="json") for lk in await self.locks.list_locks()]

        # --- Agent management endpoints ---

        @self.app.post("/agents/{agent_id}/heartbeat")
        async def rest_heartbeat(agent_id: str):
            await self.registry.heartbeat(agent_id)
            return {"status": "ok"}

        @self.app.post("/agents/{agent_id}/active-work")
        async def update_active_work(agent_id: str, req: dict):
            await self.registry.update_active_work(agent_id, req.get("work"))
            return {"status": "ok"}

        # --- Skills endpoints ---

        @self.app.post("/skills/{agent_id}")
        async def assign_skill(agent_id: str, req: SkillRequest):
            skill = await self.skills.assign_role(
                agent_id, req.role, req.responsibilities, req.audit_questions
            )
            return skill.model_dump(mode="json")

        @self.app.get("/skills/{agent_id}")
        async def get_skills(agent_id: str):
            return [s.model_dump(mode="json") for s in await self.skills.get_roles(agent_id)]

        # --- Handoff endpoints ---

        @self.app.post("/tasks/{task_id}/handoff")
        async def handoff_task(task_id: str, req: dict):
            from_agent = req.get("from_agent", "")
            to_agent = req.get("to_agent", "")
            if not from_agent or not to_agent:
                return JSONResponse({"error": "from_agent and to_agent required"}, status_code=400)

            task = await self.tasks.get(task_id)
            if not task:
                return JSONResponse({"error": "Task not found"}, status_code=404)

            now_iso = datetime.now(timezone.utc).isoformat()
            await self.db.conn.execute(
                """UPDATE tasks SET owner = ?, updated_at = ? WHERE task_id = ?""",
                (to_agent, now_iso, task_id),
            )
            await self.db.conn.commit()

            handoff_body = {
                "type": "handoff",
                "topic": task_id,
                "detail": req.get("summary", ""),
                "context": {
                    "task_id": task_id,
                    "from_agent": from_agent,
                    "files_touched": req.get("files_touched", []),
                    "decisions_made": req.get("decisions_made", []),
                    "open_questions": req.get("open_questions", []),
                    "state": req.get("state", {}),
                },
            }

            envelope = Envelope(
                from_agent=from_agent,
                to_agent=to_agent,
                message_type=MessageType.HANDOFF,
                body=handoff_body,
                reply_needed=True,
                related_task=task_id,
            )
            msg_id = await self.inbox.deliver(envelope)
            await self._push_to_agent(to_agent, envelope)

            updated = await self.tasks.get(task_id)
            return {
                "status": "handed_off",
                "task": updated.model_dump(mode="json") if updated else None,
                "message_id": msg_id,
            }

        # --- Project context endpoint ---

        @self.app.get("/project/context")
        async def get_project_context():
            from pathlib import Path as P

            ctx_path = P.cwd() / ".agent-bus" / "context.yaml"
            if not ctx_path.exists():
                return {}
            import yaml
            with open(ctx_path) as f:
                return yaml.safe_load(f) or {}

        @self.app.post("/project/context")
        async def update_project_context(req: dict):
            from pathlib import Path as P
            import yaml

            ctx_path = P.cwd() / ".agent-bus" / "context.yaml"
            ctx_path.parent.mkdir(parents=True, exist_ok=True)

            existing = {}
            if ctx_path.exists():
                with open(ctx_path) as f:
                    existing = yaml.safe_load(f) or {}

            field = req.get("field")
            value = req.get("value")
            if not field:
                return JSONResponse({"error": "field required"}, status_code=400)

            if field in ("tech_stack", "conventions") and isinstance(value, list):
                s = set(existing.get(field, []))
                s.update(value)
                existing[field] = list(s)
            elif field == "decisions" and isinstance(value, dict):
                existing.setdefault("decisions", []).append(value)
            elif field == "files_map" and isinstance(value, dict):
                existing.setdefault("files_map", {}).update(value)
            else:
                existing[field] = value

            with open(ctx_path, "w") as f:
                yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

            return existing

        # --- Kickoff endpoints ---

        @self.app.post("/kickoff/start")
        async def start_kickoff():
            steps = await self.kickoff.start()
            return [s.model_dump(mode="json") for s in steps]

        @self.app.get("/kickoff/progress")
        async def kickoff_progress():
            steps = await self.kickoff.get_progress()
            return [s.model_dump(mode="json") for s in steps]

        @self.app.post("/kickoff/step/{step}")
        async def complete_kickoff_step(step: int, req: KickoffStepRequest):
            result = await self.kickoff.complete_step(step, req.result, req.completed_by)
            if not result:
                return JSONResponse({"error": "Step not found"}, status_code=404)
            return result.model_dump(mode="json")

    async def _push_to_agent(self, agent_id: str, envelope: Envelope) -> None:
        data = envelope.model_dump_json()
        for q in list(self._sse_subscribers.get(agent_id, set())):
            await q.put(data)
        for q in list(self._global_sse_subscribers):
            await q.put(data)
        if agent_id in self._ws_connections:
            try:
                await self._ws_connections[agent_id].send_text(data)
            except Exception:
                pass


def create_app() -> FastAPI:
    """Factory for uvicorn."""
    from contextlib import asynccontextmanager

    from agent_bus.config import load_config

    config = load_config()
    db = Database(config.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.initialize()
        yield
        await db.close()

    registry = AgentRegistry(heartbeat_miss_threshold=config.bus.heartbeat_miss_threshold)
    inbox = InboxManager(db)
    bus = MessageBus(db=db, registry=registry, inbox=inbox)
    bus.app.router.lifespan_context = lifespan
    return bus.app
