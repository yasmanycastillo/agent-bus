"""MCP tools backed by the hub, served through the official SDK over stdio."""

from __future__ import annotations

import asyncio
import copy
import os
import json
import logging
import sys
from typing import Any
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS, CallToolRequestParams, CallToolResult, ListToolsResult,
    PaginatedRequestParams, TextContent, Tool,
)
from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr

from agent_bus.security import AuthenticationError, async_bus_client, load_session
from agent_bus.core.sse import iter_sse_frames
from agent_bus.mcp.transport import cancellable_stdio

logger = logging.getLogger("agent_bus.mcp")

SERVER_NAME = "agent-bus"
SERVER_VERSION = "0.1.0"


class DecisionToolArguments(BaseModel):
    """Public MCP arguments adapted to the hub's DecisionRequest contract."""

    title: StrictStr = Field(min_length=1, description="Título de la decisión")
    what: StrictStr = Field(min_length=1, description="Descripción de la decisión")
    decided_by: StrictStr = Field(min_length=1, description="Agente responsable")
    context: StrictStr = Field(default="", description="Contexto adicional")


class WaitArguments(BaseModel):
    agent_id: StrictStr = Field(min_length=1)
    timeout: StrictInt = Field(default=120, ge=1, le=120, description="Plazo total en segundos, incluida la consulta inicial")
    event_cursor: StrictStr | None = Field(default=None, min_length=1, max_length=2048,
                                           description="Cursor de eventos para reanudar; distinto del cursor del inbox")


class PostMessageArguments(BaseModel):
    from_agent: StrictStr
    to_agent: StrictStr = Field(min_length=1)
    text: StrictStr
    idempotency_key: StrictStr = Field(min_length=1, max_length=128, description="Conserva esta clave al reintentar el mismo envío")
    reply_needed: StrictBool = False
    related_task: StrictStr | None = None


class ReadMessagesArguments(BaseModel):
    agent_id: StrictStr
    cursor: StrictStr | None = None
    limit: StrictInt = Field(default=50, ge=1, le=100)
    reply_needed: StrictBool | None = None


class AckMessagesArguments(BaseModel):
    agent_id: StrictStr
    message_ids: list[StrictStr] = Field(min_length=1, max_length=100)


class ReplyMessageArguments(BaseModel):
    agent_id: StrictStr
    message_id: StrictStr = Field(min_length=1)
    text: StrictStr
    idempotency_key: StrictStr = Field(min_length=1, max_length=128)
    reply_needed: StrictBool = False
    acknowledge: StrictBool = False


TOOLS_DEFINITIONS = [
    {
        "name": "wait_for_updates",
        "description": "Bloquea la ejecución hasta recibir un nuevo mensaje, tarea o evento del bus por SSE (patrón long-polling reactivo).",
        "inputSchema": WaitArguments.model_json_schema(),
    },
    {
        "name": "post_message",
        "description": "Enviar un mensaje; conserva idempotency_key para reintentos del mismo contenido.",
        "inputSchema": PostMessageArguments.model_json_schema(),
    },
    {
        "name": "read_messages",
        "description": "Leer una página del inbox pendiente sin confirmar mensajes. Continúa con next_cursor.",
        "inputSchema": ReadMessagesArguments.model_json_schema(),
    },
    {
        "name": "ack_messages",
        "description": "Confirmar explícitamente mensajes procesados. Repetir la confirmación es seguro.",
        "inputSchema": AckMessagesArguments.model_json_schema(),
    },
    {
        "name": "reply_message",
        "description": "Responder al mensaje original conservando conversación. acknowledge=true confirma el original al guardar la respuesta.",
        "inputSchema": ReplyMessageArguments.model_json_schema(),
    },
    {
        "name": "claim_task",
        "description": "Reclamar una tarea disponible en el bus.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID de la tarea (ej: T1)"},
                "agent_id": {"type": "string", "description": "ID del agente que reclama"},
            },
            "required": ["task_id", "agent_id"],
        },
    },
    {
        "name": "complete_task",
        "description": "Marcar una tarea como completada (done).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID de la tarea (ej: T1)"},
                "agent_id": {"type": "string", "description": "ID del agente"},
            },
            "required": ["task_id", "agent_id"],
        },
    },
    {
        "name": "acquire_lock",
        "description": "Bloquear un archivo antes de modificarlo para evitar colisiones.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Ruta del archivo"},
                "agent_id": {"type": "string", "description": "ID del agente"},
                "reason": {"type": "string", "description": "Motivo del bloqueo"},
            },
            "required": ["file_path", "agent_id"],
        },
    },
    {
        "name": "release_lock",
        "description": "Liberar el bloqueo de un archivo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Ruta del archivo"},
                "agent_id": {"type": "string", "description": "ID del agente"},
            },
            "required": ["file_path", "agent_id"],
        },
    },
    {
        "name": "get_project_status",
        "description": "Obtener el resumen global del estado del bus, tareas y agentes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "record_decision",
        "description": "Registrar una decisión arquitectónica (ADR) compartida con el equipo.",
        "inputSchema": DecisionToolArguments.model_json_schema(),
    },
]


class McpServer:
    def __init__(self, bus_url: str = "http://127.0.0.1:8420", agent_id: str | None = None) -> None:
        self.bus_url = bus_url.rstrip("/")
        self._event_cursors: dict[str, str] = {}
        self.session = None
        if os.environ.get("AGENT_BUS_ALLOW_UNSIGNED") != "1" or os.environ.get("AGENT_BUS_SESSION_FILE"):
            self.session = load_session(agent_id)
        self.agent_id = self.session["agent_id"] if self.session else agent_id
        self.tools = copy.deepcopy(TOOLS_DEFINITIONS)
        if self.session:
            for tool in self.tools:
                schema = tool["inputSchema"]
                for field in ("agent_id", "from_agent", "decided_by"):
                    schema.get("properties", {}).pop(field, None)
                    if field in schema.get("required", []):
                        schema["required"].remove(field)

        self._validators = {}
        for tool in self.tools:
            schema = tool["inputSchema"]
            schema["additionalProperties"] = False
            Draft202012Validator.check_schema(schema)
            self._validators[tool["name"]] = Draft202012Validator(schema)

    def sdk_server(self) -> Server:
        """Keep application contracts while the SDK owns RPC, negotiation and cancellation."""
        return Server(
            SERVER_NAME, version=SERVER_VERSION,
            on_list_tools=self._list_tools, on_call_tool=self._call_tool,
        )

    async def _list_tools(
        self, context: ServerRequestContext, params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(tools=[Tool.model_validate(tool) for tool in self.tools])

    @staticmethod
    def _tool_result(value: dict[str, Any], *, error: bool = False) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
            structured_content=value, is_error=error,
        )

    async def _call_tool(
        self, context: ServerRequestContext, params: CallToolRequestParams,
    ) -> CallToolResult:
        validator = self._validators.get(params.name)
        if validator is None:
            raise MCPError(INVALID_PARAMS, f"Unknown tool: {params.name}")
        arguments = params.arguments if params.arguments is not None else {}
        try:
            # The low-level SDK preserves schemas but deliberately leaves tool
            # validation to the application. Validate BEFORE binding identity.
            validator.validate(arguments)
            result = await self.execute_tool(params.name, arguments)
            return self._tool_result(result, error=result.get("status") == "error")
        except SchemaValidationError as exc:
            failure = {"code": "invalid_arguments", "error": exc.message}
        except AuthenticationError as exc:
            failure = {"code": "unauthenticated", "error": str(exc)}
        except ValueError as exc:
            failure = {"code": "invalid_arguments", "error": str(exc)}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            failure = {"code": "hub_error", "http_status": status,
                       "error": f"Hub rejected the operation (HTTP {status})"}
        except httpx.TransportError:
            failure = {"code": "bus_unavailable", "error": "Cannot complete the hub request"}
        except Exception:
            # Never put credentials, backend bodies or tracebacks on the wire.
            failure = {"code": "internal_error", "error": "Internal tool error"}
        # Cancellation is a BaseException and deliberately propagates to the SDK.
        logger.warning("Tool %s failed: %s", params.name, failure["code"])
        return self._tool_result({"status": "error", **failure}, error=True)

    def _client(self, timeout: float | None = 30.0):
        return async_bus_client(
            self.agent_id, session=self.session, base_url=self.bus_url, timeout=timeout,
        )

    def _bind_identity(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.session:
            return dict(args)
        bound = dict(args)
        for field in ("agent_id", "from_agent", "decided_by"):
            if field in bound and bound[field] != self.agent_id:
                raise ValueError(f"{field} must match the authenticated MCP session")
            bound[field] = self.agent_id
        return bound

    async def execute_tool(self, name: str, args: dict[str, Any]) -> Any:
        args = self._bind_identity(args)
        if name == "wait_for_updates":
            wait = WaitArguments.model_validate(args)
            return await self._wait_for_updates(wait.agent_id, wait.timeout, wait.event_cursor)
        async with self._client() as client:
            if name == "post_message":
                message = PostMessageArguments.model_validate(args)
                payload = {
                    "from_agent": message.from_agent,
                    "to_agent": message.to_agent,
                    "message_type": "inbox",
                    "body": {"text": message.text},
                    "idempotency_key": message.idempotency_key,
                    "reply_needed": message.reply_needed,
                    "related_task": message.related_task,
                }
                resp = await client.post("/messages", json=payload)
                resp.raise_for_status()
                return resp.json()

            elif name == "read_messages":
                page = ReadMessagesArguments.model_validate(args)
                params = page.model_dump(exclude={"agent_id"}, exclude_none=True)
                resp = await client.get(f"/inbox/{page.agent_id}/messages", params=params)
                resp.raise_for_status()
                return resp.json()

            elif name == "ack_messages":
                ack = AckMessagesArguments.model_validate(args)
                resp = await client.post(f"/inbox/{ack.agent_id}/ack", json={"message_ids": ack.message_ids})
                resp.raise_for_status()
                return resp.json()

            elif name == "reply_message":
                reply = ReplyMessageArguments.model_validate(args)
                payload = {
                    "body": {"text": reply.text}, "idempotency_key": reply.idempotency_key,
                    "reply_needed": reply.reply_needed, "acknowledge": reply.acknowledge,
                }
                resp = await client.post(f"/inbox/{reply.agent_id}/{reply.message_id}/reply", json=payload)
                resp.raise_for_status()
                return resp.json()

            elif name == "claim_task":
                task_id = args["task_id"]
                agent_id = args["agent_id"]
                resp = await client.post(f"/tasks/{task_id}/claim", json={"agent_id": agent_id})
                resp.raise_for_status()
                return resp.json()

            elif name == "complete_task":
                task_id = args["task_id"]
                agent_id = args["agent_id"]
                resp = await client.post(f"/tasks/{task_id}/done", json={"agent_id": agent_id})
                resp.raise_for_status()
                return resp.json()

            elif name == "acquire_lock":
                payload = {
                    "file_path": args["file_path"],
                    "agent_id": args["agent_id"],
                    "reason": args.get("reason"),
                }
                resp = await client.post("/locks/acquire", json=payload)
                resp.raise_for_status()
                return resp.json()

            elif name == "release_lock":
                payload = {
                    "file_path": args["file_path"],
                    "agent_id": args["agent_id"],
                }
                resp = await client.post("/locks/release", json=payload)
                resp.raise_for_status()
                return resp.json()

            elif name == "get_project_status":
                async def get_json(path):
                    response = await client.get(path)
                    response.raise_for_status()
                    return response.json()

                status = await get_json("/status")
                tasks = await get_json("/tasks")
                locks = await get_json("/locks")
                agents = await get_json("/agents")
                return {
                    "server": status,
                    "tasks": tasks,
                    "locks": locks,
                    "agents": agents,
                }

            elif name == "record_decision":
                decision = DecisionToolArguments.model_validate(args)
                payload = {
                    "decision_id": str(uuid4()),
                    "title": decision.title,
                    "decision": decision.what,
                    "decided_by": decision.decided_by,
                    "context": decision.context,
                }
                resp = await client.post("/decisions", json=payload)
                resp.raise_for_status()
                return resp.json()

            else:
                raise ValueError(f"Unknown tool: {name}")

    async def _wait_for_updates(self, agent_id: str, timeout: int, event_cursor: str | None = None) -> dict[str, Any]:
        """Capture a durable cursor before reading pending messages, within one deadline."""
        wait = WaitArguments(agent_id=agent_id, timeout=timeout, event_cursor=event_cursor)
        if self.session and agent_id != self.agent_id:
            raise ValueError("agent_id must match the authenticated MCP session")
        cursor = wait.event_cursor or self._event_cursors.get(agent_id)
        phase = "checkpoint"

        def remember(value):
            nonlocal cursor
            if not isinstance(value, str) or not value or len(value) > 2048:
                raise ValueError("Hub returned no valid event cursor")
            cursor = value
            self._event_cursors[agent_id] = value

        def error(code, message, **extra):
            return {"status": "error", "code": code, "error": message,
                    "event_cursor": cursor, **extra}

        def pending(page, *, event=None):
            result = {
                "status": "pending_messages" if event is None else "event_received",
                "count": len(page["messages"]), "messages": page["messages"],
                "next_cursor": page.get("next_cursor"), "event_cursor": cursor,
                "hint": "Procesa y confirma con ack_messages; next_cursor continúa el inbox, event_cursor reanuda eventos.",
            }
            if event is not None:
                result["event"] = event
            return result

        try:
            # An HTTP read timeout restarts after each received chunk. This
            # deadline also bounds lookup, connection, comments and checkpoints.
            async with asyncio.timeout(wait.timeout):
                async with self._client(timeout=None) as client:
                    response = await client.get(
                        f"/inbox/{agent_id}/events/cursor", params={"cursor": cursor} if cursor else {},
                    )
                    response.raise_for_status()
                    remember(response.json()["cursor"])

                    async def read_page():
                        response = await client.get(f"/inbox/{agent_id}/messages", params={"limit": 5})
                        response.raise_for_status()
                        page = response.json()
                        if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
                            raise ValueError("Hub returned no inbox page")
                        return page

                    phase = "inbox"
                    page = await read_page()
                    if page["messages"]:
                        return pending(page)
                    phase = "stream"
                    async with client.stream(
                        "GET", f"/inbox/{agent_id}/events", headers={"Last-Event-ID": cursor},
                    ) as response:
                        if response.is_error:
                            await response.aread()
                        response.raise_for_status()
                        async for frame in iter_sse_frames(response.aiter_lines()):
                            kind = frame["event"]
                            if kind == "checkpoint":
                                remember(frame.get("id") or json.loads(frame["data"])["cursor"])
                                continue
                            if kind == "reset":
                                control = json.loads(frame["data"])
                                if not isinstance(control, dict) or control.get("error") != "cursor_expired":
                                    raise ValueError("Hub returned an invalid reset event")
                                remember(control["cursor"])
                                return error("cursor_expired", "Event cursor expired; recover pending messages",
                                             recovery="read_messages")
                            if kind != "message":
                                continue
                            event = json.loads(frame["data"])
                            if not isinstance(event, dict):
                                raise ValueError("Hub returned an invalid event")
                            # Events are hints; an archived historical delivery
                            # must not cause a new response loop on reconnection.
                            page = await read_page()
                            remember(frame["id"])
                            if page["messages"]:
                                return pending(page, event=event if any(
                                    item.get("message_id") == event.get("message_id")
                                    for item in page["messages"]
                                ) else None)
                    return error("stream_closed", "Event stream closed before the deadline")
        except TimeoutError:
            return {"status": "timeout", "message": f"No pending updates within the total {timeout}s deadline",
                    "event_cursor": cursor}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 410:
                try:
                    remember(exc.response.json()["cursor"])
                except (ValueError, KeyError):
                    return error("protocol_error", "Expired-cursor response has no recovery cursor")
                return error("cursor_expired", "Event cursor expired; recover pending messages",
                             recovery="read_messages")
            code = {401: "unauthenticated", 403: "forbidden", 422: "cursor_invalid"}.get(status, "hub_error")
            return error(code, f"Hub rejected the {phase} request (HTTP {status})")
        except AuthenticationError as exc:
            return error("unauthenticated", str(exc))
        except httpx.ConnectError:
            return error("bus_unavailable", "Cannot connect to the hub")
        except httpx.TransportError as exc:
            return error("stream_closed" if phase == "stream" else "bus_unavailable", str(exc))
        except (ValueError, KeyError, TypeError) as exc:
            return error("protocol_error", str(exc))


async def run_mcp_server(bus_url: str = "http://127.0.0.1:8420", agent_id: str | None = None) -> None:
    """Run one SDK connection; EOF and cancellation release its in-flight requests."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    server = McpServer(bus_url=bus_url, agent_id=agent_id).sdk_server()
    try:
        async with cancellable_stdio() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    except* (BrokenPipeError, ConnectionResetError):
        logger.warning("MCP stdio output closed")


if __name__ == "__main__":
    asyncio.run(run_mcp_server())
