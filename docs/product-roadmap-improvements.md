# Plan de Mejoras para Comercialización y Casos de Uso Empresariales

Este documento define el roadmap para convertir `agent-bus` en un producto de coordinación de agentes desplegable en local, on-premise o SaaS. El producto central es el bus, la identidad, la entrega durable, los worktrees y el gate de integración. El adaptador HTTP `HermesOrchestrator` y la conexión MCP de Hermes Agent están implementados; la aceptación T-18 usa un puente de publicación validado, descrito en [hermes-agent.md](hermes-agent.md). Ninguno es un requisito del núcleo.

---

## 1. Mejoras Técnicas Necesarias por Nivel

### Estado actual: core implementado, operación real pendiente de ampliar
* [x] **T-12: Locks Robustos y Fencing Tokens:**
  - Scope `checkout` y `project`, canonicalización de rutas y rechazo de escapes de worktree.
  - Leases por sesión, TTL acotado (300 s default), reclamación de expirados y tokens de adquisición (`acquisition_id`) para evitar colisiones por titulares desfasados.
* [x] **T-13: Validación con clientes reales:**
  - Claude Code y Codex CLI validados mediante stdio; AGY y Grok tienen pruebas headless del runner.
  - La matriz completa y sus límites están en `docs/acceptance-t13.md`.
* [x] **T-14: Workers recuperables y adaptadores nativos:**
  - Adaptadores separados para Claude, Codex, Aider, AGY y Grok.
  - Intentos persistentes y sesiones reanudables; la aceptación de cada proveedor sigue siendo desigual.
* [x] **T-15: Integración Git automatizada:**
  - `BranchIntegrator` con verificación de estado previo (preflight), merges protegidos y rollback seguro ante fallos.
  - Cola `in-review` y comandos CLI del ciclo de vida del integrador (`agent-bus integrator start/status/stop`).
* [x] **Onboarding y experiencia interactiva:**
  - `agent-bus onboard` crea el proyecto, credenciales, hub, worktrees y workers con confirmación.
  - Cada proyecto puede aislar su puerto para no mezclar hubs.

**Limitación operativa actual:** el flujo local está listo para una prueba controlada, pero todavía hay que medir recuperación, costes y comportamiento de cada proveedor en proyectos reales. El dashboard existente es operativo; no es aún la Team Edition web descrita más adelante.

---

### Nivel 2: Orquestación estructurada (siguiente prioridad)
* [x] **Contrato de inferencia y `HermesOrchestrator` HTTP:**
  - Soporte para endpoints compatibles con OpenAI / vLLM / Ollama / OpenRouter para conectar instancias de Hermes (ej. `Hermes-3-Llama-3.1-70B` / `Hermes-3-8B`).
  - Solicitud de Structured Outputs y validación local de JSON Schema y DAG antes de publicar; una respuesta inválida se rechaza. Evidencia automatizada con inferencia simulada en TASK.md.
* [x] **Desglose mediante inferencia HTTP:**
  - `breakdown` / `orchestrate` convierten un objetivo textual en un DAG validado y publican tareas en lote con `operation_key`.
* [x] **Conexión y aceptación real de Hermes Agent (T-18):**
  - Hermes Agent v0.21.0 / gpt-5.6-sol conectado por MCP con identidad propia; mensajes, ACK, reanudación y desglose idempotente verificados. Publicación mediante puente Python al batch HTTP; no se implementó un worker Hermes ni una herramienta MCP de creación.
* [x] **Gatekeeper de integración (T-19):**
  - `CodeReviewGatekeeper` evalúa diff y resultados de tests mediante reglas; `BranchIntegrator` aplica la política `--require-approval` y registra el veredicto. La implementación actual no utiliza Hermes como revisor.


---

### Nivel 3: Team Edition y control operativo
* [ ] **Dashboard Web Interactivo Moderno (React / WebSockets):**
  - Visualización en tiempo real de agentes activos, roles, archivos bloqueados (locks), tareas en progreso y flujo de mensajes.
  - Interfaz gráfica para que los desarrolladores humanos puedan pausar agentes, reasignar tareas o inyectar feedback humano en el bucle (*Human-in-the-Loop*).
* [ ] **Gateway de Cuotas y Costes (Budget Control):**
  - Límite de gasto por tarea, sprint o agente para evitar que un agente en bucle consuma créditos excesivos de APIs comerciales.
  - Métricas de consumo de tokens y tiempo de resolución por modelo y por agente.
* [x] **CLI simplificado inicial (`agent-bus onboard`):** prepara un proyecto con preguntas y confirmaciones.
* [ ] **Evolución del onboarding:** detectar puertos ocupados, validar autenticación de proveedor y ofrecer recuperación guiada.

---

### Nivel 4: Nivel Enterprise (On-Premise / Seguridad / SDLC)
* [ ] **Integración Bidireccional con Jira / GitHub Issues / GitLab:**
  - Sincronización automática: un nuevo ticket en Jira crea una tarea en el bus; el cierre exitoso por el agente actualiza el estado del ticket con el link al PR.
* [ ] **Auditoría Inmutable y Compliance (SOC2 / ISO27001):**
  - Registro de auditoría persistente y exportable que detalle cada cambio de código, decisión de arquitectura y modelo utilizado.
* [ ] **Autenticación Empresarial (SSO / RBAC):**
  - Roles de acceso (Administrador, Desarrollador, Solo Lectura, Auditor) integrados con Okta / Google Workspace / Azure AD.
* [ ] **Soporte Air-Gapped / Multi-Model 100% Local:**
  - Pipeline probado usando únicamente modelos locales de código abierto:
    - **Orquestador / PM:** Hermes 3 (70B / 8B)
    - **Coders Especializados:** DeepSeek-Coder-V2 / Qwen2.5-Coder (32B / 7B)
    - **Revisores / Testers:** Llama-3.3 / StarCoder2

---

## 2. Plan ejecutable y criterios de salida

| Fase | Objetivo | Dependencias | Criterio de salida |
| :--- | :--- | :--- | :--- |
| P0. Operación real | Completar una tarea con worker e integrador en un repositorio de prueba | Core actual | `submit → commit → in_review → tests → merge → done` reproducible |
| P1. Contrato de tareas | Añadir dependencias, criterios de aceptación, comando de pruebas y DAG persistente | P0 | Hermes crea tareas idempotentes y el bus rechaza ciclos |
| P2. Orquestador | Adaptador OpenAI-compatible/vLLM/Ollama con JSON Schema | P1 | Un desglose válido produce tareas trazables y asignables |
| P3. Control operativo | Dashboard web, cuotas, costes, pausas y feedback humano | P0, P1 | Un operador detiene, reasigna y audita sin editar SQLite |
| P4. Enterprise | Integraciones SDLC, SSO, auditoría exportable y air-gapped | P1–P3 | Instalación y recuperación documentadas y probadas |

Cada entrega debe medir: tareas que llegan a `done`, tiempo hasta `in_review`, reintentos, conflictos de merge, bloqueos, coste por tarea, tiempo de resolución y recuperación tras reinicio. No se debe cerrar una fase por tener sólo clases o mocks.

## 3. Decisiones de producto

El primer mercado recomendado es mantenimiento de deuda técnica y migraciones: tareas acotadas, ramas aisladas, pruebas automatizadas y resultado verificable. Data engineering, SRE y documentación viva quedan como verticales posteriores.

Hermes debe ser un adaptador intercambiable. El contrato del bus no debe depender de un modelo concreto; se selecciona por capacidad, coste, latencia, ventana de contexto, despliegue y calidad de salida estructurada.

El gateway de cuotas y costes debe preceder a la ejecución autónoma comercial: limita por agente, tarea y proyecto, registra tokens y tiempo, y detiene trabajo con estado visible.

## 4. Modelos de Coding Recomendados por Rol

| Rol | Modelo Recomendado | Despliegue / Ubicación | Justificación |
| :--- | :--- | :--- | :--- |
| **Orquestador / Tech Lead** | **Hermes 3 (Llama-3.1-70B / 8B)** | Local (vLLM) / OpenRouter | Excelente razonamiento en asignación de tareas, bajo coste continuo y llamadas a funciones robustas. |
| **Arquitecto / Heavy Refactor** | **Claude 3.7 Sonnet / DeepSeek-V3** | API Cloud / Local | Máxima precisión en refactorizaciones de múltiples archivos y diseño de sistemas complejos. |
| **Implementador / Fast Coder** | **Qwen2.5-Coder-32B / DeepSeek-Coder** | Local (vLLM / Ollama) | Muy rápido, eficiente en tareas atómicas y edición directa de funciones con cero coste de API externa. |
| **Generador de Tests / QA** | **Aider + Qwen2.5-Coder-7B** | Local | Enfoque quirúrgico en cobertura de pruebas unitarias y verificación de regresiones. |

---

## 5. Otras Posibilidades de Uso y Mercados Verticales

Más allá del desarrollo de software convencional, esta arquitectura de bus coordinado con Hermes abre varios casos de uso adicionales de alto valor:

### A. "Virtual Data Engineering Team" (Pipelines y Calidad de Datos)
* **Caso de uso:** Equipos de datos donde un agente escribe consultas SQL/dbt, otro genera esquemas de validación y Hermes valida que los pipelines no rompan dependencias ni generen sobrecostes en BigQuery/Snowflake.

### B. Mantenimiento Autónomo de Deuda Técnica y Migraciones
* **Caso de uso:** Tareas continuas de fondo (migrar de Python 3.10 a 3.12, actualizar dependencias con vulnerabilidades CVE, refactorizar tests a `pytest`).
* El orquestador toma tickets de seguridad del repositorio, asigna branches aisladas a los agentes y sólo genera PRs cuando todos los tests y análisis estáticos pasan.

### C. Soporte y Resolución Rápida de Incidentes (SRE / DevOps)
* **Caso de uso:** Un agente analiza logs de fallos en producción, otro localiza la línea causante en el repositorio, un tercero redacta el hotfix y Hermes coordina la validación antes de alertar al ingeniero de guardia.

### D. Creación de Contenido Técnico y Documentación Viva
* **Caso de uso:** Enjambre que audita el código en cada commit y sincroniza automáticamente la documentación de API (OpenAPI, Markdown, tutoriales) manteniendo el contexto actualizado sin intervención humana.
