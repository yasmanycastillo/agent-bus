# Primer uso MCP

Esta guía prepara un hub local y sesiones para dos aplicaciones. No arranca
workers ni el integrador. La versión inicial se valida en Linux con Python 3.12+.
El paquete aún no tiene una release pública verificada en PyPI: no asumir que
`uvx agent-bus` descarga este proyecto.

## Sistemas operativos

| Entorno | Estado actual |
|---|---|
| Linux | Validado con pruebas automatizadas y clientes reales |
| macOS | Puede probarse en un entorno Unix, pero instalación, permisos y procesos aún no están validados aquí |
| Windows con Python nativo desde PowerShell o CMD | No soportado actualmente: el código importa `fcntl` y utiliza permisos y procesos Unix |
| Windows con WSL2 | Alternativa para ejecutar la versión Linux; no equivale a soporte nativo ni a una prueba específica ya completada en WSL2 |

El bloqueo de Windows no se limita a la sintaxis de los comandos. El paquete
importa [`fcntl`, disponible en Unix](https://docs.python.org/3/library/fcntl.html),
y también utiliza `os.getuid`, permisos de archivos Unix y lanzadores `sh`.
Cambiar PowerShell por Git Bash conservando Python de Windows no elimina esas
dependencias. La compatibilidad de macOS debe confirmarse en un equipo macOS;
no basta con que ambos sistemas sean Unix.

Para la alternativa WSL2, sigue la [instalación oficial de Microsoft](https://learn.microsoft.com/es-es/windows/wsl/install).
Desde PowerShell con permisos de administrador puedes preparar WSL con:

```powershell
wsl --install
```

Después, abre la distribución Linux y realiza allí la instalación de Python,
uv, agent-bus y los CLIs de proveedores. Para esta primera prueba, conserva
repositorio, runtime y credenciales dentro del sistema de archivos Linux de WSL.
Los pasos siguientes se ejecutan en esa terminal Linux. Conectar un cliente
Windows al servidor stdio dentro de WSL requiere adaptar su comando de inicio;
no lo cubre la configuración directa de esta guía.

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

## Workers persistentes para evitar esperas

El servidor MCP y una terminal Claude/Grok no son ejecutores persistentes. El MCP
por sí solo no inicia un worker y `wait_for_updates` deja de esperar cuando termina
la llamada. Por eso, si dos agentes deben continuar intercambiando mensajes sin
intervención humana, inicia un worker por identidad desde una terminal separada:

```sh
agent-bus --project /ruta/mi-proyecto worker start --agent claude --provider claude
agent-bus --project /ruta/mi-proyecto worker start --agent grok --provider grok
agent-bus --project /ruta/mi-proyecto worker status --agent claude
agent-bus --project /ruta/mi-proyecto worker status --agent grok
```

El worker debe usar la misma URL del hub, proyecto y credencial que MCP. Para usar
un supervisor externo, ejecútalo en primer plano con `--foreground`; así el supervisor
puede detectar su salida y reiniciarlo:

```sh
agent-bus --project /ruta/mi-proyecto worker start \
  --agent grok --provider grok --foreground
```

Con `systemd --user`, configura `Restart=on-failure` y `RestartSec=5`; con `tmux`,
mantén la sesión abierta. Revisa el log del runtime activo: `.agent-bus/runtime/workers/<agente>.log` dentro del
proyecto (o `~/.agent-bus/workers/<agente>.log` cuando se usa un runtime global).
El worker atiende el inbox, mantiene heartbeat y confirma sólo después de producir
y entregar la respuesta.

Para que el worker avise a la TUI sin inyectar comandos, configura el pane destino
de tmux antes de iniciarlo:

```sh
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_title}'
export AGENT_BUS_TMUX_TARGET='odoo:0.2'
agent-bus --project /ruta/mi-proyecto worker start --agent grok --provider grok
```

Si no usas tmux, puedes activar notificaciones de escritorio sin inyectar nada en
la terminal:

```sh
export AGENT_BUS_NOTIFY_DESKTOP=1
agent-bus --project /ruta/mi-proyecto worker start --agent grok --provider grok
```

En Linux usa `notify-send` cuando está disponible. El worker no marca el mensaje
como leído por mostrar la notificación; la confirmación sigue ocurriendo sólo
después de generar y entregar la respuesta.

También puedes usar `--foreground` bajo un supervisor; conserva la variable
`AGENT_BUS_TMUX_TARGET` o `AGENT_BUS_NOTIFY_DESKTOP` en el entorno del servicio.

Un worker `--provider agy` no puede pedir permisos: agy headless deniega cualquier
herramienta que los necesite (por ejemplo ejecutar `pytest` o `git diff` al revisar)
y el turno falla; `last_error` del mensaje indica las acciones denegadas. Añade a
`permissions.allow` del `settings.json` de agy reglas para los comandos de revisión
(p. ej. `command(<target>)`). Para pasar flags extra a agy, usa
`AGENT_BUS_AGY_ARGS` (se divide como en el shell y se añade al final del comando):

```sh
export AGENT_BUS_AGY_ARGS='--sandbox --effort high'
agent-bus --project /ruta/mi-proyecto worker start --agent agy --provider agy
```

agent-bus nunca añade `--dangerously-skip-permissions` por su cuenta; hacerlo
mediante esa variable aprueba todas las herramientas y es decisión del operador.

No uses `watch` y `worker` simultáneamente con la misma identidad: ambos intentan
ser el ejecutor automático y `ExecutionGuard` rechazará uno. Si además mantienes
una TUI abierta para supervisar, trátala como interfaz manual; no es el componente
que garantiza la entrega. Para automatización completa usa una identidad worker
separada de la identidad de la TUI y muestra alertas en la terminal o por otro
canal, sin inyectar comandos ciegamente en el prompt.

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

### Despertar una TUI abierta en muxel

Si el agente ya está abierto en un panel de [muxel](https://github.com/projecthax/muxel),
el watcher puede escribirle allí en lugar de lanzar un turno headless. Requiere muxel
en ejecución con *Settings > Grok Bot > Allow outside tools to control muxel* activado:

```sh
agent-bus watch --agent claude --cli muxel --muxel-agent Claude
```

`--muxel-agent` acepta el id, el nombre o `proyecto/nombre` que muestra `muxel ctl panes`.
El watcher escribe la solicitud solo si el panel está `idle` o `done`, espera con
`muxel ctl wait` y publica la respuesta únicamente si el turno termina (`finished`).
Un panel trabajando o bloqueado en una pregunta aplaza la solicitud sin contar un
fallo, y la solicitud no se vuelve a escribir mientras se espera ese turno. Como
muxel 0.2.8 no detecta cuándo trabaja la versión actual de Claude Code, el watcher
también consulta el estado que Claude publica en `~/.claude/sessions/` para la
carpeta del panel; cualquier Claude ocupado en esa carpeta aplaza la solicitud.
Por esa misma razón, `muxel ctl wait` puede tardar unos 30 s más en devolver la
respuesta.
Como respaldo para cualquier CLI (muxel 0.2.8 también marcó `idle` a Codex
trabajando), el watcher lee las últimas 12 líneas de `muxel ctl screen` y aplaza
la solicitud si muestran un indicador de turno en curso: `esc to interrupt`
(Codex, Claude Code), `Thinking…` o `[stop]` (Grok). Si `screen` falla, lo
registra y sigue con las demás comprobaciones.

Con `--cli muxel` el watcher también avisa al panel cuando cambian sus tareas:
una tarea nueva asignada, una revisión liberada (pasa de `blocked` a `in_progress`
al entregarse las implementaciones que cubre) o una implementación reabierta por
`changes_requested` (queda `pending` y libre; hay que reclamarla de nuevo). Escribe
un aviso breve que pide ejecutar `my_pending_items` y seguir el protocolo; no espera
el turno ni publica nada en el bus. El watcher compara cada pocos segundos las tareas
propias (`GET /tasks?owner=<agente>`) con las ya avisadas, guardadas en
`task_nudges.json` dentro de su directorio de estado, así que cada cambio se avisa
una vez aunque se reinicie. La primera vez que arranca, sin ese archivo, avisa una
vez de las tareas activas (`pending` o `in_progress`) que el agente ya tenga. Usa
las mismas comprobaciones de ocupado: un panel ocupado aplaza el aviso sin perderlo.
Las solicitudes con respuesta pendiente van antes que los avisos. Desactiva esta
consulta de tareas con `--no-task-nudges`.

Los mensajes con los que el hub entrega trabajo (el de `assign_work` al asignado,
con `body.role`; el de asignación del War Room, con `body.type = task_assigned`; y
el de `changes_requested` al implementador, con `body.verdict`) no se escriben como
consulta de solo lectura: se convierten en el mismo aviso, fusionado con el de la
tarea si coincide, y se confirman con `/inbox/<agente>/ack` solo tras un `send`
correcto, sin respuesta ni intentos fallidos. Esto vale también con
`--no-task-nudges`. Las preguntas normales entre agentes siguen el flujo de
consulta descrito arriba.

Al asignar una revisión con `assign_work`, el hub crea la tarea en `pending` y la
bloquea justo después hasta que se entreguen las implementaciones. Si la consulta
del watcher cae entre ambos pasos, el agente recibe un aviso de "tarea asignada"
para una revisión que aún no puede empezar; `my_pending_items` la mostrará
`blocked` y llegará otro aviso cuando se libere.

Abre muxel desde el escritorio o desde una terminal normal, no desde una sesión de
Claude Code: si hereda sus variables `CLAUDE_CODE_*`, el Claude de cada panel
arranca sin sesión iniciada (`Not logged in`). Las
preguntas de permiso del panel las responde el usuario en muxel.

### Paneles tmux que maneja el coordinador (prototipo)

`agent-bus panes` abre TUIs de agentes en un servidor tmux propio (`tmux -L agent-bus`,
sesión `agents`), separado del tmux del usuario. El coordinador los abre, les escribe y
los cierra; el usuario solo mira:

```sh
agent-bus panes spawn rev-1 agy --cwd ~/proyecto --agent agy --prompt "Ejecuta my_pending_items"
agent-bus panes spawn impl-1 claude --cwd ~/proyecto -- --permission-mode acceptEdits
agent-bus panes list            # nombre, preset y estado: idle, working, blocked o dead
agent-bus panes send impl-1 "Continúa con la tarea t-12"
agent-bus panes screen impl-1
agent-bus panes close impl-1
agent-bus panes view            # solo lectura (tmux attach -r); desconectar: prefijo + d
```

Presets: `claude` y `agy`. El estado sale de las últimas 12 líneas de la pantalla:
Claude Code 2.1 dibuja su spinner como `✶ Verbo…` (versiones anteriores, `esc to
interrupt`) y agy muestra `esc to cancel` mientras trabaja; ambos piden confirmar
(`Enter to confirm` / `enter Confirm`) al preguntar por confianza o permisos, y eso
cuenta como `blocked`. `send` rechaza un panel que no esté `idle` salvo con `--force`.
Un agente que termina queda `dead` hasta `close`.

Con `--agent` el panel recibe las variables `AGENT_BUS_*` de esa credencial, como un
worker. El panel no hereda las variables `CLAUDE*` de quien lo abre (sesión, socket y
token de un Claude Code coordinador); Claude vuelve a aplicar el `env` de su
`settings.json`. Como solo escribe el coordinador y el prototipo aún no navega los menús
de permiso, evítalos con los argumentos de permisos del CLI tras `--` y confía antes en
la carpeta del proyecto; un panel `blocked` lo resuelve el usuario con `tmux -L agent-bus
attach` (sin `-r`).

Para que el bus despierte al agente de un panel, arranca su watcher con
`--cli tmux`:

```sh
agent-bus watch --agent claude --cli tmux --tmux-agent impl-1
```

Escribe en el panel los mismos avisos de tareas que `--cli muxel` (tarea asignada,
revisión liberada, implementación reabierta y los mensajes del hub que entregan
trabajo, que confirma tras escribirlos). Como tmux no da el texto de la respuesta,
las preguntas con respuesta pendiente no se convierten en turnos del watcher: las
escribe una sola vez (los ids quedan en `task_nudges.json` como `asked`) y el agente
las contesta con `reply_message`; el watcher no responde ni confirma por él. Un panel
`working` o `blocked` aplaza el aviso sin contar un fallo; uno `dead` o inexistente
lo reintenta a los 30 s.

El coordinador también los maneja por MCP con la herramienta `agent_panes`
(`action` = `list`, `spawn`, `send`, `screen` o `close`). Solo aparece si el MCP del
coordinador arranca con `AGENT_BUS_PANES=1` en su entorno (el bloque `env` de la
entrada `agent-bus` en la configuración MCP del cliente) y requiere `bootstrap_agent`.
Por MCP no hay argumentos libres del CLI ni directorio: el panel arranca en la raíz
del proyecto del MCP, así que un agente no puede abrir otro sin preguntas de permiso.
`as_agent` le da al panel la identidad de un agente al que ese coordinador asignó
trabajo con `assign_work` en alguno de sus encargos (`GET /instructions/assignees`,
que responde siempre por la sesión autenticada); estar en el roster no basta. La
opción `--agent` de la CLI no tiene esa restricción: la usa quien ya tiene acceso
local a las credenciales.

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
| `agent_busy` | Con `--cli muxel`: el panel está trabajando o esperando al usuario; se reintenta sin contar un fallo |
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
