# agent-bus

Protocolo de comunicacion y coordinacion entre agentes IA.

## Inicio rapido

```bash
cd ~/src/agent-bus

# 1. Inicializar (primera vez)
uv run agent-bus init

# 2. Iniciar servidor
uv run agent-bus start

# 3. En otra terminal: setup interactivo
uv run agent-bus setup
```

`setup` pregunta todo: nombre del proyecto, cuantos agentes, tareas iniciales. Configura todo automaticamente.

## Uso diario

```bash
# Trabajar con tareas
uv run agent-bus work claim T1        # Reclamar tarea
uv run agent-bus work done T1         # Completar tarea
uv run agent-bus work task T2 "X"     # Crear tarea nueva

# Bloquear archivos
uv run agent-bus work lock src/main.py --reason "refactoring"
uv run agent-bus work unlock src/main.py

# Mensajeria
uv run agent-bus work msg codex "Revisa el PR"
uv run agent-bus work inbox           # Ver inbox
uv run agent-bus work inbox --archive <id>

# Decisiones
uv run agent-bus work decide "Usar SQLite" "Persistencia local"

# Kickoff
uv run agent-bus work kickoff 1 --result '{"card":"Claude, dev"}'

# Cambiar agente activo
uv run agent-bus work as claude

# Ver estado
uv run agent-bus show                 # Dashboard completo
uv run agent-bus show tasks           # Solo tareas
uv run agent-bus show inbox           # Solo inbox
uv run agent-bus show locks           # Solo locks
uv run agent-bus show decisions       # Solo decisiones
uv run agent-bus show kickoff         # Progreso kickoff
uv run agent-bus show agents          # Agentes registrados
```

## Comandos

| Comando | Descripcion |
|---|---|
| `agent-bus init` | Configuracion inicial (claves, config, db) |
| `agent-bus start` | Iniciar servidor |
| `agent-bus setup` | Wizard interactivo: agentes, kickoff, tareas |
| `agent-bus status` | Health check del servidor |
| `agent-bus work as <id>` | Cambiar agente por defecto |
| `agent-bus work task <id> <title>` | Crear tarea |
| `agent-bus work claim <id>` | Reclamar tarea |
| `agent-bus work done <id>` | Completar tarea |
| `agent-bus work lock <file>` | Bloquear archivo |
| `agent-bus work unlock <file>` | Liberar archivo |
| `agent-bus work msg <to> <text>` | Enviar mensaje |
| `agent-bus work inbox` | Ver inbox |
| `agent-bus work decide <title> <what>` | Registrar decision |
| `agent-bus work kickoff <step>` | Completar paso kickoff |
| `agent-bus show` | Dashboard completo |
| `agent-bus show tasks/inbox/locks/decisions/kickoff/agents` | Ver componente especifico |

## Testing

```bash
uv run pytest
```
