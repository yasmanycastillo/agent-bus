# Hermes Agent y agent-bus

Hermes Agent es un cliente MCP del bus. La clase Python `HermesOrchestrator`
es un adaptador de inferencia HTTP independiente: no ejecuta el CLI `hermes`.

## Configuración local

Provisione una identidad con rol `agent` en el proyecto, usando la misma base y
configuración del hub (ver [autenticación](authentication.md)):

```bash
uv run agent-bus auth create --agent hermes --provider hermes --role agent
```

El comando no sobrescribe credenciales existentes. La sesión dura 24 horas por
defecto; para renovarla, use un archivo nuevo con `--output` y actualice su ruta
en la configuración MCP. Nunca copie el token al repositorio.

Añada el servidor con rutas absolutas y el ID real del proyecto:

```bash
hermes mcp add agent-bus \
  --command /ruta/agent-bus/.venv/bin/agent-bus \
  --env AGENT_BUS_CONFIG_DIR=/ruta/proyecto/.agent-bus/runtime \
        AGENT_BUS_PROJECT_ROOT=/ruta/proyecto \
        AGENT_BUS_PROJECT_ID=ID_DEL_PROYECTO \
        AGENT_BUS_AGENT_ID=hermes \
        AGENT_BUS_SESSION_FILE=/ruta/proyecto/.agent-bus/runtime/credentials/hermes.json \
        AGENT_BUS_URL=http://localhost:8420 \
  --args mcp-server
```

Seleccione las herramientas al terminar el descubrimiento. La configuración se
guarda en `~/.hermes/config.yaml`; contiene la ruta de la credencial, no su token.
Queda vinculada a ese proyecto incluso si Hermes se abre en otro directorio.
No reutilice esta entrada para otro proyecto sin cambiar su configuración.

```bash
hermes mcp test agent-bus
hermes chat --oneshot -Q --ignore-rules --max-turns 8 --run-budget 90 \
  -q 'Usa exclusivamente get_project_status de agent-bus y resume su resultado.'
```

En v0.21.0, `-t mcp-agent-bus` por sí solo filtra fuera el servidor: el filtro de
descubrimiento usa la clave `agent-bus`. La corrida verificada no usa `-t`.
`--ignore-rules` se utiliza para aislar la aceptación; no es una recomendación
para saltarse las instrucciones de un proyecto durante desarrollo.

## Aceptación real del 2026-09-06

Cliente: Hermes Agent v0.21.0, modelo configurado **gpt-5.6-sol**. No se sustituyó
por un modelo Hermes 3. El servidor descubrió 12 herramientas; una prueba local
ejecutó `get_project_status` y `post_message` a Codex, que confirmó la entrega.

El [harness optativo](../scripts/acceptance_hermes.py) ejecuta dos turnos reales en
un repositorio Git temporal con hub, base, credenciales y configuración Hermes
aislados. Copia privadamente la autenticación del proveedor configurado; requiere
red y consume inferencia. No forma parte de pytest.

```bash
uv run python scripts/acceptance_hermes.py
# Auditar una corrida conservada sin nuevas llamadas al modelo:
uv run python scripts/acceptance_hermes.py --verify /tmp/agent-bus-hermes-XXXXX
```

Resultado verificado en [evidencia sanitizada](evidence/t18-hermes.json):

- Lectura y respuesta MCP con ACK; repetición idéntica de la respuesta sin duplicación.
- Cuatro entregas, todas confirmadas, sin retransmisión humana del contenido.
- Reanudación explícita con el mismo ID de sesión; recuerda un marcador que no
  aparece en el prompt de continuación ni en el segundo mensaje del bus.
- Plan de dos tareas con dependencia, generado por Hermes y validado mediante
  JSON Schema/DAG. Dos publicaciones iguales producen sólo dos tareas, con
  estados `pending` y `blocked`.
- La traza de Hermes sólo contiene descubrimiento, lectura y respuesta MCP;
  no ejecuta herramientas de shell o archivos en estos turnos.

El MCP actual no tiene una herramienta para crear desgloses. El harness valida
el JSON recibido por el bus y llama a `HermesOrchestrator.publish_breakdown`
contra `/tasks/batch` con identidad autenticada. Esta es la conexión de publicación
probada; no se acredita publicación directa por una herramienta MCP de Hermes.
Tampoco se acreditan despertar espontáneo de TUI, worker Hermes, implementación
de las tareas ni merges autónomos.

La primera corrida completó ambos turnos y la publicación, pero falló en la
consulta final del resumen por usar `acknowledged` en vez de `archived`. El
verificador corregido auditó las bases y trazas conservadas sin repetir la
inferencia. Artefactos privados: `/tmp/agent-bus-hermes-0qnsj6go`; pueden expirar.
Sólo el JSON sanitizado se conserva en Git.

## Respuestas automáticas a consultas: Hermes → Codex

La aceptación anterior arrancaba los turnos explícitamente. No demostraba que
un mensaje entrante provocara por sí solo una respuesta. Esa brecha se corrigió
y se verificó el 2026-09-06 con ambos clientes reales:

```bash
# En el checkout del proyecto, con la credencial Codex vigente:
uv run agent-bus watch --agent codex --cli codex
```

El watcher permanece activo, recibe solicitudes con `reply_needed=true`, ejecuta
`codex exec --json` y entrega la respuesta con ACK atómico del mensaje original.
Interpreta JSONL (`thread.started`, `item.completed`, `turn.completed`); guarda
la sesión por conversación para usar `codex exec resume` en mensajes posteriores.
Una salida incompleta o fallida no produce respuesta ni ACK. Tras cinco fallos
durables deja la solicitud pendiente con el error, sin seguir llamando al modelo.

Los turnos del watcher Codex usan sandbox `read-only` para consultar el proyecto.
No reclaman tareas ni sustituyen un worker de implementación. El listener ya está
en el proceso padre; el prompt instruye al receptor a devolver texto y dejar el
envío/ACK al watcher. No debe arrancar otro listener para la misma identidad.

Desde otra terminal, abra una sesión nueva de Hermes y pídale:

> Envía a codex por agent-bus una consulta del estado del proyecto, con
> reply_needed=true y una clave de idempotencia única. Espera su respuesta con
> wait_for_updates; si vence el plazo, vuelve a esperar. Confirma la respuesta
> con ack_messages y muéstrala.

No hace falta pedir a Codex que revise el inbox. Para continuar el mismo hilo,
Hermes usa `reply_message` sobre la respuesta con `reply_needed=true`.

Prueba real: Hermes envió una consulta sobre README.md/TASK.md, el watcher lanzó
Codex automáticamente y Hermes esperó, recibió y confirmó la respuesta. Las dos
entregas quedaron archivadas con cero intentos fallidos. [Evidencia](evidence/t18-auto-reply.json).
Validación de código: **660 pruebas aprobadas**, dos avisos de deprecación de
WebSocket, en 151,02 s.

El receptor ejecuta una conversación Codex en segundo plano, independiente de
la TUI que esté abierta. La respuesta se muestra en Hermes; no aparece un turno
nuevo en aquella TUI. `--dry-run` sólo observa y no responde. El proceso activo
no instala un servicio de arranque tras reiniciar el equipo; las credenciales
tienen vencimiento y deben renovarse según la guía de autenticación.
