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


TOOL_GUIDANCE = {
    "bootstrap_agent": "Primera llamada al conectar: bootstrap_agent({}). Requiere una credencial provisionada. Después atiende my_pending_items.",
    "get_agent_instructions": "Requiere sesión provisionada. Para incorporarte al proyecto, continúa con bootstrap_agent({}).",
    "my_pending_items": "Empieza con bootstrap_agent. Después procesa pendientes y usa ack_messages o reply_message según corresponda.",
    "prepare_edit": "Antes: bootstrap_agent y una tarea propia. Edita sólo tras authorized=true; después renueva con renew_lock o entrega con complete_handoff.",
    "complete_handoff": "Antes: bootstrap_agent, tarea propia y tokens vigentes de los locks que liberas. Adjunta evidencia real; después consulta pendientes o espera respuesta.",
    "wait_for_updates": "Antes: bootstrap_agent y my_pending_items. Conserva event_cursor; al recibir trabajo, procésalo antes de confirmar.",
    "post_message": "Antes: bootstrap_agent. Conserva destinatario, contenido e idempotency_key al reintentar; si pides respuesta, continúa con wait_for_updates.",
    "read_messages": "Para entrar al proyecto usa bootstrap_agent. Lee todas las páginas; después confirma sólo lo procesado con ack_messages.",
    "ack_messages": "Requiere IDs de mensajes de tu inbox que ya procesaste. Obtén pendientes con my_pending_items después de bootstrap_agent.",
    "reply_message": "Requiere un mensaje de tu inbox obtenido con my_pending_items o read_messages. Si acknowledge=true, responde y confirma en una operación.",
    "claim_task": "Antes: bootstrap_agent y consulta de tareas disponibles. Requiere tarea libre y pendiente; tras reclamar, usa prepare_edit antes de editar.",
    "register_capabilities": "Declara capacidades de tu sesión. La aprobación del proyecto la hace un administrador. Después enruta con route_task.",
    "route_task": "Elige un agente elegible para una tarea libre. No reclama. Si el resultado no te incluye, no reclames esa tarea.",
    "publish_artifact": "Publica contenido acotado de una tarea. Guarda el artifact_id. No envíes un file:// ni un binario grande.",
    "list_task_artifacts": "Lista metadatos de los artefactos de una tarea. El contenido se pide aparte con get_artifact_metadata y la ruta de contenido.",
    "get_artifact_metadata": "Devuelve metadatos y el uri artifact://. No incluye la ruta local ni el cuerpo.",
    "complete_task": "Requiere una tarea propia en estado permitido. Para entregar evidencia, mensaje y locks juntos, prefiere complete_handoff; usa in_review si falta revisión.",
    "acquire_lock": "Antes: bootstrap_agent. Para varios archivos usa prepare_edit; después renueva con renew_lock o libera con release_lock usando el token recibido.",
    "release_lock": "Requiere el acquisition_id vigente de la misma sesión, ruta y scope. Si falla, consulta get_project_status y no liberes un lock ajeno.",
    "renew_lock": "Requiere el token vigente de la misma sesión. Si vence o falla la renovación, detén la edición y consulta get_project_status antes de adquirir de nuevo.",
    "get_project_status": "Para incorporarte al proyecto usa primero bootstrap_agent. Después selecciona una tarea disponible o consulta tus pendientes con my_pending_items.",
    "record_decision": "Antes: bootstrap_agent y revisión de decisiones recientes. Registra sólo un acuerdo alcanzado; después comunica el resultado si corresponde.",
    "record_verdict": "Sólo el dueño de la tarea de review. El SHA es el del intento de implementación. approve autoriza la integración. changes_requested reabre la implementación y deja la integración pendiente.",
}


def connection_instructions(authenticated: bool) -> str:
    if authenticated:
        return INSTRUCTIONS
    return (
        "agent-bus está en modo legacy sin sesión autenticada. Las operaciones compactas "
        "requieren una credencial propia del proyecto. Solicita al operador provisionarla "
        "con agent-bus auth create y configura el cliente según docs/authentication.md; "
        "no inventes identidades ni muestres tokens. Reconecta MCP y ejecuta bootstrap_agent({}) "
        "como primera llamada. Las herramientas legacy conservan sus argumentos de identidad."
    )


def recovery_hint(name: str, code: str, http_status: int | None = None) -> str:
    """Static guidance only: never include backend bodies, tokens or tracebacks."""
    if code in ("cursor_expired", "cursor_invalid"):
        return ("Recupera pendientes con read_messages sin cursor de paginación. Usa el event_cursor "
                "de recuperación si se devuelve; si no, obtén uno nuevo con my_pending_items antes de esperar.")
    if code == "unauthenticated" or http_status == 401:
        return ("Solicita al operador una credencial vigente del proyecto; configura su archivo "
                "en el cliente, reconecta MCP y ejecuta bootstrap_agent({}). No muestres el token.")
    if http_status == 403:
        return ("La sesión no tiene permiso para esta operación. Comprueba tu identidad/proyecto "
                "y trabaja sólo sobre recursos autorizados; no suplantes a otro agente.")
    if code == "bus_unavailable" or http_status == 503:
        return ("Comprueba que el hub del proyecto esté disponible. Reintenta con espera acotada "
                "y la misma clave y contenido; una respuesta perdida no demuestra que la operación no ocurrió.")
    if http_status == 409:
        if name in ("prepare_edit", "acquire_lock", "release_lock", "renew_lock"):
            return ("Detén la edición y consulta get_project_status. Verifica ruta, scope, sesión y "
                    "acquisition_id vigente. Repite exactamente una petición pendiente; una reserva "
                    "vencida requiere una nueva adquisición cuando el recurso esté disponible.")
        if name == "claim_task":
            return ("Actualiza las tareas con get_project_status: puede tener propietario o dependencias "
                    "pendientes. Reclama sólo trabajo disponible y autorizado.")
        return ("Consulta my_pending_items y get_project_status para verificar propietario, estado y "
                "resultado anterior. Conserva la clave y contenido de la operación original; no fuerces cambios.")
    if http_status == 404:
        return ("Comprueba que el hub incluya estas herramientas y que el recurso exista en este proyecto. "
                "Tras actualizar/reconectar, empieza con bootstrap_agent({}) y consulta get_project_status.")
    if code == "invalid_arguments" or http_status == 422:
        return ("Revisa el esquema y los requisitos de la herramienta; consulta get_agent_instructions({}). "
                "Usa bootstrap_agent({}) al entrar y conserva los IDs y tokens devueltos por el servidor.")
    return ("Consulta get_agent_instructions({}) y verifica el estado del hub antes de reintentar. "
            "Conserva claves y contenido; no declares completada una operación cuyo resultado desconoces.")


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
