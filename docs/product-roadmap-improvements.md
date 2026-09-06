# Plan de Mejoras para Comercialización y Casos de Uso Empresariales

Este documento detalla el **roadmap técnico y de producto** necesario para convertir `agent-bus` en un producto monetizable (Open-Core / On-Premise / SaaS Empresarial) con **Hermes AI** como orquestador central y modelos de coding especializados.

---

## 1. Mejoras Técnicas Necesarias por Nivel

### Nivel 1: Conclusión del Core y Estabilidad Base (T-12 y T-13)
* [ ] **T-12: Locks Robustos y Fencing Tokens:**
  - Garantizar que los locks expiren limpiamente y que un titular expirado no pueda sobreescribir ni liberar el lock de un nuevo titular.
  - Canonicalización de rutas relativas/absolutas entre diferentes worktrees de Git.
* [ ] **T-13: Validación con Clientes Reales:**
  - Validación automatizada y manual de interoperabilidad stdio con al menos dos clientes MCP externos reconocidos (ej. Claude Code, Aider, Cursor).

---

### Nivel 2: Integración del Orquestador Hermes AI (El "Tech Lead Autónomo")
* [ ] **Módulo `HermesOrchestrator` / Adapter de Inferencia:**
  - Soporte para endpoints compatibles con OpenAI / vLLM / Ollama / OpenRouter para conectar instancias de Hermes (ej. `Hermes-3-Llama-3.1-70B` / `Hermes-3-8B`).
  - Soporte de Structured Outputs / JSON Schema nativo para garantizar que el desglose de tareas siempre sea sintácticamente válido.
* [ ] **Desglose Autónomo de Tareas (Epic Breakdown):**
  - Capacidad de Hermes para leer un issue o especificación de alto nivel y convertirlo en un grafo de tareas acíclico dirigido (DAG) con dependencias claras.
  - Publicación y asignación automática en el bus (`broadcast_assignment` / `create_task`).
* [ ] **Evaluación y Gatekeeper de Integración:**
  - Hermes actuando como revisor de código: evalúa diffs, lee la salida de los tests en `.worktrees/` y decide si aprobar el merge a `main` o solicitar correcciones al agente asignado.

---

### Nivel 3: Experiencia para Equipos y Desarrollo Local (Team Edition)
* [ ] **Dashboard Web Interactivo Moderno (React / WebSockets):**
  - Visualización en tiempo real de agentes activos, roles, archivos bloqueados (locks), tareas en progreso y flujo de mensajes.
  - Interfaz gráfica para que los desarrolladores humanos puedan pausar agentes, reasignar tareas o inyectar feedback humano en el bucle (*Human-in-the-Loop*).
* [ ] **Gateway de Cuotas y Costes (Budget Control):**
  - Límite de gasto por tarea, sprint o agente para evitar que un agente en bucle consuma créditos excesivos de APIs comerciales.
  - Métricas de consumo de tokens y tiempo de resolución por modelo y por agente.
* [ ] **CLI Simplificado (`agent-bus up / team init`):**
  - Un único comando que levante el hub, configure las credenciales de los agentes del equipo y arranque el listener en segundo plano sin fricción.

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

## 2. Modelos de Coding Recomendados por Rol

| Rol | Modelo Recomendado | Despliegue / Ubicación | Justificación |
| :--- | :--- | :--- | :--- |
| **Orquestador / Tech Lead** | **Hermes 3 (Llama-3.1-70B / 8B)** | Local (vLLM) / OpenRouter | Excelente razonamiento en asignación de tareas, bajo coste continuo y llamadas a funciones robustas. |
| **Arquitecto / Heavy Refactor** | **Claude 3.7 Sonnet / DeepSeek-V3** | API Cloud / Local | Máxima precisión en refactorizaciones de múltiples archivos y diseño de sistemas complejos. |
| **Implementador / Fast Coder** | **Qwen2.5-Coder-32B / DeepSeek-Coder** | Local (vLLM / Ollama) | Muy rápido, eficiente en tareas atómicas y edición directa de funciones con cero coste de API externa. |
| **Generador de Tests / QA** | **Aider + Qwen2.5-Coder-7B** | Local | Enfoque quirúrgico en cobertura de pruebas unitarias y verificación de regresiones. |

---

## 3. Otras Posibilidades de Uso y Mercados Verticales

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
