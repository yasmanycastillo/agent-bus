# Visión de Producto y Monetización: agent-bus + Hermes AI

## 1. Contexto y Oportunidad del Mercado

El desarrollo de software asistido por IA está evolucionando rápidamente de modelos individuales que operan sobre chats aislados hacia **equipos multi-agente especializados** que colaboran concurrentemente sobre un mismo repositorio (Claude Code, Cursor, Aider, Antigravity, Devin, etc.).

Sin embargo, el uso descoordinado de múltiples agentes en entornos reales sufre de:
- **Colisiones de código y race conditions** (dos agentes modificando los mismos archivos sin sincronización).
- **Bloat de contexto y consumo descontrolado de tokens** al compartir transcripciones de chat completas.
- **Falta de gobernanza, trazabilidad y control de costos** para equipos de ingeniería.
- **Falta de garantías de concurrencia y persistencia** en los buses de mensajes locales existentes.

`agent-bus` resuelve el problema de fontanería crítica (concurrencia atómica, mensajería durable, locks por archivo y protocolo MCP), convirtiéndose en la infraestructura ideal para desplegar una **fuerza de trabajo de desarrollo autónoma y coordinada**.

---

## 2. Arquitectura de Producto: Hermes AI como Orquestador Central

La combinación de **Hermes AI (Nous Hermes)** con `agent-bus` crea una división del trabajo óptima en términos de coste, privacidad y rendimiento:

```
+-------------------------------------------------------------------------+
|                  HERMES AI (Tech Lead / Product Manager)                |
|  - Desglose de historias de usuario y tareas de alto nivel             |
|  - Asignación inteligente por rol y especialidad del agente            |
|  - Arbitraje de consensos, aprobación de arquitectura y revisión de PRs |
|  - Control de presupuestos de tokens y políticas de seguridad           |
+------------------------------------+------------------------------------+
                                     |
                                     v (MCP / SSE / JSON-RPC)
+-------------------------------------------------------------------------+
|                          AGENT-BUS (Coordination Core)                  |
|  - Aislamiento por proyecto, base SQLite y worktrees Git               |
|  - File Locks atómicos con fencing tokens (T-12)                        |
|  - Mensajería duradera con idempotencia, ACK y cursores firmados        |
|  - Control de concurrencia y observabilidad local en tiempo real        |
+--------------------+---------------------+------------------------------+
                     |                     |
                     v                     v
            +-----------------+   +------------------+
            |   Claude Code   |   |   Qwen / Aider   |  ... (Agentes Coder)
            | (Refactoring /  |   | (Unit Tests /    |
            | Arquitectura)   |   | Fixes rápidos)   |
            +-----------------+   +------------------+
```

### ¿Por qué Hermes AI en este rol?
1. **Eficiencia Económica:** La orquestación continua requiere miles de llamadas de control. Usar modelos propietarios de gama alta (como Opus/GPT-4o) para el loop de supervisión es prohibitivo. Hermes ofrece bajo coste por token y alto rendimiento en razonamiento y *tool calling*.
2. **Privacidad Total (On-Premise / Local):** Puede ejecutarse localmente con vLLM/Ollama o en la VPC del cliente, garantizando que el código y las decisiones de arquitectura nunca salgan de la empresa.
3. **Neutralidad de Proveedor:** No sesga la integración hacia una sola plataforma propietaria.

---

## 3. Estrategia de Monetización y Modelos de Negocio

### A. Modelo Open-Core (Desarrollador Individual vs. Equipos)
* **Community Edition (Código Abierto / Gratuito):**
  - Bus local monousuario (`agent-bus` core).
  - Soporte para clientes MCP locales (Claude Code, Cursor, Aider).
  - Bloqueo de archivos local y persistencia SQLite en máquina local.
* **Team Edition (Licencia Comercial por Asiento / Proyecto):**
  - Orquestador Hermes preconfigurado con prompts de Tech Lead y PM.
  - Dashboard Web multiusuario con visualización de sprints y asignaciones en vivo.
  - Sincronización multi-desarrollador (agentes en diferentes máquinas trabajando en el mismo repo).
  - Control de presupuestos y cuotas de tokens por desarrollador/agente.

### B. Enterprise Edition (On-Premise / VPC / Air-Gapped)
* **Seguridad y Cumplimiento:**
  - Integración con SSO / SAML y control de acceso basado en roles (RBAC).
  - Registro de auditoría inmutable de todas las acciones y decisiones tomadas por la IA.
  - Soporte para redes cerradas (Air-Gapped) y modelos 100% locales (Hermes + DeepSeek/Qwen).
* **Integración con el Ciclo de Vida del Software (SDLC):**
  - Conexión nativa bidireccional con Jira, Linear, GitHub Enterprise y GitLab.
  - Branch Integrator automatizado que ejecuta CI/CD y gestiona pull requests verificados.

### C. SaaS Híbrido (Outcome-Based / Capacidad de Trabajo)
* **Plano de Control en la Nube + Agentes en Local:**
  - El cliente ejecuta los agentes y el código en sus máquinas o runners de CI/CD.
  - El SaaS ofrece el orquestador Hermes, analíticas de rendimiento del equipo, benchmarking de agentes y optimización de costes.
* **Facturación basada en Resultados:**
  - Cobro por tarea completada y verificada contra la suite de pruebas del proyecto.

---

## 4. Análisis Competitivo: Diferenciación frente a Hermes Agent (`hermes-agent.ai`)

Hermes Agent (v0.6.0) introduce un sistema de orquestación multi-agente con un tablero Kanban en SQLite y perfiles para tareas de investigación, web scraping y generación de contenido (como el flujo de trabajo de *Tonbi Studio*). Aunque conceptualmente ambos hablan de "múltiples agentes", **operan en niveles de abstracción totalmente diferentes y resuelven problemas distintos**:

| Dimensión | Hermes Agent Multi-Agent | `agent-bus` |
| :--- | :--- | :--- |
| **Dominio Principal** | Investigación web, marketing, scraping, bots y tareas de texto general. | **Ingeniería de software en repositorios de código reales**. |
| **Control de Concurrencia de Archivos** | **Inexistente.** Si 2 agentes editan el mismo archivo a la vez, se sobreescriben. | **Locks Atómicos con Fencing Tokens (T-04 / T-12)** para prevenir sobreescrituras y race conditions. |
| **Aislamiento de Código (Git)** | Sin noción nativa de ramas ni worktrees de Git por agente. | **Git Worktrees aislados** (`.worktrees/<agent_id>`) y fusión automática con tests en verde. |
| **Protocolo Estándar de la Industria** | Protocolo cerrado/propietario de Hermes Agent. | **Model Context Protocol (MCP)** oficial (SDK 2.1.1 stdio / SSE) estándar de Anthropic/Linux Foundation. |
| **Interoperabilidad de Herramientas** | Solo agentes que corren dentro del runtime de Hermes. | **Cualquier agente MCP externo**: Claude Code, Cursor, Aider, Codex, Antigravity, etc. |
| **Garantías de Mensajería** | Paso de mensajes simple sobre base de datos. | **Idempotencia en SQLite, ACK explícito, cursores firmados y reconexión SSE duradera**. |
| **Identidad y Seguridad** | Perfiles en archivo de configuración plano. | **Sesiones Bearer locales con expiración, permisos RBAC y trazabilidad auditada**. |

### Propuesta de Posicionamiento:
1. **Hermes Agent es la capa de Aplicación / Prompting**: Es el cerebro que decide *"¿Qué tareas hay que hacer y a quién asignarlas?"*.
2. **`agent-bus` es la Infraestructura / Bus de Datos del Repositorio**: Es el motor que garantiza que *"Nadie rompa el repositorio Git, los archivos estén bloqueados mientras se editan, los mensajes no se pierden en caídas y Claude Code puede colaborar con Cursor y Aider sin colisionar"*.

**Tesis de Valor:** Cualquier framework multi-agente puede planificar tareas, pero **ninguno puede ejecutar 5 agentes programando concurrentemente sobre el mismo repositorio Git** sin romper el código por falta de locks atómicos y worktrees aislados. `agent-bus` es el motor de coordinación segura que hace eso posible.

