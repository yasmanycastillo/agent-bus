# Conectar un cliente MCP

Para dejar el servidor instalado en un cliente, usa [Instalar como MCP](../README.md#instalar-como-mcp). Esta página explica la conexión cuando hace falta prepararla a mano.

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

## Instalación automática en clientes

Puedes configurar directamente tus clientes sin copiar ni editar JSON manualmente:

```sh
# Instalar en Cursor, Claude, Gemini, Codex, Grok, Hermes y AGY:
agent-bus mcp install --client all

# Instalar para un cliente o agente concreto:
agent-bus mcp install --client cursor,claude --agent backend

# Instalar en el ámbito global del usuario (~/.cursor, ~/.config/Claude, ~/.hermes, etc.):
agent-bus mcp install --scope global --client agy --agent agy
agent-bus mcp install --scope global --client hermes --agent hermes

# Simular cambios sin escribir en disco:
agent-bus mcp install --client all --dry-run
```

La instalación conserva los demás servidores. Codex escribe solo la tabla `[mcp_servers.agent-bus]` de `~/.codex/config.toml`. AGY escribe en `~/.gemini/config/mcp_config.json`. Hermes escribe solo el bloque `agent-bus` de `~/.hermes/config.yaml`. El ámbito global no fija un proyecto ni un puerto.

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

## Espera, listeners y workers

`wait_for_updates` sólo espera mientras la llamada MCP permanece activa. MCP no
inicia procesos en segundo plano, no mantiene una TUI despierta y `mcp-server` no
es un worker. Si el agente debe responder aunque Claude/Grok esté en reposo,
arranca un worker persistente desde una terminal con la misma identidad, proyecto,
URL y credencial:

```sh
agent-bus --project /ruta/mi-proyecto worker start --agent grok --provider grok
agent-bus --project /ruta/mi-proyecto worker status --agent grok
```

Mantén ese proceso bajo un supervisor (por ejemplo `systemd --user`, `tmux` o un
servicio equivalente). `worker status` comprueba el proceso local y su PID; para
confirmar presencia en el hub consulta `agent-bus show agents` o la consola y
verifica el heartbeat. No ejecutes `watch` y `worker` para
la misma identidad: ambos son ejecutores y la exclusión local rechazará el segundo.
Una TUI abierta puede servir para supervisión manual, pero no sustituye al worker.
Para una respuesta automática sin worker usa `watch`; es headless y tampoco inyecta
texto en la TUI. La configuración completa está en
[workers y recuperación](first-run.md#workers-persistentes-para-evitar-esperas).

El repositorio no activa hooks de Claude por defecto. Si deseas habilitarlos,
usa [el ejemplo de configuración](../examples/claude/settings.json): incorpora su
entrada `hooks.Stop` a tu `.claude/settings.json` local, preservando las entradas
que ya tengas. El ejemplo supone que ejecutas Claude desde la raíz de este
repositorio. Para otro proyecto, usa la ruta absoluta del script y configura
`AGENT_BUS_PACKAGE_DIR` si no hay un `agent-bus` instalado en PATH.

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
