# Eventos recuperables (T-09)

El inbox es la fuente de trabajo pendiente. SSE avisa de entregas; recibir un evento no confirma ni ejecuta el mensaje. La confirmación sigue siendo explícita según [el contrato de mensajes](messaging.md).

## Persistencia y retención

Cada entrega nueva guarda su evento en la misma transacción SQLite. Un rollback no deja eventos huérfanos y un reintento idempotente no crea otro evento. Broadcast genera un evento por destinatario, también en el feed administrativo. El historial sobrevive a desconexiones y reinicios; no depende de que el aviso en memoria llegue a publicarse.

Los eventos se conservan hasta siete días, con un máximo global de 10 000 registros por base, lo que ocurra primero. Se purga un prefijo al escribir o consultar; una marca durable registra hasta dónde se eliminó. Estos valores son defaults del código, no opciones de CLI. La retención no elimina mensajes pendientes, estados de entrega ni registros de idempotencia. Tampoco establece una política integral de borrado de datos.

La migración incorpora las entregas pendientes existentes una sola vez. Las confirmadas anteriores no generan historial. La marca de migración y las marcas por entrega impiden regenerar eventos purgados. El cursor es opaco y firmado con un secreto persistente de la base, vinculado al destinatario o al feed administrativo. No es intercambiable con el cursor de páginas del inbox. Un cursor puede vencer por actividad de otros agentes, pues la retención es global.

## HTTP y SSE

Todas las rutas requieren la sesión y los permisos habituales:

| Ruta | Uso |
| --- | --- |
| `GET /inbox/{agent}/events/cursor` | Captura la posición actual. Con `?cursor=...`, valida y devuelve ese mismo cursor, sin adelantarlo. |
| `GET /inbox/{agent}/events` | Feed personal; acepta `Last-Event-ID` o `?cursor=...`. El encabezado tiene prioridad. |
| `GET /events/all/cursor` y `/events/all` | Checkpoint y feed global, solo administradores. |
| `/events/{agent}/cursor` y `/events/{agent}` | Alias compatibles; `all` está reservado al feed global. Para un agente llamado `all`, usar la ruta canónica `/inbox/all/events`. |

Sin cursor, el stream comienza en la posición actual; no reproduce todo el historial. Para evitar la ventana entre consulta y suscripción: capturar cursor, leer pendientes, suscribirse con ese cursor. Para continuar después de una desconexión, enviar el último cursor procesado correctamente.

Un mensaje SSE contiene `id: <cursor>`, `event: message` y `data: <Envelope JSON>`. Los eventos `checkpoint` contienen `data: {"cursor":"..."}` e ID; permiten avanzar aunque solo haya actividad fuera del ámbito personal. No son mensajes de trabajo. El parser admite datos multilínea, ignora comentarios y descarta el último frame si EOF llega sin línea vacía. Limita la acumulación de cada frame a 256 KiB.

El servidor mantiene una señal coalescida por suscriptor y lee lotes de hasta 50 eventos. Mientras espera novedades, consulta SQLite cada segundo aunque no llegue un aviso en memoria. Una escritura bloqueada del stream tiene un plazo de 10 segundos; una sesión se revalida antes de enviar y durante la espera. Al desconectar o cancelar se elimina la suscripción. La consulta reserva el escritor SQLite para mantener una vista coherente de retención y página: muchos streams concurrentes aumentan la contención y requieren medir capacidad. Esto acota la cola SSE; no promete un límite global de conexiones o del tamaño de los cuerpos de mensajes.

## Errores y recuperación

| Situación | Respuesta y acción |
| --- | --- |
| Cursor inválido, alterado, de otro ámbito o futuro | HTTP `422`, `error: cursor_invalid`. Corregir el cursor; no reintentar indefinidamente. |
| Cursor vencido antes de abrir SSE | HTTP `410`, con `error: cursor_expired`, cursor nuevo y `recovery: read_inbox`. |
| Cursor vence durante SSE | Evento `reset` con el mismo contenido de recuperación; el servidor cierra el stream. |
| Sesión inválida o sin permiso | HTTP `401`/`403`; una sesión revocada también cierra el stream activo. Renovar/provisionar según corresponda. |
| EOF o red interrumpida | Reconectar con el último cursor procesado; los clientes aplican espera entre intentos. |

Al vencer un cursor, guardar el cursor nuevo **antes** de recorrer el inbox pendiente, procesar y confirmar según corresponda, y reconectar desde esa posición. No se puede recuperar el historial purgado, pero los mensajes pendientes siguen disponibles. Los consumidores deben tolerar repetir avisos y comprobar el estado actual del mensaje. Un evento retenido puede corresponder a una entrega ya confirmada.

## MCP, worker y panel

`wait_for_updates` acepta `timeout` entero de 1 a 120 segundos y `event_cursor` opcional. Su plazo total incluye checkpoint, lectura inicial, conexión y recepción, aun con comentarios o checkpoints continuos. Devuelve como máximo cinco pendientes; `next_cursor` continúa las páginas del inbox y `event_cursor` reanuda eventos. Conserva el cursor por agente mientras vive la instancia MCP; el cliente puede guardar y reenviar el cursor devuelto para sobrevivir a su propio reinicio.

Los resultados son `pending_messages`, `event_received`, `timeout` o `error`. Este último incluye `code`: `cursor_expired` (con `recovery: read_messages`), `cursor_invalid`, `stream_closed`, `bus_unavailable`, `unauthenticated`, `forbidden`, `hub_error` o `protocol_error`. EOF no se presenta como un timeout. Cancelar la coroutine cierra la conexión; T-10 añade cancelación concurrente por JSON-RPC/stdio mediante el SDK. Los resultados con `status: error` se entregan con `isError: true`; timeout es un resultado normal.

Worker y panel conservan cursores en memoria al reconectar, avanzan después del procesamiento local del aviso y tratan checkpoints por separado. Worker despierta el polling ante un reset; el panel refresca su vista. Reiniciar esos clientes pierde su cursor local: el polling/consulta inicial recupera el trabajo pendiente. Cerrar sesión en el panel borra credencial y cursor. Los errores de autenticación no habilitan una conexión sin credenciales.

La validación automatizada incluye SQLite, ASGI, HTTP real efímero, clientes simulados y ejecución del JavaScript del panel con Node. No sustituye la aceptación con dos clientes MCP externos ni una sesión de navegador completa (T-13).
