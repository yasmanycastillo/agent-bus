# Mensajes, respuestas y confirmaciones (T-08)

El ciclo de mensajes usa sesiones autenticadas y una base SQLite por proyecto, según [authentication.md](authentication.md). Leer un mensaje no lo confirma. La confirmación retira una entrega del inbox pendiente; no afecta las entregas del mismo broadcast a otros agentes.

## Identidades y garantías

- `message_id`: identifica un mensaje lógico. Un broadcast conserva ese ID en todas sus entregas.
- `conversation_id`: identifica el hilo. Un mensaje nuevo recibe su propio ID como conversación si no se proporcionó uno. Los mensajes históricos usan su correlación anterior o su ID.
- `correlation_id`: en una respuesta creada por `/reply`, apunta al mensaje concreto que se responde. La respuesta conserva la conversación y la tarea relacionada del original.
- Entrega: pareja `(message_id, to_agent)`, con secuencia durable, confirmación y registro de fallos propios.
- `idempotency_key`: clave del envío, con 1 a 128 caracteres, asociada al remitente autenticado. Debe conservarse al repetir la misma operación; usarla para contenido diferente produce `409`.

El servidor guarda el resultado y todas las entregas de un envío en una transacción. Un reintento con la misma clave devuelve el ID original y `replayed: true`. En broadcasts conserva también los destinatarios originales: no incorpora agentes registrados después ni vuelve a abrir entregas ya confirmadas. Las claves persisten entre conexiones y reinicios; renovar una sesión del mismo agente no cambia su alcance.

Los avisos SSE/WebSocket se emiten después del commit, únicamente para el primer envío con clave. Si se pierde el aviso, el mensaje sigue en el inbox. T-09 guarda el evento con la entrega y permite [reanudar SSE](events.md); no se afirma entrega exactamente una vez de eventos ni de efectos producidos por el runner.

## API HTTP

Las rutas de inbox requieren la sesión de su propietario, incluso para administradores. Los endpoints administrativos del panel tienen su política propia.

| Operación | Request | Resultado |
| --- | --- | --- |
| Enviar | `POST /messages` con `to_agent`, `body`, `idempotency_key` y opcionales | ID, conversación, estado y `replayed` |
| Leer página | `GET /inbox/{agent}/messages?limit=50&cursor=...` | `{messages: [...], next_cursor: ...}` |
| Confirmar | `POST /inbox/{agent}/ack` con `{message_ids: [...]}` | `{acknowledged: [...]}` |
| Responder | `POST /inbox/{agent}/{id}/reply` con `body`, `idempotency_key` | ID de respuesta y conversación |
| Consultar entrega | `GET /inbox/{agent}/{id}` | Mensaje y campos de estado |
| Registrar fallo | `POST /inbox/{agent}/{id}/fail` con `{error: "..."}` | Estado actualizado, sin confirmar |

Enviar sin destinatario, con `to_agent: "*"` o con `message_type: "broadcast"` distribuye a los agentes registrados, excluyendo al remitente. Un destinatario directo puede estar desconectado.

`/reply` deriva remitente, destinatario, correlación y conversación del mensaje original y de la sesión. No acepta un destinatario elegido por el cliente. `reply_needed` es falso por defecto; activarlo inicia otra solicitud de respuesta. `acknowledge: true` solicita explícitamente guardar la respuesta y confirmar el original en la misma transacción. Si cualquiera de esas escrituras falla, ninguna se conserva. Sin esa opción, responder deja el original pendiente.

Confirmar admite entre 1 y 100 IDs. Si alguno no pertenece a ese inbox o no existe, devuelve `404` y no confirma ninguno. Repetir una confirmación existente devuelve éxito sin cambiar su fecha. Una sesión ajena recibe `403`. Un ID existente con contenido o autor diferente no puede reutilizarse para alterar el mensaje.

El estado incluye `acknowledged`, `acknowledged_at`, `attempts`, `last_error`, `failed_at` y `sequence`. `attempts` cuenta fallos registrados, no todos los turnos exitosos. El error admite hasta 2000 caracteres. Registrar un fallo de una entrega ya confirmada devuelve conflicto y no la vuelve a abrir.

## Paginación

`limit` admite 1 a 100 mensajes; por defecto devuelve hasta 50. `reply_needed=true` permite consumir únicamente solicitudes de respuesta. Las lecturas no confirman ni modifican los mensajes.

El cursor está firmado, vinculado al agente y al filtro. Incluye la última secuencia leída y un límite superior fijado al comenzar el recorrido. Los mensajes nuevos quedan para un recorrido posterior; confirmar mensajes entre páginas no desplaza posiciones como ocurriría con offsets. Una entrega confirmada entretanto puede desaparecer del recorrido: el cursor no congela su estado de confirmación.

Reutilizar un cursor de otro inbox, alterar su contenido o cambiar el filtro produce `422`. Al recibir `next_cursor: null`, terminar ese recorrido. Para buscar llegadas posteriores, comenzar sin cursor. Los límites temporales y cursores de eventos SSE tienen [un contrato separado](events.md).

## MCP

Las herramientas usan la identidad fijada al arrancar el proceso MCP:

```text
post_message(to_agent="bob", text="Revisa T08", idempotency_key="T08-review-1", reply_needed=true)
read_messages(limit=20)
reply_message(message_id="ID_RECIBIDO", text="Revisado", idempotency_key="T08-answer-1", acknowledge=true)
ack_messages(message_ids=["OTRO_ID_RECIBIDO"])
```

Guardar la clave de la operación antes de llamar a `post_message` o `reply_message` y conservarla si se pierde la respuesta. Generar otra clave en cada reintento crea otra operación.

**Cambio de contrato:** `read_messages` devuelve un objeto con `messages` y `next_cursor`, en lugar de una lista. Las claves son obligatorias en las herramientas MCP de envío/respuesta. `wait_for_updates` devuelve como máximo cinco mensajes pendientes y el cursor correspondiente; los confirmados dejan de aparecer como pendientes. La espera tiene un plazo total de 1 a 120 segundos y un event_cursor separado; T-10 incorpora el SDK y la cancelación por stdio. Cancelar no revierte una mutación ya guardada: conservar su clave de idempotencia al reintentar.

## CLI

```bash
uv run agent-bus work msg bob "Revisa T08" --reply-needed --idempotency-key T08-review-1
uv run agent-bus work inbox --limit 20
uv run agent-bus work inbox --limit 20 --cursor CURSOR_DE_LA_PAGINA
uv run agent-bus work reply ID_RECIBIDO "Revisado" --idempotency-key T08-answer-1 --ack
uv run agent-bus work ack ID_1 ID_2
```

Si no se indica clave en `work msg` o `work reply`, la CLI genera una y la imprime antes de enviar. Conservarla para repetir un intento cuya respuesta se haya perdido. El inbox muestra IDs completos para responder o confirmar. El alias `work inbox --archive ID` continúa como confirmación explícita.

## Workers y watcher

El daemon y el watcher consultan páginas de solicitudes pendientes. SSE solo los despierta: también consultan almacenamiento al arrancar y durante el polling, para recuperar mensajes que llegaron mientras estaban desconectados.

Antes de ejecutar, consultan el estado de la entrega. Solicitan al runner una respuesta textual y el wrapper la envía mediante `/reply` con una clave estable y `acknowledge: true`. No piden al runner enviar además otra respuesta por CLI. Un resultado fallido, una excepción o un fallo al guardar la respuesta conserva la entrega pendiente y registra el error cuando el hub está disponible. Los reintentos tienen espera local para evitar un bucle inmediato de fallos.

La cancelación termina y recoge el subproceso directo. Los errores estructurados de Claude con salida cero tampoco se consideran éxito. Una respuesta HTTP perdida después del commit no obliga a repetir el runner en la siguiente consulta: el original ya está confirmado.

Esto no proporciona exclusión entre varios procesos del mismo agente ni recuperación exactamente una vez de efectos externos. Los leases por sesión y los presupuestos/reintentos persistentes pertenecen a T-11/T-14.

## Migración y retención

La migración conserva las columnas históricas, el archivo y los índices del inbox; añade conversación, secuencias por entrega, estados de fallo y registros de idempotencia. Las secuencias no se reciclan. La ruta antigua `GET /inbox/{agent}` se mantiene como lista completa por compatibilidad; los clientes actualizados usan páginas. Los envíos HTTP sin clave siguen admitidos por compatibilidad y no tienen garantía de idempotencia de request.

Los registros de idempotencia y de secuencias sobreviven a la limpieza de entregas confirmadas, para no volver a entregar un envío antiguo. Esa conservación incluye el resultado del envío; no constituye una política de eliminación completa de datos. Si la limpieza ya eliminó el mensaje padre de una respuesta, `/reply` devuelve `404`, incluso al intentar repetir esa respuesta. La retención de eventos está definida en [events.md](events.md); la política integral de datos sigue pendiente.

Las pruebas usan bases y hubs efímeros, sesiones provisionadas, clientes MCP en proceso y runners controlados. No equivalen todavía a la aceptación de dos clientes MCP externos por stdio (T-13).
