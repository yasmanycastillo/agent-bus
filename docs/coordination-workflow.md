# Coordinación MCP compacta

Las operaciones de `agents-mcp-workspace` se adaptaron al hub de `agent-bus`.
La identidad y el proyecto proceden de una sesión Bearer provisionada; las tools
no aceptan un `agent_id` aportado por el modelo. El proyecto conserva su única
base SQLite y sus locks por sesión/adquisición. No se importan bases ni sesiones
del otro proyecto.

## Entrada y pendientes

1. Provisiona la credencial y configura el MCP según [autenticación](authentication.md).
2. Llama `bootstrap_agent({"display_name":"Codex","limit":20})`.
3. Usa `my_pending_items` para consultar mensajes pendientes y tareas propias.

El bootstrap reutiliza la sesión autenticada, registra/actualiza presencia y
entrega instrucciones, vencimiento, agentes, decisiones recientes, tareas libres
y pendientes propios. No rota tokens ni cierra otras sesiones. Los listados del
bootstrap están limitados; `available_task_count` y `agent_count` indican el total.
`get_project_status` conserva la consulta global de tareas, agentes y locks.
`get_agent_instructions` devuelve las instrucciones sin escribir en el hub.

`my_pending_items` devuelve:

- `messages` y `next_cursor`: páginas del inbox persistente; leer no confirma.
- `event_cursor`: checkpoint capturado **antes** de leer mensajes. Se puede pasar
  a `wait_for_updates`; no confundirlo con el cursor de paginación del inbox.
- `tasks` y `next_task_offset`: tareas propias no terminadas. La paginación de
  tareas refleja el estado actual, no congela una fotografía entre llamadas.
- `pending_message_count`, `reply_needed_count`, locks de la sesión y su total.
  Los listados no revelan tokens de adquisición; conserva los de `prepare_edit`.

Continúa las páginas de mensajes hasta `next_cursor=null`. Confirma únicamente
mensajes procesados con `ack_messages` o como parte del handoff. Los errores de
cursor mantienen el contrato del inbox existente. Los snapshots de bootstrap y
pendientes son lecturas compuestas, no una transacción global de todos los datos.

## Preparar edición

Después de reclamar la tarea:

```json
{
  "paths": ["src/service.py", "tests/test_service.py"],
  "scope": "project",
  "reason": "Implementar T1",
  "ttl_seconds": 300,
  "operation_key": "T1-edit-1"
}
```

`prepare_edit` adquiere **todos los archivos o ninguno**. La normalización y los
límites de rutas son los del sistema de locks existente. `project` protege el
recurso lógico compartido entre worktrees; `checkout` protege la ruta física.

Conserva `file_path`, `scope`, `acquisition_id` y `expires_at` de cada resultado.
Los resultados contienen rutas físicas canónicas reutilizables con scope
`checkout`. Renueva con `renew_lock` antes de vencer. Una reserva vigente, incluso
de la misma sesión, entra en conflicto con otra adquisición distinta.

- Repetir la misma clave y argumentos devuelve los mismos tokens, sin extender TTL.
- Reutilizar la clave con otro contenido produce 409.
- Si algún lock fue liberado, sustituido o venció, el replay produce 409: adquiere
  una reserva nueva con otra clave. No se devuelve un permiso de edición obsoleto.
- Un conflicto o error no deja reservas parciales.

Las claves se guardan por sesión y operación en `coordination_operations`. El
resultado persiste ante reconexiones o reinicios. Una nueva sesión necesita
nuevas reservas; la renovación de credenciales no hereda locks anteriores.

## Entrega de trabajo

```json
{
  "task_id": "T1",
  "to_agent": "reviewer",
  "summary": "Cambio implementado; listo para revisar",
  "operation_key": "T1-handoff-1",
  "task_status": "in_review",
  "files_touched": ["src/service.py"],
  "validation_commands": ["pytest -q tests/test_service.py"],
  "validation_summary": "Pruebas aprobadas",
  "acknowledge_message_ids": [],
  "release_locks": [
    {"file_path": "/ruta/proyecto/src/service.py", "scope": "checkout", "acquisition_id": "token-devuelto"}
  ]
}
```

`complete_handoff` valida propietario y estado, cambia la tarea, guarda evidencia
y mensaje al destinatario, registra auditoría, confirma los IDs explícitos y
libera sólo las adquisiciones indicadas en **una transacción**. Un ACK ajeno,
un token obsoleto o una tarea ajena impide todos los efectos.

El destino recibe un mensaje persistente aunque no esté conectado. Se conserva
el propietario de la tarea; el destinatario del mensaje no se convierte en su
nuevo dueño. `in_review` es el estado predeterminado; también se admiten `blocked`
y `done`. Una tarea bloqueada requiere desbloqueo antes de entregarse como
revisada/terminada. Completar una dependencia desbloquea sus dependientes cuando
todas sus dependencias están terminadas; no desbloquea bloqueos manuales ajenos.

Reintenta exactamente la misma petición y clave: obtendrás el resultado previo,
sin mensajes duplicados ni liberación de locks adquiridos después. El resultado
repetido describe la operación original, no el estado actual de la tarea.

La evidencia es **declarada por el agente** (`validation_source=reported_by_agent`).
El handoff no ejecuta comandos, crea commits, evalúa criterios ni fusiona Git.
No revoca la credencial compartida por MCP/watcher. Se mantienen el watcher,
`wait_for_updates` y los mecanismos existentes de ejecución/revisión.

## Integración Git y credenciales

El integrador captura el SHA candidato y la base destino antes de ejecutar tests,
verifica que ambos permanecen iguales y que los checkouts están limpios después,
y revisa el diff entre esos commits. Fusiona el SHA revisado, no el nombre mutable
de la rama. Rechaza un destino que cambió después de revisar. Las carpetas de
runtime y artefactos de tests deben estar ignoradas en Git. Los procesos externos
que modifiquen simultáneamente el checkout siguen requiriendo coordinación.

`auth create` guarda el secreto sin imprimirlo por defecto. Usa `--show-token`
explícitamente si necesitas copiarlo a la consola; `--quiet` prevalece.

## API y validación

| Tool | Endpoint |
|---|---|
| `bootstrap_agent` | `POST /coordination/bootstrap` |
| `my_pending_items` | `GET /coordination/pending` |
| `prepare_edit` | `POST /coordination/prepare-edit` |
| `complete_handoff` | `POST /coordination/handoff` |

Los endpoints requieren autenticación incluso si el hub permite modo legacy.
Las sesiones se revalidan dentro de la transacción de escritura. El vencimiento
de locks nunca supera el de la sesión. Un escritor compartido pendiente devuelve
503 reintentable, sin confirmar cambios de otra operación.

Pruebas: `tests/test_coordination_workflow.py`, `tests/test_integrator_exact_sha.py`
y `test_two_stdio_agents_bootstrap_claim_lock_and_handoff` en
`tests/integration/test_mcp_stdio.py`. Usan bases/repositorios temporales y, en el
último caso, dos procesos MCP stdio y un hub HTTP reales. No representan una
aceptación con aplicaciones/modelos externos ni una prueba de autonomía continua.

Los registros de idempotencia de coordinación no se purgan automáticamente.
Las operaciones nuevas no implementan cuotas de consumo ni cambian los pendientes
T-20/T-21. Los locks siguen siendo cooperativos, sin interceptar escrituras de un
editor que ignore el protocolo.
