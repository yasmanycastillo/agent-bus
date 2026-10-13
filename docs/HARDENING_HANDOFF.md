# Handoff: estabilización de agent-bus

Actualizado: 2026-09-17 16:59 AST (UTC-04:00)

Este archivo es una nota operativa para reanudar el trabajo después de apagar el equipo.
No está incluido todavía en el diff staged ni en ningún commit.

## Repositorio y rama

- Repositorio principal: `/home/Yasmany/src/agent-bus`
- Worktree de integración: `/home/Yasmany/src/agent-bus/.worktrees/recommendations-hardening`
- Rama candidata: `integration/recommendations-hardening`
- HEAD actualmente confirmado: `7b4e21a55ef5a74d9b20892b2d59137fa8327700`
- Rama objetivo: `main`
- `main` todavía no ha sido modificada por esta integración.

## Trabajo ya confirmado en la rama

- Atomicidad inicial de inbox y regresiones transaccionales.
- Autorización y precondiciones para operaciones sobre tareas.
- Endurecimiento y reconciliación del integrador.
- Apertura y ciclo de vida seguro de SQLite.
- Validación temprana de identificadores y módulo de validación sin ciclo de imports.
- CI con acciones fijadas por SHA, dependencias locked, Ruff, pruebas, build y gate incremental de mypy.
- Metadatos de paquete y documentación de contribución, seguridad y release.

Commits recientes relevantes:

- `7b4e21a` — elimina whitespace final detectado por `git diff --check`.
- `2002dea` — documentación y preparación de release.
- `6107917` — baseline incremental de mypy.
- `193d85d` — gates de CI.
- `ed40909` — validación de IDs y ciclo de vida SQLite.

## Cambios staged pendientes de commit

Los siguientes cambios corrigen hallazgos de la revisión exacta del HEAD:

- `src/agent_bus/core/tasks.py`
  - Todas las mutaciones usan transacciones dedicadas mediante `Database.transaction()`.
  - `create()` ya no consulta estado no confirmado de la conexión compartida.
  - Creación, claim, reasignación, finalización, revisión, bloqueo y locks no confirman trabajo ajeno.
- `src/agent_bus/worker/integrator.py`
  - Los reintentos se cuentan desde revisiones persistidas y sobreviven reinicios.
  - Cada entrada del historial se valida como `ReviewDecision`.
  - El historial debe contener la revisión recién persistida.
  - Un bloqueo sólo se declara exitoso después de validar POST y GET de lectura posterior.
- `tests/unit/test_task_transactions.py`
  - Regresiones de propiedad transaccional y estado no confirmado.
- `tests/unit/test_task_claims.py`
  - Concurrencia entre writers con transacciones propias.
- `tests/unit/test_worker_integrator.py`
  - Reintentos tras reinicio, historial inválido y verificación durable del bloqueo.
- `mypy-baseline.json`
  - Deuda heredada reducida de 606 a 595 errores.

## Última verificación local

Ejecutada sobre el contenido staged actual:

- `uv run pytest -q`: **1268 passed**, 2 warnings conocidos de APIs WebSocket obsoletas.
- `uv run ruff check .`: aprobado.
- `uv run python scripts/check_mypy_baseline.py`: **595/595**, aprobado.
- `git diff --cached --check`: aprobado.

En el HEAD anterior también aprobaron:

- `uv build` y la instalación aislada del wheel.
- `agent-bus --help` desde el wheel instalado.
- `uv run agent-bus demo --yes`.

Como ahora hay cambios staged posteriores, build y demo deben repetirse sobre el HEAD final antes de fusionar.

## Revisión independiente en curso

- Delegación: `deleg_196b5866`
- Subagente: `sa-0-ad1845ff`
- Iniciada aproximadamente a las 16:54 AST.
- Objetivo: revalidar los tres últimos hallazgos y el diff staged completo.

La ejecución del subagente no sobrevive a un apagado. Si el equipo se apaga antes de recibir el resultado, la revisión debe relanzarse sobre el diff exacto.

## Pendiente al reanudar

1. Entrar al worktree:

   ```sh
   cd /home/Yasmany/src/agent-bus/.worktrees/recommendations-hardening
   git status --short --branch
   ```

2. Confirmar que los seis archivos staged anteriores siguen presentes y que este archivo permanece sin stage.

3. Si no quedó un dictamen válido de `deleg_196b5866`, relanzar una revisión independiente fail-closed del diff staged.

4. Si la revisión detecta defectos:
   - escribir primero una regresión que falle;
   - aplicar una corrección acotada;
   - ejecutar pruebas focalizadas y la suite completa;
   - actualizar el baseline de mypy sólo si la cantidad disminuye;
   - repetir la revisión independiente.

5. Cuando la revisión staged apruebe, crear el commit verificado de las correcciones. Este archivo puede mantenerse como nota operativa sin stage o actualizarse antes de incluirlo deliberadamente.

6. Ejecutar sobre el HEAD final exacto:

   ```sh
   uv sync --locked --all-extras --dev
   uv run ruff check .
   uv run python scripts/check_mypy_baseline.py
   uv run pytest -q
   rm -rf /tmp/agent-bus-final-dist
   uv build --out-dir /tmp/agent-bus-final-dist
   uv run agent-bus demo --yes
   git diff main...HEAD --check
   git status --short --branch
   ```

7. Solicitar una última revisión independiente del **HEAD exacto**. Cualquier commit posterior invalida ese dictamen.

8. Sólo si todo está verde, integrar en `main`:

   ```sh
   git -C /home/Yasmany/src/agent-bus merge --ff-only integration/recommendations-hardening
   ```

9. Verificar `main` después del merge y no hacer push ni publicar release sin una solicitud explícita.

## Deuda deliberadamente no resuelta

- Baseline mypy todavía registra 595 errores heredados; CI bloquea regresiones nuevas, pero no afirma tipado limpio.
- Persisten 2 warnings deprecados del stack WebSocket.
- La separación del monolito `MessageBus` en routers/policies debe hacerse en cambios posteriores y acotados; no conviene mezclarla con este hardening de alto impacto.
- No existe todavía una release publicada ni publicación automática a PyPI.
