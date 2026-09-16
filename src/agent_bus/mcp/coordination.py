"""Compact MCP facade over the authenticated hub coordination workflows."""
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from agent_bus.core.coordination import (
    BootstrapRequest, HandoffRequest, INSTRUCTIONS, PrepareEditRequest,
)
from agent_bus.core.lock_paths import client_lock_path
from agent_bus.security import AuthenticationError


class PendingArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=100, strict=True)
    cursor: StrictStr | None = None
    task_offset: int = Field(default=0, ge=0, strict=True)


class InstructionsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


MODELS = {
    "bootstrap_agent": BootstrapRequest,
    "my_pending_items": PendingArguments,
    "prepare_edit": PrepareEditRequest,
    "complete_handoff": HandoffRequest,
    "get_agent_instructions": InstructionsArguments,
}
DESCRIPTIONS = {
    "bootstrap_agent": "Entrar con tu sesión existente y recibir instrucciones, pendientes, agentes y decisiones. No crea credenciales.",
    "my_pending_items": "Consultar mensajes y tareas propios sin confirmar. Pagina con next_cursor y next_task_offset.",
    "prepare_edit": "Reservar todos los archivos o ninguno. Conserva operation_key, acquisition_id y expires_at; reintentar no renueva locks.",
    "complete_handoff": "Entregar tarea, evidencia declarada, ACK y liberaciones en una transacción. in_review por defecto; conserva operation_key al reintentar.",
    "get_agent_instructions": "Obtener el protocolo de coordinación sin crear sesión ni cambiar estado.",
}
TOOLS = [{"name": name, "description": DESCRIPTIONS[name], "inputSchema": model.model_json_schema()}
         for name, model in MODELS.items()]


async def execute(server, name, args):
    if not server.session:
        raise AuthenticationError("Coordination workflows require a provisioned MCP session")
    payload = MODELS[name].model_validate(args).model_dump(exclude_none=True)
    if name == "get_agent_instructions":
        return {"instructions": INSTRUCTIONS, "agent_id": server.agent_id, "project_id": server.project_id}

    def path(value, scope):
        if scope == "project" and server._lock_project_root is None:
            raise ValueError("Project-scoped locks require a project root when the MCP starts")
        return client_lock_path(value, scope, cwd=server._lock_cwd, project_root=server._lock_project_root)

    if name == "prepare_edit":
        payload["paths"] = [path(value, payload["scope"]) for value in payload["paths"]]
    elif name == "complete_handoff":
        for lock in payload["release_locks"]:
            lock["file_path"] = path(lock["file_path"], lock["scope"])
    routes = {"bootstrap_agent": "bootstrap", "my_pending_items": "pending",
              "prepare_edit": "prepare-edit", "complete_handoff": "handoff"}
    async with server._client() as client:
        if name == "my_pending_items":
            response = await client.get("/coordination/pending", params=payload)
        else:
            response = await client.post("/coordination/" + routes[name], json=payload)
        response.raise_for_status()
        return response.json()
