"""Servidor MCP nativo de agent-bus sobre stdio JSON-RPC 2.0.

Permite a cualquier cliente MCP (Claude Code, Claude Desktop, Antigravity, Cursor, Zed)
interactuar directamente con el bus y usar la herramienta bloqueante `wait_for_updates`
para recibir eventos SSE en su misma sesión de forma nativa y sin intermediarios.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

import httpx

logger = logging.getLogger("agent_bus.mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "agent-bus"
SERVER_VERSION = "0.1.0"

TOOLS_DEFINITIONS = [
    {
        "name": "wait_for_updates",
        "description": "Bloquea la ejecución hasta recibir un nuevo mensaje, tarea o evento del bus por SSE (patrón long-polling reactivo).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Identificador del agente que espera eventos (ej: claude, antigravity)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Tiempo máximo en segundos a esperar antes de retornar timeout (default: 120)",
                    "default": 120,
                },
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "post_message",
        "description": "Enviar un mensaje directo a otro agente a través del bus.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_agent": {"type": "string", "description": "Remitente"},
                "to_agent": {"type": "string", "description": "Destinatario"},
                "text": {"type": "string", "description": "Contenido del mensaje"},
                "reply_needed": {
                    "type": "boolean",
                    "description": "Si el mensaje requiere respuesta obligatoria",
                    "default": False,
                },
                "related_task": {
                    "type": "string",
                    "description": "ID de la tarea relacionada (opcional)",
                },
            },
            "required": ["from_agent", "to_agent", "text"],
        },
    },
    {
        "name": "read_messages",
        "description": "Leer los mensajes pendientes o recientes del inbox de un agente.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "ID del agente"},
            },
            "required": ["agent_id"],
        },
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
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Título de la decisión"},
                "what": {"type": "string", "description": "Descripción de la decisión"},
                "decided_by": {"type": "string", "description": "Agente responsable"},
                "context": {"type": "string", "description": "Contexto adicional"},
            },
            "required": ["title", "what", "decided_by"],
        },
    },
]


class McpServer:
    def __init__(self, bus_url: str = "http://localhost:8420") -> None:
        self.bus_url = bus_url.rstrip("/")

    async def handle_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }

        elif method == "notifications/initialized":
            return None

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS_DEFINITIONS},
            }

        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            try:
                res = await self.execute_tool(tool_name, tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}]
                    },
                }
            except Exception as exc:
                logger.error("Error executing tool %s: %s", tool_name, exc)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(exc)},
                }

        elif method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    async def execute_tool(self, name: str, args: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(base_url=self.bus_url, timeout=30.0) as client:
            if name == "wait_for_updates":
                agent_id = args["agent_id"]
                timeout = int(args.get("timeout", 120))
                return await self._wait_for_updates(agent_id, timeout)

            elif name == "post_message":
                payload = {
                    "from_agent": args["from_agent"],
                    "to_agent": args["to_agent"],
                    "message_type": "inbox",
                    "body": {"text": args["text"]},
                    "reply_needed": bool(args.get("reply_needed", False)),
                    "related_task": args.get("related_task"),
                }
                resp = await client.post("/messages", json=payload)
                resp.raise_for_status()
                return resp.json()

            elif name == "read_messages":
                agent_id = args["agent_id"]
                resp = await client.get(f"/inbox/{agent_id}")
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
                status = (await client.get("/status")).json()
                tasks = (await client.get("/tasks")).json()
                locks = (await client.get("/locks")).json()
                agents = (await client.get("/agents")).json()
                return {
                    "server": status,
                    "tasks": tasks,
                    "locks": locks,
                    "agents": agents,
                }

            elif name == "record_decision":
                payload = {
                    "title": args["title"],
                    "what": args["what"],
                    "decided_by": args["decided_by"],
                    "context": args.get("context"),
                }
                resp = await client.post("/decisions", json=payload)
                resp.raise_for_status()
                return resp.json()

            else:
                raise ValueError(f"Unknown tool: {name}")

    async def _wait_for_updates(self, agent_id: str, timeout: int) -> dict[str, Any]:
        """Verifica inbox pendiente o espera un evento SSE del bus hasta el timeout."""
        # 1. Chequeo rápido de pendientes existentes
        async with httpx.AsyncClient(base_url=self.bus_url, timeout=10.0) as client:
            try:
                pending = (await client.get(f"/inbox/{agent_id}/pending")).json()
                if pending.get("count", 0) > 0:
                    # solo reply_needed (requieren acción) y acotado a 5 para
                    # no saturar el contexto de la sesión despertada
                    inbox = (await client.get(f"/inbox/{agent_id}")).json()
                    actionable = [
                        m for m in inbox if m.get("reply_needed")
                    ][:5] or inbox[-5:]
                    return {
                        "status": "pending_messages",
                        "count": pending["count"],
                        "total_in_inbox": len(inbox),
                        "messages": actionable,
                        "hint": "usa read_messages para ver el inbox completo",
                    }
            except Exception:
                pass

        # 2. Espera reactiva sobre el stream SSE
        try:
            async with httpx.AsyncClient(base_url=self.bus_url, timeout=timeout) as client:
                async with client.stream("GET", f"/events/{agent_id}") as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            raw_data = line[len("data:") :].strip()
                            try:
                                event = json.loads(raw_data)
                            except Exception:
                                event = {"raw": raw_data}
                            return {
                                "status": "event_received",
                                "event": event,
                            }
        except httpx.TimeoutException:
            return {"status": "timeout", "message": f"No events received within {timeout}s"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

        return {"status": "timeout", "message": "Stream closed"}


async def run_mcp_server(bus_url: str = "http://localhost:8420") -> None:
    """Corre el servidor MCP escuchando en stdin/stdout en formato JSON-RPC 2.0."""
    server = McpServer(bus_url=bus_url)
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            break
        text = line.decode().strip()
        if not text:
            continue
        try:
            req = json.loads(text)
            resp = await server.handle_request(req)
            if resp is not None:
                out = json.dumps(resp, ensure_ascii=False) + "\n"
                sys.stdout.write(out)
                sys.stdout.flush()
        except json.JSONDecodeError:
            pass
        except Exception as exc:
            logger.error("Error processing line: %s", exc)


if __name__ == "__main__":
    asyncio.run(run_mcp_server())
