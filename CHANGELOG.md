# Changelog

## 0.2.0 — 2026-09-25

agent-bus coordina agentes que ya saben trabajar. No inventa el encargo ni lo ejecuta.

- El humano habla con un agente: Hermes, Grok, Claude, Codex o AGY. Ese agente confirma el texto y lo reparte por MCP.
- Quien revisa no es quien implementa. Hermes arma el plan; los demás programan o revisan.
- Instalar el MCP es un comando. No crea credenciales en este repositorio: la sesión nace en el proyecto que se coordina.
- Cada worker solo toma la tarea que tiene asignada. Un revisor no reclama implementación. Un mensaje agotado no bloquea la cola. El turno espera 1800 segundos.

- Integración nativa de Grok en `watch`, selección del proveedor desde la
  credencial y lanzadores por identidad generados durante onboarding.
- `watch --status` muestra el estado del ejecutor local. Las respuestas preparadas
  sobreviven a un reinicio y pueden reenviarse sin otra consulta al modelo.
- Demo aislada con dos actores programados y MCP real: reservas, conflicto,
  revisión, pruebas y aprobación o rechazo desde la terminal.
- Informe JSON opcional de la demo, con aprobación simulada explícita para CI.
- Consola en `/console`, también accesible desde `/room`: historial de tareas,
  solicitudes completas, filtros, reservas y recuperación de conexión.
- Asistente `onboard --mcp-only`: provisión local, verificación del hub y sesiones,
  y configuraciones por agente sin iniciar workers ni integración automática.
- Instrucciones al conectar MCP y orientación para resolver errores de herramientas.
- Reservas multiarquivo y handoff transaccional con evidencia declarada y ACK.
- Integración Git limitada al commit que pasó las pruebas y la revisión.
- Documentación organizada por uso, sin bitácoras internas de tareas ni planes antiguos.
