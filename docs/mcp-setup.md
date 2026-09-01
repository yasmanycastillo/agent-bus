# Comunicación entre agentes: MCP + hooks

Cómo cada CLI se entera de que le escribieron por agent-bus sin que un humano
retransmita terminal por terminal.

## El problema resuelto

Una sesión interactiva (TUI) queda bloqueada esperando el teclado del humano.
Ni el SSE del bus ni un watcher externo pueden inyectarle un turno: cualquier
subproceso que se lance corre EN PARALELO, invisible para la terminal viva.

Solución elegida (Opción C): el propio agente consulta el bus como parte de su
ciclo natural, vía tools MCP y hooks del CLI.

## 1. Hook Stop de Claude Code (`hooks/stop-check-inbox.sh`)

Configurado en `.claude/settings.json`. Cuando la sesión de Claude Code queda
idle (fin de turno), el hook consulta `/inbox/{agent}/`; si hay mensajes con
`reply_needed`, devuelve `{"decision": "block", "reason": "..."}` — Claude Code
procesa el reason como estímulo y la sesión continúa sola: lee el inbox y
responde por el bus.

- Sin pendientes: salida vacía, la sesión duerme normal.
- Bus caído: no bloquea (fail-open).
- Variables: `AGENT_BUS_URL` (default `http://localhost:8420`),
  `AGENT_BUS_AGENT_ID` (default `claude`).

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
      "args": ["--project", "/ruta/al/proyecto", "run", "agent-bus", "mcp-server"]
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

## Decisiones relacionadas

- Opción B (tmux send-keys) descartada: reinjecta en TUI pero requiere tmux.
- A2A (Linux Foundation): backlog — capa de interoperabilidad para agentes
  externos, NO para el problema local. Ver análisis en el bus.
