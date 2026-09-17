# Changelog

## Unreleased

- Operaciones de inbox, tareas y revisiones ahora usan transacciones SQLite
  independientes con rollback y cierre deterministas bajo cancelación.
- Autorización reforzada para mutaciones de tareas y revisiones; la identidad
  persistida del review es autoritativa durante la integración.
- El integrador prueba y publica el SHA fijado, verifica movimientos de refs y
  deja la rama objetivo intacta cuando falla el gate o la publicación.
- Bases SQLite en disco se abren con permisos privados y descriptores anclados;
  IDs usados en rutas se validan antes de cualquier acceso al filesystem.
- CI con acciones fijadas por SHA, dependencias bloqueadas, Ruff global,
  suite completa, build y gate incremental de deuda mypy.
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
