# TASK — Plan de estabilización MCP

Base: [evaluación inicial](docs/evaluacion-mcp.md). Contexto: [context.md](context.md).

## Reglas de seguimiento

- Estados: pendiente, en curso, bloqueada, completada. Consultar la tabla y los criterios marcados para distinguir avance parcial de cierre.
- Antes de trabajar, respetar AGENTS.md, consultar inbox/locks y revisar cambios ajenos.
- Al iniciar, registrar responsable y estado. Al cerrar, añadir pruebas, resultado, commit si existe y decisiones relevantes.
- Una tarea bloqueada debe indicar causa y dependencia. Una tarea completada debe satisfacer sus criterios de aceptación.
- P0: integridad o identidad. P1: necesario para el MVP. P2: posterior al MVP.
- Los identificadores siguientes son del backlog documental; todavía no son tareas creadas en el bus.

## Orden recomendado

T-01 habilita validaciones reproducibles. Después corregir T-02/T-03/T-04/T-05/T-06 y T-07. Continuar con T-08/T-09/T-10/T-11/T-12. Cerrar el MVP con T-13. T-14/T-15 son posteriores.

| ID | Prioridad | Trabajo | Dependencias | Estado | Responsable |
| --- | --- | --- | --- | --- | --- |
| T-01 | P1 | Pruebas aisladas y contratos | — | completada | codex-integrator |
| T-02 | P0 | Persistencia de broadcasts | T-01 | completada | codex-broadcast |
| T-03 | P0 | Claim atómico de tareas | T-01 | completada | codex-claims |
| T-04 | P0 | Adquisición atómica de locks | T-01 | completada | codex-locks |
| T-05 | P0 | Identidad y credenciales confiables | T-01 | completada | codex-security / codex-clients |
| T-06 | P0 | Autorización de operaciones | T-03, T-05 | completada | codex-policy |
| T-07 | P1 | Contrato de decisiones | T-01; cierre de identidad: T-05/T-06 | completada | codex-integrator |
| T-08 | P1 | Confirmación, respuestas e idempotencia | T-02, T-05, T-06 | completada | codex-integrator / codex-t08-storage / codex-t08-clients / codex-t08-workers |
| T-09 | P1 | Eventos recuperables y espera acotada | T-08 | completada | codex-integrator / codex-t09-storage / codex-t09-server / codex-t09-clients |
| T-10 | P1 | SDK MCP y pruebas stdio | T-05, T-07, T-08, T-09 | completada | codex-integrator / codex-t10-stdio / codex-t10-contracts |
| T-11 | P1 | Aislamiento por proyecto y sesión | T-05, T-06 | completada | codex-integrator / codex-t11-project / codex-t11-security / codex-t11-workers |
| T-12 | P1 | Renovación y alcance de locks | T-04, T-11 | completada | codex-integrator / codex-t12-storage / codex-t12-paths / codex-t12-clients |
| T-13 | P1 | Validación con dos clientes reales | T-01 a T-12 | completada | codex-integrator / codex-t13-acceptance |
| T-14 | P2 | Workers recuperables y adaptadores | T-13 | pendiente | — |
| T-15 | P2 | Integración Git verificada | T-14 | pendiente | — |

## T-01 — Pruebas aisladas y contratos

Eliminar la dependencia de un hub personal en `tests/test_mcp_server.py`. Preparar fixtures con base, credenciales y servidor efímeros. Reproducir H-01 a H-06 mediante regresiones que se incorporen con sus correcciones; evitar dejar fallos esperados ocultando defectos.

- [x] La prueba de espera distingue timeout, bus caído y mensaje pendiente con resultados deterministas.
- [x] La suite corre con `localhost:8420` apagado sin consultar ni modificar servicios del usuario.
- [x] Los recursos temporales se limpian aun cuando falla una aserción.
- [x] Cada herramienta MCP tiene una prueba de contrato contra el backend real de pruebas.

## T-02 — Persistencia de broadcasts (H-01)

Separar identidad del mensaje de entrega por destinatario. Definir una migración que conserve mensajes y estado de archivo existentes.

- [x] Un broadcast a tres agentes queda disponible para los tres tras reconectar o reiniciar.
- [x] Confirmar una entrega no elimina las de otros destinatarios.
- [x] El reintento con la misma clave no crea entregas duplicadas.
- [x] La migración desde el esquema anterior se prueba con datos existentes.

## T-03 — Claim atómico (H-02)

Comprobar filas afectadas o usar una operación equivalente que identifique al ganador. Revisar el consumidor del resultado en el worker.

- [x] Ante claims concurrentes de agentes distintos, solo uno obtiene éxito; los demás reciben conflicto.
- [x] El perdedor no ejecuta el runner para esa tarea.
- [x] Una tarea finalizada no se reclama; las transiciones permitidas quedan documentadas.
- [x] Se define y prueba el comportamiento del reintento del mismo propietario.

## T-04 — Locks atómicos (H-03)

Eliminar el falso éxito producido por consultar antes de insertar e ignorar el conflicto.

- [x] Solo un solicitante obtiene el lock ante adquisiciones concurrentes.
- [x] El perdedor recibe un conflicto explícito con el propietario actual.
- [x] Liberar un lock ajeno falla; la liberación no borra una adquisición posterior de otra sesión.
- [x] Se prueban contención y reintentos con conexiones independientes.

## T-05 — Identidad confiable (H-04)

Definir el modelo local de confianza, registro y vinculación de credenciales. Verificar contra credenciales registradas, no contra una clave arbitraria enviada en el request. Integrar autenticación en los clientes que escriben.

Implementación de la segunda tanda: sesiones Bearer provisionadas por operador local; se retira la autenticación HTTP por firmas. Ver [provisión, permisos y migración](docs/authentication.md). Servidor y clientes se integran conjuntamente, con rutas HTTP/WS/SSE protegidas.

- [x] La reproducción de suplantación con clave desconocida es rechazada.
- [x] El actor declarado coincide con la identidad de la sesión autorizada; se retira el firmante HTTP legacy.
- [x] Resolver firmas por operación: retiradas; no aplica nonce de firma. Tokens reutilizables hasta expiración/revocación; idempotencia sigue en T-08.
- [x] HTTP y WebSocket aplican la política; revisar también lecturas y SSE según el modelo de acceso elegido.
- [x] El modo local seguro usa loopback y rechaza escrituras sin credenciales; cualquier modo de desarrollo inseguro es explícito.
- [x] MCP, CLI y workers autorizados siguen funcionando con la política activada.

## T-06 — Autorización de operaciones (H-05)

Aplicar permisos a finalizar/reasignar tareas, liberar locks, confirmar inbox y registrar decisiones. Derivar el actor del contexto autenticado. Distinguir operaciones de agente y administración humana.

- [x] Un agente no finaliza tareas ni confirma mensajes ajenos.
- [x] La reasignación administrativa requiere el permiso definido y deja trazabilidad.
- [x] Un cambio de propietario invalida los permisos anteriores de forma consistente.
- [x] Hay pruebas positivas y negativas por operación protegida.

## T-07 — Contrato de decisiones (H-06)

Compartir o adaptar explícitamente los modelos MCP/HTTP. Resolver generación de ID, campos requeridos y valores opcionales.

- [x] `record_decision` crea una decisión válida y esta puede recuperarse.
- [x] El payload mínimo documentado no produce HTTP 422.
- [x] Los argumentos inválidos generan errores accionables.
- [x] La integración final respeta la identidad definida por T-05/T-06.

Contrato funcional corregido en `b284c24`; identidad cerrada con `63a285e` + `e561e7f` y aceptación `7e58fe7`. MCP deriva el autor de la sesión, rechaza actores discordantes y el backend rechaza IDs de decisión duplicados con 409 sin alterar el registro existente.

## T-08 — Ciclo completo de mensajes

Agregar operaciones equivalentes a `read_messages(cursor, limit)`, `ack_messages`, `reply_message` e idempotencia de envío. Definir mensaje, conversación, correlación y entrega en el contrato. Implementación y migración: [messaging.md](docs/messaging.md).

- [x] Leer no confirma automáticamente; confirmar es explícito e idempotente.
- [x] Una respuesta conserva correlación con la solicitud y conversación.
- [x] Repetir un envío tras perder su respuesta no duplica el mensaje lógico.
- [x] Si el runner falla, el mensaje sigue recuperable y se registra el fallo.
- [x] Los mensajes confirmados no provocan un bucle infinito de espera/respuesta.
- [x] La paginación es estable y evita cargar el inbox completo en el contexto del agente.

## T-09 — Eventos recuperables y espera acotada

Persistir secuencia/cursor de eventos y usar SSE como señal. Cerrar la ventana entre consulta y suscripción. Definir retención y recuperación de un cursor vencido.

- [x] Un evento que llega entre lectura y suscripción se recupera.
- [x] Desconexión y reinicio no pierden entregas pendientes.
- [x] `wait_for_updates` termina en su plazo total incluso recibiendo keepalives.
- [x] Un cliente lento no produce crecimiento ilimitado de colas.
- [x] Cursor inválido/vencido, stream cerrado y bus caído tienen respuestas documentadas.

## T-10 — SDK MCP y transporte stdio

Elegir SDK mantenido y versiones compatibles con clientes objetivo. Migrar el transporte preservando los contratos corregidos; no basta con cambiar `PROTOCOL_VERSION`.

- [x] Un cliente MCP de prueba realiza initialize, tools/list y tools/call sobre un proceso stdio real.
- [x] Una espera activa permite atender otra solicitud y puede cancelarse.
- [x] EOF y cierre durante una espera liberan los recursos del proceso.
- [x] stdout contiene exclusivamente mensajes del protocolo; logs van a stderr.
- [x] Errores de herramienta y de protocolo se distinguen y los schemas se validan.
- [x] Cada conexión deriva su identidad de configuración/credenciales, sin remitente libre elegido por el modelo. Implementado antes del SDK en T-05; conservarlo al migrar.

## T-11 — Proyecto y sesión

Definir namespace, descubrimiento de raíz y aislamiento de configuración/datos. Incluir subdirectorios y worktrees. Revisar `quickstart`: conecta con AGENT_BUS_URL pero su autostart conserva 127.0.0.1:8420. Evitar identidad operativa única por nombre del proveedor.

- [x] Dos proyectos simultáneos no mezclan mensajes, tareas ni locks.
- [x] Dos sesiones del mismo proveedor tienen identidades distinguibles.
- [x] Cada entrada resuelve explícitamente el proyecto correcto desde un worktree o subdirectorio.
- [x] Una sesión expirada no mantiene permisos indefinidos. T-05 valida requests y revalida streams.
- [x] Si se admite reconectar una misma sesión desde varios procesos, se define quién puede consumir/ejecutar trabajo.

## T-12 — Renovación y alcance de locks

Definir recurso protegido, ruta canónica, propietario por sesión, lease y renovación. Coordinar el namespace con los worktrees.

- [x] Rutas equivalentes identifican el mismo recurso cuando corresponde.
- [x] Los recursos compartidos siguen protegidos y los archivos aislados no se bloquean entre sí por accidente.
- [x] Un lock abandonado expira según la política y una sesión viva puede renovarlo.
- [x] Un titular vencido no renueva ni libera el lock de su sucesor; usar token de adquisición o mecanismo equivalente.
- [x] Documentar los límites de los locks cooperativos frente a editores externos.

## T-13 — Aceptación del MVP y documentación realista

Elegir dos clientes reales y registrar versiones, configuración y evidencias. Crear una matriz que distinga comunicación, espera, continuación de sesión y headless. Corregir el README según resultados, incluyendo el alias Codex/Aider.

- [x] Dos agentes envían, leen, responden y confirman mensajes sin retransmisión humana.
- [x] El receptor se desconecta, recibe mensajes mientras está fuera y los recupera al volver.
- [x] Competir por una tarea o lock produce un único ganador.
- [x] Los reintentos controlados no duplican efectos en el escenario de aceptación.
- [x] Cada cliente demuestra su mecanismo de espera/activación o documenta explícitamente su limitación.
- [x] La suite completa pasa con servicios efímeros; las pruebas manuales incluyen pasos reproducibles.
- [x] README, configuración MCP y context.md reflejan lo comprobado.

## T-14 — Workers y adaptadores, posterior al MVP

- [ ] Proveedores tienen adaptadores propios o alias honestamente documentados; Codex no invoca Aider bajo una promesa de soporte nativo.
- [ ] Fallos del runner no descartan mensajes; reintentos y límites persisten tras reiniciar.
- [ ] Presupuestos y límites detienen o bloquean trabajo con estado observable, sin bucles silenciosos.
- [ ] La continuación de sesión se verifica por cliente y se distingue de iniciar otro subproceso.
- [ ] `submit` entrega el objetivo a los agentes previstos. T-08 normaliza `*` como broadcast en el hub; falta la aceptación completa del objetivo por el equipo. La prueba de CLI de T-01 comprueba la respuesta HTTP, no todo ese flujo.

## T-15 — Integración Git, posterior al MVP

- [ ] El integrador se conecta explícitamente al flujo de entrega de trabajo.
- [ ] Integra en un worktree dedicado, verificando rama objetivo, limpieza y SHA candidato.
- [ ] Se prueban los cambios combinados con la base actual antes de actualizar la rama objetivo.
- [ ] Se serializan integraciones y se verifican códigos de salida de merge, commit y abort.
- [ ] Un commit fallido nunca produce estado integrado; reintentos/conflictos quedan trazables.
- [ ] No se pierde trabajo sin commit durante limpieza de worktrees y se conserva trabajo ajeno.

## Registro de avance

| Fecha | Tarea | Resultado y evidencia |
| --- | --- | --- |
| 2026-09-05 | Preparación documental | Análisis, contexto y backlog creados. Sin correcciones de producción ni tareas de implementación completadas. |
| 2026-09-05 | T-01 | Completada: hubs efímeros y guardia HTTP; `902d480` + ajuste `762ffcf`. Cobertura de las nueve herramientas completada con T-07. |
| 2026-09-05 | T-02 | Completada: `0efe8ae` (origen `b7d904b`), migración y entregas independientes. 21 pruebas dirigidas en worktree del autor. Idempotencia HTTP general queda en T-08. |
| 2026-09-05 | T-03 | Completada: `bea86f8` + `4089b8e` (origen `165eac4` + `4b41ff9`). Revisión cruzada detectó cursor RETURNING activo; regresión falla antes y pasa después. 32 pruebas dirigidas. |
| 2026-09-05 | T-04 | Completada: `14f44e4` (origen `49c869f`). 14 pruebas dirigidas, contención con conexiones independientes y liberación frente a sucesor distinto. |
| 2026-09-05 | T-07 | Contrato funcional corregido en `b284c24`, 12 pruebas MCP; cierre de identidad bloqueado por T-05/T-06. |
| 2026-09-05 | Integración primera tanda | `218 passed` en 27,16 s sobre código combinado `4089b8e`; dos avisos de deprecación del soporte WebSocket de dependencias. `git diff --check` limpio. Sin aceptación con clientes MCP externos todavía. |
| 2026-09-05 | T-05 | Completada: configuración `8e0d600`, sesiones `398c4e8`, clientes `63a285e`, proxies `090dc6f`, aislamiento/hook `06c606b`. Tokens locales con expiración/revocación; provisión explícita. Se retira firma HTTP, sin promesa de anti-replay/idempotencia. |
| 2026-09-05 | T-06 | Completada: `e561e7f` (origen `c07ebed`). Autorización HTTP/WS/SSE, propiedad SQL y auditoría atómica. Revisión cruzada detectó mutación de decisión ajena por ID duplicado; corregida con 409 e inmutabilidad. 93 pruebas dirigidas y regresión final de handoff aprobadas. |
| 2026-09-05 | T-07 | Completada: principal vinculado a herramientas y decisiones; schemas seguros sin actor elegible. Regresión de suplantación y persistencia contra hub real efímero en `7e58fe7`. |
| 2026-09-05 | Integración segunda tanda | **303 passed**, dos avisos de deprecación WebSocket, en **45,63 s**, sobre código combinado `7e58fe7`. `UV_PROJECT_ENVIRONMENT=/home/Yasmany/src/agent-bus/.venv PYTHONPATH=$PWD/src uv run --no-sync pytest -q`; Bash/JavaScript y `git diff --check` correctos. Aceptación CLI → credenciales → create_app → MCP/HTTP en cinco pruebas. Sin cliente MCP externo ni navegador real todavía. |

La segunda tanda se integra localmente a `main`. El hub de coordinación que ya estaba en ejecución conserva el proceso anterior; no se reinició ni se detuvieron listeners. Para activar la política en ese servicio, provisionar sus sesiones y reiniciar servidor/clientes con la configuración documentada. La siguiente tanda, T-08, se documenta a continuación.


## Registro de T-08

Completada en worktrees separados, integrados localmente a `main` después de verificar el código combinado:

- `5ab3deb` (origen `371f42f`): secuencias y estados de entrega, migración, idempotencia, cursor firmado y reply+ack atómico. 36 pruebas dirigidas de almacenamiento.
- `d5c139e` (origen `cf22a87`): herramientas MCP de respuesta/confirmación, páginas e idempotencia; CLI con claves recuperables e IDs completos.
- `2d33fc7` (origen `a6acf45`): worker y watcher conservan fallos, consultan almacenamiento y confirman solo al guardar una respuesta.
- `12e18fe` (origen `504a132`): errores JSON de Claude y cancelación del subproceso no producen confirmaciones incorrectas.
- `607b678`: endpoints HTTP/WS autenticados, aprobación humana atómica y seis escenarios de aceptación/regresión con backend real o ASGI aislado.
- `a774a8c`: watcher rechaza salida Claude malformada/no textual; fallos sin detalle conservan un error registrable.

Validación final: **374 passed**, dos avisos de deprecación WebSocket, en **65,92 s** sobre `a774a8c`. Comando: `UV_PROJECT_ENVIRONMENT=/home/Yasmany/src/agent-bus/.venv PYTHONPATH=$PWD/src uv run --no-sync pytest -q`. `git diff --check` limpio. La revisión cruzada encontró y corrigió el caso de una entrega confirmada eliminada entre lectura y consulta de estado, además del error JSON con exit code cero y del subproceso que seguía vivo tras cancelar.

Garantías delimitadas: conservar la clave al reintentar; HTTP sin clave sigue como compatibilidad; la retención de un padre limita respuestas posteriores; múltiples procesos de una misma identidad todavía requieren T-11. La idempotencia del envío no hace exactamente una vez los efectos externos del runner. Los tests no sustituyen T-13 con dos clientes MCP externos.

**Al cerrar T-08, siguiente tarea: T-09**, completada en el registro siguiente. El hub de coordinación previo conserva su proceso anterior; no se detuvieron listeners ni se migró ese servicio durante esta implementación.


## Registro de T-09

Completada el 2026-09-06 con subagentes y worktrees separados; integración manual a `main` tras verificar el código combinado. Antes de comenzar, T-08 se publicó en `origin/main` hasta `063ce22`.

- `ea80a5c` (origen `2923754`): parser SSE compartido, cursores y reconexión autenticada de worker/panel; pruebas de JavaScript ejecutadas con Node.
- `e986f3e` (origen `157afd5`): SSE durable personal/global, checkpoints, respuestas 422/410, recuperación por reset y señales coalescidas.
- `817e2dc` (origen `cc58511`): evento atómico con entrega, cursores firmados, retención, migración única de pendientes y protección contra recrear una entrega purgada.
- `0ebefd8` (origen `e247b14`): no registrar un suscriptor antes de iniciar el generador; evita fugas si el transporte aborta al enviar encabezados.
- `85773c1`: MCP captura cursor antes de consultar, aplica plazo total, distingue errores y descarta eventos históricos como trabajo nuevo; reset despierta el polling del daemon.

Validación final: **446 passed**, dos avisos de deprecación WebSocket, en **96,03 s** sobre `85773c1`. Comando: `UV_PROJECT_ENVIRONMENT=/home/Yasmany/src/agent-bus/.venv PYTHONPATH=$PWD/src uv run --no-sync pytest -q`. `git diff --check` limpio. Las pruebas incluyen reinicio de SQLite/hub, entrega entre lectura y suscripción contra HTTP real, falta de aviso tras commit, clientes lentos, revocación, expiración de cursor, rollback e idempotencia, deadline con keepalives y cancelación de coroutine.

La revisión cruzada corrigió el evento confirmado que acompañaba a pendientes distintos, la recreación legacy de una entrega purgada y el registro prematuro del suscriptor. Contrato y límites de retención, errores y recuperación en [events.md](docs/events.md).

**Al cerrar T-09, siguiente tarea: T-10**, completada en el registro siguiente. La aceptación con aplicaciones externas sigue pendiente de T-13. El hub de coordinación previo no se reinició; los listeners permanecen activos y las pruebas usaron servicios efímeros.


## Registro de T-10

Completada el 2026-09-06 con subagentes y worktrees separados; integración manual a `main` después de probar el conjunto. Antes de comenzar se publicó T-09 en `origin/main` hasta `7fa6b54`.

- `f34662e` (origen `6ec6104`): pruebas mediante el cliente SDK, validación de schemas, errores de herramienta/protocolo y vínculo de identidad; conserva escenarios HTTP reales de las tandas anteriores.
- `718143f`: SDK oficial Python `mcp==2.1.1`, dependencias fijadas en `uv.lock`, servidor de bajo nivel, resultados estructurados y textuales, validación antes de vincular identidad, CLI con URL explícita/entorno y tuberías cancelables para stdio.
- `910e854`: errores de validación entregados por el SDK se convierten a respuestas JSON-RPC -32700/-32600, en lugar de descartarse sin respuesta.
- `7fad883` (origen `0fea789`): nueve escenarios con procesos CLI reales, cliente SDK y peer JSON-RPC independiente contra hub y credenciales efímeros.

Validación final: **493 passed**, dos avisos de deprecación WebSocket, en **109,70 s** sobre `7fad883`. Comando desde el integrador: `uv run --locked pytest -q`, usando su `.venv` preparada mediante `uv sync --extra dev`. `ruff check --select F,E9` sobre MCP y los contratos nuevos, y `git diff --check`, limpios. El entorno raíz del hub previo no se sincronizó durante esta tanda.

La aceptación automatizada verifica SDK 2.1.1/moderno `2026-07-28`, initialize legacy `2024-11-05` y `2025-06-18`, listado/llamada de herramientas, envío/lectura autenticados, espera concurrente, cancelación por ID que libera SSE, EOF/stdout roto, salida saturada, JSON malformado y límite de entrada de 4 MiB sin ejecutar el mensaje. Los tests no usan el bus de coordinación real.

La revisión y las reproducciones detectaron bloqueos del transporte por lectura/escritura en threads, espera indefinida de drain tras EOF y descarte silencioso de errores de entrada. La capa local conserva el parser/serializador del SDK y corrige esos cierres/respuestas; no reintroduce un dispatcher JSON-RPC propio. El contrato y el cambio de API Python se documentan en [mcp-setup.md](docs/mcp-setup.md).

**Siguiente tarea: T-11**, aislamiento por proyecto y sesión. La aceptación con dos aplicaciones cliente externas, sus mecanismos de reactivación y navegador completo sigue en T-13; estas pruebas de procesos se ejecutaron en Linux. El hub previo y los listeners permanecen activos, sin migrar ese servicio automáticamente.


## Registro de T-11

T-10 publicado previamente en `origin/main` hasta `9010455`. Desarrollo paralelo en worktrees de configuración, seguridad y workers; integración revisada en `agent/codex-integrator` hasta `6e573d5`.

Se implementó un hub/base por proyecto, vinculación persistente del propietario de la base, descubrimiento ascendente y canónico entre worktrees, runtime y contexto aislados, URL configurada en todos los clientes, identidades generadas por proveedor y exclusión local de ejecutores automáticos. MCP conserva proyecto/URL/sesión; `--project` conserva el significado de rutas relativas de invocación. Contrato y migración en [projects.md](docs/projects.md).

Validación: **539 passed**, dos avisos de deprecación WebSocket, en **129,10 s** con `uv run --locked pytest -q`. Tras el ajuste final de rutas relativas de `--project`, **28 passed** en **9,19 s** sobre CLI, clientes autenticados y aceptación de dos hubs; incluye una regresión adicional. `ruff check --select F,E9` sobre configuración, proyecto, seguridad, DB, bus, CLI principal, MCP, guard y nuevos contratos seleccionados, más `git diff --check`, limpios.

La aceptación automatizada cubre dos hubs HTTP simultáneos con nombres de tarea/lock/clave idénticos, rechazo de credenciales/cabeceras cruzadas, contextos persistidos independientes, MCP fijado pese a cambios de entorno, provisión CLI desde un worktree real/subdirectorio, autostart en el puerto configurado, migraciones legacy y exclusión entre procesos con recuperación por muerte del propietario.

Límites: varias conexiones manuales con la misma identidad comparten autoridad e inbox; no se promete consumo exclusivo por lectura. El guard automático es cooperativo, local Unix y requiere la misma base canónica; no es lease distribuida ni garantía exactamente una vez de efectos externos. Bases legacy con sesiones de distintos proyectos fallan sin separación automática.

**Siguiente tarea: T-12**, alcance, propietario por sesión y renovación de locks de recursos. T-13 conserva aceptación con aplicaciones MCP externas. Se mantienen el hub previo y los listeners, sin reiniciar ni migrar automáticamente el servicio de coordinación.


## Registro de T-12

T-11 estaba publicado en `origin/main` hasta `2a75289`. Desarrollo aislado en worktrees de almacenamiento, rutas y clientes; integración hasta `33eb90e` en `agent/codex-integrator`.

Implementado:

- Scope físico `checkout` y lógico compartido `project`, con rutas canónicas, aliases por symlink, soporte de subdirectorios/worktrees y rechazo de escapes del proyecto.
- Leases por sesión y token de adquisición, TTL 300 s por defecto (1–3600 s), limitado por la sesión. Reclamación de vencidos y renovación/liberación condicionales y atómicas; un token viejo no afecta a su sucesor, incluso bajo el mismo agente/sesión.
- `renew_lock` MCP y `work renew-lock` CLI; token obligatorio al renovar/liberar. Listados y dashboard omiten el token. Instrucciones generadas, runner y AGENTS.md explican la conservación y renovación.
- Migración transaccional de locks históricos, que quedan vencidos cuando no contienen una lease. Lectura del reloj tras obtener acceso de escritura SQLite; contención por transacción pendiente devuelve 503 reintentable sin confirmar trabajo ajeno.

Validación final: **592 passed**, dos avisos de deprecación WebSocket, en **137,77 s**, con `uv run --locked pytest -q`. Antes, **117 pruebas dirigidas** de almacenamiento, HTTP y MCP pasaron en **35,85 s**, y **19 pruebas de clientes/rutas** en **0,71 s**. `ruff check --select F,E9` sobre implementación de locks/rutas, bus, DB, tipos, CLI, MCP y nuevas pruebas, más `git diff --check`, limpios. Revisión cruzada sin hallazgos materiales pendientes.

Contrato y migración: [locks.md](docs/locks.md). Límites explícitos: renovación deliberada, sin fencing de escrituras del filesystem ni renovación automática por heartbeat; la revocación impide nuevas operaciones pero la lease existente conserva su vencimiento; `tasks/lock-files` sigue siendo metadato. El guard de ejecución local de T-11 es independiente.

**Siguiente tarea: T-13**, aceptación con aplicaciones MCP externas y documentación realista de comunicación, espera y reactivación. El hub previo y los listeners permanecen activos; no se reinicia ni migra automáticamente ese servicio.

## Registro de T-13

Aceptación sobre T-12 (`ca847a8`) en el worktree `agent/codex-t13-acceptance`; harness y evidencia pública en `a571714`. Clientes elegidos por disponibilidad y autenticación local: **Claude Code 2.1.185 / GLM-5.1** y **Codex CLI 0.153.4 / gpt-6-astra**. No se cambió su configuración global ni se utilizaron los adaptadores del worker.

Se verificaron descubrimiento MCP, intercambio sin retransmisión humana, espera SSE de Claude anterior al envío, reintentos con claves estables, competencia con un único ganador por tarea/lock, mensaje persistido mientras Claude estaba desconectado, y reanudación explícita de ambos clientes conservando ID y un marcador de contexto no repetido en el nuevo prompt. Los cuatro turnos terminaron correctamente. Resultado: cuatro operaciones lógicas, cuatro entregas y tres ACK; la respuesta final permanece pendiente porque la corrida termina al enviarla. La tarea terminó y el lock se liberó.

La auditoría adicional leyó SQLite, trazas y orden de peticiones: un HTTP 200 y un 409 por competencia, reintentos reales de envío/respuesta, confirmaciones realizadas por ambos clientes y ausencia de acciones shell/archivo en los turnos. Se conservan hashes de las trazas y una evidencia sanitizada sin tokens en [docs/evidence/t13.json](docs/evidence/t13.json).

Validación final: **592 passed**, dos avisos de deprecación WebSocket, en **138,75 s** mediante `PYTHONPATH=$PWD/src ../codex-integrator/.venv/bin/python -m pytest -q`. La aceptación con modelos se ejecuta separadamente mediante `scripts/acceptance_real_clients.py`; su verificación de artefactos no vuelve a llamar a los modelos. `ruff check --select F,E9` del harness y `git diff --check`, limpios.

README, configuración MCP y context.md distinguen comunicación, espera, continuación de conversación y headless. Se retira la promesa de autonomía universal; el alias `codex` de `AgentRunner` sigue invocando Aider y queda declarado expresamente. La skill OpenAI Docs ayudó a contrastar la configuración y reanudación nativas de Codex; los resultados se basan en las ejecuciones registradas.

Matriz, comandos y límites en [docs/acceptance-t13.md](docs/acceptance-t13.md). No se acreditan TUI que se despierta sola, hooks Stop en aplicación viva, navegador ni AGY/Aider/Grok. **Siguiente tarea: T-14**, workers recuperables y adaptadores; después T-15. El hub personal y sus listeners permanecen activos con su versión anterior, y los hubs de aceptación se cerraron al terminar.
