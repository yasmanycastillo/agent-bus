# agent-bus

Coordina agentes de distintas herramientas sobre un mismo proyecto: comparte
mensajes y tareas, reserva archivos y entrega resultados para revisión mediante MCP.

El hub conserva los pendientes en SQLite. Cada agente tiene una identidad propia;
puede trabajar desde su cliente MCP o responder consultas mediante un listener.
La consola permite supervisar tareas, solicitudes humanas y reservas.

## Empezar

Requisitos: Python 3.12+ y uv en un entorno Unix. **Linux es la plataforma validada**;
macOS todavía no se ha validado. Windows nativo (Python en PowerShell o CMD) no está
soportado actualmente; para probar desde Windows, utiliza Linux dentro de WSL2,
un entorno que también requiere validación propia.

Para usar modelos reales necesitas, además, el cliente del proveedor instalado
y autenticado en el entorno donde se ejecutará. [Detalle por plataforma](docs/first-run.md#sistemas-operativos).

Desde este repositorio:

```sh
uv tool install .
agent-bus --project /ruta/mi-proyecto onboard --mcp-only
```

El asistente prepara un hub local, credenciales y configuraciones MCP para dos
agentes. Copia la configuración indicada a cada cliente, reconecta y pide
`bootstrap_agent({})`. No inicia modelos, workers ni merges automáticamente.

Sigue la [guía de primer uso](docs/first-run.md) para conectar los clientes,
iniciar listeners y resolver problemas. La instalación documentada es desde este
repositorio; no presupone que haya un paquete público disponible en PyPI.

### Configurar clientes MCP automáticamente

Configura `agent-bus` directamente en los clientes soportados (**Cursor**, **Claude**, **Gemini / Antigravity**, **Codex**, **Grok**) sin editar archivos JSON a mano:

```sh
# Instalar en todos los clientes en el proyecto actual:
agent-bus mcp install --client all

# Instalar para un agente específico en clientes puntuales:
agent-bus mcp install --client cursor,claude --agent hermes

# Instalar a nivel global en la máquina del usuario (~/.cursor, ~/.config/Claude, ~/.gemini, etc.):
agent-bus mcp install --client gemini --global

# Simular qué archivos y configuraciones se tocarían sin modificarlos:
agent-bus mcp install --client all --dry-run

# Ver el snippet JSON resultante sin escribir en disco:
agent-bus mcp show --client codex

# Desinstalar la configuración MCP de un cliente:
agent-bus mcp uninstall --client cursor
```

La instalación realiza una combinación no destructiva (*merge*): preserva cualquier otro servidor MCP preexistente en tus archivos de configuración.

## Probar sin modelos

```sh
agent-bus demo
```

Dos actores programados usan MCP real para reservar un archivo, detectar un
conflicto, revisar una entrega y pedir una decisión humana. No consume modelos
comerciales ni modifica tu proyecto. [Recorrido de la demo](docs/demo.md).

## Respuestas automáticas

El asistente genera lanzadores para Claude, Codex y Grok con el entorno de cada
identidad. Ejecuta el lanzador que imprime en otra terminal y déjalo activo.
También puedes iniciar y consultar un listener desde el proyecto:

```sh
agent-bus watch --agent grok --cli grok
agent-bus watch --agent grok --status
```

Un mensaje con `reply_needed=true` inicia un turno headless. El listener publica
la respuesta y confirma el mensaje después del éxito. No despierta una TUI
existente; `--dry-run` solo observa. Grok responde consultas de texto sin herramientas
ni ediciones. [Estados y recuperación](docs/first-run.md#listeners-para-responder-automáticamente).

## Coordinar trabajo

El flujo MCP recomendado es:

`bootstrap_agent` → `my_pending_items` → `claim_task` → `prepare_edit` → `complete_handoff`.

Las reservas multiarquivo se adquieren juntas o ninguna. La entrega conserva
resumen, evidencia declarada, estado, mensaje y liberaciones explícitas en una
transacción. [Guía de coordinación](docs/coordination-workflow.md).

Para supervisar el proyecto, abre `agent-bus ui` e inicia sesión con una credencial
administrativa. [Guía de la consola](docs/console.md).

## Alcance y límites

- La configuración está orientada a proyectos locales con un entorno Unix. Cada proyecto
  necesita su propio hub, base y credenciales.
- Leer un mensaje no lo confirma. La entrega durable no sustituye a un cliente
  que consulte pendientes o a un listener activo.
- Las reservas son cooperativas: un editor que ignore el protocolo puede escribir.
- Los workers y el integrador requieren configuración explícita; el handoff MCP
  no ejecuta pruebas ni fusiona código por sí mismo.
- No hay medición central completa de costes ni cuotas comerciales persistentes.
- La compatibilidad de herramientas no garantiza autonomía continua ni que todos
  los modelos respondan igual. Revisa el resultado de cada tarea.

## Documentación y desarrollo

- [Índice de documentación](docs/README.md)
- [Instalación y operación](docs/first-run.md)
- [Conexión MCP](docs/mcp-setup.md)
- [Desarrollo y pruebas](docs/development.md)
- [Cambios del producto](CHANGELOG.md)

Licencia [MIT](LICENSE).
