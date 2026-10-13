# Contribuir

## Preparación

Necesitas Linux, Python 3.12.7 y `uv` 0.11.25 o versiones compatibles con el lock.

```sh
uv sync --locked --all-extras --dev
```

Lee `AGENTS.md` antes de editar: el proyecto usa Agent Bus para coordinar tareas,
mensajes y reservas de archivos. No incluyas credenciales, bases locales, trazas
privadas ni salidas de proveedores en commits.

## Flujo de cambio

1. Crea una rama o worktree desde `main`.
2. Escribe primero una prueba que reproduzca el comportamiento requerido.
3. Reserva los archivos antes de editarlos cuando el hub esté activo.
4. Ejecuta las pruebas focalizadas y luego los gates completos.
5. Mantén el cambio acotado y actualiza documentación y changelog si altera una
   interfaz, un comando, una garantía de seguridad o el proceso de operación.

```sh
uv run ruff check .
uv run python scripts/check_mypy_baseline.py
uv run pytest -q
uv build
```

El gate de mypy es incremental: el repositorio conserva deuda heredada en
`mypy-baseline.json`. No añadas errores nuevos ni regeneres el baseline para ocultar
regresiones. `--update` sólo se usa después de corregir deuda y revisar el diff.

## Pull requests

Incluye el problema, el alcance, las pruebas ejecutadas y cualquier riesgo o límite
conocido. Mantén commits revisables. Los cambios de seguridad deben incluir pruebas
negativas que demuestren el mecanismo exacto de rechazo.

Los cambios no se consideran listos mientras fallen Ruff, el gate mypy, pytest, el
build o una revisión independiente del commit final.
