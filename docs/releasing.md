# Publicar una versión

El repositorio aún no publica automáticamente a PyPI. Este proceso produce artefactos
verificables; la publicación requiere una decisión explícita del mantenedor.

## 1. Preparar la versión

1. Trabaja desde un checkout limpio de `main`.
2. Decide una versión semántica y actualiza `project.version` en `pyproject.toml`.
3. Mueve las entradas relevantes de `Unreleased` en `CHANGELOG.md` a una sección con
   versión y fecha.
4. Comprueba que `uv.lock` permanece consistente.

## 2. Ejecutar gates

```sh
uv sync --locked --all-extras --dev
uv run ruff check .
uv run python scripts/check_mypy_baseline.py
uv run pytest -q
rm -rf dist
uv build
sha256sum dist/*.whl dist/*.tar.gz > dist/SHA256SUMS
```

Instala el wheel resultante en un entorno temporal fuera del checkout y verifica:

```sh
release_env="$(mktemp -d)"
python -m venv "$release_env/venv"
"$release_env/venv/bin/pip" install "$(pwd)"/dist/agent_bus-<version>-py3-none-any.whl
"$release_env/venv/bin/agent-bus" --help
"$release_env/venv/bin/agent-bus" demo --yes
rm -rf "$release_env"
```

La demo no llama a modelos comerciales. El onboarding y las pruebas opcionales con
proveedores reales se ejecutan sólo cuando la versión los afecta y con credenciales de
prueba.

## 3. Etiquetar y publicar

1. Obtén una revisión independiente del commit exacto que será etiquetado.
2. Crea un tag anotado `v<version>` y verifica que apunta al commit revisado.
3. Crea el release de GitHub con las notas del changelog y adjunta el sdist y wheel.
4. Publica en PyPI sólo mediante una credencial o trusted publisher configurado fuera
   del repositorio. Nunca guardes tokens en Git, scripts o logs.
5. Descarga los artefactos publicados y verifica que coinciden con `dist/SHA256SUMS`,
   generado inmediatamente después del build revisado.

Si cualquier artefacto cambia después de la revisión, repite build, smoke test y
revisión antes de publicar.
