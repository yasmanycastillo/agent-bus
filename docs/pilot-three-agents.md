# Piloto real con Codex, Claude y AGY

Fecha: 2026-09-06. Resultado: cambio integrado en `main` mediante
`BranchIntegrator`, con pruebas completas y revisión nativa de AGY vinculada
al SHA del candidato. Evidencia estructurada: [pilot-three-agents.json](evidence/pilot-three-agents.json).

## Problema y cambio

El worker rechazaba auto-commit cuando su worktree coincidía con el directorio
actual del proceso, precisamente el caso del CLI normal. La comparación tampoco
protegía el checkout principal cuando el proceso tenía otro directorio actual.
Ahora compara `git rev-parse --git-dir --git-common-dir` con rutas absolutas:
rechaza el checkout principal y permite worktrees enlazados, independientemente
del directorio actual. Los errores de consulta abortan el envío.

## Ejecución observada

- Base guardada antes del piloto: `3ee1316`, integración Hermes y respuestas
  automáticas Codex; suite previa de 660 pruebas aprobada.
- `codex-worker-pilot`, CLI Codex real: implementó el cambio en `d6f6ded`.
  Su daemon hizo commit y envió `PILOT-WORKER-FIX` a revisión.
- `claude-worker-pilot`, CLI Claude real: añadió tres regresiones con repositorios
  Git temporales en `83c8cd0` y corrigió aislamiento/restauración de cwd en
  `19d6955`. Dos regresiones fallaban sin el cambio de Codex y pasan juntas con él.
- `agy-review-pilot`, CLI AGY real: aprobó el diff exacto del candidato
  `2a88e860727906452c2e9a4146a8a31cd4fee629` por mensajería del bus.
- Cinco pruebas dirigidas pasaron. La suite completa pasó con 663 pruebas y
  dos avisos de deprecación de websockets. El integrador volvió a ejecutar la
  suite completa y registró `rev-006babed`, incluyendo el dictamen AGY.
- `BranchIntegrator` fusionó en `08ab262`. `PILOT-WORKER-FIX` quedó `done`;
  el coordinador cerró `PILOT-WORKER-TESTS` después de verificar por ancestría
  Git que su commit estaba incluido. Fue una integración conjunta de ambas tareas.

## Recuperaciones y límites

El piloto fue supervisado: el coordinador provisionó identidades distintas del
watcher, creó worktrees desde la misma base, reclamó las tareas por identidad,
ensambló el candidato y exigió la aprobación AGY del mismo SHA antes del merge.
Los procesos usaron `WorkerDaemon` y `AgentRunner` reales mediante un supervisor
local, con cwd del supervisor en el checkout principal y cwd de cada ejecutor en
su worktree. Esto permitió corregir el defecto previo del CLI; no demuestra por
sí solo un lanzamiento completo mediante `run-team` sin preparación.

Claude necesitó reiniciar su worker con comandos concretos permitidos para
resolver denegaciones de Bash, conservando tarea y worktree. Una respuesta inicial
afirmó incorrectamente que el test mock no alcanzaba el auto-commit; el coordinador
señaló la llamada indirecta y Claude corrigió el aislamiento. Las ediciones de esa
primera respuesta no pudieron adquirir locks: es una limitación observada del
piloto, no evidencia de cumplimiento completo del protocolo por los proveedores.

AGY no pudo ejecutar su primera revisión porque su política denegó `RunCommand`.
Se respetó esa restricción: recibió el diff y SHA en una nueva solicitud, revisó
sin herramientas y devolvió aprobación. No se atribuye a AGY ejecución de pruebas.

El entorno virtual compartido sufrió reinstalaciones al ejecutar `uv run` desde
worktrees. La verificación final y el integrador utilizaron un entorno dedicado
en `.agent-bus/runtime/pilot-python`. El supervisor añadió `UV_NO_SYNC=1` para
las ejecuciones posteriores. Aislamiento de entornos, permisos y asignación de
tareas deben quedar automatizados antes de escalar el equipo.

Se preservaron los listeners Codex existentes. Este piloto demuestra ejecución
real, respuesta automática por bus, corrección tras feedback, revisión e
integración supervisada. No demuestra que un mensaje pueda despertar cualquier
sesión interactiva arbitraria: responde el proceso automático registrado.
T-20, T-21 y T-22 no se publicaron ni se declaran completadas. Credenciales,
sesiones nativas y logs privados permanecen fuera de Git.
