# agent-bus ⚡

[![Tests](https://img.shields.io/badge/tests-184%20passed-brightgreen.svg)](https://github.com/yasmanycastillo/agent-bus)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com)
[![MCP](https://img.shields.io/badge/MCP-2024--11--05-orange.svg)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/license-MIT-purple.svg)](LICENSE)

**Protocolo y bus de eventos distribuido para la orquestación autónoma de equipos multi-agente de Inteligencia Artificial.**

`agent-bus` permite que múltiples agentes de IA (**Claude Code**, **Antigravity / AGY**, **OpenAI Codex**, **Grok**, **Aider**) colaboren en un mismo proyecto de código en tiempo real, de forma **completamente autónoma y sin requerir un humano como mensajero manual entre terminales**.

---

## 🌟 Características Principales

```mermaid
flowchart TD
    Human["👤 Humano / Web UI"] -->|"agent-bus quickstart / submit"| Bus["⚡ agent-bus Hub (FastAPI + SSE Pub/Sub + SQLite)"]

    subgraph "Clientes Interactivos (MCP Hooks)"
        MCP1["💻 Claude Code (MCP Server)"]
        MCP2["💻 Antigravity IDE (MCP Server)"]
    end

    subgraph "Aislamiento por Git Worktrees"
        WT1[".worktrees/claude (rama agent/claude)"]
        WT2[".worktrees/antigravity (rama agent/antigravity)"]
    end

    subgraph "Workers Autónomos Headless"
        D1["🤖 WorkerDaemon (Claude Runner)"]
        D2["🤖 WorkerDaemon (Antigravity Runner)"]
    end

    Bus <-->|"JSON-RPC stdio / wait_for_updates"| MCP1
    Bus <-->|"JSON-RPC stdio / wait_for_updates"| MCP2

    Bus -->|"SSE Push Instantáneo"| D1
    Bus -->|"SSE Push Instantáneo"| D2

    D1 -->|"Reclama tarea y adquiere locks"| Bus
    D2 -->|"Reclama tarea y adquiere locks"| Bus

    D1 -->|"Commits locales"| WT1
    D2 -->|"Commits locales"| WT2

    WT1 -->|"Tests & Merge"| Integrator["🛡️ BranchIntegrator (Tech Lead)"]
    WT2 -->|"Tests & Merge"| Integrator

    Integrator -->|"Tests verdes -> Merge limpio"| Main["🌿 Rama main"]
    Integrator -->|"Tests fallan -> Feedback al autor"| Bus
```

1. **Integración Nativa MCP (Model Context Protocol)**:
   * Servidor MCP integrado sobre JSON-RPC 2.0 `stdio` con la herramienta bloqueante `wait_for_updates`.
   * Permite que las sesiones interactivas de **Claude Code** y **Antigravity** esperen eventos del bus y respondan **dentro de su propia consola activa**, preservando todo el contexto conversacional.
2. **Bucle Multi-Agente 100% Autónomo**:
   * Los agentes reciben asignaciones de tareas y consultas urgentes vía push por **Server-Sent Events (SSE Pub/Sub)**.
   * Ejecución headless desacoplada de la terminal con reanudación de sesiones (`--resume <session_id>`).
3. **Aislamiento en Git Worktrees**:
   * Cada agente trabaja en su propio directorio `.worktrees/<agent_id>` y rama `agent/<agent_id>`, eliminando cualquier riesgo de colisión o sobreescritura de archivos en disco.
4. **Integrador Autónomo (Rol Tech Lead)**:
   * [`BranchIntegrator`](src/agent_bus/worker/integrator.py) valida automáticamente la suite de tests en la rama del agente antes de fusionar.
   * Si los tests pasan, ejecuta el merge a `main`. Si fallan o hay conflictos, envía feedback detallado al autor con hasta 2 reintentos antes de alertar al humano.
5. **Soporte Multi-Modelo y Multi-CLI**:
   * Conectores nativos para **Claude Code** (`claude -p`), **Antigravity / AGY** (`agy --prompt`), **Aider / Codex** (`aider --message`), **Grok / xAI** y ejecutores personalizados.
6. **Seguridad Criptográfica Ed25519**:
   * Claves asimétricas por agente (`~/.agent-bus/agents/<id>.key`) con middleware en el Hub que verifica firmas canónicas en operaciones de escritura.
7. **Resiliencia & Circuit Breakers**:
   * Límite de turnos e intercambios por tarea (`TaskTurnBreaker`), control de presupuesto de tokens (`BudgetBreaker`), detección de locks expirados (`StaleLockDetector`) y resolución de deadlocks por ciclos de espera (`detect_deadlock`).
8. **Dashboard TUI en Tiempo Real (`top`)**:
   * Monitor interactivo de terminal construido con Rich Live para observar a los agentes, tareas, locks y decisiones en vivo.

---

## 🔌 Servidor MCP Nativo (Model Context Protocol)

`agent-bus` incluye un servidor MCP oficial para que cualquier asistente de IA interactivo se conecte directamente al bus.

### Herramientas MCP Disponibles

| Herramienta | Descripción |
| :--- | :--- |
| `wait_for_updates(agent_id, timeout)` | **Long-poll reactivo**: bloquea la sesión en espera de eventos SSE del bus sin gastar tokens hasta que otro agente envíe un mensaje |
| `post_message(from_agent, to_agent, text, ...)` | Envía mensajes directos o respuestas a otros agentes |
| `read_messages(agent_id)` | Consulta el inbox y mensajes pendientes del agente |
| `claim_task(task_id, agent_id)` | Reclama una tarea disponible en el backlog |
| `complete_task(task_id, agent_id)` | Marca una tarea como finalizada |
| `acquire_lock(file_path, agent_id, reason)` | Bloquea un archivo antes de editarlo para evitar colisiones |
| `release_lock(file_path, agent_id)` | Libera el bloqueo de un archivo |
| `get_project_status()` | Consulta el estado global del servidor, agentes y tareas |
| `record_decision(title, what, decided_by)` | Registra una decisión de arquitectura compartida (ADR) |

### Cómo Conectar tu Entorno al Servidor MCP

#### 1. Claude Code
Agrega el servidor MCP ejecutando en tu terminal:
```bash
claude mcp add agent-bus -- uv run agent-bus mcp-server
```

#### 2. Claude Desktop / Cursor / Antigravity / Zed
Agrega la siguiente configuración a tu archivo `mcp.json` o `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "agent-bus": {
      "command": "uv",
      "args": ["run", "agent-bus", "mcp-server"]
    }
  }
}
```

---

## 🚀 Inicio Rápido (1 solo comando)

Para inicializar el servidor, registrar agentes y poner a trabajar a tu equipo autónomo:

```bash
uv run agent-bus quickstart
```

Esto ejecuta automáticamente:
1. Inicialización del proyecto (`.agent-bus/`).
2. Arranque del servidor daemon en background (`http://localhost:8420`).
3. Registro de agentes (`claude`, `antigravity`).
4. Creación de Git Worktrees aislados y arranque de los daemons de ejecución.

---

## 🕹️ Flujo de Trabajo y Comandos

### 1. Monitoreo en Vivo (TUI Dashboard)
Abre un dashboard interactivo en tiempo real con refresco automático:

```bash
uv run agent-bus top
```

### 2. Enviar Objetivos al Equipo Autónomo
Envía un requerimiento para que el equipo lo descomponga, reclame tareas y lo implemente:

```bash
uv run agent-bus submit "Implementar autenticación JWT y tests de integración"
```

### 3. Gestión de Workers en Background
Controla los procesos daemon de cada agente:

```bash
uv run agent-bus worker status               # Ver estado de los daemons activos
uv run agent-bus worker start --agent claude # Iniciar daemon para un agente
uv run agent-bus worker stop --agent claude  # Detener daemon
```

### 4. Lanzar Equipo con Configuración Personalizada
```bash
# Lanzar equipo con agentes específicos y worktrees sobre la rama main
uv run agent-bus run-team --agents "claude,antigravity,codex" --base-ref main
```

---

## 🛠️ Operaciones Diarias y CLI

| Comando | Descripción |
| :--- | :--- |
| `agent-bus quickstart` | Onboarding en 1 paso (inicializa bus, agentes y workers) |
| `agent-bus top` | Dashboard TUI interactivo en tiempo real con Rich Live |
| `agent-bus mcp-server` | Inicia el servidor MCP nativo sobre stdio (JSON-RPC 2.0) |
| `agent-bus run-team` | Inicializa worktrees y arranca daemons de fondo |
| `agent-bus submit "<meta>"` | Envía un objetivo global al equipo |
| `agent-bus serve --daemon` | Inicia el servidor FastAPI como servicio de fondo |
| `agent-bus serve --stop` | Detiene el servidor |
| `agent-bus show` | Visualiza el dashboard del estado actual |
| `agent-bus show tasks` | Lista las tareas y sus responsables |
| `agent-bus show locks` | Muestra los archivos actualmente bloqueados |
| `agent-bus show agents` | Muestra el estado y capacidades de los agentes |
| `agent-bus work claim <id>` | Reclama una tarea disponible |
| `agent-bus work done <id>` | Marca una tarea como completada |
| `agent-bus work reassign <id> <agente>` | Reasigna el responsable de una tarea |
| `agent-bus work lock <archivo>` | Bloquea un archivo para edición concurrente segura |
| `agent-bus work unlock <archivo>` | Libera el bloqueo de un archivo |
| `agent-bus work msg <agente> "<texto>"` | Envía un mensaje directo al inbox de otro agente |
| `agent-bus work decide "<titulo>" "<desc>"` | Registra un registro de decisión arquitectónica (ADR) |

---

## 💻 Desarrollo Multi-Terminal

Si tienes varias terminales abiertas (por ejemplo, una con **Claude Code** y otra con **Antigravity** o un humano), puedes aislar la identidad de cada terminal exportando la variable de entorno:

```bash
# Terminal 1 (Claude)
export AGENT_BUS_AGENT_ID=claude

# Terminal 2 (Antigravity)
export AGENT_BUS_AGENT_ID=antigravity
```

---

## 🧪 Suite de Pruebas

`agent-bus` cuenta con una suite completa de 184 pruebas unitarias y de integración end-to-end:

```bash
uv run pytest
```

---

## 📖 Arquitectura Detallada

Para consultar el diseño técnico completo, flujo de diagramas de secuencia Mermaid, criptografía Ed25519 y modelo de consenso BFT, consulta:
👉 **[`docs/autonomous_multi_agent_architecture.md`](docs/autonomous_multi_agent_architecture.md)**
