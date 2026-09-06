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
- Variables: `AGENT_BUS_URL` (default `http://localhost:8420`),
  `AGENT_BUS_AGENT_ID` (o identidad seleccionada en la configuración/sesión), `AGENT_BUS_CONFIG_DIR`,
  `AGENT_BUS_PROJECT_ID` y `AGENT_BUS_SESSION_FILE`. El hook usa el cliente autenticado común.
- Runtime: usa `agent-bus` instalado en PATH o `uv run --no-sync` sobre el repositorio del hook. Si se copia a otro proyecto o se ejecuta desde un worktree sin entorno instalado, definir `AGENT_BUS_PACKAGE_DIR` con la ruta de la instalación preparada.

## 2. Servidor MCP nativo (`agent-bus mcp-server`)

Implementado en `src/agent_bus/mcp/server.py` — stdio JSON-RPC 2.0. Tools:
`wait_for_updates` (long-poll bloqueante: chequea pendientes o conecta al SSE
`/events/{id}`), `post_message`, `read_messages`, `claim_task`, `complete_task`,
`acquire_lock`, `release_lock`, `get_project_status`, `record_decision`.

La sesión del agente la llama y queda esperando ahí; al llegar un mensaje/tarea,
la tool lo devuelve y el agente lo procesa EN SU MISMA SESIÓN (contexto
completo, visible en terminal).

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

Si el cliente soporta MCP stdio, mismo patrón. Si no: su runner nativo se
reactiva cuando el subproceso waiter termina — lanzar `agent-bus mcp-server`
como ese subproceso y procesar lo que devuelva.

## 3. Antigravity / AGY y Codex

- AGY: su runner nativo ya se reactiva cuando un subproceso de fondo termina —
  usar el waiter MCP como ese subproceso.
- Codex/Aider: instrucción de protocolo en su archivo CODEX.md: al terminar
  cualquier tarea, ejecutar `agent-bus work inbox` antes de ceder el control.

## Alcance verificado

Las pruebas de autenticación ejercitan clientes MCP en proceso contra un hub HTTP real y efímero, además de CLI, HTTP, SSE y WebSocket. La negociación stdio con dos clientes MCP externos, la espera cancelable y la recuperación de eventos siguen en T-09/T-10/T-13. Los mecanismos descritos de activación deben validarse en cada cliente; no equivalen a una prueba de interoperabilidad universal.

## Decisiones relacionadas

- Opción B (tmux send-keys) descartada: reinjecta en TUI pero requiere tmux.
- A2A (Linux Foundation): backlog — capa de interoperabilidad para agentes
  externos, NO para el problema local. Ver análisis en el bus.
