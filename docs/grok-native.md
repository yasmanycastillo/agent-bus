# Grok nativo: configuración, estado y recuperación

La integración sustituye el lanzador específico del piloto por
`agent-bus watch --agent <identidad> --cli grok`. El proveedor también se selecciona
desde la credencial cuando no se indica `--cli`. `onboard --mcp-only` genera un
`start.sh` con el entorno del proyecto para Grok, Claude y Codex, sin copiar tokens.
[Guía de operación y estados](first-run.md#listeners-para-responder-automáticamente).

## Aceptación real

El 17 de septiembre de 2026 (UTC), Grok CLI 1.0.34 / Grok 4.6 respondió
`NATIVO-8467` a una solicitud enviada por Codex en **11,762 segundos**. El listener
estaba activo antes del mensaje, obtuvo el proveedor de la credencial y ejecutó
el turno sin lanzador auxiliar. Publicó la respuesta, confirmó la solicitud y
volvió a `waiting`. [Evidencia sanitizada](evidence/grok-native.json).
La captura precede al ACK de la respuesta por Codex; después de verificarla,
Codex la confirmó y su bandeja quedó vacía.

El operador provisionó una identidad separada en el hub del piloto, inició el
listener, envió la consulta y verificó las entregas. No se acredita instalación
por una persona externa sin asistencia ni despertar de una TUI existente.

## Pruebas automatizadas de recuperación

Validación completa: **752 pruebas aprobadas**, dos avisos de deprecación
WebSocket, en **214,10 s** mediante `uv run pytest -q`. También pasaron el lint
`ruff --select F,E9` de los archivos Python modificados y `git diff --check`.

- Caída real de un proceso después de guardar el texto y antes de confirmar el
  POST: otro proceso entrega el texto guardado sin volver a invocar al proveedor.
- Respuesta HTTP perdida después del commit: el siguiente intento consulta el
  ACK y no vuelve a publicar ni a ejecutar el modelo.
- Credencial del bus vencida: el listener registra `auth_error`, no ejecuta al
  modelo y procesa el pendiente al restaurar una credencial válida. Se usan HTTP
  autenticado y SQLite reales en un proyecto temporal.
- JSON malformado, salida vacía y turnos incompletos de Grok: sin respuesta ni ACK.
- Estado con archivo PID antiguo, reserva de un worker o telemetría vencida:
  no se anuncia disponibilidad para ejecutar.
- Onboarding repetido: conserva credenciales y lanzador, que permite consultar
  estado desde fuera del directorio del proyecto.

Estas pruebas simulan la salida del proveedor salvo la aceptación real indicada
arriba. No equivalen a inyectar fallos en el servicio de xAI. Una caída anterior
al guardado de la respuesta todavía puede repetir la consulta al modelo. El
estado del listener no garantiza saldo, cuota ni disponibilidad del proveedor.

Pendiente: piloto externo sin ayuda del autor. No se considera completado con
estas pruebas técnicas.
