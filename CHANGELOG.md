# Changelog

## Unreleased

- Consola única en `/console` y `/room`: detalle/historial de tareas, solicitudes
  completas, filtros, reservas y recuperación de conexión.
- Protección frente a respuestas de sesiones anteriores y cancelación del stream
  al cerrar sesión; prueba reproducible en Chromium.

- Asistente `onboard --mcp-only`: provisión local, verificación del hub y sesiones,
  y snippets JSON por agente sin iniciar workers ni integración automática.
- Plan de lanzamiento por etapas y guía de primer uso.
- Archivo de licencia MIT, coherente con la licencia anunciada en el README.

## Historial previo a la primera release pública

- `9fb8191`: instrucciones al conectar MCP, recuperación orientada por herramienta,
  actualización del contexto y roadmap; 719 pruebas aprobadas.
- `433a08a`: coordinación compacta, reservas multiarquivo, handoff transaccional y
  validación del SHA exacto al integrar; 710 pruebas aprobadas.

Estos identificadores son commits del repositorio, no releases publicadas en PyPI.
