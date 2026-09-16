# Roadmap de producto y operación de agent-bus

Actualizado: **2026-09-15**. Base publicada: **`433a08a` en `origin/main`**.

`agent-bus` combina coordinación MCP, identidad autenticada, entrega durable,
workers, worktrees y revisión/integración Git. La prioridad de producto es hacer
ese flujo sencillo de operar, recuperable y medible antes de ampliar el alcance
comercial. El núcleo es local; on-premise empresarial y SaaS son objetivos futuros.

Este documento establece prioridades y criterios de salida. [TASK.md](../TASK.md)
es el backlog técnico y [context.md](../context.md) resume el estado y su historial.

## 1. Estado comprobado

La última suite completa del código publicado terminó con **710 pruebas aprobadas**,
dos avisos de deprecación WebSocket, en **177,69 s**. Se ejecutó antes de publicar
`433a08a`; la presente revisión documental no constituye una ejecución nueva.

| Capacidad | Estado | Evidencia y límites |
|---|---|---|
| Identidad y coordinación básica | Implementada | Sesiones Bearer por proyecto, autorización, entregas independientes, ACK, reintentos y cursores durables |
| Locks T-12 | Implementados | Scopes `checkout`/`project`, TTL y token por adquisición/sesión; protección cooperativa, no intercepta escrituras externas |
| Clientes externos T-13 | Aceptación histórica | Claude Code y Codex CLI; consultar [matriz](acceptance-t13.md). No extender sus resultados a todos los proveedores |
| Workers e integración T-14/T-15 | Implementados con pilotos acotados | Adaptadores, worktrees, commit y cola `in_review`; falta medir operación sostenida y recuperación por proveedor |
| Tareas y DAG T-17 | Implementados | Dependencias, criterios de aceptación, comandos de prueba y desglose idempotente |
| Hermes T-18 | Adaptador y aceptación histórica | HTTP de inferencia separado de Hermes Agent por MCP; publicación por puente HTTP, sin worker Hermes ni tool MCP de desglose |
| Gatekeeper T-19 | Implementado | Evaluación por reglas y evidencia de tests; no equivale a revisión semántica independiente de todos los criterios |
| Consola T-21 | Implementada parcialmente | React local, API admin, eventos SSE, tareas, mensajes, reasignación y pausa/reanudación; faltan worktrees y aceptación completa en navegador |
| Coordinación compacta T-23 | Implementada y publicada | Cinco tools nuevas, reservas multiarquivo y handoff transaccional; pruebas con dos procesos MCP reales y conexiones SQLite independientes |
| Cuotas y métricas T-20 | Pendientes | `/room/api/usage` conserva `available=false`; un contrato y un presupuesto en memoria no son control persistente de gasto |
| Enterprise T-22 | Propuesto | Integraciones SDLC, SSO/RBAC, auditoría exportable y operación aislada todavía no acreditadas |

**Conclusión de madurez:** base técnica y coordinación local utilizables en pilotos
supervisados. La suite automatizada no demuestra autonomía continua, recuperación
sin intervención ni preparación comercial de todas las capacidades.

## 2. Entrega reciente: coordinación compacta T-23

Se adaptó el flujo útil de `agents-mcp-workspace` a las garantías de `agent-bus`,
sin importar su base ni crear otro estado de coordinación:

- `bootstrap_agent`: instrucciones, sesión existente, agentes, decisiones, tareas
  libres y pendientes propios en una llamada.
- `my_pending_items`: mensajes y tareas propias paginados; leer no confirma.
- `prepare_edit`: todos los archivos o ninguno, tokens de adquisición y claves
  persistentes por sesión; rechaza reintentos de reservas que dejaron de estar vigentes.
- `complete_handoff`: estado de tarea, mensaje, evidencia declarada, auditoría,
  ACK y liberaciones explícitas en una transacción; replay sin duplicar efectos.
- `get_agent_instructions`: protocolo de uso sin cambiar estado.

También se corrigió la regresión de credenciales (`--show-token` explícito) y se
reforzó el integrador: candidato/base capturados antes de tests, estado estable y
limpio, veredicto ligado al SHA y merge del commit revisado, no de una rama mutable.

[Guía del flujo y sus límites](coordination-workflow.md). El handoff no ejecuta
comandos de validación ni fusiona código. Las pruebas nuevas usan dos servidores
MCP stdio como procesos reales; no son una aceptación nueva con aplicaciones o
modelos externos. Para usar las tools hay que actualizar el hub y reconectar MCP.

## 3. Próximas prioridades y criterios de salida

### Prioridad 1 — T-20: cuotas, presupuesto y consumo persistente

El control de consumo debe preceder a una oferta de ejecución autónoma comercial.

- [ ] Registrar proveedor, modelo, tokens, duración y coste por agente, tarea y proyecto.
- [ ] Persistir límites y consumo para que sobrevivan al reinicio del hub/worker.
- [ ] Definir cómo tratar proveedores sin métricas: mostrar desconocido, no cero ficticio.
- [ ] Detener nuevas ejecuciones al alcanzar un límite y mostrar una causa accionable.
- [ ] Publicar métricas reales respetando el contrato existente de `/room/api/usage`.
- [ ] Probar límites con ejecuciones concurrentes, reintentos y reinicios sin doble conteo.

**Salida:** el operador puede consultar gasto/consumo, fijar límites y comprobar
que se respetan tras fallos y reanudaciones, sin editar SQLite.

### Prioridad de lanzamiento — Primer uso y aceptación externa

El [plan de primera versión pública](launch-plan.md) fija la secuencia inmediata:
onboarding MCP, consola única, demo reproducible, release y pilotos externos antes
de difusión. Esta secuencia de lanzamiento precede a las ampliaciones de producto
enumeradas aquí; no convierte las cuotas pendientes en una garantía disponible.

### Prioridad 2 — Recuperación guiada y aceptación operativa

`agent-bus onboard` ya existe. Su evolución debe reducir los pasos manuales que
siguen apareciendo al operar con proveedores y proyectos reales.

- [ ] Diagnosticar puerto ocupado, hub de otro proyecto y credencial vencida o revocada.
- [ ] Guiar renovación de credenciales y reconexión MCP sin mostrar secretos por defecto.
- [ ] Validar permisos del CLI/proveedor, dependencias y entorno del worktree antes de ejecutar.
- [ ] Reproducir caída de worker, interrupción del integrador y respuesta perdida tras una mutación.
- [ ] Aplicar el flujo T-23 con aplicaciones externas; registrar versiones y límites por proveedor.
- [ ] Definir conservación, respaldo y limpieza de registros de idempotencia sin reabrir efectos antiguos.

**Salida:** otra persona reproduce instalación, tarea, fallo y recuperación con
una guía; quedan medidas las intervenciones humanas y la ausencia de entregas,
ACK o integraciones duplicadas. No prometer exactamente una vez para efectos
externos que el bus no controla.

### Prioridad 3 — Completar T-21 y la experiencia de supervisión

La consola React unificada está disponible en `/console` y `/room`.
Ver [comportamiento y aceptación de navegador](console.md).

- [x] Arranque local con `agent-bus ui`, sin SaaS.
- [x] Tareas, agentes, locks, mensajes y eventos SSE sobre API autenticada.
- [x] Creación/reasignación de tareas y pausa/reanudación de workers.
- [ ] Vista de worktrees y relación entre tarea, rama, revisión y commit integrado.
- [x] Prueba end-to-end de desconexión/reconexión en Chromium real, con replay.
- [x] Detalle de tareas, solicitudes completas, filtros y aislamiento entre sesiones.
- [ ] Validación manual del flujo completo y de estados de error/credencial vencida.
- [ ] Mostrar consumo real cuando T-20 lo suministre; mantener estado explícito de datos no disponibles.

**Salida:** un operador crea y sigue trabajo, pausa, reasigna y recupera la vista
tras desconexión desde el navegador. Distingue evidencia declarada de validación
ejecutada y puede identificar qué commit se revisó e integró.

### Prioridad 4 — T-22: capacidades empresariales

Después de cerrar los controles operativos anteriores:

- [ ] Sincronización de tickets y PRs con GitHub/GitLab/Jira, con reglas de propiedad e idempotencia.
- [ ] SSO y roles empresariales; los roles locales agente/admin no sustituyen ese modelo.
- [ ] Auditoría exportable, retención y respaldo/restauración probados. El log SQLite
  actual no acredita inmutabilidad ni certificación SOC 2/ISO 27001.
- [ ] Instalación on-premise y ejecución sin acceso exterior verificadas con proveedores locales.
- [ ] Definir aislamiento entre organizaciones antes de ofrecer una modalidad SaaS.

**Salida:** instalación, operación, respaldo y recuperación documentados y probados
en el entorno de destino, con responsabilidades de soporte y permisos explícitos.

## 4. Fases del producto

Las fases conservan su identificador original; el estado evita volver a planificar
como inexistentes piezas que ya se implementaron.

| Fase | Estado al publicar T-23 | Trabajo para cerrar la fase operativa |
|---|---|---|
| P0 — Operación real | Ciclo worker/integrador y pilotos acotados existentes | Repetir con proveedores reales, fallos y recuperación guiada |
| P1 — Contrato de tareas | DAG y contrato T-17 implementados | Medir calidad de planes, asignaciones y criterios en los pilotos |
| P2 — Orquestación | Adaptador HTTP y conexión Hermes Agent existentes | Ampliar aceptación; cualquier worker Hermes/tool de desglose requiere alcance propio |
| P3 — Control operativo | Consola parcial y coordinación T-23; cuotas pendientes | T-20, worktrees visibles y aceptación del navegador |
| P4 — Enterprise | Propuesto | Depende del cierre operativo de P0–P3 y de requisitos del despliegue objetivo |

Medir en cada piloto: tareas terminadas, tiempo hasta `in_review`, reintentos,
conflictos de merge, esperas de locks, recuperación tras reinicio, intervención
humana, consumo y coste por tarea. Hasta T-20, declarar qué datos se midieron
externamente y cuáles siguen desconocidos.

## 5. Decisiones de producto y proveedores

El primer caso comercial propuesto sigue siendo deuda técnica y migraciones:
trabajo acotado, ramas aisladas y aceptación verificable. Una experiencia simple
para los agentes debe convivir con garantías de identidad, reintentos y locks.

Mantener una sola fuente de estado en el hub. Extender las operaciones compactas
de T-23 con evidencia de uso; no duplicar almacenamiento ni sustituir las leases
por identidades declaradas libremente. Mantener modelos y aplicaciones como
adaptadores intercambiables.

La tabla anterior de modelos concretos era una propuesta histórica, no un
benchmark vigente. La selección debe basarse en pruebas del proyecto:

| Rol | Qué evaluar antes de elegir proveedor/modelo |
|---|---|
| Planificación | Validez de JSON Schema/DAG, criterios verificables y coste del desglose |
| Implementación | Calidad del cambio, permisos, reanudación, pruebas aprobadas y coste por tarea terminada |
| Revisión | Capacidad de detectar defectos, independencia y vínculo de la evidencia con el SHA |
| QA | Regresiones útiles y reproducibles, sin depender sólo de pruebas que imiten la implementación |

Distinguir aplicación, modelo y endpoint: Hermes Agent es una aplicación cliente;
`HermesOrchestrator` es un adaptador HTTP; Aider es una aplicación, no un modelo.
La compatibilidad declarada no sustituye la aceptación del proveedor elegido.

## 6. Verticales posteriores

Son hipótesis de producto, no funcionalidades entregadas:

- **Data engineering:** cambios SQL/dbt y validación de calidad/coste de pipelines.
- **SRE/DevOps:** diagnóstico y propuestas de hotfix con aprobación humana para acciones operativas.
- **Documentación técnica:** propuestas de actualización ligadas a cambios de código y revisión.

Cada vertical necesita su contrato de tareas, límites de permisos, entorno de
validación y métricas de aceptación antes de considerarse parte de la oferta.
