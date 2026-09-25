# Operar feature-development

`feature-development` es el workflow incluido. Compilarlo crea cinco tareas: discovery, design, implementation, review e integration. No las ejecuta. El avance las despacha en ese orden. La integración no es otro runtime: espera un `approve` del revisor asignado y entonces corre la suite y fusiona.

La definición no cambia entre corridas. Cambian el `instance` y los agentes que tienen las capacidades de cada paso.

## Compilar

```bash
agent-bus workflow compile --name feature-development --instance run-1
```

Imprime el nombre y los cinco `task_id`, con la forma `feature-development-run-1-<paso>`. `--advisory` se rechaza: este workflow exige `review: approved`.

Un YAML propio se compila con `--file camino.yaml --instance run-1`. Solo se acepta la versión 1. Cada paso necesita un `id`. `depends_on` solo puede nombrar pasos ya definidos.

## Qué tiene que existir antes de avanzar

Cada paso libre se enruta por capacidades. Hace falta un agente con perfil aprobado, heartbeat reciente y un runtime `native`, `external` o `sandbox`. `sandbox` solo arranca si el proceso inyectó un abridor; sin ese abridor la tarea sigue `pending`. La review que el sandbox escribe como `openhands-runtime` no autoriza el merge.

| Paso | Capacidades | Quién lo cierra |
|---|---|---|
| discovery | `repository-analysis`, `long-context` | El runtime. Si es external y el comando termina bien, `advance` marca la tarea `done`. |
| design | `architecture` | Igual que discovery. |
| implementation | `implementation`, `tests` | Igual. El SHA del intento es el candidato que la review tiene que aprobar. |
| review | `code-review` | El runtime marca el paso `done`. Eso no aprueba el merge. El dueño registra el veredicto aparte. |
| integration | ninguna; no se despacha | `advance` con el checkout y el repo, después del `approve`. |

El revisor no puede ser el dueño de implementation. Si nadie más tiene `code-review`, review se queda `pending` y el avance responde `unroutable`.

## Avanzar

No hay un subcomando `workflow advance`. El avance es `POST /workflows/advance`, con la misma sesión que los otros comandos: `Authorization: Bearer` y `X-Agent-Bus-Project`.

```json
{
  "workflow": "feature-development",
  "instance_id": "run-1",
  "workspace_ref": "/ruta/al/worktree",
  "timeout": 30
}
```

Repetir esa llamada para discovery, design, implementation y review. `workspace_ref` es el checkout donde corre el comando. Una respuesta `dispatched` con `task_status: done` desbloquea el paso siguiente.

Sin `repo_dir`, cuando solo queda integration, la respuesta es `waiting_for_integration` y `main` no se mueve.

### Runtime native

`advance` abre el intento native y deja la tarea `pending`. No la marca `done`. El worker de ese agente cierra discovery, design, implementation y review cuando el intento termina bien:

```bash
agent-bus worker start --agent <agent_id>
```

Integration no entra en ese atajo. La cierra el avance con repo, como se describe abajo.

## Veredicto

`work review` solo pasa una tarea a `in_review`. No autoriza el merge. El veredicto lo registra la credencial del dueño de la tarea de review (`agent-bus work as` cambia el agente por defecto solo si esa identidad tiene sesión):

```bash
agent-bus work verdict feature-development-run-1-review --verdict approve
agent-bus work verdict feature-development-run-1-review --verdict changes_requested --reason "falta el caso vacío"
```

Sin `--sha`, se usa el último intento completado de implementation. Un SHA distinto se rechaza. El implementador no puede registrar el veredicto, ni un agente que no sea el dueño de review.

MCP expone la misma operación como `record_verdict`.

`approve` es lo que la integración lee. Tiene que ser sobre el SHA de ese intento y del revisor asignado. `changes_requested` devuelve implementation a `pending`, avisa al implementador y deja integration en `pending`. El intento nuevo tiene otro SHA: el `approve` anterior no vale. `main` no se mueve.

Si integration llega a `waiting_for_review`, el dueño de review recibe un mensaje con el SHA y el motivo. El mismo SHA y el mismo motivo no se reenvían.

## Integrar

Cuando el `approve` ya está, el mismo `POST /workflows/advance` necesita el repo de destino y el checkout del candidato:

```json
{
  "workflow": "feature-development",
  "instance_id": "run-1",
  "workspace_ref": "/ruta/al/worktree",
  "repo_dir": "/ruta/al/repo",
  "candidate_branch": "agent/impl"
}
```

Antes de fusionar corre el `test_cmd` de implementation. Si ese paso no tiene comando, el comando es `uv run pytest -q`. La salida queda en un artefacto `test-report`. Si el comando falla, integration pasa a `blocked`, `main` no cambia y `blocked_reason` guarda el texto. El implementador y el revisor lo reciben una vez.

Si el checkout no es el SHA del intento, o la política de evidencia no se puede leer, tampoco hay merge. Un 404 de la política significa que la tarea no tiene política estricta; integration sí la tiene, porque el compilador la marcó `review: approved`.

Con suite verde y `approve` vigente, el merge usa ese SHA como segundo padre. La tarea de integration queda `done` aunque estuviera `pending`. Si el bus no puede guardar el `done`, el resultado no es éxito y una segunda llamada no vuelve a fusionar el mismo candidato.

`GET /tasks/feature-development-run-1-integration` muestra `status` y, si quedó bloqueada, `blocked_reason`.
