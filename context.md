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
- El usuario autorizó comenzar la implementación con subagentes y worktrees. La primera tanda cerró T-01 a T-04 y el contrato funcional de T-07. La segunda cerró T-05/T-06 y vinculó T-07 con la identidad autenticada; ambas se integran localmente a main.

Estos resultados son una fotografía inicial. Verificar de nuevo el código y registrar evidencia al completar tareas; no presentar esta suite como estado actual indefinidamente.

## Primera tanda implementada

Validación combinada: **218 pruebas aprobadas** en 27,16 s sobre `4089b8e`, con dos avisos de deprecación de dependencias WebSocket. Comando desde el worktree integrador: `PYTHONPATH=$PWD/src /home/Yasmany/src/agent-bus/.venv/bin/python -m pytest -q`. La rama integradora contiene las correcciones revisadas; consultar Git para verificar su integración actual.

- T-01: hubs HTTP/SSE efímeros, SQLite y credenciales temporales, guardia contra HTTP ajeno en pytest, pruebas CLI sin usar el checkout de trabajo y limpieza de subprocesos. Las nueve herramientas MCP tienen cobertura contra backend real de pruebas; esto no equivale a validar un cliente MCP externo por stdio.
- T-02: inbox identifica entregas por `(message_id, to_agent)`. Migra el esquema anterior conservando filas, archivo e índices; rollback probado. Repetir la misma entrega es idempotente; reintentar un nuevo request HTTP sigue pendiente de T-08.
- T-03: claim atómico solo para tareas libres y pendientes; competidores y reintentos del mismo propietario reciben conflicto. `UPDATE RETURNING` se consume dentro de una sola operación aiosqlite para no interferir con commits de otras coroutines.
- T-04: adquisición atómica y liberación condicionada por propietario. No hay leases ni tokens de sesión todavía; corresponde a T-12.
- T-07: `record_decision` valida argumentos, genera un UUID y adapta `what` a `decision`, con contexto vacío por defecto. La segunda tanda añade su vínculo con la identidad autenticada.

Se trabajó en `.worktrees/codex-integrator`, `.worktrees/codex-broadcast`, `.worktrees/codex-claims` y `.worktrees/codex-locks`. La integración se revisa manualmente; no se usa BranchIntegrator para publicar cambios. Los worktrees anteriores de otros agentes se conservan.

## Segunda tanda: T-05/T-06 y cierre de identidad T-07

Validación combinada: **303 pruebas aprobadas** en 45,63 s sobre `7e58fe7`, con dos avisos de deprecación WebSocket. Incluye cinco escenarios de provisión CLI y MCP/HTTP contra `create_app` real efímero. T-08 se completó posteriormente; su resultado se describe en la sección siguiente.

Decisión: sesiones Bearer locales persistentes, provisionadas por el operador del sistema operativo. Se retira Ed25519 como autenticación HTTP; no existe inscripción HTTP anónima ni fallback a una clave aportada por el solicitante. Ver [guía de provisión y migración](docs/authentication.md).

- `security.py` vincula token aleatorio de 32 bytes a `agent_id`, `project_id`, `session_id`, rol `agent/admin`, expiración y revocación. SQLite guarda hash SHA-256; el cliente carga un archivo propio `0600`, sin symlinks.
- `auth create/revoke` opera localmente sobre la base configurada. El operador debe provisionar cada agente y un administrador para el panel/reasignaciones. Presencia y heartbeat no crean confianza.
- HTTP exige sesión excepto `/health` y la página estática `/room`. WS y SSE verifican identidad y revalidan la sesión mientras permanecen abiertos. `AGENT_BUS_ALLOW_UNSIGNED=1` es compatibilidad explícita de desarrollo.
- MCP fija la sesión al arrancar y elimina remitente/autor de los schemas seguros. CLI, watcher, hook, workers e integrador usan clientes comunes; los runners reciben el archivo de su agente, sin heredar autoridad administrativa.
- Las operaciones de tareas comprueban propietario/estado dentro de SQL. Reasignación administrativa y handoff dejan auditoría de actor, sesión y cambio de propietario en una misma operación SQLite; decisiones existentes no se modifican mediante un ID reutilizado.
- La configuración admite directorio, base y proyecto explícitos por entorno; selección de identidad dinámica y rutas absolutas en subprocesos. `quickstart` todavía autoinicia solo 127.0.0.1:8420; otras URL deben tener hub iniciado explícitamente (T-11). Los clientes restringen credenciales al origen del hub y requieren HTTPS fuera de loopback. Cada proyecto debe usar una base separada: el namespace integral de T-11 sigue pendiente.
- El panel recibe un token administrativo en un formulario y lo mantiene en memoria; usa `fetch` con Authorization para HTTP/SSE.

El token puede reutilizarse hasta revocación/vencimiento; por sí solo no aporta anti-replay por solicitud ni idempotencia. T-08 implementa esta última para envíos con clave, como se describe abajo. Tareas y locks continúan asociados al nombre del agente, sin leases por sesión (T-11/T-12). Compartir usuario Unix con acceso a todas las credenciales o a la base no protege frente a un agente malicioso.

La suite legacy usa compatibilidad explícita; las pruebas nuevas de seguridad usan sesiones estrictas. La aceptación automatizada incluye provisión CLI y clientes MCP en proceso contra un hub efímero; no demuestra todavía interoperabilidad externa stdio ni el navegador completo (T-10/T-13).

Worktrees de la tanda: `codex-security`, `codex-policy`, `codex-clients` e integrador. Se integran commits revisados y se verifica el código combinado antes de actualizar `main`; no se activa el integrador Git autónomo.

## T-08 implementada: ciclo completo de mensajes

Validación actual: **374 pruebas aprobadas** en 65,92 s sobre `a774a8c`, con dos avisos de deprecación WebSocket. Contratos, ejemplos y migración en [messaging.md](docs/messaging.md). Siguiente tarea: **T-09**.

- Un mensaje lógico tiene ID, conversación y correlación con el mensaje al que responde. Cada destinatario tiene una entrega independiente, con secuencia durable, confirmación e historial de fallos.
- Envíos con clave se deduplican por remitente y contenido en SQLite. Broadcast congela destinatarios; reintentar no abre una entrega confirmada. La clave debe conservarse en el cliente.
- `GET /inbox/{agent}/messages` devuelve páginas con cursor firmado y límite superior de secuencia; no confirma. `/ack` confirma hasta 100 IDs sin efectos parciales. `/reply` deriva autor/destino/hilo y admite confirmación atómica explícita.
- MCP incorpora `ack_messages` y `reply_message`. `read_messages` ahora devuelve `{messages, next_cursor}`; envío/respuesta requieren clave. CLI añade `work ack/reply`, claves recuperables y páginas con IDs completos.
- Worker/watcher usan almacenamiento como fuente de pendientes y SSE como aviso. Un fallo conserva la entrega y registra error; un éxito guarda respuesta+ack. Claude `is_error` con exit0 no es éxito; cancelar recoge el subproceso directo.
- Retención: se conservan secuencias y registros de idempotencia aunque se limpien entregas confirmadas; responder requiere que el padre siga disponible. HTTP sin clave es compatibilidad, sin garantía de idempotencia de request.

No hay ejecución exactamente una vez de efectos externos ni exclusión entre procesos del mismo agente (T-11/T-14). La espera MCP sigue pendiente del plazo total/cancelación y recuperación de eventos de T-09/T-10. La aceptación con clientes MCP externos por stdio sigue en T-13.

Worktrees: `codex-t08-storage`, `codex-t08-clients`, `codex-t08-workers` e integrador. Se integran commits revisados manualmente a `main`; los listeners permanecen activos y el hub de coordinación previo no se reinició.

## Orientación acordada para el trabajo

El usuario aceptó el diagnóstico y pidió convertirlo en documentación y tareas. La dirección propuesta es:

- Priorizar un MVP local de comunicación MCP con entrega persistente y coordinación segura.
- Conservar SQLite inicialmente y usar SSE para avisar, con recuperación desde almacenamiento.
- Asociar proyecto, agente y sesión a cada conexión; derivar la identidad del contexto autenticado.
- Entrega al menos una vez, confirmación explícita e idempotencia; no prometer exactamente una vez para efectos externos.
- Adoptar un SDK MCP mantenido; elegir SDK y versiones compatibles durante la implementación.
- Diferenciar comunicación MCP, reactivación de sesión y ejecución headless; validar cada cliente.
- Posponer consenso BFT, reputación y merges autónomos hasta estabilizar el núcleo.

El esquema definitivo de entregas, las versiones del SDK y los dos clientes iniciales siguen pendientes de concretar; las sesiones locales se describen arriba. Registrar esas decisiones y sus motivos al implementarlas.

## Mapa del código

| Área | Ubicación |
| --- | --- |
| Servidor MCP y contratos de herramientas | `src/agent_bus/mcp/server.py` |
| HTTP, SSE, WebSocket y panel | `src/agent_bus/core/bus.py` |
| Inbox, tareas y locks | `src/agent_bus/core/{inbox,tasks,locks}.py` |
| Esquema y conexión SQLite | `src/agent_bus/reputation/database.py` |
| Sesiones y provisión local | `src/agent_bus/security.py`, `src/agent_bus/cli/auth_cmds.py` |
| Utilidades criptográficas legacy (no autentican HTTP) | `src/agent_bus/worker/auth.py` |
| Worker y consumo de eventos | `src/agent_bus/worker/{daemon,client}.py` |
| Ejecución de clientes | `src/agent_bus/worker/runner.py` |
| Worktrees e integración | `src/agent_bus/worker/{worktrees,integrator}.py` |
| CLI y watcher | `src/agent_bus/cli/{main,worker_cmds,watch_cmds}.py` |
| Configuración global y proyecto | `src/agent_bus/{config,project}.py` |
| Hook de fin de turno | `hooks/stop-check-inbox.sh` |

## Validación y continuidad

Comando base: `uv run pytest -q`. Hacer primero las pruebas dirigidas a cada cambio y completar las comprobaciones exigidas por AGENTS.md antes de publicar. Los tests nuevos deben aislar base, puerto, credenciales y procesos, sin enviar mensajes al bus real.

Al terminar una tarea: actualizar estado, evidencia y decisiones en TASK.md; actualizar este contexto si cambió la arquitectura o una limitación relevante. Mantener el análisis como registro histórico. No marcar una tarea completa solo porque existen mocks o clases sin conectar al flujo real.

En la sesión inicial se inició el hub en `127.0.0.1:8420` para adquirir locks. Había un watcher `codex-review` en modo observación. Estos son datos de sesión, no garantía de procesos activos en sesiones futuras; comprobarlos y cumplir AGENTS.md.

El hub de coordinación existente no se reinició durante la segunda tanda: conserva el proceso anterior aunque el código de main cambie. Activar la política requiere provisión y reinicio coordinado de servidor/clientes; no confundir las pruebas efímeras aprobadas con una migración del servicio en ejecución. Los listeners se dejaron activos según AGENTS.md.
