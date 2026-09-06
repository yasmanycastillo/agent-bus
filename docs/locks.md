# Locks por recurso, sesión y adquisición

Un lock es una lease cooperativa de edición dentro de la base de un proyecto. Su propietario combina `agent_id`, `session_id` y un `acquisition_id` aleatorio nuevo en cada adquisición. Dos credenciales del mismo agente no intercambian permisos sobre sus locks. Ni el administrador libera una adquisición ajena mediante la API normal.

## Alcance y rutas

| Scope | Recurso protegido |
| --- | --- |
| `checkout` (predeterminado) | Archivo físico; CLI y MCP envían su ruta absoluta canónica. Dos worktrees con copias diferentes no colisionan. |
| `project` | Archivo lógico compartido entre los worktrees del mismo proyecto; se representa bajo su raíz canónica. Todos los participantes de esa coordinación deben elegir este alcance. |

`.` y `..`, rutas absolutas/relativas equivalentes y symlinks se normalizan. Un alias al mismo archivo físico conserva el mismo lock. Una ruta de proyecto que escapa de la raíz, incluso por symlink, se rechaza. Las rutas pueden identificar archivos aún inexistentes. No se compara contenido ni inodos de hardlinks.

En CLI/MCP, las rutas relativas parten del cwd del cliente. MCP fija cwd y raíz al iniciar la conexión. En HTTP directo, `checkout` relativo parte de la raíz fijada del hub, o del directorio de su base si no hay raíz; enviar un absoluto para identificar otro checkout. `project` en HTTP exige una ruta relativa a la raíz canónica y una raíz de proyecto configurada. Enviar el mismo alcance y recurso al renovar/liberar.

El alcance compartido es un acuerdo de coordinación: no impide que otro participante elija expresamente un lock físico sobre su propia copia. Tampoco un lock sobre un directorio bloquea automáticamente sus descendientes.

## Adquirir, renovar y liberar

```bash
agent-bus work lock src/service.py --ttl 300 --reason "Implementar tarea"
# Conservar acquisition_id y expires_at impresos.
agent-bus work renew-lock src/service.py --acquisition-id "$LOCK_ACQUISITION_ID" --ttl 300
agent-bus work unlock src/service.py --acquisition-id "$LOCK_ACQUISITION_ID"
```

Asignar `LOCK_ACQUISITION_ID` al identificador devuelto por esa adquisición. Para coordinar todos los worktrees, añadir `--scope project` en las tres operaciones. No existe un caché que sustituya automáticamente un token viejo por el del titular actual.

MCP ofrece `acquire_lock(file_path, reason?, scope?, ttl_seconds?)`, `renew_lock(file_path, acquisition_id, scope?, ttl_seconds?)` y `release_lock(file_path, acquisition_id, scope?)`. La sesión determina el actor. HTTP utiliza `/locks/acquire`, `/locks/renew` y `/locks/release` con esos campos; `agent_id` sólo puede coincidir con la sesión autenticada.

La adquisición/renovación devuelve ruta canónica, titular, sesión, fecha de adquisición, token y vencimiento. Listados y dashboard omiten el token. Conservar la respuesta original; un reintento de adquisición activa devuelve conflicto, aunque sea del mismo propietario. Si se pierde la respuesta de adquisición no se recupera su token mediante el listado: esperar a su vencimiento y adquirir de nuevo.

La duración predeterminada es **300 segundos**, con valores enteros entre **1 y 3600**. El vencimiento nunca supera el de la sesión. Renovar antes de expirar —por ejemplo, a mitad de la duración— y comprobar la respuesta. No hay renovación automática por heartbeat, lectura del inbox o ejecución del runner. Cada renovación fija el plazo solicitado desde el reloj del hub; pedir un plazo menor puede acortar la lease.

Los locks vencidos desaparecen del listado activo y otra adquisición puede reemplazarlos atómicamente. El token anterior nunca renueva ni libera al sucesor, incluso si vuelve a ser del mismo agente y sesión. Liberar una ruta ya ausente es idempotente; renovar una ausente o vencida falla. Tras perder o vencer la lease, detener la edición, adquirir otra y volver a comprobar el contenido antes de continuar.

Errores HTTP: `422` para formato/ruta/TTL inválidos; `401` para sesión inválida, expirada o revocada; `409` para adquisición/renovación en conflicto; `403` para liberación por titular/token incorrectos. Una transacción pendiente en la conexión compartida devuelve `503` con `Retry-After: 1`, sin confirmar trabajo ajeno ni ejecutar la mutación; reintentar esa solicitud mientras la lease siga vigente. Revocar una sesión bloquea sus nuevas operaciones inmediatamente; la adquisición que dejó atrás permanece hasta su vencimiento original.

## Migración y límites

La migración añade metadatos de sesión/adquisición/vencimiento de forma transaccional. Los locks antiguos sin lease quedan vencidos y requieren una adquisición nueva; no se les atribuye una sesión inventada con permiso indefinido. Actualizar clientes junto al hub: liberar sin `acquisition_id` deja de estar admitido. El despliegue activo no se reinicia automáticamente.

El recurso se identifica dentro de una base vinculada a un proyecto. La comparación temporal utiliza el reloj UTC del hub, no el del cliente; mantener estable ese reloj. Los locks no cercan escrituras del sistema de archivos: un editor externo, un agente que ignore el protocolo o un proceso antiguo que continúe después de expirar puede escribir. Cambios de symlinks, renombrados y hardlinks requieren coordinación adicional. Las rutas físicas presuponen un filesystem local compartido; no traducen montajes diferentes entre máquinas.

`tasks/{id}/lock-files` conserva sólo la lista descriptiva de archivos de una tarea; no adquiere estas leases. El guard local worker/watch de T-11 evita ejecutores automáticos duplicados y es independiente de los locks de edición. La validación con aplicaciones MCP externas continúa en T-13.
