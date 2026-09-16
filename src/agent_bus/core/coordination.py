"""Authenticated coordination workflows; SQLite remains the only durable state."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from agent_bus.core.lock_paths import server_lock_path
from agent_bus.core.locks import LockBusyError, LockError
from agent_bus.core.inbox import IdempotencyConflict, MessageNotFound
from agent_bus.types import AgentInfo, Envelope, MessageType


Text = Annotated[StrictStr, Field(min_length=1, max_length=4096)]
Key = Annotated[StrictStr, Field(min_length=1, max_length=128)]


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: Text = "Agent"
    capabilities: list[Text] = Field(default_factory=list, max_length=50)
    limit: int = Field(default=20, ge=1, le=100, strict=True)


class PrepareEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[Text] = Field(min_length=1, max_length=100)
    scope: Literal["checkout", "project"] = "checkout"
    reason: Text = "Coordinated edit"
    ttl_seconds: int = Field(default=300, ge=1, le=3600, strict=True)
    operation_key: Key


class HandoffLock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: Text
    scope: Literal["checkout", "project"] = "checkout"
    acquisition_id: Key


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: Key
    to_agent: Key
    summary: Text
    operation_key: Key
    task_status: Literal["in_review", "blocked", "done"] = "in_review"
    files_touched: list[Text] = Field(default_factory=list, max_length=100)
    validation_commands: list[Text] = Field(default_factory=list, max_length=50)
    validation_summary: StrictStr = Field(default="", max_length=8192)
    release_locks: list[HandoffLock] = Field(default_factory=list, max_length=100)
    acknowledge_message_ids: list[Key] = Field(default_factory=list, max_length=100)


INSTRUCTIONS = """agent-bus coordina agentes que trabajan en el mismo proyecto.
Al conectar, tu primera llamada debe ser bootstrap_agent({}); los argumentos son opcionales.
No necesitas un mensaje adicional del usuario para consultar tu contexto de coordinación.
La identidad y el proyecto provienen de tu sesión autenticada: no inventes agent_id,
no compartas credenciales ni cambies de proyecto mediante argumentos de las herramientas.
1. Lee las instrucciones y el estado devueltos por bootstrap_agent. Si necesitas releer
   este protocolo sin cambiar estado, usa get_agent_instructions({}).
2. Usa my_pending_items({}) para atender mensajes y tareas propios antes de editar.
3. Conserva next_cursor para paginar mensajes; leer no confirma. Confirma sólo lo procesado.
4. Reclama una tarea libre con claim_task antes de trabajar; get_project_status permite
   consultar tareas y locks del proyecto. Respeta el trabajo autorizado por el usuario.
5. Usa prepare_edit con una operation_key estable y conserva cada acquisition_id y expires_at.
6. Renueva locks antes de vencer; si falla, deja de editar y adquiere una reserva nueva.
7. Entrega con complete_handoff (in_review por defecto), resumen y evidencia de validación.
   Conserva la misma operation_key y argumentos al reintentar. Sólo libera los tokens indicados.
8. Si esperas respuesta, usa wait_for_updates con event_cursor. No despierta una TUI cerrada.
Si una petición falla o se pierde su respuesta, conserva la clave y el contenido original
al reintentar; no confirmes mensajes ni declares trabajo completado sin verificar el resultado.
Los locks son cooperativos; el handoff registra evidencia declarada y no ejecuta pruebas ni merge.
"""


class Coordination:
    def __init__(self, bus):
        self.bus = bus

    def path(self, value, scope):
        return server_lock_path(value, scope, project_root=self.bus.project_root,
                                base_dir=self.bus.lock_base_dir)

    async def pending(self, principal, *, limit=20, cursor=None, task_offset=0):
        # Checkpoint BEFORE reading the inbox to close the read/subscribe gap.
        event_cursor = await self.bus.events.checkpoint(principal.agent_id)
        messages = await self.bus.inbox.page(principal.agent_id, cursor=cursor, limit=limit)
        rows = await self.bus.db.conn.execute_fetchall(
            "SELECT * FROM tasks WHERE owner=? AND status!='done' ORDER BY task_id LIMIT ? OFFSET ?",
            (principal.agent_id, limit + 1, task_offset),
        )
        tasks = [self.bus.tasks._row_to_task(row).model_dump(mode="json") for row in rows[:limit]]
        counts = await self.bus.db.conn.execute_fetchall(
            "SELECT COUNT(*) AS total, COALESCE(SUM(reply_needed),0) AS reply_needed "
            "FROM inbox WHERE to_agent=? AND archived=0", (principal.agent_id,),
        )
        own_locks = [lock for lock in await self.bus.locks.list_locks()
                     if lock.session_id == principal.session_id]
        return {
            "messages": messages["messages"], "next_cursor": messages["next_cursor"],
            "event_cursor": event_cursor, "pending_message_count": counts[0]["total"],
            "reply_needed_count": counts[0]["reply_needed"], "tasks": tasks,
            "next_task_offset": task_offset + limit if len(rows) > limit else None,
            "locks": [lock.model_dump(mode="json", exclude={"acquisition_id"}) for lock in own_locks[:limit]],
            "active_lock_count": len(own_locks),
            "hint": "Leer no confirma mensajes. Pagina con next_cursor y next_task_offset; /locks lista las reservas.",
        }

    async def operation(self, principal, kind, key, payload, action):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        def transaction(conn):
            # Revalidate after obtaining the SQLite writer reservation.
            now = self.bus.locks._clock()
            valid = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id=? AND agent_id=? AND project_id=? "
                "AND revoked=0 AND expires_at>?",
                (principal.session_id, principal.agent_id, principal.project_id, now),
            ).fetchone()
            if not valid:
                raise HTTPException(401, "Session expired or revoked")
            row = conn.execute(
                "SELECT fingerprint,result_json FROM coordination_operations "
                "WHERE session_id=? AND kind=? AND operation_key=?",
                (principal.session_id, kind, key),
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise IdempotencyConflict("Operation key already used with different content")
                result = json.loads(row["result_json"])
                if kind == "prepare_edit":
                    for lock in result["locks"]:
                        if not conn.execute(
                            "SELECT 1 FROM locks WHERE file_path=? AND session_id=? "
                            "AND acquisition_id=? AND expires_at>?",
                            (lock["file_path"], principal.session_id, lock["acquisition_id"], now),
                        ).fetchone():
                            raise LockError("Previous reservation is no longer active; use a new operation_key")
                return {**result, "replayed": True}
            result = action(conn, now)
            conn.execute("INSERT INTO coordination_operations VALUES (?,?,?,?,?)",
                         (principal.session_id, kind, key, fingerprint, json.dumps(result)))
            return {**result, "replayed": False}

        # The existing lease boundary owns a complete, rollback-safe transaction.
        return await self.bus.locks._mutate(transaction)

    async def prepare(self, principal, req):
        paths = sorted(set(self.path(path, req.scope) for path in req.paths))
        payload = {**req.model_dump(), "paths": paths}

        def action(conn, now):
            expiry = self.bus.locks._expiry(now, req.ttl_seconds, principal.expires_at)
            locks = []
            for path in paths:
                token = secrets.token_urlsafe(24)
                row = conn.execute(
                    "INSERT INTO locks(file_path,locked_by,locked_at,reason,session_id,acquisition_id,expires_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(file_path) DO UPDATE SET "
                    "locked_by=excluded.locked_by,locked_at=excluded.locked_at,reason=excluded.reason,"
                    "session_id=excluded.session_id,acquisition_id=excluded.acquisition_id,expires_at=excluded.expires_at "
                    "WHERE COALESCE(locks.expires_at,0)<=? RETURNING *",
                    (path, principal.agent_id, datetime.fromtimestamp(now, timezone.utc).isoformat(), req.reason,
                     principal.session_id, token, expiry, now),
                ).fetchone()
                if row is None:
                    raise LockError(f"File '{path}' is locked; no files were acquired")
                lock = self.bus.locks._row_to_lock(row).model_dump(mode="json")
                # Canonical absolute paths are directly reusable with checkout scope.
                locks.append({**lock, "scope": "checkout"})
            return {"authorized": True, "locks": locks}

        return await self.operation(principal, "prepare_edit", req.operation_key, payload, action)

    async def handoff(self, principal, req):
        payload = req.model_dump()
        releases = [(self.path(lock.file_path, lock.scope), lock.acquisition_id) for lock in req.release_locks]

        def action(conn, now):
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (req.task_id,)).fetchone()
            if task is None:
                raise HTTPException(404, "Task not found")
            if task["owner"] != principal.agent_id:
                raise HTTPException(403, "Only the task owner can hand off work")
            if task["status"] not in ("in_progress", "in_review", "blocked"):
                raise HTTPException(409, "Task is not active")
            if task["status"] == "blocked" and req.task_status != "blocked":
                raise HTTPException(409, "Unblock the task before submitting work")
            for path, token in releases:
                if not conn.execute(
                    "SELECT 1 FROM locks WHERE file_path=? AND locked_by=? AND session_id=? "
                    "AND acquisition_id=? AND expires_at>?",
                    (path, principal.agent_id, principal.session_id, token, now),
                ).fetchone():
                    raise LockError("Handoff requires current lock acquisitions; nothing was changed")
            if req.acknowledge_message_ids:
                self.bus.inbox._acknowledge(conn, principal.agent_id, req.acknowledge_message_ids)
            timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
            conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE task_id=?",
                         (req.task_status, timestamp, req.task_id))
            conn.execute(
                "INSERT INTO audit_log(action,task_id,actor_agent_id,actor_session_id,previous_owner,new_owner,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("complete_handoff", req.task_id, principal.agent_id, principal.session_id,
                 principal.agent_id, principal.agent_id, timestamp),
            )
            if req.task_status == "done":
                for blocked in conn.execute("SELECT task_id,depends_on FROM tasks WHERE status='blocked'").fetchall():
                    dependencies = json.loads(blocked["depends_on"] or "[]")
                    if req.task_id in dependencies and all(conn.execute(
                        "SELECT 1 FROM tasks WHERE task_id=? AND status='done'", (dep,),
                    ).fetchone() for dep in dependencies):
                        conn.execute("UPDATE tasks SET status='pending',updated_at=? WHERE task_id=?",
                                     (timestamp, blocked["task_id"]))
            body = {key: payload[key] for key in ("summary", "task_status", "files_touched",
                                                  "validation_commands", "validation_summary")}
            body["kind"] = "handoff"
            body["text"] = req.summary
            body["validation_source"] = "reported_by_agent"
            envelope = Envelope(from_agent=principal.agent_id, to_agent=req.to_agent,
                                message_type=MessageType.HANDOFF, related_task=req.task_id,
                                reply_needed=req.task_status != "done", body=body)
            sent = self.bus.inbox._send(conn, envelope, [req.to_agent], None)
            for path, token in releases:
                conn.execute("DELETE FROM locks WHERE file_path=? AND acquisition_id=?", (path, token))
            return {"task_id": req.task_id, "task_status": req.task_status,
                    "message_id": sent.envelope.message_id, "released_lock_count": len(set(releases)),
                    "acknowledged": req.acknowledge_message_ids}

        return await self.operation(principal, "complete_handoff", req.operation_key, payload, action)


def setup_coordination_routes(bus):
    coordination = Coordination(bus)

    def principal(request):
        if request.state.principal is None:
            raise HTTPException(401, "Coordination workflows require an authenticated session")
        return request.state.principal

    async def mutate(operation):
        try:
            return await operation
        except LockBusyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503, headers={"Retry-After": "1"})
        except LockError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ValueError as exc:
            if isinstance(exc, (IdempotencyConflict, MessageNotFound)):
                raise
            return JSONResponse({"error": str(exc)}, status_code=422)

    @bus.app.post("/coordination/bootstrap")
    async def bootstrap(req: BootstrapRequest, request: Request):
        actor = principal(request)
        existing = await bus.registry.get(actor.agent_id)
        if existing:
            await bus.registry.heartbeat(actor.agent_id)
        else:
            await bus.registry.register(AgentInfo(agent_id=actor.agent_id, display_name=req.display_name,
                                                  capabilities=req.capabilities))
        pending = await coordination.pending(actor, limit=req.limit)
        agents = await bus.registry.list_all()
        decisions = await bus.db.conn.execute_fetchall(
            "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (req.limit,),
        )
        ready = await bus.db.conn.execute_fetchall(
            "SELECT * FROM tasks WHERE owner='free' AND status='pending' ORDER BY task_id LIMIT ?",
            (req.limit,),
        )
        ready_count = await bus.db.conn.execute_fetchall(
            "SELECT COUNT(*) FROM tasks WHERE owner='free' AND status='pending'",
        )
        return {"project_id": bus.project_id, "agent_id": actor.agent_id, "session_id": actor.session_id,
                "session_expires_at": actor.expires_at, "instructions": INSTRUCTIONS, "pending": pending,
                "agents": [agent.model_dump(mode="json") for agent in agents[:req.limit]],
                "agent_count": len(agents), "recent_decisions": [dict(row) for row in decisions],
                "available_tasks": [bus.tasks._row_to_task(row).model_dump(mode="json") for row in ready],
                "available_task_count": ready_count[0][0]}

    @bus.app.get("/coordination/pending")
    async def pending(request: Request, limit: int = Query(20, ge=1, le=100),
                      cursor: str | None = None, task_offset: int = Query(0, ge=0)):
        return await coordination.pending(principal(request), limit=limit, cursor=cursor, task_offset=task_offset)

    @bus.app.post("/coordination/prepare-edit")
    async def prepare(req: PrepareEditRequest, request: Request):
        return await mutate(coordination.prepare(principal(request), req))

    @bus.app.post("/coordination/handoff")
    async def handoff(req: HandoffRequest, request: Request):
        return await mutate(coordination.handoff(principal(request), req))
