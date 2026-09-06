from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Annotated

import os
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from sse_starlette.sse import EventSourceResponse

from agent_bus.core.events import CursorExpired, CursorInvalid, EventLog
from agent_bus.core.decisions import DecisionLog
from agent_bus.core.inbox import (
    IdempotencyConflict, InboxManager, InvalidCursor, MessageNotFound, SendResult,
)
from agent_bus.core.kickoff import KickoffManager
from agent_bus.core.locks import LockError, LockManager
from agent_bus.core.registry import AgentRegistry
from agent_bus.core.skills import SkillRegistry
from agent_bus.core.tasks import TaskManager
from agent_bus.reputation.database import Database
from agent_bus.types import AgentInfo, AutonomyLevel, Envelope, MessageType
from agent_bus.security import AuthenticationError, Principal, SessionStore


class SendMessageRequest(BaseModel):
    from_agent: str | None = None
    to_agent: str | None = None
    message_type: MessageType = MessageType.INBOX
    body: dict | None = None
    reply_needed: bool = False
    related_task: str | None = None
    signature: str | None = None
    correlation_id: str | None = None
    metadata: dict | None = None
    conversation_id: str | None = None
    idempotency_key: StrictStr | None = Field(default=None, min_length=1, max_length=128)


class AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_ids: list[Annotated[StrictStr, Field(min_length=1)]] = Field(min_length=1, max_length=100)


class ReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: dict
    idempotency_key: StrictStr = Field(min_length=1, max_length=128)
    reply_needed: StrictBool = False
    acknowledge: StrictBool = False


class FailureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: StrictStr = Field(min_length=1, max_length=2000)


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
    agent_id: str | None = None


class ReassignRequest(BaseModel):
    new_owner: str


class LockRequest(BaseModel):
    file_path: str
    agent_id: str | None = None
    reason: str | None = None


class ReleaseRequest(BaseModel):
    file_path: str
    agent_id: str | None = None


class DecisionRequest(BaseModel):
    decision_id: str
    title: str
    context: str
    decision: str
    decided_by: str | None = None
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
    def __init__(self, db: Database, registry: AgentRegistry, inbox: InboxManager, project_id: str | None = None) -> None:
        self.db = db
        self.project_id = project_id or os.environ.get("AGENT_BUS_PROJECT_ID", "default")
        self.sessions = SessionStore(db, self.project_id)
        self.events = EventLog(db)
        self.registry = registry
        self.inbox = inbox
        self.tasks = TaskManager(db)
        self.decisions = DecisionLog(db)
        self.locks = LockManager(db)
        self.skills = SkillRegistry(db)
        self.kickoff = KickoffManager(db)
        self.app = FastAPI(title="agent-bus", version="0.1.0")
        self._sse_subscribers: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._global_sse_subscribers: set[asyncio.Event] = set()
        self._ws_connections: dict[str, WebSocket] = {}
        self._setup_routes()

    def _setup_routes(self) -> None:
        # Storage owns atomic conflict checks; translate failures consistently.
        @self.app.exception_handler(IdempotencyConflict)
        async def idempotency_conflict(request: Request, exc: IdempotencyConflict):
            return JSONResponse({"error": str(exc)}, status_code=409)

        @self.app.exception_handler(MessageNotFound)
        async def missing_delivery(request: Request, exc: MessageNotFound):
            return JSONResponse({"error": str(exc)}, status_code=404)

        @self.app.exception_handler(InvalidCursor)
        async def invalid_cursor(request: Request, exc: InvalidCursor):
            return JSONResponse({"error": str(exc)}, status_code=422)

        @self.app.middleware("http")
        async def authenticate_request(request: Request, call_next):
            request.state.principal = None
            request.state.token = None
            path = request.url.path
            if path in ("/health", "/room"):
                return await call_next(request)
            try:
                principal, token = await self._authenticate(request.headers.get("authorization"))
            except AuthenticationError:
                return JSONResponse({"error": "Valid bearer session required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            request.state.principal = principal
            request.state.token = token
            if principal:
                parts = path.strip("/").split("/")
                admin_only = (
                    path.startswith("/room/api/") or path in ("/events/all", "/events/all/cursor")
                    or path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
                    or (request.method not in ("GET", "HEAD", "OPTIONS") and (
                        path.startswith("/skills/") or path.startswith("/kickoff/")
                        or path == "/project/context"
                        or (len(parts) == 3 and parts[0] == "tasks" and parts[2] == "reassign")
                    ))
                )
                if admin_only and not principal.is_admin:
                    return JSONResponse({"error": "Administrator role required"}, status_code=403)
                if len(parts) >= 2 and parts[0] in ("inbox", "events", "agents"):
                    if path not in ("/events/all", "/events/all/cursor") and parts[1] != principal.agent_id:
                        return JSONResponse({"error": "Identity does not own this resource"}, status_code=403)
                # Legacy actor arguments are accepted only when consistent with
                # the verified session. They never confer authority.
                if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                    try:
                        body = await request.json()
                    except (ValueError, UnicodeDecodeError):
                        body = {}
                    if isinstance(body, dict):
                        actors = []
                        if path in ("/messages",) or path.endswith(("/handoff", "/reply")):
                            actors.append("from_agent")
                        if path == "/decisions":
                            actors.append("decided_by")
                        if path == "/register" or path.startswith("/locks/") or (
                            len(parts) == 3 and parts[0] == "tasks"
                            and parts[2] in ("claim", "done", "lock-files")
                        ):
                            actors.append("agent_id")
                        if path.startswith("/kickoff/step/"):
                            actors.append("completed_by")
                        if any(body.get(field) is not None and body[field] != principal.agent_id for field in actors):
                            return JSONResponse({"error": "Actor does not match authenticated session"}, status_code=403)
            return await call_next(request)

        @self.app.get("/health")
        async def health():
            return {"status": "ok"}

        @self.app.get("/auth/me")
        async def auth_me(request: Request):
            principal = request.state.principal
            if principal is None:
                return JSONResponse({"error": "No authenticated session"}, status_code=401)
            return {key: getattr(principal, key) for key in (
                "agent_id", "session_id", "project_id", "role", "expires_at"
            )}

        # --- Agent endpoints ---

        @self.app.post("/register")
        async def register(req: RegisterRequest, request: Request):
            if request.state.principal and req.public_key is not None:
                raise HTTPException(422, "Presence registration does not enroll public keys")
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
        async def post_message(req: SendMessageRequest, request: Request):
            req.from_agent = self._actor(request, req.from_agent)
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
                conversation_id=req.conversation_id,
            )
            return await self._send_message(envelope, req.idempotency_key)

        @self.app.get("/inbox/{agent_id}/messages")
        async def read_messages(
            agent_id: str, cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100), reply_needed: bool | None = None,
        ):
            return await self.inbox.page(agent_id, cursor=cursor, limit=limit, reply_needed=reply_needed)

        @self.app.post("/inbox/{agent_id}/ack")
        async def acknowledge_messages(agent_id: str, req: AcknowledgeRequest):
            return await self.inbox.acknowledgments(agent_id, req.message_ids)

        @self.app.post("/inbox/{agent_id}/{message_id}/reply")
        async def reply_message(agent_id: str, message_id: str, req: ReplyRequest):
            result = await self.inbox.reply(
                agent_id, message_id, req.body, idempotency_key=req.idempotency_key,
                reply_needed=req.reply_needed, acknowledge=req.acknowledge,
            )
            return await self._publish_send(result)

        @self.app.post("/inbox/{agent_id}/{message_id}/fail")
        async def fail_message(agent_id: str, message_id: str, req: FailureRequest):
            try:
                return await self.inbox.record_failure(agent_id, message_id, req.error)
            except MessageNotFound:
                raise
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        # Register canonical event routes before the generic message-id lookup.
        @self.app.get("/events/all/cursor")
        async def global_event_cursor(request: Request):
            return await self._checkpoint_response(None, request.query_params.get("cursor"))

        @self.app.get("/events/all")
        async def sse_all_stream(request: Request):
            return await self._event_response(request, None, self._global_sse_subscribers)

        @self.app.get("/inbox/{agent_id}/events/cursor")
        @self.app.get("/events/{agent_id}/cursor")
        async def personal_event_cursor(agent_id: str, request: Request):
            return await self._checkpoint_response(agent_id, request.query_params.get("cursor"))

        @self.app.get("/inbox/{agent_id}/events")
        @self.app.get("/events/{agent_id}")
        async def sse_stream(agent_id: str, request: Request):
            return await self._event_response(request, agent_id, self._sse_subscribers[agent_id])

        @self.app.get("/inbox/{agent_id}")
        async def get_inbox(agent_id: str):
            messages = await self.inbox.get_inbox(agent_id)
            return [m.model_dump(mode="json") for m in messages]

        @self.app.get("/inbox/{agent_id}/pending")
        async def pending_inbox(agent_id: str):
            return await self.inbox.pending_summary(agent_id)

        @self.app.get("/inbox/{agent_id}/{message_id}")
        async def get_message(agent_id: str, message_id: str):
            msg = await self.inbox.get_message(agent_id, message_id)
            if not msg:
                return JSONResponse({"error": "Message not found"}, status_code=404)
            state = await self.inbox.delivery_state(agent_id, message_id)
            if state is None:
                # Retention may remove an acknowledged delivery after the first
                # read. Never turn missing state into an apparently pending item.
                raise HTTPException(404, "Message delivery no longer exists")
            return {**msg.model_dump(mode="json"), **state}

        @self.app.post("/inbox/{agent_id}/{message_id}/archive")
        async def archive_message(agent_id: str, message_id: str):
            await self.inbox.archive(agent_id, message_id)
            return {"status": "archived"}

        @self.app.websocket("/ws/{agent_id}")
        async def websocket_endpoint(websocket: WebSocket, agent_id: str):
            try:
                principal, token = await self._authenticate(websocket.headers.get("authorization"))
            except AuthenticationError:
                await websocket.close(code=1008)
                return
            if principal and principal.agent_id != agent_id:
                await websocket.close(code=1008)
                return
            websocket.state.token = token
            await websocket.accept()
            previous = self._ws_connections.get(agent_id)
            if previous:
                await previous.close(code=1000)
            self._ws_connections[agent_id] = websocket
            try:
                while await self._session_valid(token):
                    try:
                        raw = await asyncio.wait_for(websocket.receive_text(), timeout=1)
                    except asyncio.TimeoutError:
                        continue
                    if not await self._session_valid(token):
                        break
                    try:
                        data = json.loads(raw)
                        envelope = Envelope.model_validate(data)
                    except ValueError:
                        await websocket.close(code=1008)
                        return
                    if principal and envelope.from_agent != principal.agent_id:
                        await websocket.close(code=1008)
                        return
                    if envelope.message_type == MessageType.HEARTBEAT:
                        if principal and envelope.to_agent not in (None, agent_id):
                            await websocket.close(code=1008)
                            return
                        await self.registry.heartbeat(agent_id)
                        await websocket.send_json({"type": "heartbeat_ack"})
                        continue
                    # A WebSocket sender retains message_id when retrying an envelope.
                    try:
                        response = await self._send_message(envelope, "ws:" + sha256(envelope.message_id.encode()).hexdigest())
                    except IdempotencyConflict as exc:
                        await websocket.send_json({"type": "error", "status": 409, "error": str(exc)})
                        continue
                    response["type"] = "broadcast_sent" if response["status"] == "broadcast" else "delivered"
                    await websocket.send_json(response)
                await websocket.close(code=1008)
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                if self._ws_connections.get(agent_id) is websocket:
                    self._ws_connections.pop(agent_id, None)

        # --- Task endpoints ---

        @self.app.post("/tasks")
        async def create_task(req: TaskRequest, request: Request):
            principal = request.state.principal
            if principal and not principal.is_admin and req.owner not in ("free", principal.agent_id):
                raise HTTPException(403, "Cannot assign tasks to another agent")
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
        async def claim_task(task_id: str, req: ClaimRequest, request: Request):
            req.agent_id = self._actor(request, req.agent_id)
            task = await self.tasks.claim(task_id, req.agent_id)
            if not task:
                return JSONResponse({"error": "Task not found or already owned"}, status_code=409)
            return task.model_dump(mode="json")

        @self.app.post("/tasks/{task_id}/reassign")
        async def reassign_task(task_id: str, req: ReassignRequest, request: Request):
            principal = request.state.principal
            if principal:
                task = await self.tasks.transfer(task_id, req.new_owner,
                    actor=principal.agent_id, session_id=principal.session_id)
            else:
                task = await self.tasks.reassign(task_id, req.new_owner)
            if not task:
                return await self._task_failure(task_id)
            return task.model_dump(mode="json")

        @self.app.post("/tasks/{task_id}/done")
        async def complete_task(task_id: str, request: Request):
            principal = request.state.principal
            task = await self.tasks.complete(task_id, actor=principal.agent_id if principal else None)
            if not task:
                return await self._task_failure(task_id, principal)
            return task.model_dump(mode="json")

        @self.app.post("/tasks/{task_id}/lock-files")
        async def lock_task_files(task_id: str, req: dict, request: Request):
            paths = req.get("files", [])
            principal = request.state.principal
            task = await self.tasks.lock_files(task_id, paths, actor=principal.agent_id if principal else None)
            if not task:
                return await self._task_failure(task_id, principal)
            return task.model_dump(mode="json")

        # --- Decision endpoints ---

        @self.app.post("/decisions")
        async def add_decision(req: DecisionRequest, request: Request):
            req.decided_by = self._actor(request, req.decided_by)
            try:
                d = await self.decisions.add(
                    req.decision_id, req.title, req.context, req.decision,
                    req.decided_by, req.alternatives, req.consequences, req.supersedes,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
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
        async def acquire_lock(req: LockRequest, request: Request):
            req.agent_id = self._actor(request, req.agent_id)
            try:
                lock = await self.locks.acquire(req.file_path, req.agent_id, req.reason)
                return lock.model_dump(mode="json")
            except LockError as e:
                return JSONResponse({"error": str(e)}, status_code=409)

        @self.app.post("/locks/release")
        async def release_lock(req: ReleaseRequest, request: Request):
            req.agent_id = self._actor(request, req.agent_id)
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

        # --- War Room endpoints (capa humana) ---

        @self.app.get("/room", response_class=HTMLResponse)
        async def room_ui():
            html_path = Path(__file__).parent.parent / "web" / "room.html"
            return HTMLResponse(html_path.read_text())

        @self.app.get("/room/api/pending-approvals")
        async def room_pending_approvals():
            """Mensajes dirigidos al humano que requieren su decisión."""
            try:
                human_inbox = await self.inbox.get_inbox("human")
            except Exception:
                human_inbox = []
            return [m.model_dump(mode="json") for m in human_inbox if m.reply_needed]

        @self.app.post("/room/api/approve")
        async def room_approve(req: dict, request: Request):
            """Aprobar/rechazar/responder una solicitud del humano; notifica al agente."""
            message_id = req.get("message_id", "")
            decision = req.get("decision", "")  # approve | reject | respond
            note = req.get("note", "")
            agent = req.get("agent", "")

            msg = await self.inbox.get_message("human", message_id)
            if not msg:
                return JSONResponse({"error": "Message not found"}, status_code=404)

            result = await self.inbox.reply(
                "human", message_id,
                {"type": "approval_decision", "decision": decision, "note": note, "original": str(msg.body)},
                idempotency_key="room-approve:" + sha256(message_id.encode()).hexdigest(),
                acknowledge=True, actor_id=self._actor(request, "human"),
            )
            response = await self._publish_send(result)
            return {**response, "status": "notified", "agent": msg.from_agent}

        @self.app.post("/room/api/assign")
        async def room_assign(req: dict, request: Request):
            """Asignar una tarea a un agente desde el War Room; notifica al agente."""
            task_id = req.get("task_id", "")
            agent_id = req.get("agent_id", "")
            if not task_id or not agent_id:
                return JSONResponse({"error": "task_id and agent_id required"}, status_code=400)

            principal = request.state.principal
            if principal:
                task = await self.tasks.transfer(task_id, agent_id, actor=principal.agent_id,
                    session_id=principal.session_id)
            else:
                task = await self.tasks.reassign(task_id, agent_id)
                if task:
                    await self.db.conn.execute("UPDATE tasks SET status = 'in_progress' WHERE task_id = ?", (task_id,))
                    await self.db.conn.commit()
            if not task:
                return await self._task_failure(task_id)

            envelope = Envelope(
                from_agent=self._actor(request, "human"),
                to_agent=agent_id,
                message_type=MessageType.INBOX,
                body={
                    "type": "task_assigned",
                    "task_id": task_id,
                    "title": task.title,
                    "detail": req.get("note", "El humano te asignó esta tarea desde el War Room."),
                },
                reply_needed=True,
                related_task=task_id,
            )
            msg_id = await self.inbox.deliver(envelope)
            await self._push_to_agent(agent_id, envelope)
            return {"status": "assigned", "task_id": task_id, "agent": agent_id, "message_id": msg_id}

        @self.app.post("/room/api/message")
        async def room_message(req: dict, request: Request):
            """Mensaje del humano a un agente (o broadcast con to_agent='*')."""
            to_agent = req.get("to_agent", "")
            text = req.get("text", "")
            if not text:
                return JSONResponse({"error": "text required"}, status_code=400)

            envelope = Envelope(
                from_agent=self._actor(request, "human"),
                to_agent=None if to_agent in ("*", "") else to_agent,
                message_type=MessageType.INBOX if to_agent not in ("*", "") else MessageType.BROADCAST,
                body={"text": text},
                reply_needed=bool(req.get("reply_needed", False)),
                related_task=req.get("related_task"),
            )
            key = req.get("idempotency_key")
            if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128):
                raise HTTPException(422, "idempotency_key must contain 1..128 characters")
            return await self._send_message(envelope, key)

        @self.app.get("/room/api/overview")
        async def room_overview():
            """Snapshot completo para el dashboard del War Room."""
            tasks = await self.tasks.list_all()
            agents = await self.registry.list_all()
            locks = await self.locks.list_locks()
            decisions = await self.decisions.list_all()
            try:
                human_inbox = await self.inbox.get_inbox("human")
            except Exception:
                human_inbox = []
            return {
                "tasks": [t.model_dump(mode="json") for t in tasks],
                "agents": [a.model_dump(mode="json") for a in agents],
                "locks": [l.model_dump(mode="json") for l in locks],
                "decisions": [d.model_dump(mode="json") for d in decisions],
                "pending_approvals": len([m for m in human_inbox if m.reply_needed]),
            }

        @self.app.post("/tasks/{task_id}/handoff")
        async def handoff_task(task_id: str, req: dict, request: Request):
            from_agent = self._actor(request, req.get("from_agent"))
            to_agent = req.get("to_agent", "")
            if not from_agent or not to_agent:
                return JSONResponse({"error": "from_agent and to_agent required"}, status_code=400)
            if to_agent == "free":
                return JSONResponse({"error": "Handoff requires an agent; administrators may reassign to free"}, status_code=422)

            principal = request.state.principal
            if principal:
                task = await self.tasks.transfer(task_id, to_agent, actor=principal.agent_id,
                    session_id=principal.session_id, require_owner=True)
            else:
                task = await self.tasks.reassign(task_id, to_agent)
            if not task:
                return await self._task_failure(task_id, principal)

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

            return {
                "status": "handed_off",
                "task": task.model_dump(mode="json"),
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
        async def complete_kickoff_step(step: int, req: KickoffStepRequest, request: Request):
            req.completed_by = self._actor(request, req.completed_by)
            result = await self.kickoff.complete_step(step, req.result, req.completed_by)
            if not result:
                return JSONResponse({"error": "Step not found"}, status_code=404)
            return result.model_dump(mode="json")

    async def _send_message(self, envelope: Envelope, idempotency_key: str | None = None) -> dict:
        if envelope.to_agent not in (None, "*") and envelope.message_type != MessageType.BROADCAST:
            recipients = [envelope.to_agent]
        else:
            recipients = [agent.agent_id for agent in await self.registry.list_all()
                          if agent.agent_id != envelope.from_agent]
        result = await self.inbox.send(envelope, recipients, idempotency_key=idempotency_key)
        return await self._publish_send(result)

    async def _publish_send(self, result: SendResult) -> dict:
        if not result.replayed:
            for recipient in result.recipients:
                await self._push_to_agent(recipient, result.envelope.model_copy(update={"to_agent": recipient}))
        broadcast = result.envelope.to_agent in (None, "*") or result.envelope.message_type == MessageType.BROADCAST
        response = {
            "message_id": result.envelope.message_id,
            "conversation_id": result.envelope.conversation_id,
            "status": "broadcast" if broadcast else "delivered",
            "replayed": result.replayed,
        }
        if broadcast:
            response["message_ids"] = [result.envelope.message_id for _ in result.recipients]
        return response

    async def _task_failure(self, task_id: str, principal: Principal | None = None) -> JSONResponse:
        # This read only explains a failed conditional write; it never authorizes
        # a later mutation, so a concurrent owner change cannot bypass the SQL.
        task = await self.tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        if principal and task.owner != principal.agent_id:
            return JSONResponse({"error": "Task belongs to another agent"}, status_code=403)
        return JSONResponse({"error": "Task state does not permit this transition"}, status_code=409)

    async def _authenticate(self, authorization: str | None) -> tuple[Principal | None, str | None]:
        if authorization is None and os.environ.get("AGENT_BUS_ALLOW_UNSIGNED", "0") == "1":
            return None, None
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or " " in token:
            raise AuthenticationError("Valid bearer session required")
        return await self.sessions.authenticate(token), token

    @staticmethod
    def _actor(request: Request, legacy_actor: str | None) -> str:
        principal = request.state.principal
        if principal:
            return principal.agent_id
        if not legacy_actor:
            raise HTTPException(422, "An actor is required in legacy mode")
        return legacy_actor

    async def _session_valid(self, token: str | None) -> bool:
        try:
            await self._authenticate(f"Bearer {token}" if token else None)
            return True
        except AuthenticationError:
            return False

    async def _event_error(self, scope: str | None, error: ValueError) -> JSONResponse:
        if isinstance(error, CursorExpired):
            return JSONResponse({
                "error": "cursor_expired", "cursor": await self.events.checkpoint(scope),
                "recovery": "read_inbox",
            }, status_code=410)
        return JSONResponse({"error": "cursor_invalid"}, status_code=422)

    async def _checkpoint_response(self, scope: str | None, cursor: str | None):
        try:
            if cursor is not None:
                # Validate without advancing: a caller can checkpoint, inspect
                # pending deliveries, then connect without losing the gap.
                await self.events.read(scope, cursor, limit=1)
            else:
                cursor = await self.events.checkpoint(scope)
            return {"cursor": cursor}
        except (CursorInvalid, CursorExpired) as exc:
            return await self._event_error(scope, exc)

    async def _event_response(self, request: Request, scope: str | None, subscribers: set):
        wake = asyncio.Event()
        subscribers.add(wake)
        token = request.state.token
        cursor = request.headers.get("last-event-id", request.query_params.get("cursor"))
        try:
            if cursor is None:
                cursor = await self.events.checkpoint(scope)
            await self.events.read(scope, cursor, limit=1)
        except (CursorInvalid, CursorExpired) as exc:
            subscribers.discard(wake)
            return await self._event_error(scope, exc)
        except BaseException:
            subscribers.discard(wake)
            raise

        async def event_generator():
            nonlocal cursor
            try:
                if not await self._session_valid(token):
                    return
                yield {"event": "checkpoint", "id": cursor, "data": json.dumps({"cursor": cursor})}
                while await self._session_valid(token):
                    # A one-bit wake hint bounds memory even for slow readers.
                    # Polling the durable log also recovers commits without push.
                    wake.clear()
                    try:
                        page = await self.events.read(scope, cursor, limit=50)
                    except CursorExpired as exc:
                        reset = await self._event_error(scope, exc)
                        if await self._session_valid(token):
                            yield {"event": "reset", "data": reset.body.decode()}
                        return
                    except CursorInvalid:
                        if await self._session_valid(token):
                            yield {"event": "reset", "data": json.dumps({
                                "error": "cursor_invalid", "recovery": "read_inbox",
                                "cursor": await self.events.checkpoint(scope),
                            })}
                        return
                    for event in page["events"]:
                        if not await self._session_valid(token):
                            return
                        yield {"id": event["id"], "event": event["event"],
                               "data": json.dumps(event["data"], ensure_ascii=False)}
                        cursor = event["id"]
                    # Advance gaps in a personal stream without inventing a
                    # message; clients persist this control checkpoint too.
                    if page["next_cursor"] != cursor:
                        if not await self._session_valid(token):
                            return
                        cursor = page["next_cursor"]
                        yield {"event": "checkpoint", "id": cursor, "data": json.dumps({"cursor": cursor})}
                    if page["has_more"]:
                        continue
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        pass
            except (asyncio.CancelledError, GeneratorExit):
                pass
            finally:
                subscribers.discard(wake)

        return EventSourceResponse(event_generator(), send_timeout=10)

    async def _push_to_agent(self, agent_id: str, envelope: Envelope) -> None:
        data = envelope.model_dump_json()
        for wake in list(self._sse_subscribers.get(agent_id, set())):
            wake.set()
        for wake in list(self._global_sse_subscribers):
            wake.set()
        if agent_id in self._ws_connections:
            try:
                websocket = self._ws_connections[agent_id]
                if await self._session_valid(websocket.state.token):
                    await websocket.send_text(data)
                else:
                    await websocket.close(code=1008)
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
    bus = MessageBus(db=db, registry=registry, inbox=inbox, project_id=config.bus.project_id)
    bus.app.router.lifespan_context = lifespan
    return bus.app
