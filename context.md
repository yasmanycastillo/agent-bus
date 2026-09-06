# Contexto del proyecto agent-bus

Actualizado: 2026-09-06, America/Santo_Domingo.

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
- CLI Click, servidor MCP stdio mediante SDK oficial Python 2.1.1, workers y runners de CLIs.
- Worktrees, integrador Git, consenso y reputación existen como componentes; su presencia no demuestra un flujo autónomo completo.
- Diagnóstico inicial: prototipo aprovechable, pendiente de corregir entrega, exclusión e identidad.
- Suite histórica del diagnóstico: 189 aprobadas, 1 fallida; la prueba de espera MCP dependía de `localhost:8420`. Esa dependencia se corrigió en T-01.
- Reproducciones temporales confirmaron H-01 a H-06 del análisis: broadcast incompleto, claims con falso éxito, locks concurrentes ambiguos, suplantación con clave aportada por el cliente, finalización de tarea ajena y contrato de decisiones inválido.
- T-13 verificó intercambio y reanudación con Claude Code 2.1.185 (modelo GLM-5.1) y Codex CLI 0.153.4 (gpt-6-astra); ver la matriz y evidencias al final de este documento.
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
- La configuración admite directorio, base y proyecto explícitos por entorno; selección de identidad dinámica y rutas absolutas en subprocesos. `quickstart` autoinicia el origen HTTP loopback configurado, incluido su puerto (T-11). Los clientes restringen credenciales al origen del hub y requieren HTTPS fuera de loopback. Cada proyecto usa una base y un hub propios; T-11 persiste su vinculación y rechaza proyectos incompatibles.
- El panel recibe un token administrativo en un formulario y lo mantiene en memoria; usa `fetch` con Authorization para HTTP/SSE.

El token puede reutilizarse hasta revocación/vencimiento; por sí solo no aporta anti-replay por solicitud ni idempotencia. T-08 implementa esta última para envíos con clave, como se describe abajo. Tareas y locks continúan asociados al nombre del agente, con leases por sesión para locks (T-12). Compartir usuario Unix con acceso a todas las credenciales o a la base no protege frente a un agente malicioso.

La suite legacy usa compatibilidad explícita; las pruebas nuevas de seguridad usan sesiones estrictas. La aceptación automatizada incluye provisión CLI y clientes MCP en proceso contra un hub efímero; la prueba de procesos stdio reales se incorpora en T-10; la aceptación con aplicaciones externas y navegador completo sigue en T-13.

Worktrees de la tanda: `codex-security`, `codex-policy`, `codex-clients` e integrador. Se integran commits revisados y se verifica el código combinado antes de actualizar `main`; no se activa el integrador Git autónomo.

## T-08 implementada: ciclo completo de mensajes

Validación de T-08: **374 pruebas aprobadas** en 65,92 s sobre `a774a8c`, con dos avisos de deprecación WebSocket. Contratos, ejemplos y migración en [messaging.md](docs/messaging.md). T-09 se describe a continuación.

- Un mensaje lógico tiene ID, conversación y correlación con el mensaje al que responde. Cada destinatario tiene una entrega independiente, con secuencia durable, confirmación e historial de fallos.
- Envíos con clave se deduplican por remitente y contenido en SQLite. Broadcast congela destinatarios; reintentar no abre una entrega confirmada. La clave debe conservarse en el cliente.
- `GET /inbox/{agent}/messages` devuelve páginas con cursor firmado y límite superior de secuencia; no confirma. `/ack` confirma hasta 100 IDs sin efectos parciales. `/reply` deriva autor/destino/hilo y admite confirmación atómica explícita.
- MCP incorpora `ack_messages` y `reply_message`. `read_messages` ahora devuelve `{messages, next_cursor}`; envío/respuesta requieren clave. CLI añade `work ack/reply`, claves recuperables y páginas con IDs completos.
- Worker/watcher usan almacenamiento como fuente de pendientes y SSE como aviso. Un fallo conserva la entrega y registra error; un éxito guarda respuesta+ack. Claude `is_error` con exit0 no es éxito; cancelar recoge el subproceso directo.
- Retención: se conservan secuencias y registros de idempotencia aunque se limpien entregas confirmadas; responder requiere que el padre siga disponible. HTTP sin clave es compatibilidad, sin garantía de idempotencia de request.

No hay ejecución exactamente una vez de efectos externos (T-14). T-11 excluye ejecutores automáticos locales del mismo agente; conexiones MCP compartidas requieren coordinación. T-09 incorpora plazo total y recuperación; T-10 incorpora cancelación concurrente por JSON-RPC/stdio. La aceptación con clientes MCP externos por stdio sigue en T-13.

Worktrees: `codex-t08-storage`, `codex-t08-clients`, `codex-t08-workers` e integrador. Se integran commits revisados manualmente a `main`; los listeners permanecen activos y el hub de coordinación previo no se reinició.

## T-09: eventos recuperables y espera acotada

Validación combinada: **446 pruebas aprobadas** en **96,03 s** sobre `85773c1`, con dos avisos de deprecación WebSocket. T-08 se publicó en `origin/main` hasta `063ce22` antes de iniciar esta tanda. Contrato de T-09 en [events.md](docs/events.md). T-10 se describe a continuación.

- Cada entrega y su evento se guardan atómicamente. Hay secuencia global durable, cursores firmados por ámbito y migración de pendientes una sola vez. Un mensaje eliminado conserva su marca de entrega y no se puede recrear mediante un envío legacy con el mismo ID.
- Retención global de eventos: siete días o 10 000 registros; no elimina pendientes. Un cursor vencido devuelve un cursor nuevo y exige recorrer el inbox. El tráfico de otros agentes también puede hacer vencer un cursor personal.
- Rutas personales canónicas: `/inbox/{agent}/events/cursor` y `/inbox/{agent}/events`. Feed global administrativo: `/events/all`. Cursor de eventos e inbox son contratos diferentes.
- SSE reproduce desde SQLite en lotes de 50 con una señal coalescida por conexión y polling cada segundo. Se revalida la sesión y se limita a diez segundos la escritura bloqueada. La consulta serializa con el escritor SQLite; medir contención antes de escalar conexiones.
- MCP captura cursor antes de consultar pendientes, cierra la ventana de suscripción y usa un plazo total de 1–120 segundos. Devuelve códigos explícitos para cursor, conexión, autenticación y protocolo. Un evento histórico confirmado no se presenta como trabajo nuevo.
- Worker y panel guardan el cursor en memoria al reconectar, procesan checkpoints/reset y recuperan pendientes. El parser SSE compartido tiene framing multilínea, límite de frame y cesión al event loop para mantener cancelación/plazos.

Al cerrar T-09 quedaba pendiente el transporte stdio concurrente; T-10 lo incorpora. La aceptación con clientes MCP externos y navegador completo sigue en T-13. Los listeners se mantienen activos y el hub de coordinación previo conserva su proceso anterior: integrar código no migra ese servicio.

Worktrees: `codex-t09-storage`, `codex-t09-server`, `codex-t09-clients` e integrador. Ver el registro de TASK.md para commits y validación combinada final.

## T-10: SDK MCP y transporte stdio

Validación combinada: **493 pruebas aprobadas** en **109,70 s** sobre `7fad883`, dos avisos de deprecación WebSocket. Antes de esta tanda, T-09 se publicó en `origin/main` hasta `7fa6b54`. T-11 se describe a continuación. Configuración y contrato en [mcp-setup.md](docs/mcp-setup.md).

- SDK oficial Python `mcp==2.1.1`, con lock reproducible. Se usa `mcp.server.Server` para preservar once herramientas y dejar negociación, protocolo, concurrencia y cancelación al SDK. Se elimina el dispatcher manual y `PROTOCOL_VERSION`.
- La API Python de pruebas es `Client(McpServer(...).sdk_server())`; desaparece `handle_request`. Se conserva `execute_tool` como lógica de aplicación.
- JSON Schema 2020-12 valida antes de vincular actores. Schemas seguros excluyen identidad y rechazan propiedades adicionales. Los resultados llevan el mismo JSON en texto y `structuredContent`; fallos de herramienta tienen `isError: true`, separados de errores JSON-RPC.
- `wait_for_updates` admite solicitudes paralelas y cancelación por ID sin confirmar pendientes. Cancelar no revierte una mutación ya persistida; mantener las claves de idempotencia.
- La E/S por threads del SDK podía quedarse viva con stdout roto o saturado. `mcp/transport.py` usa tuberías asyncio cancelables y termina la conexión al recibir EOF, conservando parser/serializador del SDK. Descriptores privados separan protocolo de impresiones accidentales; cada línea entrante está limitada a 4 MiB. La capa traduce fallos de validación del SDK a respuestas -32700/-32600, evitando descartarlos sin responder.
- CLI `--bus-url` prevalece sobre `AGENT_BUS_URL`; default loopback. Falla en stderr si faltan credenciales, sin iniciar automáticamente el hub.

Pruebas de transporte en Linux con un cliente SDK real y peer JSON-RPC independiente: protocolo moderno `2026-07-28`, handshake `2024-11-05` y `2025-06-18`, envío/lectura autenticados, concurrencia, cancelación, cierre de SSE y del proceso. No acreditan todavía Windows, dos aplicaciones MCP externas ni reactivación de sus TUI (T-13). Ver TASK.md para el resultado final de la suite y commits.

El entorno de validación de esta tanda es `.worktrees/codex-integrator/.venv`, instalado con `uv sync --extra dev`; usar `uv run --locked pytest -q`. La `.venv` raíz y el hub de coordinación previo no se actualizaron: sus listeners siguen activos. Preparar las dependencias con `uv sync --locked --extra dev` al activar el nuevo código.

## Orientación acordada para el trabajo

El usuario aceptó el diagnóstico y pidió convertirlo en documentación y tareas. La dirección propuesta es:

- Priorizar un MVP local de comunicación MCP con entrega persistente y coordinación segura.
- Conservar SQLite inicialmente y usar SSE para avisar, con recuperación desde almacenamiento.
- Asociar proyecto, agente y sesión a cada conexión; derivar la identidad del contexto autenticado.
- Entrega al menos una vez, confirmación explícita e idempotencia; no prometer exactamente una vez para efectos externos.
- Usar el SDK oficial MCP; T-10 fija 2.1.1 y verifica negociación con las revisiones indicadas.
- Diferenciar comunicación MCP, reactivación de sesión y ejecución headless; validar cada cliente.
- Posponer consenso BFT, reputación y merges autónomos hasta estabilizar el núcleo.

Entregas, sesiones y versión del SDK están definidos en las secciones anteriores. T-13 eligió Claude Code y Codex CLI por estar instalados y autenticados; la matriz de aceptación delimita sus versiones, modelos y mecanismos comprobados.

## Mapa del código

| Área | Ubicación |
| --- | --- |
| Servidor MCP y contratos de herramientas | `src/agent_bus/mcp/server.py` |
| HTTP, SSE, WebSocket y panel | `src/agent_bus/core/bus.py` |
| Historial de eventos y parser SSE | `src/agent_bus/core/{events,sse}.py` |
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


## T-11: aislamiento por proyecto y sesión

T-10 se publicó en `origin/main` hasta `9010455` antes de iniciar esta tanda. Implementación integrada hasta `6e573d5`: suite completa de **539 pruebas** en **129,10 s**, más **28 pruebas dirigidas** tras el ajuste final de rutas relativas; dos avisos de deprecación WebSocket. Contrato completo, precedencias y migración en [projects.md](docs/projects.md).

- Un hub/base por proyecto; `project_metadata` impide reprovisionar o abrir una base bajo otro ID. Una base heredada se adopta sólo si las sesiones existentes son compatibles. Las tablas de negocio no son multitenant.
- Descubrimiento ascendente con límites de repositorio Git; los worktrees comparten `.agent-bus/runtime` del checkout canónico. `--project` o `AGENT_BUS_PROJECT_ROOT` permiten selección explícita. El contexto del hub usa el runtime fijado, no el cwd de cada petición.
- `auth create --provider claude` genera participantes distintos; `--agent` conserva identidad explícita. MCP captura proyecto/URL/credencial y los subprocesos reciben entorno absoluto.
- Worker y watcher automático comparten guard `flock` por base/proyecto/agente. Observadores dry-run coexisten. Varias conexiones MCP con la misma identidad comparten inbox sin reserva de lectura; deben coordinar consumo/ACK. El guard es local Unix, no una lease distribuida.
- `quickstart` respeta el puerto HTTP loopback configurado y rechaza un hub de otro proyecto; los destinos remotos no se autoinician.

T-12 incorpora alcance y renovación de locks por sesión, como se detalla a continuación. T-13 conserva la aceptación con aplicaciones MCP externas. El hub previo y sus listeners siguen activos sin reiniciarse ni migrarse automáticamente.


## T-12: alcance y renovación de locks

Implementación hasta `33eb90e`, validada con **592 pruebas aprobadas** en **137,77 s**, dos avisos de deprecación WebSocket. T-11 estaba publicado hasta `2a75289`. Contrato en [locks.md](docs/locks.md). Cada adquisición tiene sesión, token aleatorio y vencimiento; la base rechaza renovación/liberación por un token viejo, incluso si su sucesor pertenece al mismo agente y sesión. Los listados no exponen el token. No se permiten liberaciones sin `acquisition_id`.

- Scope `checkout`: ruta física canónica desde el cwd del cliente, aislada entre copias de worktree. Scope `project`: recurso lógico bajo raíz canónica compartida, rechazando escapes. HTTP directo exige ruta relativa para `project`; CLI/MCP hacen la traducción desde su checkout.
- TTL predeterminado 300 s, rango 1–3600 s, limitado por vencimiento de sesión. Renovación explícita mediante CLI `work renew-lock` o MCP `renew_lock`. Si falla o expira, detener la edición y adquirir de nuevo antes de continuar.
- Migración: los locks antiguos sin sesión/token/lease quedan vencidos; volver a adquirir. La provisión Bearer, el hub y las credenciales se conservan. No se reinició el servicio previo de coordinación.
- Las mutaciones consumen cursores y confirman en un callback atómico; el reloj se lee después de obtener acceso de escritura SQLite. Una transacción compartida pendiente produce un 503 reintentable, sin confirmar trabajo ajeno.
- El lock es cooperativo: no intercepta editores externos ni frena escrituras de un proceso que ignora su vencimiento. No se cubren hardlinks, cambios de symlink posteriores ni montajes remotos distintos. `tasks/lock-files` es metadato descriptivo, no una adquisición de lease.

Al cerrar T-12 seguía T-13, completada en el registro siguiente.

## T-13: aceptación con dos aplicaciones reales

Evidencias y reproducción en [acceptance-t13.md](docs/acceptance-t13.md), resumen sanitizado en [t13.json](docs/evidence/t13.json), harness optativo en `scripts/acceptance_real_clients.py`. Se ejecutó contra el código de T-12 (`ca847a8`), con hub/base/proyecto efímeros y credenciales independientes. La suite completa pasó: **592 pruebas en 138,75 s**, dos avisos de deprecación WebSocket.

- Claude Code 2.1.185 usa aquí GLM-5.1 por la configuración disponible; Codex CLI 0.153.4 usa gpt-6-astra. Son aplicaciones MCP reales; no se pasó por `AgentRunner` ni por un mock del cliente.
- Cuatro turnos headless: intercambio concurrente y reanudación explícita de ambos clientes. Cada uno conservó el ID de conversación y recordó un marcador que no se repitió en el prompt de reanudación.
- Un único ganador para tarea y lock; envío y respuesta repetidos con la misma clave sin duplicar entregas. Cuatro operaciones lógicas, cuatro entregas y tres ACK; la respuesta final queda pendiente intencionalmente al finalizar el escenario.
- Claude mantuvo SSE antes del envío y recuperó un mensaje escrito mientras estaba desconectado. Codex recuperó y confirmó su pendiente al reanudar. El harness inicia los procesos, pero no transporta el texto entre clientes ni lo copia a sus prompts.
- No se acreditan reactivación espontánea de TUI, hooks Stop en aplicación viva, AGY/Aider/Grok, ni la autonomía completa del worker. El alias `provider=codex` del runner todavía invoca Aider; el README ya lo declara y T-14 debe resolverlo.
- La skill OpenAI Docs se usó para contrastar configuración MCP y reanudación de Codex con la versión instalada. La evidencia local prevalece para afirmar qué se ejecutó.

T-14 y T-15 quedan completadas: el runner tiene adaptadores separados y sesiones persistentes opcionales; el worker puede crear el commit y pasar la tarea a `in_review`; el integrador valida, fusiona y expone `agent-bus integrator start/status/stop`. La aceptación operativa con proyectos reales, cuotas/costes persistentes, DAG de tareas y Hermes siguen en el roadmap de [product-roadmap-improvements.md](docs/product-roadmap-improvements.md). Las referencias anteriores a T-13 pendiente describen el estado histórico al cerrar cada tanda. El hub personal y sus listeners siguen con su proceso anterior; ninguna aceptación reinició ese servicio. Los archivos privados de la corrida viven en `/tmp/agent-bus-t13-n21ax5md` y pueden desaparecer; el resumen sanitizado y el harness quedan en Git.

## T-16: Piloto real de operación end-to-end

Completada e integrada en `main` (`0d3e177` / merge). Suite completa: **600 pruebas aprobadas** en **151,11 s**, dos avisos de deprecación WebSocket (`uv run pytest -q`).

- Validado el ciclo operativo completo: `init/onboard → submit → claim → commit → in_review → tests → merge → done` en un repositorio y hub SQLite aislados (`tests/integration/test_pilot_e2e.py`).
- Flujo de feedback y reintentos del `BranchIntegrator` ante fallos de tests unitarios verificado.
- Persistencia y recuperación del estado (decisiones ADR, tareas completadas e historial) comprobada ante reinicios de procesos simulados.
- Correcciones de compatibilidad aplicadas a `AgentRunner` (stream-json de AGY) y `TaskManager` / `MessageBus` para transición y completitud fluida de tareas en revisión.

Las siguientes tareas operativas (DAG de dependencias T-17, HermesOrchestrator T-18, Gatekeeper T-19, cuotas/presupuesto T-20 y consola local React T-21) continúan según el backlog de [TASK.md](TASK.md).

## T-17: Contrato de tareas, dependencias y DAG

Completada e integrada en `main`. Suite completa: **613 pruebas aprobadas** en **153,15 s**, dos avisos de deprecación WebSocket (`uv run pytest -q`).

- `Task` enriquecida con `acceptance_criteria: list[str]`, `test_cmd: list[str] | None`, `depends_on: list[str]` y `operation_key: str | None`.
- Migración no destructiva en `Database._migrate_tasks()` con índice `idx_tasks_operation_key`.
- Validación atómica de dependencias y detección de ciclos en grafos dirigidos (DFS de 3 colores) al crear tareas individuales o desgloses en lote (`POST /tasks/batch`, `POST /tasks/breakdown`).
- Desglose idempotente: llamadas con la misma `operation_key` devuelven las tareas existentes sin duplicación.
- Control de desbloqueo: tareas con dependencias incompletas inician en estado `blocked`. Al completarse una tarea (`done`), sus dependientes se desbloquean automáticamente a `pending`.
- `WorkerDaemon` consulta exclusivamente tareas desbloqueadas (`ready_only=true`). Intentos de reclamo sobre tareas bloqueadas son rechazados con 409 Conflict.
- Dashboard (`generate_dashboard_renderable`, `print_tasks_table`, CLI `agent-bus top`, `agent-bus show tasks`) muestra columnas `Depends On` e indicador visual `[blocked]`.

Las siguientes tareas operativas (HermesOrchestrator T-18, Gatekeeper T-19, cuotas/presupuesto T-20 y consola local React T-21) continúan según el backlog de [TASK.md](TASK.md).
