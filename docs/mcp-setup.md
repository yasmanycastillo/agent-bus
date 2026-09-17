# Conectar un cliente MCP

El servidor MCP permite que un agente consulte pendientes, coordine archivos y
entregue trabajo al hub. Funciona por stdio: el cliente inicia un proceso
`agent-bus mcp-server` y se comunica con él mediante JSON-RPC.

## Preparar la conexión

1. Sigue [primer uso](first-run.md) para preparar el proyecto, el hub y una
   credencial por agente con `onboard --mcp-only`.
2. Abre el JSON que imprime el asistente en `.agent-bus/runtime/mcp/<agente>.json`.
3. Copia `command`, `args` y `env` a la configuración MCP de tu aplicación,
   conservando sus otras entradas. Si usa TOML u otro formato, adapta los campos;
   no pegues JSON directamente en ese archivo.
4. Reconecta el cliente y pide `bootstrap_agent({})`. Comprueba que devuelve el
   agente y proyecto esperados antes de reclamar tareas o reservar archivos.

El JSON generado fija intérprete, proyecto, base, URL y ruta de la credencial;
no contiene el token. Conserva el entorno Python al que apunta. El comando
`mcp-server` no arranca el hub ni provisiona credenciales.

Para una configuración manual, la forma del cliente JSON es:

```json
{
  "mcpServers": {
    "agent-bus": {
      "command": "/ruta/entorno/bin/agent-bus",
      "args": ["mcp-server"],
      "env": {
        "AGENT_BUS_PROJECT_ROOT": "/ruta/proyecto",
        "AGENT_BUS_CONFIG_DIR": "/ruta/proyecto/.agent-bus/runtime",
        "AGENT_BUS_PROJECT_ID": "ID_REAL_DEL_PROYECTO",
        "AGENT_BUS_AGENT_ID": "backend",
        "AGENT_BUS_SESSION_FILE": "/ruta/proyecto/.agent-bus/runtime/credentials/backend.json",
        "AGENT_BUS_URL": "http://127.0.0.1:8421"
      }
    }
  }
}
```

Usa los valores reales que generó onboarding. La instancia MCP fija la identidad
al arrancar; cambiar un argumento de una herramienta no permite suplantar a otro
agente. Al renovar la credencial, reconecta MCP para cargar la sesión nueva.
[Autenticación y permisos](authentication.md), [proyectos y sesiones](projects.md).

## Herramientas

| Uso | Herramientas |
|---|---|
| Inicio y contexto | `bootstrap_agent`, `get_agent_instructions`, `get_project_status` |
| Pendientes y espera | `my_pending_items`, `read_messages`, `wait_for_updates` |
| Comunicación | `post_message`, `reply_message`, `ack_messages` |
| Trabajo | `claim_task`, `complete_task`, `complete_handoff` |
| Reservas | `prepare_edit`, `acquire_lock`, `renew_lock`, `release_lock` |
| Decisiones | `record_decision` |

El servidor entrega instrucciones al conectar y cada herramienta describe sus
requisitos. El cliente debe presentarlas al modelo: conectar no ejecuta bootstrap
por su cuenta. El flujo de edición y entrega está en
[coordinación MCP](coordination-workflow.md).

Leer no confirma mensajes. Usa `ack_messages` después de procesarlos, o
`reply_message` con `acknowledge=true`. Conserva la misma clave de idempotencia
al repetir un envío cuyo resultado se perdió. [Contrato de mensajería](messaging.md).

## Espera y ejecución automática

`wait_for_updates` consulta pendientes o espera eventos, con un plazo total de
1 a 120 segundos. Puede devolver una llegada mientras la llamada está activa;
no inicia un cliente que ya terminó. Al vencer el plazo, el cliente decide si
vuelve a esperar. [Eventos y recuperación](events.md).

Para responder sin intervención del usuario, inicia un listener `watch` con su
propia identidad. Es un proceso headless separado de cualquier TUI abierta.
Consulta [listeners y estados](first-run.md#listeners-para-responder-automáticamente).

El hook opcional [stop-check-inbox.sh](../hooks/stop-check-inbox.sh) consulta
pendientes al finalizar un turno. Si hay una solicitud de respuesta, pide al
cliente que continúe. No es un listener permanente. Si el bus o la credencial
fallan, el hook deja terminar el turno. Usa `agent-bus` instalado en PATH o una
instalación preparada indicada por `AGENT_BUS_PACKAGE_DIR`.

## Transporte y errores

- `command` debe existir y poder importar el paquete. Si el cliente no descubre
  herramientas, comprueba la configuración, el entorno y el permiso del cliente
  para iniciar ese servidor.
- `401` indica sesión inválida, vencida o revocada; `403`, falta de permisos;
  `409`, conflicto de estado. Corrige la causa antes de volver a editar.
- Las herramientas devuelven `content` y `structuredContent`. Los fallos incluyen
  `isError=true`, `code` y orientación; un timeout de espera es un resultado normal.
- El SDK admite solicitudes concurrentes y cancelación. Cancelar no revierte una
  mutación ya guardada: conserva su clave y consulta el estado antes de reintentar.
- Los logs van a stderr; stdout está reservado al protocolo. La entrada limita
  cada línea JSON-RPC a 4 MiB y cierra la conexión si se supera ese límite.

Las versiones de dependencias están fijadas en `uv.lock`. Para actualizar el SDK
o comprobar el transporte en otra plataforma, sigue [desarrollo y pruebas](development.md).
