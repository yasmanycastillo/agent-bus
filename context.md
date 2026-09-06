# Contexto del proyecto agent-bus

Actualizado: 2026-09-05, America/Santo_Domingo.

## Objetivo

Convertir agent-bus en un MCP confiable para comunicar y coordinar agentes que trabajan en un mismo proyecto. El servidor MCP ya existe; el objetivo inmediato es estabilizar sus garantías y demostrar interoperabilidad real.

## Lectura al iniciar una sesión

1. Leer [AGENTS.md](AGENTS.md) y respetar listener, inbox, locks y aislamiento cuando corresponda.
2. Leer este archivo y comprobar el checkout, la rama y los cambios locales actuales.
3. Consultar [TASK.md](TASK.md): es el backlog operativo y registro del avance.
4. Consultar [evaluación MCP](docs/evaluacion-mcp.md) para causas y evidencia inicial.
5. Usar [arquitectura original](docs/autonomous_multi_agent_architecture.md), [configuración MCP](docs/mcp-setup.md) y [README](README.md) como documentación existente, contrastando sus afirmaciones con el código.

## Estado conocido

- Paquete Python `agent-bus`, versión declarada 0.1.0; Python >=3.12.
- Hub FastAPI, persistencia SQLite/aiosqlite, avisos SSE y endpoint WebSocket.
- CLI Click, servidor MCP stdio implementado manualmente, workers y runners de CLIs.
- Worktrees, integrador Git, consenso y reputación existen como componentes; su presencia no demuestra un flujo autónomo completo.
- Diagnóstico inicial: prototipo aprovechable, pendiente de corregir entrega, exclusión e identidad.
- Suite histórica del diagnóstico: 189 aprobadas, 1 fallida; la prueba de espera MCP dependía de `localhost:8420`. Esa dependencia se corrigió en T-01.
- Reproducciones temporales confirmaron H-01 a H-06 del análisis: broadcast incompleto, claims con falso éxito, locks concurrentes ambiguos, suplantación con clave aportada por el cliente, finalización de tarea ajena y contrato de decisiones inválido.
- No se ha demostrado aquí un intercambio completo con clientes MCP reales.
- El usuario autorizó comenzar la implementación con subagentes y worktrees. La primera tanda comprende T-01 a T-04 y la corrección funcional del contrato de decisiones T-07; T-05/T-06 tienen revisión de diseño, sin implementación.

Estos resultados son una fotografía inicial. Verificar de nuevo el código y registrar evidencia al completar tareas; no presentar esta suite como estado actual indefinidamente.

## Primera tanda implementada

Validación combinada: **218 pruebas aprobadas** en 27,16 s sobre `4089b8e`, con dos avisos de deprecación de dependencias WebSocket. Comando desde el worktree integrador: `PYTHONPATH=$PWD/src /home/Yasmany/src/agent-bus/.venv/bin/python -m pytest -q`. La rama integradora contiene las correcciones revisadas; consultar Git para verificar su integración actual.

- T-01: hubs HTTP/SSE efímeros, SQLite y credenciales temporales, guardia contra HTTP ajeno en pytest, pruebas CLI sin usar el checkout de trabajo y limpieza de subprocesos. Las nueve herramientas MCP tienen cobertura contra backend real de pruebas; esto no equivale a validar un cliente MCP externo por stdio.
- T-02: inbox identifica entregas por `(message_id, to_agent)`. Migra el esquema anterior conservando filas, archivo e índices; rollback probado. Repetir la misma entrega es idempotente; reintentar un nuevo request HTTP sigue pendiente de T-08.
- T-03: claim atómico solo para tareas libres y pendientes; competidores y reintentos del mismo propietario reciben conflicto. `UPDATE RETURNING` se consume dentro de una sola operación aiosqlite para no interferir con commits de otras coroutines.
- T-04: adquisición atómica y liberación condicionada por propietario. No hay leases ni tokens de sesión todavía; corresponde a T-12.
- T-07: `record_decision` valida argumentos, genera un UUID y adapta `what` a `decision`, con contexto vacío por defecto. Su integración con identidad autenticada sigue pendiente de T-05/T-06.

Se trabajó en `.worktrees/codex-integrator`, `.worktrees/codex-broadcast`, `.worktrees/codex-claims` y `.worktrees/codex-locks`. La integración se revisa manualmente; no se usa BranchIntegrator para publicar cambios. Los worktrees anteriores de otros agentes se conservan.

## Preparación de T-05/T-06

La revisión confirma que los clientes productivos todavía no invocan `sign_operation`. Activar únicamente el modo estricto rompería CLI, MCP y workers y dejaría abiertas otras rutas. La clave del registro en memoria y los archivos `.pub` actuales no constituyen una autoridad única.

Propuesta pendiente de implementación:

1. Definir principal y contratos de proyecto/agente/sesión; coordinar su alcance con T-11.
2. Persistir enrollment y credenciales confiables; separar registro de confianza de presencia/heartbeat.
3. Compartir un cliente autenticado síncrono/asíncrono entre CLI, MCP, workers y consumidores de eventos.
4. Unificar verificación HTTP/WS y permisos, incluyendo inbox, SSE global, `/agents/*`, handoff y War Room.
5. Condicionar en SQL las transiciones por propietario y estado; no comprobar permisos con un SELECT seguido de escritura incondicional.
6. Publicar servidor y clientes compatibles juntos, con pruebas de replay, suplantación, reinicio y acceso cruzado.

Si se conserva Ed25519, definir timestamp/nonce y query/cuerpo canónicos. El aislamiento frente a agentes maliciosos que comparten el mismo usuario Unix y acceso a todas las claves requiere medidas adicionales del sistema; las firmas por sí solas no lo proporcionan.

## Orientación acordada para el trabajo

El usuario aceptó el diagnóstico y pidió convertirlo en documentación y tareas. La dirección propuesta es:

- Priorizar un MVP local de comunicación MCP con entrega persistente y coordinación segura.
- Conservar SQLite inicialmente y usar SSE para avisar, con recuperación desde almacenamiento.
- Asociar proyecto, agente y sesión a cada conexión; derivar la identidad del contexto autenticado.
- Entrega al menos una vez, confirmación explícita e idempotencia; no prometer exactamente una vez para efectos externos.
- Adoptar un SDK MCP mantenido; elegir SDK y versiones compatibles durante la implementación.
- Diferenciar comunicación MCP, reactivación de sesión y ejecución headless; validar cada cliente.
- Posponer consenso BFT, reputación y merges autónomos hasta estabilizar el núcleo.

El esquema definitivo de entregas, el mecanismo de credenciales, las versiones del SDK y los dos clientes iniciales siguen pendientes de concretar. Registrar esas decisiones y sus motivos al implementarlas.

## Mapa del código

| Área | Ubicación |
| --- | --- |
| Servidor MCP y contratos de herramientas | `src/agent_bus/mcp/server.py` |
| HTTP, SSE, WebSocket y panel | `src/agent_bus/core/bus.py` |
| Inbox, tareas y locks | `src/agent_bus/core/{inbox,tasks,locks}.py` |
| Esquema y conexión SQLite | `src/agent_bus/reputation/database.py` |
| Credenciales y firmas | `src/agent_bus/worker/auth.py` |
| Worker y consumo de eventos | `src/agent_bus/worker/{daemon,client}.py` |
| Ejecución de clientes | `src/agent_bus/worker/runner.py` |
| Worktrees e integración | `src/agent_bus/worker/{worktrees,integrator}.py` |
| CLI y watcher | `src/agent_bus/cli/{main,worker_cmds,watch_cmds}.py` |
| Configuración global y proyecto | `src/agent_bus/{config,project}.py` |
| Hook de fin de turno | `hooks/stop-check-inbox.sh` |

## Validación y continuidad

Comando base: `uv run pytest -q`. Hacer primero las pruebas dirigidas a cada cambio y completar las comprobaciones exigidas por AGENTS.md antes de publicar. Los tests nuevos deben aislar base, puerto, credenciales y procesos, sin enviar mensajes al bus real.

Al terminar una tarea: actualizar estado, evidencia y decisiones en TASK.md; actualizar este contexto si cambió la arquitectura o una limitación relevante. Mantener el análisis como registro histórico. No marcar una tarea completa solo porque existen mocks o clases sin conectar al flujo real.

En esta sesión documental se inició el hub en `127.0.0.1:8420` para adquirir locks. Había un watcher `codex-review` en modo observación. Estos son datos de sesión, no garantía de procesos activos en sesiones futuras; comprobarlos y cumplir AGENTS.md.
