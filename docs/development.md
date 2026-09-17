# Desarrollo y pruebas

## Preparar el entorno

```sh
uv sync --locked --extra dev
uv run agent-bus --help
uv run pytest
```

Trabaja con Python 3.12 o posterior en Linux. Lee [AGENTS.md](../AGENTS.md) para
coordinar cambios con otros agentes. Las pruebas usan proyectos, hubs y bases
temporales; no deben depender de las credenciales o del hub personal del autor.

## Organización del código

| Directorio | Responsabilidad |
|---|---|
| `src/agent_bus/core/` | Hub HTTP, mensajes, tareas, eventos y reservas |
| `src/agent_bus/mcp/` | Herramientas, instrucciones y transporte MCP |
| `src/agent_bus/cli/` | Instalación guiada, comandos y listeners |
| `src/agent_bus/worker/` | Ejecución de proveedores, worktrees y revisión/integración |
| `src/agent_bus/web/console/` | Consola de supervisión |
| `tests/` | Pruebas unitarias, de contratos y de integración |

El hub conserva el estado del proyecto en SQLite. Los clientes MCP consultan y
coordinan; los listeners ejecutan consultas headless; los workers ejecutan tareas
en worktrees. Entregar trabajo mediante MCP no ejecuta pruebas ni crea merges:
la revisión e integración Git son operaciones separadas.

## Validar cambios

- Ejecuta primero las pruebas del área modificada y luego la suite completa
  antes de integrar código en `main`.
- Usa `git diff --check` para detectar errores de espacios.
- Al editar guías, comprueba sus enlaces locales y los comandos con `--help`.
- Conserva las distinciones entre pruebas con actores programados, modelos reales
  y operaciones realizadas por el usuario. No publiques credenciales ni trazas privadas.

La [guía de consola](console.md#prueba-reproducible-en-navegador) explica la prueba
con Chromium y Playwright. La [demo](demo.md) verifica coordinación MCP sin
llamar a proveedores comerciales.

## Pruebas opcionales con proveedores reales

Los scripts siguientes requieren clientes instalados, autenticación del proveedor
y red; consumen inferencia. No forman parte de `pytest`. Lee sus opciones antes
de ejecutarlos:

```sh
uv run python scripts/acceptance_real_clients.py --help
uv run python scripts/acceptance_hermes.py --help
```

Usan proyectos temporales para comprobar comunicación y reanudación. Conserva
informes sanitizados fuera del repositorio. Una corrida local no garantiza el
comportamiento de otras versiones, modelos o configuraciones.

## Construir una distribución

```sh
uv build
```

Antes de distribuir un wheel, instálalo en un entorno limpio fuera del checkout
y prueba `agent-bus --help`, `agent-bus demo` y el onboarding de un proyecto
temporal. La instalación del paquete no instala ni autentica los CLIs de modelos.
