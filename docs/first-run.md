# Primer uso MCP

Esta guía prepara un hub local y sesiones para dos aplicaciones. No arranca
workers ni el integrador. La versión inicial se valida en Linux con Python 3.12+.
El paquete aún no tiene una release pública verificada en PyPI: no asumir que
`uvx agent-bus` descarga este proyecto.

## Instalar desde el repositorio

Con uv instalado, desde el checkout:

```sh
uv tool install .
```

Esto deja una instalación persistente y el comando `agent-bus` en el PATH de tools
de uv. Si la terminal no lo encuentra, ejecutar `uv tool update-shell` y abrir otra.
El usuario final podrá instalar una versión del paquete sin clonar cuando exista
la release pública. El flujo desde un wheel también está soportado por uv.

## Preparar tu proyecto

```sh
agent-bus --project /ruta/mi-proyecto onboard --mcp-only
```

Por defecto prepara `backend` (etiqueta Claude), `qa` (etiqueta Codex) e `integrator`
(admin). Las etiquetas no instalan ni ejecutan esos clientes. Personaliza:

```sh
agent-bus --project /ruta/mi-proyecto onboard --mcp-only \
  --agents backend:claude,qa:codex --admin human --port 8421
```

`--yes` autoriza explícitamente la provisión local sin preguntas. Las identidades
se validan antes de escribir; no usar identidades duplicadas ni compartir la del
administrador con un agente. El asistente conserva el hub configurado al repetir;
no cambia el puerto de un proyecto existente.

## Conectar tus aplicaciones

El asistente imprime un archivo JSON por agente en `.agent-bus/runtime/mcp/`.
Contiene el ejecutable Python instalado, argumentos y rutas absolutas a sesiones;
no incluye los tokens. Copiar la entrada `mcpServers.agent-bus` al formato JSON
MCP de tu aplicación, conservando las demás entradas. Para clientes con otro
formato, trasladar `command`, `args` y `env` según su documentación; no copiar
JSON directamente a un archivo TOML.

Conectar una aplicación como `backend` y otra como `qa`, reiniciar la conexión
y pedir `bootstrap_agent({})`. La comprobación HTTP del asistente confirma hub y
credenciales; el primer bootstrap del cliente confirma la conexión MCP completa.

Conservar el entorno Python usado por los snippets. Una reinstalación que cambie
su ruta requiere regenerar la configuración. El asistente no sobrescribe un
snippet diferente ni modifica archivos propios del cliente.

## Listeners para responder automáticamente

Para Grok y Codex:

```sh
agent-bus --project /ruta/mi-proyecto onboard --mcp-only --agents grok:grok,qa:codex
```

El asistente prepara MCP y muestra un `watch/<agente>/start.sh` por cada proveedor
compatible (Claude, Codex y Grok). No inicia modelos ni listeners automáticamente.
Con el CLI del proveedor instalado y autenticado, ejecutar el lanzador impreso
en otra terminal y dejarlo activo. Conserva directorio, intérprete, identidad y
rutas de credenciales sin incluir tokens.

```sh
/ruta/mi-proyecto/.agent-bus/runtime/watch/grok/start.sh
# Desde otra terminal:
/ruta/mi-proyecto/.agent-bus/runtime/watch/grok/start.sh --status
```

También se puede ejecutar `agent-bus --project /ruta/mi-proyecto watch --agent grok`:
el proveedor se obtiene de su credencial. `--cli grok` lo selecciona explícitamente;
`--model` cambia el modelo Grok (por defecto `grok-4.6`). El adaptador Grok funciona
como consulta de texto sin herramientas ni ediciones. El watcher publica la
respuesta y confirma el mensaje. No despierta una TUI ni instala un servicio de
inicio del sistema. Un heartbeat o `--dry-run` no habilita ejecución automática.

`--status` devuelve JSON con `active`, `state` y `can_dispatch`. Comprueba la reserva
real del ejecutor en el sistema operativo y la actualidad de su estado; un archivo
PID antiguo no basta. `can_dispatch=true` significa que el watcher está esperando
tras consultar correctamente el hub, no que haya validado cuota o acceso al modelo.
Los procesos de versiones anteriores que no publican estado aparecen como `unknown`.

| Estado | Interpretación / acción |
|---|---|
| `waiting` | Esperando solicitudes con respuesta requerida |
| `running` / `delivering` | Consultando al modelo / enviando su respuesta |
| `stopped` | Iniciar el lanzador |
| `auth_error` | Revisar expiración, revocación e identidad de la credencial del bus |
| `provider_error` | Revisar autenticación del CLI, acceso al modelo y salida fallida |
| `hub_error` | Recuperar conexión con el hub |
| `delivery_error` | Respuesta conservada; se reintentará su entrega |
| `attempt_limit` | Cinco fallos antes de preparar una respuesta; requiere intervención |
| `unknown` / `other_executor` | Estado antiguo/desconocido o un worker ocupa esa identidad |

El watcher relee las credenciales del bus al consultar. Después de provisionar una
sesión válida según [autenticación](authentication.md), retoma los pendientes; nunca
los confirma por una consulta rechazada. No renueva credenciales automáticamente.

Antes del envío, conserva únicamente el texto final y la sesión en un archivo
privado dentro de `watch/<agente>/outbox/`. Si cae el proceso después de guardarlo,
el reinicio reenvía la misma respuesta y clave idempotente, sin otra llamada al
modelo. Si el hub ya confirmó la solicitud, no vuelve a ejecutarla. Una respuesta
HTTP perdida puede dejar un archivo local pendiente de reconciliación; no se debe
interpretar su existencia como una nueva solicitud. Una caída antes de guardar el
texto puede repetir la consulta al modelo. No se promete exactamente una ejecución
de efectos externos; el modo Grok de consulta no permite herramientas.

## Operación y recuperación

- Hub ocupado por otro proyecto: para un proyecto nuevo, elegir otro `--port`.
- Hub sin arrancar: consultar `.agent-bus/runtime/bus.log` y repetir.
- Credencial vencida/revocada: seguir [autenticación](authentication.md); no se
  reemplaza silenciosamente una sesión existente. Duración inicial: 24 horas.
- Variables `AGENT_BUS_*` de otro entorno: abrir una terminal limpia y seleccionar
  el proyecto mediante `--project`; se rechazan overrides ambiguos.
- Consola: abrir la URL `/console` que imprime el asistente. La sesión admin se
  guarda con permisos 0600; el token no se imprime. Consulta [la guía de la consola](console.md) para iniciar sesión y supervisar el proyecto.
- Detener el hub propio: `agent-bus --project /ruta/mi-proyecto serve --stop`.

No versionar `.agent-bus/runtime/`: contiene credenciales y datos operativos.
Consulta el [índice de documentación](README.md) para las guías de operación e integración.
