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

## 2. Servidor MCP nativo (MCP-1, en progreso)

Tool `wait_for_updates`: long-poll bloqueante hasta 120s. La sesión del agente
la llama y queda esperando ahí; al llegar un mensaje/tarea, la tool lo devuelve
y el agente lo procesa EN SU MISMA SESIÓN (contexto completo, visible).

Patrón de uso en la sesión del agente:

```
Usuario> revisa el bus y quédate atento
Agente  > llama bus_wait_for_updates(agent="agy")   # bloquea hasta 2 min
         ... llega mensaje ...
Agente  > procesa y responde con bus_post_message(...)
Agente  > llama bus_wait_for_updates() de nuevo      # ciclo
```

## 3. Antigravity / AGY y Codex

- AGY: su runner nativo ya se reactiva cuando un subproceso de fondo termina —
  usar el waiter MCP como ese subproceso.
- Codex/Aider: instrucción de protocolo en su archivo CODEX.md: al terminar
  cualquier tarea, ejecutar `agent-bus work inbox` antes de ceder el control.

## Decisiones relacionadas

- Opción B (tmux send-keys) descartada: reinjecta en TUI pero requiere tmux.
- A2A (Linux Foundation): backlog — capa de interoperabilidad para agentes
  externos, NO para el problema local. Ver análisis en el bus.
