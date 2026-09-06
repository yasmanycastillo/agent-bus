# Comunicación entre agentes: MCP + hooks

Cómo cada CLI se entera de que le escribieron por agent-bus sin que un humano
retransmita terminal por terminal.

## El problema resuelto

Una sesión interactiva (TUI) queda bloqueada esperando el teclado del humano.
Ni el SSE del bus ni un watcher externo pueden inyectarle un turno: cualquier
subproceso que se lance corre EN PARALELO, invisible para la terminal viva.

Solución elegida (Opción C): el propio agente consulta el bus como parte de su
ciclo natural, vía tools MCP y hooks del CLI.

## Credenciales antes de conectar

Provisionar una sesión por agente mediante `agent-bus auth create` como operador local. Seguir [identidad y autorización](authentication.md) para seleccionar proyecto, base y archivo de sesión. El servidor rechaza por defecto conexiones sin credenciales; registrar un nombre no crea confianza.

El proceso MCP fija la sesión al arrancar y deriva de ella los actores de sus herramientas. Cambiar `from_agent`, `agent_id` o `decided_by` en un argumento no permite actuar como otra identidad.

## 1. Hook Stop de Claude Code (`hooks/stop-check-inbox.sh`)

Configurado en `.claude/settings.json`. Cuando la sesión de Claude Code queda
idle (fin de turno), el hook consulta `/inbox/{agent}/`; si hay mensajes con
`reply_needed`, devuelve `{"decision": "block", "reason": "..."}` — Claude Code
procesa el reason como estímulo y la sesión continúa sola: lee el inbox y
responde por el bus.

- Sin pendientes: salida vacía, la sesión duerme normal.
- Bus caído o credencial inválida: no bloquea (fail-open). El hook es una ayuda al ciclo de sesión, no una garantía de entrega o ejecución.
- Variables: `AGENT_BUS_URL` (opcional; hereda la configuración del proyecto),
  `AGENT_BUS_AGENT_ID` (o identidad seleccionada en la configuración/sesión), `AGENT_BUS_CONFIG_DIR`,
  `AGENT_BUS_PROJECT_ROOT`, `AGENT_BUS_PROJECT_ID` y `AGENT_BUS_SESSION_FILE`. El hook usa el cliente autenticado común.
- Runtime: usa `agent-bus` instalado en PATH o `uv run --no-sync` sobre el repositorio del hook. Si se copia a otro proyecto o se ejecuta desde un worktree sin entorno instalado, definir `AGENT_BUS_PACKAGE_DIR` con la ruta de la instalación preparada.

## 2. Servidor MCP nativo (`agent-bus mcp-server`)

Implementado en `src/agent_bus/mcp/server.py` con el SDK oficial Python `mcp==2.1.1`; dependencias exactas en `uv.lock`. El SDK gestiona stdio JSON-RPC, negociación, solicitudes concurrentes y cancelación. Herramientas:
`wait_for_updates` (long-poll bloqueante: chequea pendientes o conecta al SSE
`/inbox/{id}/events`), `post_message`, `read_messages`, `claim_task`, `complete_task`,
`acquire_lock`, `release_lock`, `get_project_status`, `record_decision`,
`ack_messages` y `reply_message`.

En T-08, `read_messages` devuelve `{messages, next_cursor}`. `post_message` y
`reply_message` requieren una clave de idempotencia que se conserva al reintentar.
Leer no confirma; usar `ack_messages` o `reply_message(..., acknowledge=true)`
tras procesar el mensaje. Ver [el contrato y ejemplos](messaging.md).

La sesión del agente la llama y queda esperando ahí; al llegar un mensaje/tarea,
la tool lo devuelve y el agente lo procesa EN SU MISMA SESIÓN (contexto
completo, visible en terminal).

### Instalación y contrato del transporte

Preparar el entorno antes de conectar el cliente: `uv sync --locked --extra dev`. Arrancar con `uv run --locked agent-bus mcp-server --agent <id> --bus-url <url>`. `--bus-url` tiene prioridad sobre `AGENT_BUS_URL`; después se consulta la configuración del proyecto y finalmente `http://127.0.0.1:8420`. Las credenciales se cargan una vez al iniciar; no se autoinicia el hub desde este comando.

Se eligió el [servidor de bajo nivel del SDK oficial](https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/) para conservar los schemas y resultados del bus. Esa API deja la validación de argumentos a la aplicación: aquí se valida JSON Schema 2020-12 antes de vincular la identidad. Los schemas seguros excluyen actores y rechazan propiedades adicionales, incluso un actor aportado con el mismo nombre de la sesión. Los clientes deben actualizar su catálogo mediante `tools/list`.

Los resultados mantienen `content` textual con JSON y añaden el mismo objeto en `structuredContent`. Un fallo de validación o de operación devuelve `CallToolResult` con `isError: true`; el objeto incluye `status: error`, `code` y `error`. Una espera agotada (`status: timeout`) es un resultado normal. Herramienta inexistente es error JSON-RPC `-32602`; método inexistente es `-32601`. JSON malformado recibe `-32700` y un sobre JSON-RPC inválido recibe `-32600`, ambos con ID nulo; la conexión puede continuar. Los errores del hub no exponen credenciales ni su cuerpo de respuesta. `McpServer.handle_request` y el bucle JSON-RPC artesanal se eliminan: los consumidores Python usan `Client(server.sdk_server())` o el proceso stdio.

Mientras `wait_for_updates` está activo, la conexión puede atender otra herramienta o solicitud de protocolo. `notifications/cancelled` cancela el ID solicitado; no confirma mensajes ni revierte una mutación que ya se haya guardado. Para reintentar una mutación cuyo resultado se perdió, conservar la clave de idempotencia y consultar el estado. Los logs van a stderr; stdout se reserva al protocolo. La entrada limita cada línea JSON-RPC a 4 MiB. Una línea superior al límite cierra la conexión; no se ejecuta parcialmente.

El SDK recibe tuberías asincrónicas de `src/agent_bus/mcp/transport.py`: su lectura/escritura por archivos en threads podía bloquear el cierre con stdout roto y stdin abierto. La capa local permite cancelar la E/S, transforma fallos de validación del SDK en respuestas de protocolo y mantiene descriptores privados para que impresiones accidentales vayan a stderr. La validación de procesos de esta tanda se ejecuta en Linux; no acredita por sí sola compatibilidad de las tuberías en Windows.

La versión de protocolo se negocia mediante el SDK; no hay constante local que finja compatibilidad. El SDK 2.x admite tanto el handshake de revisiones anteriores como la conexión moderna por solicitudes; ver [versiones del protocolo](https://py.sdk.modelcontextprotocol.io/protocol-versions/). La versión del paquete queda fijada para que una actualización del SDK requiera repetir las pruebas.

### Conectar Claude Code

```bash
export AGENT_BUS_AGENT_ID=claude
export AGENT_BUS_SESSION_FILE="$AGENT_BUS_CONFIG_DIR/credentials/claude.json"
claude mcp add agent-bus -- uv run agent-bus mcp-server
# desde el directorio del proyecto (necesita uv + el paquete instalado)
```

En la sesión: "conéctate al bus como agente claude y espera novedades con
wait_for_updates" → la tool bloquea hasta 120s → llega mensaje → responde con
post_message → vuelve a llamar wait_for_updates.

### Conectar Claude Desktop / otros clientes MCP

Config JSON del cliente (stdio):

```json
{
  "mcpServers": {
    "agent-bus": {
      "command": "uv",
      "args": ["--project", "/ruta/agent-bus", "run", "agent-bus", "mcp-server"],
      "env": {
        "AGENT_BUS_CONFIG_DIR": "/ruta/proyecto/.agent-bus/runtime",
        "AGENT_BUS_PROJECT_ID": "mi-proyecto",
        "AGENT_BUS_AGENT_ID": "claude",
        "AGENT_BUS_SESSION_FILE": "/ruta/proyecto/.agent-bus/runtime/credentials/claude.json"
      }
    }
  }
}
```

### Conectar AGY / Antigravity

Si el cliente soporta MCP stdio, usar el mismo patrón de configuración. Arrancar `agent-bus mcp-server` por sí solo no consulta el inbox ni espera novedades: necesita un cliente que envíe las solicitudes del protocolo. La conexión de AGY/Antigravity concreto sigue pendiente de aceptación en T-13.

## 3. Antigravity / AGY y Codex

La reactivación de una sesión interactiva depende del cliente. Las pruebas del transporte no demuestran que una TUI pueda recibir un turno espontáneo. Los hooks y la consulta explícita de `agent-bus work inbox` siguen siendo mecanismos complementarios que deben verificarse por cliente.

## Alcance verificado

Las pruebas de autenticación ejercitan clientes MCP en proceso contra un hub HTTP real y efímero, además de CLI, HTTP, SSE y WebSocket. T-09 implementa [eventos recuperables y espera con plazo total](events.md). T-10 verifica procesos stdio reales contra un hub efímero: SDK 2.1.1 en modo moderno (`2026-07-28`), handshake `2024-11-05` y `2025-06-18`, herramientas, cancelación y liberación de SSE, EOF, stdout roto y salida saturada. La aceptación con dos aplicaciones MCP externas sigue en T-13. Los mecanismos descritos de activación deben validarse en cada cliente; no equivalen a una prueba de interoperabilidad universal.

## Decisiones relacionadas

- Opción B (tmux send-keys) descartada: reinjecta en TUI pero requiere tmux.
- A2A (Linux Foundation): backlog — capa de interoperabilidad para agentes
  externos, NO para el problema local. Ver análisis en el bus.

El proyecto y la URL también se fijan al arrancar MCP. Para varias sesiones del mismo proveedor y resolución desde worktrees, seguir [proyectos y sesiones](projects.md).
