# Changelog

## 0.3.0 — 2026-10-13

- Paneles de agente: `agent-bus panes spawn|send|screen|close|list|view` abre Claude,
  agy, Codex o Grok en un servidor tmux privado por proyecto. El coordinador los
  maneja con la herramienta MCP opcional `agent_panes` (`AGENT_BUS_PANES=1`); el
  humano los mira en solo lectura con `view` o en el panel «Agentes en vivo» de la
  consola, y responde sus preguntas de permisos con `panes answer`.
- Cada panel arranca con su propio watcher (`watch --cli tmux`), que le escribe las
  tareas asignadas, los avisos del hub y los mensajes pendientes, y le recuerda hacer
  handoff si una tarea lleva 15 minutos en `in_progress` sin cambios.
- `assign_work` puede limitar una revisión a implementaciones concretas; la revisión
  espera a que estén entregadas y se cierra sola al aprobarlas todas.
  `complete_handoff` guarda el SHA candidato y `changes_requested` devuelve la tarea a
  quien la tenía.
- El coordinador puede cerrar con `complete_task` una implementación aprobada que
  integró por su cuenta. `my_pending_items` filtra por `instruction_id`.
- Los agentes registrados sobreviven al reinicio del hub y pasan a offline cuando
  dejan de latir. `bootstrap_agent` avisa si MCP y hub tienen versiones distintas.
- Seguridad: solo el coordinador de una instrucción asigna su trabajo o la marca
  integrada; los paneles no heredan claves de API, `SSH_AUTH_SOCK` ni la identidad del
  coordinador, y `send` elimina caracteres de control; el directorio de credenciales
  trae su propio `.gitignore`.
- Cada operación de tareas usa su propia transacción SQLite; el integrador cuenta los
  reintentos desde las revisiones persistidas, prueba el SHA revisado exacto y rechaza
  checkouts que no son Git.
- `wait_for_updates` espera como máximo 50 s para que los clientes MCP reciban el
  timeout vacío. Los errores del hub y de agy explican la regla o la acción denegada.

## 0.2.1 — 2026-10-07

- `watch --cli muxel --muxel-agent <agente>` escribe la solicitud en un panel vivo de
  muxel en lugar de lanzar un turno headless. Espera si el agente está ocupado, no
  repite la solicitud y solo responde cuando el turno terminó.
- La consola muestra qué agentes tienen sesión en el proyecto y si Claude está
  trabajando, esperando al usuario o inactivo. Se observan también las sesiones de
  agy y las de Codex que quedaban detrás de otros proyectos.
- Instalar el MCP en Codex aprueba sus herramientas de agent-bus.
- Los coordinadores reciben el protocolo vigente aunque el hub sea anterior.
- Corregido: sesiones de Claude no detectadas en rutas con `.` o `_`; turnos de
  Claude sin historial al heredar `CLAUDE_CODE_CHILD_SESSION`; borrar un worktree
  podía perder trabajo sin commitear.

## 0.2.0 — 2026-09-25

agent-bus coordina agentes que ya saben trabajar. No inventa el encargo ni lo ejecuta.

- El humano habla con un agente: Hermes, Grok, Claude, Codex o AGY. Ese agente confirma el texto y lo reparte por MCP.
- Quien revisa no es quien implementa. Hermes arma el plan; los demás programan o revisan.
- Instalar el MCP es un comando. No crea credenciales en este repositorio: la sesión nace en el proyecto que se coordina.
- Cada worker solo toma la tarea que tiene asignada. Un revisor no reclama implementación. Un mensaje agotado no bloquea la cola. El turno espera 1800 segundos.

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
